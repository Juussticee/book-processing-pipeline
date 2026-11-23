"""Google Sheets service for Milestone 5.

Exports book metadata to a Google Sheets spreadsheet using service
account authentication.

IMPORTANT: This service NEVER logs credentials, API keys, or service
account file contents. Only operation summaries are logged.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from core.logger import setup_logger

logger = setup_logger(__name__)


class GoogleSheetsService:
    """Exports book metadata to a Google Sheets spreadsheet.

    Uses a service account JSON key for authentication. The credentials
    file path is never logged.

    Attributes:
        _credentials_file: Path to service account JSON key (never logged).
        _sheets_id: Google Sheets document ID.
        _tab_name: Worksheet tab name.
        _client: gspread client (created lazily).
        _sheet: gspread worksheet (opened lazily).
    """

    # Column order must match the spreadsheet header row. ``Status`` is first so
    # a customer sees the confidence of every record before anything else.
    COLUMNS: List[str] = [
        "Status",
        "Title",
        "Authors",
        "ISBN-13",
        "Publisher",
        "Published Date",
        "Language",
        "Categories",
        "Description",
        "Open Cover",
    ]

    # Header name of the cover column. The cover cell is the one typed
    # exception in an otherwise all-text spreadsheet.
    COVER_COLUMN = "Open Cover"

    # Customer-facing status column and its three labels. The label is derived
    # from persisted state only (see ``export_status``); it never depends on
    # whether the export itself succeeded.
    STATUS_COLUMN = "Status"
    STATUS_CONFIRMED = "Confirmed"
    STATUS_NEEDS_REVIEW = "Needs Review"
    STATUS_INCOMPLETE = "Incomplete"

    # Below this confidence the identification pipeline itself routes a book to
    # review (CONFIDENCE_MANUAL_REVIEW_THRESHOLD = 0.50). A persisted record under
    # it can never be presented to the customer as confirmed.
    MIN_ACCEPTED_CONFIDENCE = 0.5

    # The sheet's metadata columns are all text (e.g. ISBNs with leading
    # zeroes), so every normal value is written with RAW input to keep them
    # exactly as typed.
    VALUE_INPUT_OPTION = "RAW"

    # The cover cell holds a real ``=IMAGE(...)`` formula (not text), so it is
    # written with a dedicated single-cell USER_ENTERED update. USER_ENTERED is
    # never used for metadata cells: that would reinterpret text like
    # ``0012345678`` and drop the leading zeroes.
    COVER_VALUE_INPUT_OPTION = "USER_ENTERED"

    def __init__(
        self,
        credentials_file: str,
        sheets_id: str,
        tab_name: str = "Sheet1",
    ) -> None:
        self._credentials_file = credentials_file
        self._sheets_id = sheets_id
        self._tab_name = tab_name
        self._client = None
        self._sheet = None
        self._spreadsheet = None
        self._external_url_access_enabled = False
        self._layout_ready = False

    def _get_spreadsheet(self):
        """Lazily open the parent spreadsheet (needed for workbook settings)."""
        if self._spreadsheet is not None:
            return self._spreadsheet
        client = self._get_client()
        self._spreadsheet = client.open_by_key(self._sheets_id)
        return self._spreadsheet

    def _get_client(self):
        """Lazily create the gspread client."""
        if self._client is not None:
            return self._client

        if not self._credentials_file:
            raise ValueError("Google Sheets credentials file path is empty")
        if not os.path.exists(self._credentials_file):
            raise FileNotFoundError(
                f"Credentials file not found: "
                f"{os.path.basename(self._credentials_file)}"
            )

        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(
            self._credentials_file, scopes=scopes
        )
        self._client = gspread.authorize(creds)
        logger.info("Google Sheets client authenticated (credentials file loaded)")
        return self._client

    def _get_sheet(self, ensure_layout: bool = True):
        """Lazily open the worksheet.

        Args:
            ensure_layout: run the ``Status``-column migration before handing
                the worksheet back. Write paths must keep the default; purely
                diagnostic reads pass ``False`` so that looking at a legacy
                workbook can never change it.
        """
        if self._sheet is not None:
            if ensure_layout:
                self._ensure_layout(self._sheet)
            return self._sheet

        client = self._get_client()
        try:
            spreadsheet = client.open_by_key(self._sheets_id)
            self._spreadsheet = spreadsheet
            self._sheet = spreadsheet.worksheet(self._tab_name)
        except Exception as exc:
            if not ensure_layout:
                # A read-only caller must never create a tab or a header row:
                # missing tab, wrong tab name and authentication failures alike
                # stop here instead of being "repaired" by a write.
                raise RuntimeError(
                    "Worksheet '%s' is not available in the configured "
                    "spreadsheet; refusing to create it on a read-only path (%s)"
                    % (self._tab_name, type(exc).__name__)
                ) from exc
            # Try to create the worksheet if it doesn't exist
            spreadsheet = client.open_by_key(self._sheets_id)
            try:
                self._sheet = spreadsheet.worksheet(self._tab_name)
            except Exception:
                self._sheet = spreadsheet.add_worksheet(
                    title=self._tab_name, rows="1000", cols=str(len(self.COLUMNS))
                )
                # Write header row
                header_range = f"A1:{chr(65 + len(self.COLUMNS) - 1)}1"
                self._sheet.update(values=[self.COLUMNS], range_name=header_range)
        if ensure_layout:
            self._ensure_layout(self._sheet)
        return self._sheet

    def _ensure_layout(self, sheet) -> None:
        """Migrate a tab to the ``Status``-first layout exactly once.

        Existing spreadsheets were created without the Status column. Rather
        than rewriting or reordering them, one column is inserted at position A
        so every existing cell keeps its book, its row and its cover formula:

        - header already contains ``Status`` -> nothing to do;
        - empty tab -> write the canonical header row;
        - legacy header (no ``Status``) -> insert one column at A and label it.
          Google Sheets shifts the existing cells right; nothing is deleted and
          no row is rewritten.

        Only the header cell is written by the migration. Legacy rows keep an
        empty Status until they are re-exported or re-labelled with
        :meth:`refresh_statuses`, and an empty Status is never read as
        ``Confirmed``.
        """
        if self._layout_ready:
            return
        try:
            values = sheet.get_all_values()
        except Exception as exc:  # pragma: no cover - API failure path
            logger.warning("Sheet layout check skipped: %s", exc)
            return

        header = [str(cell).strip() for cell in values[0]] if values else []
        while header and not header[-1]:
            header.pop()

        if self.STATUS_COLUMN in header:
            self._layout_ready = True
            return

        if not values:
            last_col = self._col_num_to_letter(len(self.COLUMNS))
            sheet.update(
                values=[list(self.COLUMNS)],
                range_name=f"A1:{last_col}1",
                value_input_option=self.VALUE_INPUT_OPTION,
            )
            self._layout_ready = True
            logger.info("Wrote header row with a Status column")
            return

        sheet.insert_cols([[self.STATUS_COLUMN]], 1)
        self._layout_ready = True
        logger.info(
            "Inserted Status column at A; %d existing row(s) preserved",
            max(len(values) - 1, 0),
        )

    def refresh_statuses(self, metadata_list) -> Dict[str, int]:
        """Re-label the Status cell of rows that already exist.

        Used once after the Status column is introduced, and whenever an
        operator wants the sheet labels to catch up with the database. It only
        writes the Status cell of a row that is already present: it never
        appends rows, never touches other cells and never deletes anything.

        Returns:
            ``{"updated": n, "unchanged": n, "missing": n}`` counts.
        """
        sheet = self._get_sheet()
        all_values = sheet.get_all_values(value_render_option="FORMULA")
        if not all_values:
            return {"updated": 0, "unchanged": 0, "missing": 0}

        # The migration in _ensure_layout normally guarantees the column, but
        # never write into a column that is not really there.
        header = [str(cell).strip() for cell in all_values[0]]
        while header and not header[-1]:
            header.pop()
        if self.STATUS_COLUMN not in header:
            logger.warning("Sheet has no Status column; nothing to refresh")
            return {"updated": 0, "unchanged": 0, "missing": 0}

        status_idx = header.index(self.STATUS_COLUMN)
        status_column = self._col_num_to_letter(status_idx + 1)
        counts = {"updated": 0, "unchanged": 0, "missing": 0}

        for metadata in metadata_list:
            located = self._locate_row(all_values, header, metadata, None)
            if located is None:
                counts["missing"] += 1
                continue
            existing = self._pad_row(all_values[located - 1], len(header))
            # Judge on the row the customer actually sees, so a legacy row that
            # already holds a publisher/cover is not labelled Incomplete.
            label = self._status_from_row(metadata, header, existing)
            current = existing[status_idx]
            if current == label:
                counts["unchanged"] += 1
                continue
            sheet.update(
                values=[[label]],
                range_name=f"{status_column}{located}",
                value_input_option=self.VALUE_INPUT_OPTION,
            )
            counts["updated"] += 1

        logger.info(
            "Status refresh: %d updated, %d unchanged, %d not in sheet",
            counts["updated"],
            counts["unchanged"],
            counts["missing"],
        )
        return counts

    def _ensure_external_url_access(self) -> None:
        """Let IMAGE()/IMPORT* formulas fetch external URLs (best effort).

        Since April 2024 Google Sheets blocks external URL access by default;
        without this workbook property every ``=IMAGE(...)`` formula evaluates
        to ``#REF!`` with the message "Please use a desktop web browser to
        allow access to fetch data from external urls.". Setting
        ``importFunctionsExternalUrlAccessAllowed`` via ``spreadsheets.batchUpdate``
        grants it without the interactive "Allow access" prompt.

        Failures are logged, never raised: an export must not break because a
        workbook setting could not be read or written.
        """
        if self._external_url_access_enabled:
            return
        try:
            spreadsheet = self._get_spreadsheet()
            props = spreadsheet.fetch_sheet_metadata(
                params={"fields": "properties.importFunctionsExternalUrlAccessAllowed"}
            )
            allowed = (props.get("properties") or {}).get(
                "importFunctionsExternalUrlAccessAllowed"
            )
            if allowed is True:
                self._external_url_access_enabled = True
                return
            spreadsheet.batch_update(
                {
                    "requests": [
                        {
                            "updateSpreadsheetProperties": {
                                "properties": {
                                    "importFunctionsExternalUrlAccessAllowed": True
                                },
                                "fields": ("importFunctionsExternalUrlAccessAllowed"),
                            }
                        }
                    ]
                }
            )
            self._external_url_access_enabled = True
            logger.info(
                "Enabled external URL access for IMAGE()/IMPORT* formulas "
                "in the spreadsheet"
            )
        except Exception as exc:
            logger.warning(
                "Could not enable external URL access for IMAGE()/IMPORT* "
                "formulas (%s: %s) — covers may show #REF! until it is "
                "allowed manually",
                type(exc).__name__,
                exc,
            )

    # ISO 639-2 three-letter code -> human-readable name
    _LANGUAGE_NAMES: Dict[str, str] = {
        "fre": "French",
        "eng": "English",
        "ger": "German",
        "spa": "Spanish",
        "ita": "Italian",
        "rus": "Russian",
        "chi": "Chinese",
        "jpn": "Japanese",
        "pol": "Polish",
        "dut": "Dutch",
        "por": "Portuguese",
        "cze": "Czech",
        "gre": "Greek",
        "ara": "Arabic",
        "hin": "Hindi",
        "kor": "Korean",
        "swe": "Swedish",
        "nor": "Norwegian",
        "dan": "Danish",
        "fin": "Finnish",
        "hun": "Hungarian",
        "slo": "Slovak",
        "slv": "Slovenian",
        "bul": "Bulgarian",
        "est": "Estonian",
        "lat": "Latin",
        "glg": "Galician",
        "cat": "Catalan",
        "heb": "Hebrew",
        "ukr": "Ukrainian",
        "ice": "Icelandic",
        "fil": "Filipino",
    }

    @classmethod
    def _human_readable_language(cls, lang_code: Optional[str]) -> str:
        """Convert ISO 639-2/639-1 code to human-readable name for the Sheet."""
        if not lang_code:
            return ""
        code = lang_code.lower().strip()
        # Handle 2-letter codes by converting to 3-letter first
        if len(code) == 2:
            two_to_three = {
                "en": "eng",
                "fr": "fre",
                "de": "ger",
                "es": "spa",
                "it": "ita",
                "ru": "rus",
                "zh": "chi",
                "ja": "jpn",
                "pl": "pol",
                "nl": "dut",
                "pt": "por",
                "cs": "cze",
                "el": "gre",
                "ar": "ara",
                "hi": "hin",
                "ko": "kor",
                "sv": "swe",
                "no": "nor",
                "da": "dan",
                "fi": "fin",
                "hu": "hun",
                "sk": "slo",
                "sl": "slv",
                "bg": "bul",
                "et": "est",
                "la": "lat",
                "ga": "gle",
                "ca": "cat",
                "he": "heb",
                "uk": "ukr",
                "is": "ice",
                "fil": "fil",
                "gl": "glg",
                "tr": "tur",
                "id": "ind",
                "ms": "may",
                "th": "tha",
                "vi": "vie",
                "ro": "rum",
                "hr": "cro",
            }
            code = two_to_three.get(code, code)
        return cls._LANGUAGE_NAMES.get(code, lang_code)

    @staticmethod
    def _build_image_formula(
        cover_url: str,
        local_cover_path: Optional[str],
        drive_file_id: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build an IMAGE() formula and a clickable open-cover link for Sheets.

        Preference order:
        1. External cover URL
        2. Public Drive fallback for books with no clean external cover
        """
        url = (cover_url or "").strip().strip('"').strip("'").strip()
        if url.startswith("="):
            return url, url
        lowered = url.lower()
        # Normalize old Drive export URLs to empty so they are never used.
        if "drive.google.com/uc?export=view?id=" in lowered:
            return "", ""
        # Only use cover_url if it looks like a real image URL.
        if (
            lowered
            and lowered.startswith(("http://", "https://"))
            and not any(
                tok in lowered
                for tok in (
                    "kindle",
                    "qr",
                    "app/",
                    "store-link",
                    "drive.google.com",
                )
            )
        ):
            # Single-argument form only: this workbook rejects the sized
            # ``=IMAGE(url, 1, 120, 80)`` variant with "Wrong number of
            # arguments to IMAGE. Expected 2 arguments, but got 4 arguments."
            # while ``=IMAGE(url)`` renders (verified live against the Sheet).
            return f'=IMAGE("{url}")', url

        return "", ""

    def _map_metadata_to_row(self, metadata, cover_path: Optional[str]) -> List[str]:
        """Map a BookMetadata object to a spreadsheet row (list of strings).

        The list is returned in ``COLUMNS`` order. Actual writes resolve the
        column index from the sheet's header row, so this order is only a
        canonical contract, not a hard-coded position.
        """
        cover_url = metadata.cover_url or metadata.thumbnail_url or ""
        # Never render an image for a cover whose download/validation failed.
        if (metadata.cover_status or "").lower() in (
            "pending_retry",
            "invalid",
            "rejected_mismatch",
        ):
            cover_url = ""

        image_formula, _ = self._build_image_formula(
            cover_url,
            cover_path,
            getattr(metadata, "drive_file_id", None),
        )

        return [
            self.export_status(metadata, cover_available=bool(image_formula)),
            metadata.title or "",
            self._format_authors(metadata.authors),
            metadata.isbn_13 or "",
            metadata.publisher or "",
            metadata.published_date or "",
            self._human_readable_language(metadata.language),
            ", ".join(metadata.categories) if metadata.categories else "",
            metadata.description or "",
            image_formula,
        ]

    @classmethod
    def export_status(cls, metadata, cover_available: Optional[bool] = None) -> str:
        """Return the customer-facing status label for one book record.

        The label is derived from persisted state only and never from the
        success of the export itself:

        ``Needs Review``
            The identity was not accepted by the identification pipeline: the
            persisted status is anything other than ``identified`` (for example
            ``needs_review`` or ``manual_review``), the title is missing or a
            placeholder such as ``(unreadable)``, or the confidence is below
            :attr:`MIN_ACCEPTED_CONFIDENCE` (0.50, the pipeline's own review
            threshold). Used whenever the evidence is ambiguous, so an
            uncertain identification can never look confirmed.
        ``Incomplete``
            The identity was accepted, but useful expected metadata or the cover
            image is missing: no publisher, or no usable cover image. A missing
            ISBN never makes a record incomplete — plenty of books have no ISBN
            at all, so its absence says nothing about the identification. An
            empty description, publication date, language or categories does
            **not** make a record incomplete either, and none of them ever makes
            it ``Needs Review``.
        ``Confirmed``
            The identity was accepted, the publisher is known and the cover is
            present. The ISBN is optional.

        Args:
            metadata: The persisted book metadata.
            cover_available: Whether the row will carry a usable cover image.
                Defaults to deriving it from the metadata itself.
        """
        raw_status = str(getattr(metadata, "status", "") or "").strip().lower()
        title = str(getattr(metadata, "title", "") or "").strip()
        try:
            confidence = float(getattr(metadata, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        if (
            raw_status != "identified"
            or not title
            or title.startswith("(")
            or confidence < cls.MIN_ACCEPTED_CONFIDENCE
        ):
            return cls.STATUS_NEEDS_REVIEW

        if cover_available is None:
            cover_available = cls._metadata_cover_available(metadata)

        publisher = str(metadata.publisher or "").strip()
        if publisher and cover_available:
            return cls.STATUS_CONFIRMED
        return cls.STATUS_INCOMPLETE

    @classmethod
    def _metadata_cover_available(cls, metadata) -> bool:
        """Whether this record can produce a usable ``=IMAGE(...)`` cover."""
        cover_url = metadata.cover_url or metadata.thumbnail_url or ""
        if (metadata.cover_status or "").lower() in (
            "pending_retry",
            "invalid",
            "rejected_mismatch",
        ):
            cover_url = ""
        image_formula, _ = cls._build_image_formula(
            cover_url,
            getattr(metadata, "local_cover_path", None),
            getattr(metadata, "drive_file_id", None),
        )
        return bool(image_formula)

    def _status_from_row(self, metadata, header: List[str], row: List[str]) -> str:
        """Status label for an existing row, judged on the merged values.

        The review decision always comes from the persisted identification state
        (:meth:`export_status`), but completeness is judged on the cells the
        customer will actually see. A re-export that arrives with poorer
        provider data can therefore never downgrade a row that already holds a
        publisher or a cover.
        """
        if self.export_status(metadata, cover_available=True) == (
            self.STATUS_NEEDS_REVIEW
        ):
            return self.STATUS_NEEDS_REVIEW

        def cell(name: str) -> str:
            if name not in header:
                return ""
            index = header.index(name)
            if index >= len(row):
                return ""
            return str(row[index]).strip()

        publisher = cell("Publisher")
        cover = cell(self.COVER_COLUMN)
        # The row may not hold its cover formula yet (the cover is written after
        # the metadata cells), so an incoming usable cover counts as present.
        has_cover = cover.startswith("=IMAGE(") or self._metadata_cover_available(
            metadata
        )
        if publisher and has_cover:
            return self.STATUS_CONFIRMED
        return self.STATUS_INCOMPLETE

    @staticmethod
    def _format_authors(authors) -> str:
        """Join authors the way the existing spreadsheet already stores them.

        Every multi-author row in the live sheet is ``"A, B"`` (e.g.
        ``"Stefan Heym, Jan Dusay"``), and row matching is punctuation
        insensitive, so this is the canonical export format.
        """
        if not authors:
            return ""
        cleaned = [str(a).strip() for a in authors if a and str(a).strip()]
        return ", ".join(cleaned)

    @staticmethod
    def _normalise_key(value: Optional[str]) -> str:
        """Normalise a cell value for identity comparison.

        Case-, accent- and whitespace-insensitive, and folds ``œ``/``æ``, so an
        OCR-normalised title (``Le coeur vert``) still matches the row a human
        wrote as ``Le cœur vert``.
        """
        import unicodedata

        if not value:
            return ""
        text = str(value).replace("œ", "oe").replace("Œ", "OE")
        text = text.replace("æ", "ae").replace("Æ", "AE")
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = "".join(ch if ch.isalnum() else " " for ch in text.casefold())
        return " ".join(text.split())

    @staticmethod
    def _is_blank(value: Optional[str]) -> bool:
        return value is None or str(value).strip() == ""

    def _read_header(self, all_values: List[List[str]]) -> List[str]:
        """Return the sheet's header row, falling back to the canonical order.

        Columns are resolved by header *name*; the existing 9 headers are never
        reordered or renamed.
        """
        if not all_values:
            return list(self.COLUMNS)
        header = [str(cell).strip() for cell in all_values[0]]
        while header and not header[-1]:
            header.pop()
        missing = [column for column in self.COLUMNS if column not in header]
        if missing:
            header.extend(missing)
        return header

    def _row_values_for_header(
        self,
        metadata,
        cover_path: Optional[str],
        header: List[str],
    ) -> List[str]:
        """Build a row aligned to *header* by column name (not position)."""
        canonical = self._map_metadata_to_row(metadata, cover_path)
        by_header = dict(zip(self.COLUMNS, canonical))
        return [by_header.get(column, "") for column in header]

    def _merge_preserving(
        self,
        existing: List[str],
        new: List[str],
        always_refresh: Optional[set] = None,
    ) -> List[str]:
        """Merge *new* into *existing* without clearing populated cells.

        A non-empty existing cell always wins, so a later provider failure
        cannot wipe data that was already exported. Empty cells are filled from
        the new metadata when available.

        Indices in *always_refresh* are an exception: they take the new value
        even when the old one is populated. The Status column uses this so a
        record that moves from ``Needs Review`` to ``Confirmed`` (after a human
        resolves it) is re-labelled instead of being frozen at its first value.
        """
        refresh = always_refresh or set()
        merged: List[str] = []
        for index, new_value in enumerate(new):
            old_value = existing[index] if index < len(existing) else ""
            if index in refresh and not self._is_blank(new_value):
                merged.append(new_value)
            elif self._is_blank(new_value):
                merged.append(old_value)
            elif self._is_blank(old_value):
                merged.append(new_value)
            else:
                merged.append(old_value)
        return merged

    @staticmethod
    def _pad_row(row: List[str], width: int) -> List[str]:
        padded = list(row)
        if len(padded) < width:
            padded.extend([""] * (width - len(padded)))
        return padded

    def _cover_column_index(self, header: List[str]) -> Optional[int]:
        """1st-0-based index of the cover column in *header*, or None."""
        if self.COVER_COLUMN in header:
            return header.index(self.COVER_COLUMN)
        return None

    def _cover_cell_range(self, header: List[str], row_number: int) -> Optional[str]:
        """A1 range of the cover cell in *row_number*, or None."""
        cover_idx = self._cover_column_index(header)
        if cover_idx is None:
            return None
        return f"{self._col_num_to_letter(cover_idx + 1)}{row_number}"

    def _cover_cell_user_entered(
        self, sheet, header: List[str], row_number: int
    ) -> Optional[dict]:
        """``userEnteredValue`` of the cover cell, or None when unreadable.

        The values API cannot tell a live ``=IMAGE(...)`` formula from its
        literal-text twin (both read back as the formula string), but the grid
        metadata can: a live formula carries ``formulaValue`` while literal
        text carries ``stringValue``.
        """
        cell_range = self._cover_cell_range(header, row_number)
        if cell_range is None:
            return None
        try:
            meta = self._get_spreadsheet().fetch_sheet_metadata(
                params={
                    "includeGridData": "true",
                    "ranges": [f"{sheet.title}!{cell_range}"],
                    "fields": "sheets(data.rowData.values(userEnteredValue))",
                }
            )
            rows = meta.get("sheets", [{}])[0].get("data", [{}])[0].get("rowData", [])
            entered = rows[0].get("values", [{}])[0].get("userEnteredValue", {})
            if not isinstance(entered, dict):
                return None
            return dict(entered)
        except Exception as exc:  # defensive: never block an export
            logger.debug("Cover cell metadata read failed (%s).", exc)
            return None

    @staticmethod
    def _image_formula_url(formula: str) -> Optional[str]:
        """The URL inside an ``=IMAGE("...")`` formula, or None if not one."""
        match = re.search(r'IMAGE\(\s*"([^"]+)"', formula or "")
        return match.group(1).strip() if match else None

    def _cover_write_needed(
        self,
        sheet,
        header: List[str],
        row_number: int,
        existing_cover: Optional[str],
        formula: str,
    ) -> bool:
        """Decide whether the cover cell needs a typed (USER_ENTERED) write.

        The cover cell must hold a live ``=IMAGE(...)`` formula.  A RAW row
        write stores that string as literal text, so the formula is applied
        with a separate single-cell USER_ENTERED update.  Merge semantics are
        still respected: a non-empty existing cover that is not formula shaped
        always wins and is never overwritten.

        ``existing_cover`` is the cover cell's *formatted* value.  The values
        API reports the ``=IMAGE(...)`` text for both a live formula and a
        literal-text copy, so live-vs-literal is resolved from the cell's
        ``userEnteredValue`` instead of the rendered string.
        """
        if not formula or self._cover_column_index(header) is None:
            return False
        existing = (existing_cover or "").strip()
        if existing and not existing.startswith("="):
            # Some other (non-formula) content already lives here.
            return False
        entered = self._cover_cell_user_entered(sheet, header, row_number)
        if entered is None:
            # Metadata read failed: write rather than leave a cover broken.
            return True
        stored_formula = str(entered.get("formulaValue") or "").strip()
        if not stored_formula:
            # Blank cell, or ``=IMAGE(...)`` stored as literal text: write it
            # once so Sheets evaluates the formula.
            return True
        stored_url = self._image_formula_url(stored_formula)
        wanted_url = self._image_formula_url(formula)
        if stored_url is None:
            # A different (non-IMAGE) formula already lives here — never clobber.
            return False
        if wanted_url is None:
            return False
        # A live formula pointing at the same image is left untouched, whatever
        # its legacy argument count. Only a changed URL needs a rewrite.
        return stored_url != wanted_url

    def _ensure_cover_formula_written(
        self,
        sheet,
        header: List[str],
        row_number: int,
        formula: str,
    ) -> None:
        """Write *formula* as a live formula in the cover cell.

        Written with USER_ENTERED input as a single-cell update so Sheets
        interprets ``=IMAGE(...)`` as a formula rather than literal text.
        Metadata cells are never sent through this path (see VALUE_INPUT_OPTION).
        """
        if not formula:
            return
        cell_range = self._cover_cell_range(header, row_number)
        if cell_range is None:
            return
        # Normalise to the single-argument form Sheets accepts in this workbook;
        # legacy ``=IMAGE(url, 1, 120, 80)`` text would otherwise be rejected
        # with "Wrong number of arguments to IMAGE."
        url = self._image_formula_url(formula)
        if url:
            formula = f'=IMAGE("{url}")'
        self._ensure_external_url_access()
        sheet.update(
            values=[[formula]],
            range_name=cell_range,
            value_input_option=self.COVER_VALUE_INPUT_OPTION,
        )

    @staticmethod
    def _metadata_cells_changed(
        existing: List[str], merged: List[str], cover_idx: Optional[int]
    ) -> bool:
        """True when any non-cover cell differs between the two rows."""
        if cover_idx is None:
            return merged != existing
        left_same = merged[:cover_idx] == existing[:cover_idx]
        right_same = merged[cover_idx + 1 :] == existing[cover_idx + 1 :]
        return not (left_same and right_same)

    def _write_row_metadata(
        self,
        sheet,
        header: List[str],
        row_number: int,
        row_values: List[str],
        cover_idx: Optional[int],
        exclude_cover: bool,
    ) -> None:
        """Write metadata cells of one row with RAW input.

        When *exclude_cover* is set the cover cell is left untouched (it is
        either already a live formula or is about to be written as one), so a
        RAW write can never turn it back into literal text.
        """
        end_letter = self._col_num_to_letter(len(header))
        if not exclude_cover or cover_idx is None:
            sheet.update(
                values=[self._pad_row(row_values, len(header))],
                range_name=f"A{row_number}:{end_letter}{row_number}",
                value_input_option=self.VALUE_INPUT_OPTION,
            )
            return
        if cover_idx > 0:
            sheet.update(
                values=[row_values[:cover_idx]],
                range_name=(
                    f"A{row_number}:{self._col_num_to_letter(cover_idx)}{row_number}"
                ),
                value_input_option=self.VALUE_INPUT_OPTION,
            )
        if cover_idx + 1 < len(row_values):
            sheet.update(
                values=[row_values[cover_idx + 1 :]],
                range_name=(
                    f"{self._col_num_to_letter(cover_idx + 2)}{row_number}:"
                    f"{end_letter}{row_number}"
                ),
                value_input_option=self.VALUE_INPUT_OPTION,
            )

    def _locate_row(
        self,
        all_values: List[List[str]],
        header: List[str],
        metadata,
        row_id: Optional[int],
    ) -> Optional[int]:
        """Find the row belonging to *metadata*.

        Order of evidence:
        1. the previously persisted row number, **verified by title** (a stale
           number must never overwrite another book's row);
        2. exact title + author match, using the sheet's own formatting.
        """
        title_idx = header.index("Title") if "Title" in header else 0
        authors_idx = header.index("Authors") if "Authors" in header else 1
        width = len(header)

        target_title = self._normalise_key(metadata.title)
        target_authors = self._normalise_key(self._format_authors(metadata.authors))

        if row_id and 2 <= row_id <= len(all_values):
            candidate = self._pad_row(all_values[row_id - 1], width)
            if self._normalise_key(candidate[title_idx]) == target_title:
                return row_id

        for index in range(1, len(all_values)):
            row = self._pad_row(all_values[index], width)
            if self._normalise_key(row[title_idx]) != target_title:
                continue
            if not target_authors or self._normalise_key(row[authors_idx]) == (
                target_authors
            ):
                return index + 1
        return None

    def get_or_create_row(
        self,
        metadata,
        cover_path: Optional[str] = None,
        row_id: Optional[int] = None,
    ) -> Optional[int]:
        """Update the book's existing row, or append a new one.

        Args:
            metadata: Book metadata to export.
            cover_path: Local cover path (kept for API compatibility).
            row_id: Previously persisted sheet row for this Drive file. It is
                used only after its title is verified.

        Returns:
            The 1-based row number holding the book.

        Raises on Google API/network failures — caller handles retries.
        """
        sheet = self._get_sheet()
        all_values = sheet.get_all_values()

        if not all_values:
            # Completely empty sheet — write header + first data row.
            header = list(self.COLUMNS)
            row_data = self._row_values_for_header(metadata, cover_path, header)
            sheet.append_rows(
                [header, row_data],
                insert_data_option="OVERWRITE",
                value_input_option=self.VALUE_INPUT_OPTION,
            )
            cover_idx = self._cover_column_index(header)
            if cover_idx is not None and row_data[cover_idx]:
                self._ensure_cover_formula_written(
                    sheet, header, 2, row_data[cover_idx]
                )
            logger.info("Created Sheets row 2 for title='%s'", metadata.title)
            return 2

        header = self._read_header(all_values)
        new_values = self._row_values_for_header(metadata, cover_path, header)

        if len(all_values) == 1:
            # Header only — append the first data row (no duplicate header).
            sheet.append_rows(
                [new_values],
                insert_data_option="OVERWRITE",
                value_input_option=self.VALUE_INPUT_OPTION,
            )
            cover_idx = self._cover_column_index(header)
            if cover_idx is not None and new_values[cover_idx]:
                self._ensure_cover_formula_written(
                    sheet, header, 2, new_values[cover_idx]
                )
            logger.info("Created Sheets row 2 for title='%s'", metadata.title)
            return 2

        located = self._locate_row(all_values, header, metadata, row_id)
        if located is not None:
            existing = self._pad_row(all_values[located - 1], len(header))
            status_idx = (
                header.index(self.STATUS_COLUMN)
                if self.STATUS_COLUMN in header
                else None
            )
            merged = self._merge_preserving(
                existing,
                new_values,
                always_refresh={status_idx} if status_idx is not None else None,
            )
            if status_idx is not None:
                merged[status_idx] = self._status_from_row(metadata, header, merged)
            cover_idx = self._cover_column_index(header)
            # Decide against the merged value (a populated existing cover wins),
            # so a stale provider URL never triggers a pointless rewrite.
            formula = merged[cover_idx] if cover_idx is not None else ""
            existing_cover = (
                existing[cover_idx]
                if cover_idx is not None and cover_idx < len(existing)
                else ""
            )

            metadata_changed = self._metadata_cells_changed(existing, merged, cover_idx)
            cover_write = self._cover_write_needed(
                sheet, header, located, existing_cover, formula
            )

            if not metadata_changed and not cover_write:
                logger.info(
                    "Sheets row %d already up to date for title='%s' — "
                    "no write needed",
                    located,
                    metadata.title,
                )
                return located

            if metadata_changed:
                exclude_cover = cover_write
                row_values = merged
                self._write_row_metadata(
                    sheet,
                    header,
                    located,
                    row_values,
                    cover_idx,
                    exclude_cover,
                )
                logger.info(
                    "Updated Sheets row %d for title='%s'",
                    located,
                    metadata.title,
                )

            if cover_write:
                formula_out = merged[cover_idx] if cover_idx is not None else ""
                self._ensure_cover_formula_written(sheet, header, located, formula_out)

            return located

        # No matching row — create new.
        new_row_number = len(all_values) + 1
        sheet.append_rows(
            [new_values],
            insert_data_option="OVERWRITE",
            value_input_option=self.VALUE_INPUT_OPTION,
        )
        cover_idx = self._cover_column_index(header)
        if cover_idx is not None and new_values[cover_idx]:
            self._ensure_cover_formula_written(
                sheet, header, new_row_number, new_values[cover_idx]
            )
        logger.info(
            "Created Sheets row %d for title='%s'",
            new_row_number,
            metadata.title,
        )
        return new_row_number

    def read_all_rows(self, ensure_layout: bool = True) -> List[Dict[str, str]]:
        """Read all data rows from the sheet and return as list of dicts.

        Each dict maps column name to its string value.
        Useful for finding rows with confidence < threshold.

        Args:
            ensure_layout: keep the default on any path that may write the
                sheet afterwards, so row values keep the ``COLUMNS`` order.
                Diagnostic callers pass ``False`` to guarantee a read-only
                look at a workbook that has not been migrated yet.

        Returns:
            List of dicts, one per data row (header row excluded).
        """
        sheet = self._get_sheet(ensure_layout=ensure_layout)
        # With FORMULA render the cover column reads as the stored
        # ``=IMAGE(...)`` text (live formula or literal text alike), exactly
        # what callers have always received; with the default render a live
        # formula would read as empty.
        all_values = sheet.get_all_values(value_render_option="FORMULA")
        if not all_values or len(all_values) < 2:
            return []
        # Build dict per row using the COLUMNS header (all_values[0] is the header row)
        col_count = len(self.COLUMNS)
        rows: List[Dict[str, str]] = []
        for row_idx in range(1, len(all_values)):
            row = all_values[row_idx]
            # Pad row to at least col_count so zip is safe
            padded = list(row) + [""] * max(0, col_count - len(row))
            row_dict = dict(zip(self.COLUMNS, padded))
            row_dict["_row_num"] = row_idx + 1  # 1-based row number
            rows.append(row_dict)
        return rows

    def update_row(self, row_id: int, columns: Dict[str, str]) -> None:
        """Update specific columns in an existing row.

        Args:
            row_id: 1-based row number.
            columns: dict mapping column name to value.

        Metadata values are written with RAW input. If the cover column is
        requested with an ``=IMAGE(...)`` value, the cover cell is written as
        a separate single-cell update with USER_ENTERED input so Sheets
        interprets it as a live formula instead of literal text.
        """
        sheet = self._get_sheet()
        col_indices = {name: idx for idx, name in enumerate(self.COLUMNS)}
        cover_idx = col_indices.get(self.COVER_COLUMN)

        # Collect the column indices to update
        col_idx_list = []
        for col_name, value in columns.items():
            if col_name in col_indices:
                col_idx_list.append((col_indices[col_name], value))

        if not col_idx_list:
            logger.warning(
                "update_row(%d): no valid column names in %s", row_id, set(columns)
            )
            return

        # Sort by column index to ensure left-to-right ordering
        col_idx_list.sort(key=lambda x: x[0])

        cover_value = columns.get(self.COVER_COLUMN) or ""
        cover_formula = (
            cover_idx is not None
            and any(idx == cover_idx for idx, _ in col_idx_list)
            and str(cover_value).strip().startswith("=")
        )

        # A formula-shaped cover value must not flow into a RAW row write —
        # RAW would store it as literal text — so the cover cell is excluded
        # there and written with USER_ENTERED below.
        raw_cols = col_idx_list
        if cover_formula:
            raw_cols = [(idx, value) for idx, value in col_idx_list if idx != cover_idx]

        if raw_cols:
            start_idx = raw_cols[0][0]
            end_idx = raw_cols[-1][0]

            # Build a flat row of values, padding skipped columns with empty
            # strings so that the values align with the contiguous range
            # [start_col:end_col].
            value_map = dict(raw_cols)
            row_values = [value_map.get(i, "") for i in range(start_idx, end_idx + 1)]

            # Build a contiguous range from start to end column
            start_letter = self._col_num_to_letter(start_idx + 1)
            end_letter = self._col_num_to_letter(end_idx + 1)
            range_str = f"{start_letter}{row_id}:{end_letter}{row_id}"

            # Write as a single 2D array: [[val1, val2, ...]]
            # This ensures all values go into ONE row, not transposed into columns.
            sheet.update(
                values=[row_values],
                range_name=range_str,
                value_input_option=self.VALUE_INPUT_OPTION,
            )

        if cover_formula:
            self._ensure_cover_formula_written(
                sheet, self.COLUMNS, row_id, str(cover_value)
            )

        logger.info("Updated Sheets row %d", row_id)

    def _build_sheet_url(self, row_id):
        """Build a URL to a specific row in the Google Sheet."""
        if row_id is None:
            return None
        try:
            row_num = int(row_id)
        except (ValueError, TypeError):
            return None
        return (
            f"https://docs.google.com/spreadsheets/d/{self._sheets_id}"
            f"/edit#gid=0&range=A{row_num}"
        )

    @staticmethod
    def _get_timestamp() -> str:
        from datetime import datetime

        return datetime.utcnow().isoformat()

    @staticmethod
    def _col_num_to_letter(n: int) -> str:
        """Convert a 1-based column number to a letter (A, B, ..., Z, AA, AB)."""
        result = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            result = chr(65 + rem) + result
        return result

    def __enter__(self) -> "GoogleSheetsService":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """Release resources."""
        self._sheet = None
        self._client = None
        self._spreadsheet = None
        self._external_url_access_enabled = False
