"""Metadata enrichment service.

Takes the final vision-resolved title + author and searches
Google Books / Open Library for supplementary fields:

ISBN-10, ISBN-13, Published Date, Language, Categories,
Description, Cover URL, Local Cover Path.

VISION TITLE + AUTHOR ARE NEVER OVERWRITTEN.
Enrichment only ADDS fields; it does not replace the vision-extracted
title/author/publisher.

If no API match is found, the book is still persisted and exported —
just with fewer enriched fields.  Rejection is NOT allowed.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional

from core.logger import setup_logger
from models.book_metadata import BookMetadata
from services.book_lookup_service import BookLookupService

logger = setup_logger(__name__)


class MetadataEnrichmentService:
    """Enriches a BookMetadata object with supplementary fields
    from Google Books / Open Library.

    Uses the vision-resolved title + author as the search identity.
    Existing vision fields are never overwritten.
    """

    # Minimum fuzzy similarity for a candidate title to be considered
    # a match for the vision-extracted title.
    TITLE_MATCH_THRESHOLD: float = 0.60
    # Minimum fuzzy similarity for author match.
    AUTHOR_MATCH_THRESHOLD: float = 0.60

    def __init__(self, book_lookup: BookLookupService) -> None:
        self._lookup = book_lookup

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enrich(self, metadata: BookMetadata) -> BookMetadata:
        """Enrich *metadata* in-place with supplementary API fields.

        Only fills fields that are currently ``None``/empty on the
        metadata object.  Vision title / author / publisher are never
        overwritten.

        Returns the (possibly enriched) metadata object.
        """
        vision_title = metadata.title or metadata.vision_title
        vision_author = (
            metadata.authors[0] if metadata.authors else metadata.vision_author
        )

        if not vision_title:
            logger.warning(
                "Cannot enrich book %s — no title available",
                metadata.drive_file_id,
            )
            # Even without a title, apply vision-publisher fallback
            if not metadata.publisher and metadata.vision_publisher:
                metadata.publisher = metadata.vision_publisher
            return metadata

        logger.info(
            "Enriching '%s' by '%s' (drive=%s)",
            vision_title[:50],
            vision_author or "(no author)",
            metadata.drive_file_id,
        )

        # ------------------------------------------------------------------
        # ISBN-first enrichment (edition-specific)
        # When the selected candidate carries a verified ISBN, that ISBN is
        # the strongest edition identity available.  Prefer it over title
        # matching so edition-specific fields (page count, publisher, date,
        # language) belong to the right edition.
        # ------------------------------------------------------------------
        isbn_candidate = self._find_isbn_candidate(metadata)
        if isbn_candidate is not None:
            logger.info(
                "Enriching '%s' from ISBN-matched edition (isbn=%s, source=%s)",
                vision_title[:50],
                metadata.isbn_13 or metadata.isbn_10,
                isbn_candidate.source or "unknown",
            )
            self._merge_enrichment(metadata, isbn_candidate)
            if isbn_candidate.open_library_work_key:
                self._enrich_from_work_details(
                    metadata, isbn_candidate.open_library_work_key
                )

        try:
            candidates = self._lookup.search_by_title_author(
                vision_title, vision_author
            )
        except Exception as e:
            logger.warning("metadata enrichment lookup failed (title+author): %s", e)
            candidates = []

        if not candidates:
            # Fallback: try title-only search
            try:
                candidates = self._lookup.search_by_title(vision_title)
            except Exception as e:
                logger.warning("metadata enrichment lookup failed (title only): %s", e)
                candidates = []

        if isbn_candidate is not None and not self._has_empty_fields(metadata):
            # ISBN-matched edition already supplied every enrichment field;
            # do not spend extra provider calls on title matching.
            candidates = []
            logger.info(
                "ISBN enrichment complete for '%s' — skipping title search",
                vision_title[:50],
            )

        if not candidates:
            logger.info(
                "No enrichment candidates for '%s' — book will be "
                "exported with vision-only data",
                vision_title[:50],
            )
        else:
            best = self._select_best_candidate(candidates, vision_title, vision_author)

            if best is not None:
                self._merge_enrichment(metadata, best)

                # Fetch detailed work/edition info from Open Library
                # to get additional fields not available in search results
                if best.open_library_work_key:
                    self._enrich_from_work_details(metadata, best.open_library_work_key)

                # Also try fetching OL work details for Google Books matches
                # (OL may have ISBNs/categories/description that GB missed)
                if best.source != "open_library" and getattr(
                    self._lookup, "_open_library_enabled", False
                ):
                    ol_candidates = self._lookup._search_open_library_by_title(
                        vision_title, vision_author
                    )
                    if ol_candidates:
                        ol_best = self._select_best_candidate(
                            ol_candidates, vision_title, vision_author
                        )
                        if ol_best and ol_best.open_library_work_key:
                            self._enrich_from_work_details(
                                metadata, ol_best.open_library_work_key
                            )

                # Fallback: try Google Books for remaining empty fields
                if self._has_empty_fields(metadata):
                    self._enrich_from_google_books(
                        metadata, vision_title, vision_author
                    )

        # Last resort: infer language from multiple evidence sources
        # (publisher, ISBN prefix, title script, author diacritics)
        # when no API source provided one (e.g., French/Russian titles
        # with no OL match)
        if not metadata.language:
            metadata.language = self._infer_language(metadata)
        else:
            # Normalize to 3-letter ISO 639-2 code
            metadata.language = self._normalize_language(metadata.language)

        # Last resort: use vision-derived publisher if still empty
        if not metadata.publisher and metadata.vision_publisher:
            metadata.publisher = metadata.vision_publisher

        # Phase 35B: Drive images are NEVER used as final covers.
        # Clean external covers are retrieved separately by CleanCoverService.
        # If no cover URL was found, cover_url stays None — the book is still
        # exported without a cover (never using the Drive photo as fallback).

        logger.info(
            "Enriched '%s' with ISBN=%s, date=%s, lang=%s, categories=%s, " "cover=%s",
            vision_title[:50],
            metadata.isbn_10 or metadata.isbn_13 or "none",
            metadata.published_date or "none",
            metadata.language or "none",
            len(metadata.categories) if metadata.categories else 0,
            "yes" if metadata.cover_url else "no",
        )
        return metadata

    # ------------------------------------------------------------------
    # ISBN-first edition lookup
    # ------------------------------------------------------------------

    def _find_isbn_candidate(self, metadata: BookMetadata) -> Optional[BookMetadata]:
        """Return the provider candidate whose ISBN equals the book's ISBN.

        Only an exact ISBN match is accepted: this is what makes the result
        edition-specific.  Returns ``None`` when the book has no ISBN, when
        the provider fails, or when no candidate carries that ISBN.
        """
        isbn = metadata.isbn_13 or metadata.isbn_10
        if not isbn:
            return None

        normalised = self._normalise_isbn(isbn)
        if not normalised:
            return None

        try:
            candidates = self._lookup.search_by_isbn(isbn)
        except Exception as exc:
            logger.warning("ISBN enrichment lookup failed: %s", exc)
            return None

        if not isinstance(candidates, list):
            return None

        for candidate in candidates:
            for value in (candidate.isbn_13, candidate.isbn_10):
                if value and self._normalise_isbn(value) == normalised:
                    return candidate
        return None

    @staticmethod
    def _normalise_isbn(value: Optional[str]) -> str:
        """Normalise an ISBN for comparison (digits plus trailing X)."""
        if not value:
            return ""
        return value.replace("-", "").replace(" ", "").strip().upper()

    # ------------------------------------------------------------------
    # Candidate selection
    # ------------------------------------------------------------------

    def _select_best_candidate(
        self,
        candidates: list[BookMetadata],
        vision_title: str,
        vision_author: Optional[str],
    ) -> Optional[BookMetadata]:
        """Pick the best candidate whose title/author matches the
        vision-extracted identity.

        A candidate is acceptable only if:
        - title similarity >= TITLE_MATCH_THRESHOLD, AND
        - (author is not provided OR author similarity >= AUTHOR_MATCH_THRESHOLD)
        """
        best: Optional[BookMetadata] = None
        best_score: float = 0.0

        for cand in candidates:
            score = self._candidate_score(cand, vision_title, vision_author)
            if score > best_score:
                best_score = score
                best = cand

        if best_score < self.TITLE_MATCH_THRESHOLD:
            return None

        return best

    def _candidate_score(
        self,
        candidate: BookMetadata,
        vision_title: str,
        vision_author: Optional[str],
    ) -> float:
        """Compute a composite score for a candidate against the
        vision-extracted identity.
        """
        title_sim = self._fuzzy_sim(candidate.title, vision_title)

        # If author is provided, require a match.
        if vision_author and candidate.authors:
            author_sim = self._fuzzy_sim(candidate.authors[0], vision_author)
        else:
            author_sim = 1.0 if not vision_author else 0.0

        # Weighted: title is primary, author secondary
        return title_sim * 0.7 + author_sim * 0.3

    @staticmethod
    def _fuzzy_sim(a: Optional[str], b: Optional[str]) -> float:
        """Case-insensitive fuzzy similarity between two strings."""
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

    # ------------------------------------------------------------------
    # Merging
    # ------------------------------------------------------------------

    def _merge_enrichment(
        self,
        metadata: BookMetadata,
        candidate: BookMetadata,
    ) -> None:
        """Merge fields from *candidate* into *metadata* without
        overwriting any existing non-None values.
        """
        # ISBN — derive the missing one if possible
        if not metadata.isbn_10 and candidate.isbn_10:
            metadata.isbn_10 = self._validate_isbn_10(candidate.isbn_10)
        if not metadata.isbn_13 and candidate.isbn_13:
            metadata.isbn_13 = self._validate_isbn_13(candidate.isbn_13)

        # If we have one ISBN but not the other, try to derive
        self._derive_missing_isbn(metadata)

        # Subtitle — only if not already set (never rewrites the title)
        if not metadata.subtitle and candidate.subtitle:
            metadata.subtitle = candidate.subtitle

        # Page count — only if not already set
        if not metadata.page_count and candidate.page_count:
            try:
                page_count = int(candidate.page_count)
            except (TypeError, ValueError):
                page_count = 0
            if page_count > 0:
                metadata.page_count = page_count

        # Published date — only if not already set
        if not metadata.published_date and candidate.published_date:
            metadata.published_date = candidate.published_date

        # Language — only if not already set
        if not metadata.language and candidate.language:
            metadata.language = self._normalize_language(candidate.language)

        # Categories — only if not already set
        if not metadata.categories and candidate.categories:
            metadata.categories = list(candidate.categories)

        # Description — only if not already set
        if not metadata.description and candidate.description:
            metadata.description = candidate.description

        # Cover URL — only if not already set
        if not metadata.cover_url and candidate.cover_url:
            metadata.cover_url = candidate.cover_url
        if not metadata.thumbnail_url and candidate.thumbnail_url:
            metadata.thumbnail_url = candidate.thumbnail_url

        # Image links — merge
        if candidate.image_links:
            merged = dict(metadata.image_links)
            merged.update(
                {
                    k: v
                    for k, v in candidate.image_links.items()
                    if k not in merged or not merged[k]
                }
            )
            metadata.image_links = merged

        # Publisher — enrichment may add; never overwrite vision-derived
        if not metadata.publisher and candidate.publisher:
            metadata.publisher = candidate.publisher

        # Source tracking
        if metadata.source:
            sources = metadata.source.split(",")
            if candidate.source not in sources:
                sources.append(candidate.source)
            metadata.source = ",".join(sources)
        else:
            metadata.source = candidate.source or ""

    # ------------------------------------------------------------------
    # Google Books fallback enrichment
    # ------------------------------------------------------------------

    @staticmethod
    def _has_empty_fields(metadata: BookMetadata) -> bool:
        """Check if any enrichment target field is missing."""
        return bool(
            not metadata.isbn_10
            or not metadata.isbn_13
            or not metadata.publisher
            or not metadata.published_date
            or not metadata.language
            or not metadata.page_count
            or not metadata.categories
            or not metadata.description
            or not metadata.cover_url
        )

    def _enrich_from_google_books(
        self, metadata: BookMetadata, vision_title: str, vision_author: Optional[str]
    ) -> None:
        """Use Google Books as a secondary enrichment source for fields
        still missing after Open Library enrichment.

        Only fills None/empty fields — never overwrites existing values
        or vision title/author.  API failures are caught silently.
        """
        # Skip if no Google Books API key configured
        if not getattr(self._lookup, "_google_books_api_key", None):
            return

        try:
            candidates = self._lookup.search_google_books_only(
                vision_title, vision_author
            )
        except Exception as exc:
            logger.debug("Google Books enrichment failed: %s", exc)
            return

        if not candidates:
            return

        # Pick the best Google Books candidate by similarity
        best = self._select_best_candidate(candidates, vision_title, vision_author)
        if best is None:
            return

        self._merge_enrichment(metadata, best)

    # ------------------------------------------------------------------
    # Open Library work detail enrichment
    # ------------------------------------------------------------------

    def _enrich_from_work_details(self, metadata: BookMetadata, work_key: str) -> None:
        """Fetch detailed Work/Edition data from Open Library and merge
        additional fields (ISBNs, publisher, categories, description,
        cover_url) into *metadata*.

        Only fills None/empty fields — never overwrites existing values.
        API failures are caught silently (book is still exported).
        """
        try:
            details = self._lookup.fetch_open_library_work_details(work_key)
        except Exception as exc:
            logger.debug("Work detail fetch failed for %s: %s", work_key, exc)
            return

        if not details:
            return

        # ISBN-10
        if not metadata.isbn_10 and details.get("isbn_10"):
            metadata.isbn_10 = self._validate_isbn_10(details["isbn_10"])
        # ISBN-13
        if not metadata.isbn_13 and details.get("isbn_13"):
            metadata.isbn_13 = self._validate_isbn_13(details["isbn_13"])
        # Derive missing ISBN from the other
        self._derive_missing_isbn(metadata)

        # Publisher
        if not metadata.publisher and details.get("publisher"):
            metadata.publisher = details["publisher"]

        # Published date — preserve source precision, don't overwrite
        if not metadata.published_date and details.get("published_date"):
            metadata.published_date = details["published_date"]

        # Language
        if not metadata.language and details.get("language"):
            metadata.language = self._normalize_language(details["language"])

        # Page count (edition-level)
        if not metadata.page_count and details.get("page_count"):
            try:
                page_count = int(details["page_count"])
            except (TypeError, ValueError):
                page_count = 0
            if page_count > 0:
                metadata.page_count = page_count

        # Categories (subjects)
        if not metadata.categories and details.get("categories"):
            metadata.categories = list(details["categories"])

        # Description
        if not metadata.description and details.get("description"):
            metadata.description = details["description"]

        # Cover URL
        if not metadata.cover_url and details.get("cover_url"):
            metadata.cover_url = details["cover_url"]

    # ------------------------------------------------------------------
    # Language inference
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_language_from_title(title: Optional[str]) -> Optional[str]:
        """Infer language from title script as a last resort.

        Only infers when evidence is strong:
        - Cyrillic characters => rus
        - French apostrophe patterns (l', d', n') => fre
        - French accented characters => fre
        - Common French words/articles => fre
        - Otherwise returns None (don't guess)
        """
        if not title or not title.strip():
            return None

        cyrillic_chars = sum(1 for ch in title if 0x0400 <= ord(ch) <= 0x04FF)
        if cyrillic_chars > 0:
            return "rus"

        french_accents = "éèàôâûêëïçÉÈÀÔÂÛÊËÏÇ"
        title_lower = title.lower()
        has_french_apostrophe = any(
            w in title_lower for w in ["l'", "d'", "n'", "m'", "s'", "t'"]
        )
        has_french_accents = any(ch in french_accents for ch in title)
        # Common French words/articles that are unlikely in English titles
        french_words = [
            "de la",
            "des ",
            "du ",
            "dans ",
            "et la",
            "et le",
            "le ",
            "la ",
            "les ",
            "une ",
            "un ",
            "d'une",
            "d'un",
        ]
        has_french_words = any(w in title_lower for w in french_words)
        if has_french_apostrophe or has_french_accents or has_french_words:
            return "fre"

        return None

    @staticmethod
    def _infer_language_from_isbn(isbn_13: Optional[str]) -> Optional[str]:
        """Infer language from ISBN-13 prefix (EAN/UPC group code).

        ISBN-13 group prefixes: https://en.wikipedia.org/wiki/ISBN#Overview
        - 978-0, 978-1, 979-10 => English (eng)
        - 978-2, 979-2         => French (fre)
        - 978-3, 979-3         => German (ger)
        - 978-4, 979-4         => Japanese (jpn)
        - 978-5, 979-5         => Russian (rus)
        - 978-6, 979-6         => Chinese (chi)
        - 978-7, 979-7         => Japanese (jpn, alternative)
        - 978-8, 979-8         => Italian (ita)
        - 978-9, 979-9         => Italian (ita, alternative)
        - 978-83, 979-83       => Polish (pol)
        - 978-950, 979-950     => Spanish (spa)
        - 978-960, 979-960     => Greek (gre)
        """
        if not isbn_13:
            return None
        cleaned = isbn_13.replace("-", "").replace(" ", "")
        if len(cleaned) < 4:
            return None
        ean = cleaned[:3]  # 978 or 979
        if ean not in ("978", "979"):
            return None

        rest = cleaned[3:]  # digits after 978/979
        # Multi-digit group codes checked before single-digit to avoid ambiguity
        # (e.g., 83 for Polish vs 8 for Italian)
        multi_digit = {
            "83": "pol",  # Polish
            "950": "spa",  # Spanish
            "953": "spa",
            "955": "spa",
            "958": "spa",
            "960": "gre",  # Greek
            "962": "zho",
            "963": "hun",
            "965": "ces",
            "966": "ukr",
            "967": "ind",
            "968": "spa",
            "971": "fil",  # Filipino
            "972": "por",  # Portuguese
            "973": "rom",  # Romanian
            "974": "spa",
            "975": "tur",  # Turkish
            "976": "spa",
            "977": "ara",  # Arabic
            "978": "spa",
            "979": "spa",
            "980": "spa",
            "981": "spa",
            "982": "spa",
            "983": "spa",
            "984": "spa",
            "985": "spa",
            "986": "spa",
            "987": "spa",
            "988": "spa",
            "989": "spa",
            "990": "spa",
            "991": "spa",
            "992": "spa",
            "993": "spa",
            "995": "spa",
            "996": "spa",
            "997": "spa",
            "998": "spa",
            "999": "spa",
        }
        for length in (3, 2):
            if len(rest) >= length and rest[:length] in multi_digit:
                return multi_digit[rest[:length]]
        # Single-digit group codes
        single_digit = {
            "0": "eng",
            "1": "eng",
            "2": "fre",
            "3": "ger",
            "4": "jpn",
            "5": "rus",
            "6": "chi",
            "7": "jpn",
            "8": "ita",
            "9": "ita",
        }
        if rest:
            return single_digit.get(rest[0])
        return None

    @staticmethod
    def _infer_language_from_publisher(publisher: Optional[str]) -> Optional[str]:
        """Infer language from known French publisher names.

        Some French publishers are recognizable by name even when the
        title has no French text indicators.
        """
        if not publisher:
            return None
        pub_lower = publisher.lower().strip()
        french_publishers = [
            "l'age d'homme",
            "l'âge d'homme",
            "age d'homme",
            "éditions du seuil",
            "du seuil",
            "grasset",
            "gallimard",
            "le rouger",
            "actes sud",
            "les éditions",
            "éditions",
            "les éditions du",
            "librairie",
            "collectif",
            "darguin",
            "d'arguin",
            "la découverte",
            "la d'école des",
            "presses universitaires",
            "écrits",
        ]
        for fp in french_publishers:
            if fp in pub_lower:
                return "fre"
        return None

    def _infer_language(self, metadata: BookMetadata) -> Optional[str]:
        """Infer language from multiple evidence sources.

        Priority:
        1. Vision-derived publisher (French publisher names)
        2. ISBN-13 group prefix
        3. Title script/diacritics (French/Russian/Cyrillic)
        4. Author name diacritics
        """
        # 1. Publisher-based inference
        lang = self._infer_language_from_publisher(
            metadata.vision_publisher or metadata.publisher
        )
        if lang:
            return lang

        # 2. ISBN-13 prefix inference
        lang = self._infer_language_from_isbn(metadata.isbn_13)
        if lang:
            return lang

        # 3. Title script inference
        lang = self._infer_language_from_title(metadata.title or metadata.vision_title)
        if lang:
            return lang

        # 4. Author name diacritics (for languages where the author name has
        #    clear script indicators, e.g., Cyrillic -> Russian)
        author = metadata.vision_author or (
            ", ".join(metadata.authors) if metadata.authors else ""
        )
        if author:
            cyrillic = sum(1 for ch in author if 0x0400 <= ord(ch) <= 0x04FF)
            if cyrillic > 0:
                return "rus"

        return None

    # ------------------------------------------------------------------
    # ISBN helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_isbn_10(isbn: str) -> Optional[str]:
        """Validate an ISBN-10 string.  Returns cleaned ISBN or None."""
        cleaned = isbn.replace("-", "").replace(" ", "").upper()
        if len(cleaned) == 10 and MetadataEnrichmentService._isbn10_valid(cleaned):
            return cleaned
        return None

    @staticmethod
    def _validate_isbn_13(isbn: str) -> Optional[str]:
        """Validate an ISBN-13 string.  Returns cleaned ISBN or None."""
        cleaned = isbn.replace("-", "").replace(" ", "")
        if len(cleaned) == 13 and MetadataEnrichmentService._isbn13_valid(cleaned):
            return cleaned
        return None

    @staticmethod
    def _isbn10_valid(isbn: str) -> bool:
        """Check ISBN-10 checksum."""
        if len(isbn) != 10:
            return False
        total = 0
        for i, ch in enumerate(isbn):
            if ch == "X":
                val = 10
            elif ch.isdigit():
                val = int(ch)
            else:
                return False
            total += val * (10 - i)
        return total % 11 == 0

    @staticmethod
    def _isbn13_valid(isbn: str) -> bool:
        """Check ISBN-13 checksum."""
        if len(isbn) != 13 or not isbn.isdigit():
            return False
        total = 0
        for i, ch in enumerate(isbn):
            val = int(ch)
            if i % 2 == 0:
                total += val
            else:
                total += val * 3
        return total % 10 == 0

    @staticmethod
    def _isbn13_to_isbn10(isbn13: str) -> Optional[str]:
        """Convert ISBN-13 to ISBN-10 (only for 978-prefixed ISBNs)."""
        if not isbn13.startswith("978"):
            return None
        core = isbn13[3:12]  # 9 digits
        if len(core) != 9:
            return None
        total = 0
        for i, ch in enumerate(core):
            total += int(ch) * (10 - i)
        check = (11 - (total % 11)) % 11
        check_char = "X" if check == 10 else str(check)
        isbn10 = core + check_char
        if MetadataEnrichmentService._isbn10_valid(isbn10):
            return isbn10
        return None

    @staticmethod
    def _isbn10_to_isbn13(isbn10: str) -> Optional[str]:
        """Convert ISBN-10 to ISBN-13."""
        if len(isbn10) != 10:
            return None
        core = isbn10[:9]
        if not core.isdigit():
            return None
        prefix = "978" + core
        total = 0
        for i, ch in enumerate(prefix):
            val = int(ch)
            if i % 2 == 0:
                total += val
            else:
                total += val * 3
        check = (10 - (total % 10)) % 10
        isbn13 = prefix + str(check)
        if MetadataEnrichmentService._isbn13_valid(isbn13):
            return isbn13
        return None

    def _derive_missing_isbn(self, metadata: BookMetadata) -> None:
        """Try to derive the missing ISBN from the other one."""
        if metadata.isbn_13 and not metadata.isbn_10:
            derived = self._isbn13_to_isbn10(metadata.isbn_13)
            if derived:
                metadata.isbn_10 = derived
        elif metadata.isbn_10 and not metadata.isbn_13:
            derived = self._isbn10_to_isbn13(metadata.isbn_10)
            if derived:
                metadata.isbn_13 = derived

    # ------------------------------------------------------------------
    # Language normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_language(lang: str) -> str:
        """Normalize language codes to ISO 639-2 three-letter codes.

        Handles:
        - 2-letter codes: en, fr, de, es, ru, etc.
        - 3-letter codes: eng, fre, deu, spa, rus, etc.
        - OL language keys: /languages/fra
        - Full names: English, French, German, Spanish
        """
        # Strip Open Library language key prefix
        if lang.startswith("/languages/"):
            lang = lang.replace("/languages/", "")
        lang_clean = lang.lower().strip()
        lang_map = {
            "en": "eng",
            "eng": "eng",
            "fr": "fre",
            "fre": "fre",
            "fra": "fre",
            "de": "ger",
            "ger": "ger",
            "deu": "ger",
            "es": "spa",
            "spa": "spa",
            "it": "ita",
            "ita": "ita",
            "ru": "rus",
            "rus": "rus",
            "zh": "chi",
            "chi": "chi",
            "zho": "chi",
            "ja": "jpn",
            "jpn": "jpn",
            "pl": "pol",
            "pol": "pol",
            "nl": "dut",
            "dut": "dut",
            "pt": "por",
            "por": "por",
            "cs": "cze",
            "cze": "cze",
        }
        return lang_map.get(lang_clean, lang_clean)
