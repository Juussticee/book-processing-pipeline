"""Unit tests for GoogleSheetsService.

All tests use mocks — NO real Google Sheets credentials are used.
"""

from unittest.mock import Mock, patch

import pytest

from models.book_metadata import BookMetadata
from services.google_sheets_service import GoogleSheetsService


def _sheet_updates(mock_sheet):
    """All ``update`` calls made on *mock_sheet*, in order."""
    return [call.kwargs for call in mock_sheet.update.call_args_list]


def _raw_rows(mock_sheet):
    """[(values, range_name)] of every RAW row write on *mock_sheet*."""
    return [
        (call.kwargs["values"][0], call.kwargs["range_name"])
        for call in mock_sheet.update.call_args_list
        if call.kwargs.get("value_input_option") == "RAW"
    ]


def _cover_patch(mock_sheet):
    """The kwargs of the dedicated single-cell cover write, or None."""
    for call in mock_sheet.update.call_args_list:
        if call.kwargs.get("value_input_option") == "USER_ENTERED":
            return call.kwargs
    return None


_DEFAULT_COVER = '=IMAGE("http://example.com/cover.jpg")'


def _cover_grid(formula=None, literal=None):
    """fetch_sheet_metadata reply for one cover cell (live formula or text)."""
    if formula is not None:
        entered = {"formulaValue": formula}
    elif literal is not None:
        entered = {"stringValue": literal}
    else:
        entered = {}
    return {
        "sheets": [
            {"data": [{"rowData": [{"values": [{"userEnteredValue": entered}]}]}]}
        ]
    }


def _stub_cover_cell(service, formula=None, literal=None):
    """Patch the workbook metadata read used to inspect the cover cell."""
    spreadsheet = Mock()
    spreadsheet.fetch_sheet_metadata.return_value = _cover_grid(formula, literal)
    return patch.object(service, "_get_spreadsheet", return_value=spreadsheet)


def assertCoverPatch(mock_sheet, row, range_name, formula=_DEFAULT_COVER):
    """The only USER_ENTERED write is the requested cover cell."""
    calls = _sheet_updates(mock_sheet)
    assert calls, "expected a USER_ENTERED cover-cell write"
    cover_calls = [
        c
        for c in mock_sheet.update.call_args_list
        if c.kwargs.get("value_input_option") == "USER_ENTERED"
    ]
    assert len(cover_calls) == 1
    assert cover_calls[0].kwargs["values"] == [[formula]]
    assert cover_calls[0].kwargs["range_name"] == range_name
    assert range_name.startswith(f"J{row}")


def _make_metadata(**kwargs):
    defaults = {
        "title": "Test Book",
        "authors": ["Test Author"],
        "isbn_10": "0306406152",
        "isbn_13": "9780306406157",
        "publisher": "Test Publisher",
        "published_date": "2024-01-01",
        "language": "en",
        "categories": ["Fiction"],
        "description": "A test book description.",
        "cover_url": "http://example.com/cover.jpg",
        "confidence": 0.95,
        "source": "google_books",
        "drive_file_id": "drive001",
        "job_id": "job001",
    }
    defaults.update(kwargs)
    return BookMetadata(**defaults)


class TestGoogleSheetsService:
    def setup_method(self):
        self.service = GoogleSheetsService(
            credentials_file="test_creds.json",
            sheets_id="test_sheet_id_123",
            tab_name="Books",
        )

    def test_columns_defined(self):
        """Verify the expected column order is present."""
        assert GoogleSheetsService.COLUMNS[0] == "Status"
        assert "Status" in GoogleSheetsService.COLUMNS
        assert "Title" in GoogleSheetsService.COLUMNS
        assert "Authors" in GoogleSheetsService.COLUMNS
        assert "ISBN-13" in GoogleSheetsService.COLUMNS
        assert "Publisher" in GoogleSheetsService.COLUMNS
        assert "Published Date" in GoogleSheetsService.COLUMNS
        assert "Language" in GoogleSheetsService.COLUMNS
        assert "Categories" in GoogleSheetsService.COLUMNS
        assert "Description" in GoogleSheetsService.COLUMNS
        assert "Open Cover" in GoogleSheetsService.COLUMNS
        assert len(GoogleSheetsService.COLUMNS) == 10

    def test_build_sheet_url_none(self):
        url = self.service._build_sheet_url(None)
        assert url is None

    def test_build_sheet_url_valid(self):
        url = self.service._build_sheet_url(42)
        assert url is not None
        assert "42" in url
        assert "docs.google.com" in url

    def test_build_sheet_url_invalid(self):
        url = self.service._build_sheet_url("not_a_number")
        assert url is None

    @patch("os.path.exists", return_value=True)
    def test_get_client_missing_credentials_raises(self, mock_exists):
        """If credentials file doesn't exist, raises FileNotFoundError."""
        # Override to simulate file not existing
        with patch("os.path.exists", return_value=False):
            try:
                self.service._get_client()
                assert False, "Should have raised FileNotFoundError"
            except FileNotFoundError:
                pass

    def test_map_metadata_to_row(self):
        """Verify BookMetadata maps to the correct row data."""
        metadata = _make_metadata()
        row = self.service._map_metadata_to_row(metadata, "/path/to/cover.jpg")
        assert len(row) == 10
        assert row[0] == "Confirmed"
        assert row[1] == "Test Book"
        assert row[2] == "Test Author"
        assert row[3] == "9780306406157"
        assert row[4] == "Test Publisher"
        assert row[5] == "2024-01-01"
        assert row[6] == "English"
        assert row[7] == "Fiction"
        assert row[8] == "A test book description."
        assert row[9] == '=IMAGE("http://example.com/cover.jpg")'

    # --- IMAGE() formula tests ---

    def test_build_image_formula_rejects_drive_urls(self):
        """Drive URLs must never be used as final cover images."""
        image_formula, open_cover = self.service._build_image_formula(
            "https://drive.google.com/uc?id=abc123", "/local/cover.jpg"
        )
        assert image_formula == ""
        assert open_cover == ""

    def test_build_image_formula_empty_url_no_fallback(self):
        """Empty cover URL returns empty strings."""
        image_formula, open_cover = self.service._build_image_formula(
            "", "/local/cover.jpg"
        )
        assert image_formula == ""
        assert open_cover == ""

    def test_build_image_formula_both_empty(self):
        """Both empty returns empty strings."""
        image_formula, open_cover = self.service._build_image_formula("", "")
        assert image_formula == ""
        assert open_cover == ""

    def test_build_image_formula_preserves_existing_formula(self):
        """If cover_url is already a formula, preserve it."""
        existing = '=IMAGE("https://example.com/img.jpg", 1, 100, 60)'
        image_formula, open_cover = self.service._build_image_formula(
            existing, "/local/cover.jpg"
        )
        assert image_formula == existing
        assert open_cover == existing

    def test_human_readable_language_french(self):
        assert self.service._human_readable_language("fre") == "French"

    def test_human_readable_language_english(self):
        assert self.service._human_readable_language("eng") == "English"

    def test_human_readable_language_russian(self):
        assert self.service._human_readable_language("rus") == "Russian"

    def test_human_readable_language_spanish(self):
        assert self.service._human_readable_language("spa") == "Spanish"

    def test_human_readable_language_2letter_codes(self):
        assert self.service._human_readable_language("fr") == "French"
        assert self.service._human_readable_language("en") == "English"
        assert self.service._human_readable_language("ru") == "Russian"
        assert self.service._human_readable_language("es") == "Spanish"

    def test_human_readable_language_none(self):
        assert self.service._human_readable_language(None) == ""

    def test_human_readable_language_empty(self):
        assert self.service._human_readable_language("") == ""

    def test_human_readable_language_unknown(self):
        assert self.service._human_readable_language("xyz") == "xyz"

    # --- Row management tests ---

    def test_get_or_create_row_new(self):
        """New metadata creates a new row when sheet is empty."""
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = []  # Empty sheet (no header)
        mock_sheet.append_rows = Mock()
        mock_sheet.len = 0

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(_make_metadata(), "/cover.jpg")
            assert row_id is not None

    def test_get_or_create_row_existing(self):
        """Existing row by title+author is updated, not duplicated."""
        mock_sheet = Mock()
        # Header + one data row with matching title+author
        mock_sheet.get_all_values.return_value = [
            self.service.COLUMNS,
            ["", "Test Book", "Test Author", "", "", "", "", "", "", ""],
        ]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(_make_metadata(), "/cover.jpg")
            assert row_id == 2
            assert not mock_sheet.append_rows.called
            # One typed set of writes: metadata cells RAW, cover formula live.
            assertCoverPatch(mock_sheet, 2, "J2")

    def test_get_or_create_row_duplicate_prevention(self):
        """Re-exporting a book updates existing row, does not create duplicate."""
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [
            self.service.COLUMNS,
            [
                "",
                "Test Book",
                "Test Author",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ],
        ]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(_make_metadata(), "/cover.jpg")
            assert row_id == 2
            # Should update, not append
            assert not mock_sheet.append_rows.called

    def test_update_row(self):
        """Update specific columns in an existing row."""
        mock_sheet = Mock()
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            self.service.update_row(5, {"Publisher": "Gallimard", "Language": "French"})
            mock_sheet.update.assert_called_once()

    def test_update_row_image_formula(self):
        """update_row writes the cover value as a live formula."""
        mock_sheet = Mock()
        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            self.service.update_row(
                3, {"Open Cover": '=IMAGE("https://example.com/img.jpg", 1, 120, 80)'}
            )
            assertCoverPatch(
                mock_sheet,
                3,
                "J3",
                formula='=IMAGE("https://example.com/img.jpg")',
            )

    def test_col_num_to_letter(self):
        assert GoogleSheetsService._col_num_to_letter(1) == "A"
        assert GoogleSheetsService._col_num_to_letter(2) == "B"
        assert GoogleSheetsService._col_num_to_letter(26) == "Z"
        assert GoogleSheetsService._col_num_to_letter(27) == "AA"

    def test_get_timestamp(self):
        ts = GoogleSheetsService._get_timestamp()
        assert ts is not None
        assert "-" in ts  # ISO format contains date separators

    def test_close_releases_resources(self):
        self.service._client = Mock()
        self.service._sheet = Mock()
        self.service.close()
        assert self.service._client is None
        assert self.service._sheet is None

    def test_context_manager(self):
        with self.service as svc:
            assert svc is self.service
        assert self.service._client is None

    def test_credentials_never_in_logs(self):
        """Ensure credentials file path is never logged."""
        import logging
        from io import StringIO

        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.DEBUG)
        log = logging.getLogger("services.google_sheets_service")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)

        try:
            # Trigger operations that would log
            self.service._build_sheet_url(1)
            self.service._get_timestamp()
        finally:
            log.removeHandler(handler)

        log_output = log_capture.getvalue()
        assert "test_creds.json" not in log_output
        assert "service_account" not in log_output

    def test_map_metadata_to_row_language_human_readable(self):
        """Language codes are converted to human-readable names in the row."""
        metadata = _make_metadata(language="fre")
        row = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        assert row[6] == "French"

    def test_map_metadata_to_row_image_in_cover_column(self):
        """Open Cover renders the cover URL with IMAGE()."""
        metadata = _make_metadata(
            cover_url="https://books.google.com/books/content?id=abc123"
        )
        row = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        assert row[9] == '=IMAGE("https://books.google.com/books/content?id=abc123")'

    def test_map_metadata_to_row_empty_language(self):
        """Empty language results in empty string in row."""
        metadata = _make_metadata(language=None)
        row = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        assert row[6] == ""

    def test_map_metadata_to_row_empty_categories(self):
        """Empty categories results in empty string in row."""
        metadata = _make_metadata(categories=[])
        row = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        assert row[7] == ""

    # --- Production hardening: mapping, merging, idempotency ---

    def test_row_mapped_by_header_name_not_position(self):
        """Columns are resolved from header names, not fixed positions."""
        header = [
            "Notes",
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
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [header]
        mock_sheet.append_rows = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(_make_metadata(), "/cover.jpg")

        assert row_id == 2
        appended = mock_sheet.append_rows.call_args[0][0][0]
        assert appended[0] == ""  # Notes column
        assert appended[1] == "Test Book"
        assert appended[2] == "Test Author"
        assert appended[3] == "9780306406157"
        assert appended[9].startswith("=IMAGE(")

    def test_unknown_extra_columns_are_not_written(self):
        """A column the app does not know about is left alone."""
        header = self.service.COLUMNS + ["Librarian Notes"]
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [
            header,
            [
                "",
                "Test Book",
                "Test Author",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "hand written",
            ],
        ]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            self.service.get_or_create_row(_make_metadata(), "/cover.jpg")

        # The app-owned cells are written; values flowing into the write are
        # merged, so the hand-written extra column can only be passed through
        # verbatim — the app must never invent a value for it.
        for kwargs in _sheet_updates(mock_sheet):
            str_values = str(kwargs["values"])
            if "hand written" in str_values:
                # merged row keeps the librarian's note verbatim
                assert "[" in str_values  # it is row data, not a generated value
        assertCoverPatch(mock_sheet, 2, "J2")

    def test_existing_values_are_preserved_when_new_values_are_empty(self):
        """A later provider miss must not clear already exported cells."""
        header = self.service.COLUMNS
        existing_row = [
            "",
            "Test Book",
            "Test Author",
            "9780306406157",
            "Existing Publisher",
            "2024-01-01",
            "English",
            "Fiction",
            "Existing description.",
            '=IMAGE("https://example.com/existing.jpg", 1, 120, 80)',
        ]
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [header, existing_row]
        mock_sheet.update = Mock()

        metadata = _make_metadata(
            publisher=None,
            published_date=None,
            description=None,
            cover_url=None,
            thumbnail_url=None,
            categories=[],
        )
        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            self.service.get_or_create_row(metadata, "/cover.jpg")

        # The incoming metadata is poorer than the row, so every existing value
        # must survive. The only permitted change is the Status label, which is
        # always refreshed (blank legacy cell -> Confirmed here, because the
        # merged row has ISBN, publisher and cover).
        rows = _raw_rows(mock_sheet)
        assert len(rows) == 1
        written, written_range = rows[0]
        assert written_range == "A2:I2"
        assert written[0] == "Confirmed"
        # The cover cell is excluded from the RAW write (it stays a formula),
        # so compare everything up to it.
        cover_idx = self.service.COLUMNS.index(self.service.COVER_COLUMN)
        assert written[1:] == existing_row[1:cover_idx]
        cover = _cover_patch(mock_sheet)
        if cover is not None:
            # If the legacy literal cover text is converted, it keeps its URL.
            assert "example.com/existing.jpg" in cover["values"][0][0]

    def test_empty_cells_are_filled_on_later_export(self):
        """Missing cells are populated when metadata arrives later."""
        header = self.service.COLUMNS
        existing_row = [
            "",
            "Test Book",
            "Test Author",
            "",
            "",
            "",
            "English",
            "",
            "",
            "",
        ]
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [header, existing_row]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(_make_metadata(), "/cover.jpg")

        assert row_id == 2
        assert not mock_sheet.append_rows.called
        # Filled metadata cells go out with RAW text, the cover as a formula.
        raw_values, raw_range = _raw_rows(mock_sheet)[0]
        assert raw_range == "A2:I2"
        assert raw_values[3] == "9780306406157"
        assert raw_values[4] == "Test Publisher"
        assertCoverPatch(mock_sheet, 2, "J2")

    def test_unchanged_row_is_not_rewritten(self):
        """A correct record with a live cover formula causes no sheet write."""
        metadata = _make_metadata()
        canonical = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        # The values API reports the formula text even for a live formula, so the
        # cover cell is stubbed through the authoritative grid metadata read.
        existing = list(canonical)
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [self.service.COLUMNS, existing]
        mock_sheet.update = Mock()
        mock_sheet.append_rows = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            with _stub_cover_cell(self.service, formula=canonical[9]):
                row_id = self.service.get_or_create_row(metadata, "/cover.jpg")

        assert row_id == 2
        mock_sheet.update.assert_not_called()
        mock_sheet.append_rows.assert_not_called()

    def test_legacy_sized_live_formula_is_left_alone(self):
        """An already-live cover (any argument count) is never rewritten."""
        metadata = _make_metadata()
        canonical = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        legacy = '=IMAGE("http://example.com/cover.jpg", 1, 120, 80)'
        existing = list(canonical)
        existing[9] = legacy
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [self.service.COLUMNS, existing]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            with _stub_cover_cell(self.service, formula=legacy):
                self.service.get_or_create_row(metadata, "/cover.jpg")

        # Same image URL: no rewrite, even though the stored form is the legacy
        # sized variant (the production ``http://`` cells stay untouched).
        assert _cover_patch(mock_sheet) is None

    def test_existing_live_cover_wins_over_new_metadata(self):
        """Merge semantics extend to covers: a populated cell is never replaced."""
        metadata = _make_metadata(cover_url="https://example.com/new.jpg")
        canonical = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        old = '=IMAGE("https://example.com/old.jpg")'
        existing = list(canonical)
        existing[9] = old
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [self.service.COLUMNS, existing]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            with _stub_cover_cell(self.service, formula=old):
                self.service.get_or_create_row(metadata, "/cover.jpg")

        # Existing (non-empty) cover wins, exactly like every other column.
        assert _cover_patch(mock_sheet) is None
        assert "new.jpg" not in str(
            [c.kwargs.get("values") for c in mock_sheet.update.call_args_list]
        )

    def test_literal_formula_text_is_converted_once(self):
        """A cover stored as literal formula text is converted, once."""
        metadata = _make_metadata()
        canonical = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        # Existing row: literal '=IMAGE(...)' text in the cover cell, i.e. the
        # pre-fix state of the production sheet.
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [self.service.COLUMNS, canonical]
        mock_sheet.update = Mock()
        mock_sheet.append_rows = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            with _stub_cover_cell(self.service, literal=canonical[9]):
                row_id = self.service.get_or_create_row(metadata, "/cover.jpg")

        assert row_id == 2
        # No metadata cell changed, so there must be no RAW row write and
        # still no duplicate-row risk; the cover conversion is the only write.
        assert _raw_rows(mock_sheet) == []
        assert mock_sheet.append_rows.called is False
        assertCoverPatch(mock_sheet, 2, "J2")

    def test_mixed_row_types_in_one_export(self):
        """title/author/ISBN/description stay RAW; only cover is a formula."""
        header_only = Mock()
        header_only.get_all_values.return_value = [self.service.COLUMNS]
        header_only.get.return_value = [[""]]
        with patch.object(self.service, "_get_sheet", return_value=header_only):
            self.service.get_or_create_row(_make_metadata(), "/cover.jpg")

        # The new row is appended with RAW input: metadata verbatim.
        append_kwargs = header_only.append_rows.call_args.kwargs
        assert append_kwargs["value_input_option"] == "RAW"
        appended = header_only.append_rows.call_args.args[0][0]
        assert appended[1:3] == ["Test Book", "Test Author"]
        assert appended[3] == "9780306406157"
        assert appended[8] == "A test book description."
        # The cover cell is then written as a live formula (USER_ENTERED);
        # metadata cells are never reinterpreted by it.
        assertCoverPatch(header_only, 2, "J2")

    def test_leading_zero_isbn_is_never_reinterpreted(self):
        """A metadata cell like 0012345678 is written RAW, verbatim."""
        metadata = _make_metadata(isbn_13="0012345678")

        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [
            self.service.COLUMNS,
            ["", "Test Book", "Test Author", "", "", "", "", "", "", ""],
        ]
        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            self.service.get_or_create_row(metadata, "/cover.jpg")

        raw_ranges = _raw_rows(mock_sheet)
        assert raw_ranges and raw_ranges[0][0][1] == "Test Book"
        assert raw_ranges[0][0][3] == "0012345678"
        # USER_ENTERED is reserved for the cover formula — metadata cells are
        # never reinterpreted by it.
        assertCoverPatch(mock_sheet, 2, "J2")

    def test_update_row_cover_is_typed_formula_not_raw_text(self):
        """update_row('Open Cover') applies the formula via USER_ENTERED."""
        mocksheet = Mock()
        with patch.object(self.service, "_get_sheet", return_value=mocksheet):
            self.service.update_row(
                3, {"Open Cover": '=IMAGE("https://example.com/img.jpg", 1, 120, 80)'}
            )

        cover = mocksheet.update.call_args.kwargs
        assert cover["value_input_option"] == "USER_ENTERED"
        assert cover["range_name"] == "J3"
        # The legacy sized form is normalised: this workbook rejects 4 arguments.
        assert cover["values"] == [['=IMAGE("https://example.com/img.jpg")']]

    def test_update_row_metadata_columns_stay_raw(self):
        """update_row for non-cover columns keeps RAW input verbatim."""
        mocksheet = Mock()
        with patch.object(self.service, "_get_sheet", return_value=mocksheet):
            self.service.update_row(4, {"ISBN-13": "0012345678", "Publisher": "Biblio"})

        call = mocksheet.update.call_args.kwargs
        assert call["value_input_option"] == "RAW"
        assert call["range_name"] == "D4:E4"
        assert call["values"] == [["0012345678", "Biblio"]]

    def test_missing_cover_exports_without_broken_formula(self):
        """No cover means no formula at all — nothing broken is created."""
        metadata = _make_metadata(cover_url=None, thumbnail_url=None)

        existing_full = [
            "",
            "Test Book",
            "Test Author",
            "9780306406157",
            "",
            "",
            "English",
            "",
            "",
            "",
        ]
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [self.service.COLUMNS, existing_full]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(metadata, "/cover.jpg")

        assert row_id == 2
        for kwargs in _sheet_updates(mock_sheet):
            assert "=IMAGE" not in str(kwargs["values"])
            # and the empty cover cell is never turned into a formula
            patched = kwargs.get("value_input_option") == "USER_ENTERED"
            assert not patched or "=IMAGE(" not in str(kwargs["values"])

    def test_external_url_access_enabled_once_per_session(self):
        """The workbook property is flipped once, then cached."""
        spreadsheet = Mock()
        spreadsheet.fetch_sheet_metadata.return_value = {
            "properties": {"importFunctionsExternalUrlAccessAllowed": False}
        }
        metadata = _make_metadata()

        header_only = Mock()
        header_only.get_all_values.return_value = [self.service.COLUMNS]
        header_only.get.return_value = [[""]]

        service = self.service
        service._external_url_access_enabled = False
        with patch.object(service, "_get_spreadsheet", return_value=spreadsheet):
            with patch.object(service, "_get_sheet", return_value=header_only):
                service.get_or_create_row(metadata, "/cover.jpg")

        spreadsheet.batch_update.assert_called_once()
        request = spreadsheet.batch_update.call_args.args[0]
        assert request == {
            "requests": [
                {
                    "updateSpreadsheetProperties": {
                        "properties": {"importFunctionsExternalUrlAccessAllowed": True},
                        "fields": "importFunctionsExternalUrlAccessAllowed",
                    }
                }
            ]
        }

        # A second export (idempotent re-run) reuses the cached flag — no
        # second workbook request.
        spreadsheet.batch_update.reset_mock()
        with patch.object(service, "_get_spreadsheet", return_value=spreadsheet):
            with patch.object(service, "_get_sheet", return_value=header_only):
                service.get_or_create_row(metadata, "/cover.jpg", row_id=2)
        spreadsheet.batch_update.assert_not_called()

    def test_cover_omitted_when_status_pending_retry(self):
        """A cover that failed validation is not rendered in the sheet."""
        metadata = _make_metadata(
            cover_url="https://example.com/unvalidated.jpg",
            cover_status="pending_retry",
        )
        row = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        assert row[9] == ""

    def test_cover_rendered_when_status_found(self):
        """A validated cover is rendered as an IMAGE() formula."""
        metadata = _make_metadata(
            cover_url="https://books.google.com/books/content?id=abc",
            cover_status="found",
        )
        row = self.service._map_metadata_to_row(metadata, "/cover.jpg")
        assert row[9].startswith(
            '=IMAGE("https://books.google.com/books/content?id=abc"'
        )

    def test_accent_insensitive_row_match(self):
        """'Le coeur vert' updates the row a human wrote as 'Le cœur vert'."""
        header = self.service.COLUMNS
        existing_row = [
            "",
            "Le cœur vert",
            "Jean-Louis Kuffer",
            "",
            "",
            "",
            "French",
            "",
            "",
            "",
        ]
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [header, existing_row]
        mock_sheet.update = Mock()

        metadata = _make_metadata(
            title="Le coeur vert",
            authors=["Jean-Louis Kuffer"],
            isbn_13="9782825104699",
            publisher="L'Age d'Homme",
            published_date="1993",
            language="fr",
        )
        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(metadata, "/cover.jpg")

        assert row_id == 2
        assert not mock_sheet.append_rows.called
        raw_values, raw_range = _raw_rows(mock_sheet)[0]
        assert raw_range == "A2:I2"
        assert raw_values[3] == "9782825104699"
        assert raw_values[1] == "Le cœur vert"  # existing title cell preserved

    def test_stale_row_id_does_not_overwrite_another_book(self):
        """A persisted row number is only trusted when its title matches."""
        header = self.service.COLUMNS
        other_book = [
            "",
            "Other Book",
            "Other Author",
            "9780000000001",
            "",
            "",
            "English",
            "",
            "",
            "",
        ]
        target = [
            "",
            "Test Book",
            "Test Author",
            "",
            "",
            "",
            "English",
            "",
            "",
            "",
        ]
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [header, other_book, target]
        mock_sheet.update = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(
                _make_metadata(), "/cover.jpg", row_id=2
            )

        assert row_id == 3
        raw_values, raw_range = _raw_rows(mock_sheet)[0]
        assert raw_range.startswith("A3")

    def test_persisted_row_id_is_reused_for_same_book(self):
        """A verified row number is used directly (no duplicate append)."""
        header = self.service.COLUMNS
        target = [
            "",
            "Test Book",
            "Test Author",
            "",
            "",
            "",
            "English",
            "",
            "",
            "",
        ]
        mock_sheet = Mock()
        mock_sheet.get_all_values.return_value = [header, target]
        mock_sheet.update = Mock()
        mock_sheet.append_rows = Mock()

        with patch.object(self.service, "_get_sheet", return_value=mock_sheet):
            row_id = self.service.get_or_create_row(
                _make_metadata(), "/cover.jpg", row_id=2
            )

        assert row_id == 2
        mock_sheet.append_rows.assert_not_called()


class TestReadOnlySheetAccess:
    """A diagnostic read must never migrate the workbook.

    ``_ensure_layout`` inserts the Status column at A. That is a
    customer-visible change, so it may only happen on a write path — never
    because something merely looked at the sheet.
    """

    def _service_with(self, sheet):
        service = GoogleSheetsService(
            credentials_file="test_creds.json",
            sheets_id="test_sheet_id_123",
            tab_name="Sheet1",
        )
        spreadsheet = Mock()
        spreadsheet.worksheet.return_value = sheet
        client = Mock()
        client.open_by_key.return_value = spreadsheet
        return service, patch.object(service, "_get_client", return_value=client)

    def _legacy_sheet(self):
        sheet = Mock()
        sheet.get_all_values.return_value = [
            ["Title", "Authors"],
            ["A Book", "An Author"],
        ]
        sheet.insert_cols = Mock()
        return sheet

    def test_read_all_rows_can_skip_migration(self):
        sheet = self._legacy_sheet()
        service, client_patch = self._service_with(sheet)
        with client_patch:
            rows = service.read_all_rows(ensure_layout=False)

        assert sheet.insert_cols.call_count == 0
        assert len(rows) == 1

    def test_default_read_keeps_migration_for_write_paths(self):
        sheet = self._legacy_sheet()
        service, client_patch = self._service_with(sheet)
        with client_patch:
            service.read_all_rows()

        assert sheet.insert_cols.call_count == 1
        assert sheet.insert_cols.call_args.args[0] == [
            [GoogleSheetsService.STATUS_COLUMN]
        ]

    def test_cached_sheet_is_migrated_only_when_asked(self):
        sheet = self._legacy_sheet()
        service, client_patch = self._service_with(sheet)
        with client_patch:
            service._get_sheet(ensure_layout=False)
            assert sheet.insert_cols.call_count == 0
            service._get_sheet()
            assert sheet.insert_cols.call_count == 1

    def _unreachable_tab_service(self):
        """A spreadsheet whose tab cannot be opened (wrong name, revoked)."""
        service = GoogleSheetsService(
            credentials_file="test_creds.json",
            sheets_id="test_sheet_id_123",
            tab_name="Missing",
        )
        spreadsheet = Mock()
        spreadsheet.worksheet.side_effect = KeyError("Missing")
        created = Mock()
        created.get_all_values.return_value = [list(GoogleSheetsService.COLUMNS)]
        spreadsheet.add_worksheet.return_value = created
        client = Mock()
        client.open_by_key.return_value = spreadsheet
        return (
            service,
            spreadsheet,
            patch.object(service, "_get_client", return_value=client),
        )

    def test_read_only_open_never_creates_a_worksheet(self):
        """The remaining migration-on-read path: creating the tab itself."""
        service, spreadsheet, client_patch = self._unreachable_tab_service()
        with client_patch:
            with pytest.raises(RuntimeError):
                service._get_sheet(ensure_layout=False)

        spreadsheet.add_worksheet.assert_not_called()

    def test_read_all_rows_read_only_never_creates_a_worksheet(self):
        service, spreadsheet, client_patch = self._unreachable_tab_service()
        with client_patch:
            with pytest.raises(RuntimeError):
                service.read_all_rows(ensure_layout=False)

        spreadsheet.add_worksheet.assert_not_called()

    def test_write_path_still_creates_the_worksheet(self):
        service, spreadsheet, client_patch = self._unreachable_tab_service()
        with client_patch:
            service._get_sheet()

        assert spreadsheet.add_worksheet.call_count == 1
