"""Tests for MetadataEnrichmentService."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from models.book_metadata import BookMetadata
from services.book_lookup_service import BookLookupService
from services.metadata_enrichment_service import MetadataEnrichmentService

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def mock_lookup() -> Mock:
    return Mock(spec=BookLookupService)


@pytest.fixture
def enrichment_service(mock_lookup: Mock) -> MetadataEnrichmentService:
    return MetadataEnrichmentService(book_lookup=mock_lookup)


@pytest.fixture
def base_metadata() -> BookMetadata:
    return BookMetadata(
        title="Test Title",
        authors=["Test Author"],
        drive_file_id="test123",
        vision_used=True,
    )


# ------------------------------------------------------------------
# Google Books metadata extraction (simulated via mock)
# ------------------------------------------------------------------


class TestGoogleBooksExtraction:
    """Tests for enrichment when Google Books returns results."""

    def test_enrich_full_metadata(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """A matching candidate should enrich all available fields."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_10="123456789X",
            isbn_13="9781234567897",
            publisher="Test Publisher",
            published_date="1990",
            language="eng",
            categories=["Fiction", "Drama"],
            description="A test book description.",
            cover_url="https://example.com/cover.jpg",
            image_links={"large": "https://example.com/large.jpg"},
            source="google_books",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        assert result.isbn_10 == "123456789X"
        assert result.isbn_13 == "9781234567897"
        assert result.publisher == "Test Publisher"
        assert result.published_date == "1990"
        assert result.language == "eng"
        assert result.categories == ["Fiction", "Drama"]
        assert result.description == "A test book description."
        assert result.cover_url == "https://example.com/cover.jpg"

    def test_google_books_api_key_absent_still_works(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Enrichment should work even without Google Books API key (OL only)."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_13="9781234567897",
            published_date="1990",
            language="eng",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        assert result.isbn_13 == "9781234567897"
        assert result.published_date == "1990"
        assert result.language == "eng"


# ------------------------------------------------------------------
# Open Library metadata extraction
# ------------------------------------------------------------------


class TestOpenLibraryExtraction:
    """Tests for enrichment when Open Library returns results."""

    def test_open_library_enrichment(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Open Library candidates should enrich fields."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            published_date="1987",
            language="fre",
            categories=["Philosophy"],
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        assert result.published_date == "1987"
        assert result.language == "fre"
        assert result.categories == ["Philosophy"]

    def test_open_library_french_title_match(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """French titles with accents should still match."""
        base_metadata.title = "L'individu et la communauté"
        base_metadata.authors = ["Théodore Litt"]

        candidate = BookMetadata(
            title="L'individu et la communauté",
            authors=["Théodore Litt"],
            published_date="1998",
            language="fre",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        assert result.published_date == "1998"
        assert result.language == "fre"


# ------------------------------------------------------------------
# ISBN extraction and validation
# ------------------------------------------------------------------


class TestISBNExtraction:

    def test_isbn_10_extracted(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_10="0306406152",
            isbn_13="9780306406157",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.isbn_10 == "0306406152"
        assert result.isbn_13 == "9780306406157"

    def test_isbn_13_only_derives_isbn_10(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If only ISBN-13 starts with 978, derive ISBN-10."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_10=None,
            isbn_13="9780306406157",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.isbn_13 == "9780306406157"
        assert result.isbn_10 == "0306406152"  # derived from ISBN-13

    def test_invalid_isbn_not_stored(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Invalid ISBN checksums should not be stored."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_10="1234567890",  # invalid checksum
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.isbn_10 is None or result.isbn_10 == ""

    def test_isbn_not_fabricated(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If no ISBN in API response, do not fabricate one."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.isbn_10 is None or result.isbn_10 == ""
        assert result.isbn_13 is None or result.isbn_13 == ""


# ------------------------------------------------------------------
# Published Date extraction
# ------------------------------------------------------------------


class TestPublishedDate:

    def test_preserve_precision(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Year-only dates should not be expanded to full dates."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            published_date="1990",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.published_date == "1990"  # not "1990-01-01"

    def test_no_fabrication(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If no published date, do not invent one."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.published_date is None or result.published_date == ""


# ------------------------------------------------------------------
# Language extraction
# ------------------------------------------------------------------


class TestLanguageExtraction:

    def test_normalize_french(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            language="fre",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.language == "fre"

    def test_normalize_from_two_letter(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            language="fr",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.language == "fre"

    def test_no_hallucination(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If no language in API, do not infer from author nationality."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.language is None or result.language == ""


# ------------------------------------------------------------------
# Categories extraction
# ------------------------------------------------------------------


class TestCategories:

    def test_categories_extracted(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            categories=["Fiction", "Philosophy"],
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.categories == ["Fiction", "Philosophy"]

    def test_no_hallucination(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.categories == [] or result.categories is None


# ------------------------------------------------------------------
# Description extraction
# ------------------------------------------------------------------


class TestDescription:

    def test_description_extracted(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            description="This is a great book about testing.",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.description == "This is a great book about testing."

    def test_no_hallucination(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If no description, do not generate one."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.description is None or result.description == ""


# ------------------------------------------------------------------
# Cover URL extraction
# ------------------------------------------------------------------


class TestCoverUrl:

    def test_cover_url_preserved(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            cover_url="https://example.com/cover.jpg",
            thumbnail_url="https://example.com/thumb.jpg",
            image_links={"large": "https://example.com/large.jpg"},
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.cover_url == "https://example.com/cover.jpg"


# ------------------------------------------------------------------
# Partial metadata from one source
# ------------------------------------------------------------------


class TestPartialMetadata:

    def test_partial_enrichment(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Candidate with only some fields should still enrich those fields."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            published_date="1985",
            language="eng",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)
        assert result.published_date == "1985"
        assert result.language == "eng"
        assert result.isbn_10 is None or result.isbn_10 == ""
        assert result.isbn_13 is None or result.isbn_13 == ""

    def test_empty_candidates_returns_original(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """0 candidates → original metadata returned unchanged."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(base_metadata)
        assert result.title == "Test Title"
        assert result.authors == ["Test Author"]
        assert result.isbn_10 is None or result.isbn_10 == ""


# ------------------------------------------------------------------
# Metadata merged from multiple sources
# ------------------------------------------------------------------


class TestMetadataMerge:

    def test_merge_does_not_overwrite_existing(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Existing vision-derived values should not be overwritten by API."""
        base_metadata.title = "Vision Title"
        base_metadata.authors = ["Vision Author"]
        base_metadata.publisher = "Vision Publisher"

        candidate = BookMetadata(
            title="API Title",
            authors=["API Author"],
            publisher="API Publisher",
            published_date="1999",
            isbn_13="9781234567897",
            source="google_books",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        # Vision title/author/publisher preserved
        assert result.title == "Vision Title"
        assert result.authors == ["Vision Author"]
        assert result.publisher == "Vision Publisher"
        # API fields added where vision had none
        assert result.published_date == "1999"
        assert result.isbn_13 == "9781234567897"

    def test_merge_multiple_candidates_best_only(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Only the best-matching candidate is used for enrichment."""
        good = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_10="0306406152",
            published_date="1990",
            source="open_library",
        )
        bad = BookMetadata(
            title="Completely Different Book",
            authors=["Different Author"],
            isbn_10="9999999999",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [bad, good]

        result = enrichment_service.enrich(base_metadata)

        assert result.isbn_10 == "0306406152"  # from good, not bad
        assert result.published_date == "1990"


# ------------------------------------------------------------------
# Wrong API candidate rejected
# ------------------------------------------------------------------


class TestWrongCandidateRejected:

    def test_wrong_title_rejected(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """A candidate with a completely different title should not enrich."""
        base_metadata.title = "L'individu et la communauté"
        base_metadata.authors = ["Théodore Litt"]

        candidate = BookMetadata(
            title="Theodori episcopi Mopsuesteni",
            authors=["Theodore, Bishop of Mopsuestia"],
            isbn_10="9999999999",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        # Should NOT have enriched with wrong candidate
        assert result.isbn_10 is None or result.isbn_10 == ""
        assert result.title == "L'individu et la communauté"

    def test_wrong_author_rejected(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """A candidate with different author should not enrich when
        the title similarity is borderline."""
        base_metadata.title = "L'INVITATION"
        base_metadata.authors = ["Blanche Berthoud"]

        candidate = BookMetadata(
            title="L'invitation au château",
            authors=["Different Author"],
            isbn_10="9999999999",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        # Title similarity alone should not be enough
        # (author mismatch should reduce score below threshold)
        # The candidate has partial title match but wrong author
        assert result.isbn_10 is None or result.isbn_10 == ""


# ------------------------------------------------------------------
# Vision title/author never overwritten
# ------------------------------------------------------------------


class TestVisionTitlesPreserved:

    def test_vision_title_not_overwritten(
        self, mock_lookup: Mock, enrichment_service: MetadataEnrichmentService
    ):
        """Vision-extracted title must remain as the final title."""
        metadata = BookMetadata(
            title="L'INVITATION",
            authors=["Blanche Berthoud"],
            drive_file_id="test",
            vision_title="L'INVITATION",
            vision_author="Blanche Berthoud",
        )
        candidate = BookMetadata(
            title="Some Other Title",
            authors=["Some Author"],
            isbn_10="1234567890",
            source="google_books",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(metadata)

        assert result.title == "L'INVITATION"
        assert result.authors == ["Blanche Berthoud"]

    def test_vision_author_not_overwritten(
        self, mock_lookup: Mock, enrichment_service: MetadataEnrichmentService
    ):
        """Vision-extracted author must remain."""
        metadata = BookMetadata(
            title="Weiser David",
            authors=["Paweł Hüelle"],
            drive_file_id="test",
        )
        candidate = BookMetadata(
            title="Weiser David",
            authors=["Wrong Author"],
            isbn_13="9781234567897",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(metadata)

        assert result.authors == ["Paweł Hüelle"]
        assert result.isbn_13 == "9781234567897"


# ------------------------------------------------------------------
# Missing metadata does NOT reject a book
# ------------------------------------------------------------------


class TestNoRejectionOnFailure:

    def test_no_candidates_book_still_valid(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If no API match found, the book should still have its vision data."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(base_metadata)

        assert result.title == "Test Title"
        assert result.authors == ["Test Author"]
        assert result.isbn_10 is None or result.isbn_10 == ""
        assert result.isbn_13 is None or result.isbn_13 == ""

    def test_api_failure_does_not_reject(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """API exceptions should not cause rejection."""
        mock_lookup.search_by_title_author.side_effect = Exception("Network error")
        mock_lookup.search_by_title.side_effect = Exception("Network error")

        result = enrichment_service.enrich(base_metadata)

        assert result.title == "Test Title"
        assert result.authors == ["Test Author"]

    def test_wrong_candidate_does_not_reject(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Wrong candidates should be ignored, not cause rejection."""
        candidate = BookMetadata(
            title="Wrong Book",
            authors=["Wrong Author"],
            isbn_10="9999999999",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]
        mock_lookup.search_by_title.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        assert result.title == "Test Title"  # vision title preserved


# ------------------------------------------------------------------
# Cover failure does NOT reject
# ------------------------------------------------------------------


class TestCoverFailureNoRejection:

    def test_no_cover_url_still_valid(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Missing cover URL is acceptable — Drive images are NEVER used as
        final covers (Phase 35B).  If no clean external cover is found,
        cover_url remains None and the book is still valid."""
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            published_date="1990",
            source="open_library",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(base_metadata)

        assert result.published_date == "1990"
        # Phase 35B: Drive images are NEVER used as final covers
        assert result.cover_url is None
        assert result.title == "Test Title"


# ------------------------------------------------------------------
# ISBN validation
# ------------------------------------------------------------------


class TestISBNValidation:

    @pytest.mark.parametrize(
        "isbn10,valid",
        [
            ("0596007124", True),  # valid
            ("0306406152", True),  # valid
            ("1234567890", False),  # invalid checksum
            ("ABCDEFGHIJ", False),  # non-numeric
            ("", False),  # empty
        ],
    )
    def test_isbn10_validation(self, isbn10, valid, enrichment_service):
        if valid:
            result = enrichment_service._validate_isbn_10(isbn10)
            assert result == isbn10
        else:
            result = enrichment_service._validate_isbn_10(isbn10)
            assert result is None

    @pytest.mark.parametrize(
        "isbn13,valid",
        [
            ("9780201379624", True),  # valid
            ("9780306406157", True),  # valid
            ("1234567890123", False),  # invalid checksum
            ("ABCDEFGHIJKLM", False),  # non-numeric
            ("", False),  # empty
        ],
    )
    def test_isbn13_validation(self, isbn13, valid, enrichment_service):
        if valid:
            result = enrichment_service._validate_isbn_13(isbn13)
            assert result == isbn13
        else:
            result = enrichment_service._validate_isbn_13(isbn13)
            assert result is None

    def test_isbn13_to_isbn10_valid_978(self):
        result = MetadataEnrichmentService._isbn13_to_isbn10("9780306406157")
        assert result == "0306406152"

    def test_isbn13_to_isbn10_invalid_prefix(self):
        result = MetadataEnrichmentService._isbn13_to_isbn10("9791234567890")
        assert result is None

    def test_isbn10_to_isbn13_valid(self):
        result = MetadataEnrichmentService._isbn10_to_isbn13("0306406152")
        assert result == "9780306406157"

    def test_isbn10_to_isbn13_invalid(self):
        result = MetadataEnrichmentService._isbn10_to_isbn13("ABCDEFGHIJ")
        assert result is None


# ------------------------------------------------------------------
# Language normalization
# ------------------------------------------------------------------


class TestLanguageNormalization:

    @pytest.mark.parametrize(
        "input,expected",
        [
            ("fr", "fre"),
            ("fra", "fre"),
            ("en", "eng"),
            ("eng", "eng"),
            ("de", "ger"),
            ("ru", "rus"),
            ("rus", "rus"),
            ("zh", "chi"),
            ("ja", "jpn"),
            ("/languages/fra", "fre"),
            ("/languages/eng", "eng"),
            ("/languages/rus", "rus"),
        ],
    )
    def test_language_normalization(self, input, expected):
        result = MetadataEnrichmentService._normalize_language(input)
        # Only exact ISO code mappings check
        if input in (
            "fr",
            "fra",
            "en",
            "eng",
            "de",
            "ger",
            "ru",
            "rus",
            "zh",
            "ja",
            "/languages/fra",
            "/languages/eng",
            "/languages/rus",
        ):
            assert result == expected


# ------------------------------------------------------------------
# Regression tests — vision title/author NEVER overwritten
# ------------------------------------------------------------------


class TestVisionPreservation:
    """Tests that vision-extracted title/author are never overwritten
    by API metadata candidates."""

    def test_vision_title_not_overwritten_by_ol_candidate(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """Even if OL returns a candidate with a different title,
        vision title must remain."""
        base_metadata.title = "L'individu et la communauté"
        base_metadata.vision_title = "L'individu et la communauté"
        base_metadata.vision_author = "Théodore Litt"
        base_metadata.authors = ["Théodore Litt"]
        base_metadata.isbn_10 = None
        base_metadata.isbn_13 = None
        base_metadata.publisher = None
        base_metadata.published_date = None
        base_metadata.language = None
        base_metadata.categories = []
        base_metadata.description = None
        base_metadata.cover_url = None

        # API returns a mismatched candidate (wrong book)
        wrong_candidate = BookMetadata(
            title="Theodori episcopi Mopsuesteni",
            authors=["Some Other Author"],
            isbn_10="1234567890",
            isbn_13="9781234567897",
            publisher="Wrong Publisher",
            published_date="1900",
            language="eng",
            categories=["History"],
            description="Wrong book description",
            cover_url="http://example.com/wrong.jpg",
            source="open_library",
            confidence=0.9,
        )
        mock_lookup.search_by_title_author.return_value = [wrong_candidate]

        result = enrichment_service.enrich(base_metadata)

        # Vision title/author must remain unchanged
        assert result.vision_title == "L'individu et la communauté"
        assert result.vision_author == "Théodore Litt"

    def test_french_books_vision_preserved(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
    ):
        """L'INVITATION by Blanche Berthoud must remain unchanged."""
        metadata = BookMetadata(
            title="L'INVITATION",
            vision_title="L'INVITATION",
            vision_author="Blanche Berthoud",
            authors=["Blanche Berthoud"],
            drive_file_id="1iZb",
            vision_used=True,
        )
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(metadata)
        assert result.vision_title == "L'INVITATION"
        assert result.vision_author == "Blanche Berthoud"

    def test_russian_book_vision_preserved(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
    ):
        """Философия общего дела by Н. Ф. Фёдоров must remain unchanged."""
        metadata = BookMetadata(
            title="Философия общего дела",
            vision_title="Философия общего дела",
            vision_author="Н. Ф. Фёдоров",
            authors=["Н. Ф. Фёдоров"],
            drive_file_id="10--",
            vision_used=True,
        )
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(metadata)
        assert result.vision_title == "Философия общего дела"
        assert result.vision_author == "Н. Ф. Фёдоров"


class TestApiFailureHandling:
    """Tests that API failures don't reject or lose books."""

    def test_api_timeout_does_not_reject(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If the API call times out, book must still be saved."""
        base_metadata.vision_title = "Test Title"
        base_metadata.vision_author = "Test Author"

        mock_lookup.search_by_title_author.side_effect = TimeoutError("timeout")
        mock_lookup.search_by_title.side_effect = TimeoutError("timeout")

        result = enrichment_service.enrich(base_metadata)
        assert result.vision_title == "Test Title"
        assert result.vision_author == "Test Author"

    def test_api_exception_does_not_reject(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """If the API raises an exception, book must still be saved."""
        mock_lookup.search_by_title_author.side_effect = Exception("API error")
        mock_lookup.search_by_title.side_effect = Exception("API error")

        result = enrichment_service.enrich(base_metadata)
        assert result is not None


# ------------------------------------------------------------------
# Language inference
# ------------------------------------------------------------------


class TestLanguageInference:
    """Tests for _infer_language_from_title — used as last resort
    when no API source provides a language."""

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Философия общего дела", "rus"),
            ("L'individu et la communauté", "fre"),
            ("L'Invitation", "fre"),
            ("La violence de l'esprit", "fre"),
            ("Weiser David", None),
            ("Le Mal Eté", "fre"),
            ("The Great Gatsby", None),  # apostrophe but no accent
            ("A Brief History of Time", None),
            ("", None),
            ("1984", None),
        ],
    )
    def test_infer_language(self, title, expected):
        result = MetadataEnrichmentService._infer_language_from_title(title)
        assert result == expected

    def test_russian_title_gets_language(
        self, mock_lookup: Mock, enrichment_service: MetadataEnrichmentService
    ):
        """A Russian title with no API match should get language='rus'."""
        from models.book_metadata import BookMetadata

        metadata = BookMetadata(
            title="Философия общего дела",
            vision_title="Философия общего дела",
            vision_author="Владимир Алым",
            drive_file_id="test-russian",
        )
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(metadata)
        assert result.language == "rus"

    def test_french_title_gets_language(
        self, mock_lookup: Mock, enrichment_service: MetadataEnrichmentService
    ):
        """A French title with no API match should get language='fre'."""
        from models.book_metadata import BookMetadata

        metadata = BookMetadata(
            title="L'individu et la communauté",
            vision_title="L'individu et la communauté",
            vision_author="Théodore Litt",
            drive_file_id="test-french",
        )
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(metadata)
        assert result.language == "fre"


# ------------------------------------------------------------------
# Vision publisher fallback
# ------------------------------------------------------------------


class TestVisionPublisherFallback:
    """Tests for vision_publisher fallback when no API source has publisher."""

    def test_vision_publisher_used_when_no_ol_match(
        self, mock_lookup: Mock, enrichment_service: MetadataEnrichmentService
    ):
        """If OL returns no match, vision_publisher should populate publisher."""
        from models.book_metadata import BookMetadata

        metadata = BookMetadata(
            title="Some Book",
            vision_title="Some Book",
            vision_author="Some Author",
            drive_file_id="test-pub",
            vision_publisher="L'Age d'Homme",
        )
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(metadata)
        assert result.publisher == "L'Age d'Homme"


# ------------------------------------------------------------------
# OL work details always fetched (regardless of source)
# ------------------------------------------------------------------


class TestWorkDetailsAlwaysFetched:
    """Tests that OL work details are fetched even when GB was the best candidate."""

    def test_ol_details_fetched_after_gb_match(
        self,
        mock_lookup: Mock,
        enrichment_service: MetadataEnrichmentService,
        base_metadata: BookMetadata,
    ):
        """When GB is the best candidate, OL work details should still be fetched."""
        from models.book_metadata import BookMetadata

        gb_candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_10="0596007124",
            publisher="GB Publisher",
            source="google_books",
        )
        ol_candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            open_library_work_key="/works/OL12345W",
            source="open_library",
        )

        mock_lookup.search_by_title_author.return_value = [gb_candidate]
        mock_lookup.search_by_title.return_value = []
        mock_lookup._search_open_library_by_title.return_value = [ol_candidate]
        mock_lookup.fetch_open_library_work_details.return_value = {
            "isbn_13": "9780596007126",
            "categories": ["Technology"],
        }

        result = enrichment_service.enrich(base_metadata)
        assert result.isbn_13 == "9780596007126"


# ------------------------------------------------------------------
# ISBN-13 prefix language inference (regression tests)
# ------------------------------------------------------------------
class TestIsbnPrefixLanguageInference:
    """Tests for ISBN-13 prefix-based language detection."""

    def test_isbn_french_prefix_inference(self, mock_lookup, enrichment_service):
        """ISBN-13 starting with 978-2 infers French."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []
        mock_lookup.fetch_open_library_work_details.return_value = {}

        metadata = BookMetadata(
            title="Weiser David",
            authors=["Paweł Hüelle"],
            isbn_10="2825101206",
            isbn_13="9782825101209",  # 978-2 = French
            publisher="L'Âge d'Homme",
            drive_file_id="test-fr",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result.language == "fre"

    def test_isbn_german_prefix_inference(self, mock_lookup, enrichment_service):
        """ISBN-13 starting with 978-3 infers German."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []
        mock_lookup.fetch_open_library_work_details.return_value = {}

        metadata = BookMetadata(
            title="Test Buch",
            authors=["Test Author"],
            isbn_13="9783161484100",  # 978-3 = German
            drive_file_id="test-de",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result.language == "ger"

    def test_isbn_polish_prefix_inference(self, mock_lookup, enrichment_service):
        """ISBN-13 starting with 978-83 infers Polish."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []
        mock_lookup.fetch_open_library_work_details.return_value = {}

        metadata = BookMetadata(
            title="Test Książka",
            authors=["Test Author"],
            isbn_13="9788376753804",  # 978-83 = Polish
            drive_file_id="test-pl",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result.language == "pol"

    def test_isbn_english_prefix_inference(self, mock_lookup, enrichment_service):
        """ISBN-13 starting with 978-0 or 978-1 infers English."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []
        mock_lookup.fetch_open_library_work_details.return_value = {}

        metadata = BookMetadata(
            title="Test Book",
            authors=["Test Author"],
            isbn_13="9780306406157",  # 978-0 = English
            drive_file_id="test-en",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result.language == "eng"

    def test_publisher_french_indicator(self, mock_lookup, enrichment_service):
        """Publisher name with French indicators infers French."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []
        mock_lookup.fetch_open_library_work_details.return_value = {}

        metadata = BookMetadata(
            title="Some Book",
            authors=["Author"],
            publisher="L'Âge d'Homme",
            drive_file_id="test-pub-fr",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result.language == "fre"


# ------------------------------------------------------------------
# No-rejection regression tests
# ------------------------------------------------------------------
class TestNoRejectionOnApiFailure:
    """Tests that books are never rejected when APIs fail."""

    def test_exception_in_fetch_does_not_reject(self, mock_lookup, enrichment_service):
        """If API fetch raises an exception, book is not rejected."""
        mock_lookup.search_by_title_author.side_effect = Exception("API down")
        mock_lookup.search_by_title.side_effect = Exception("API down")
        mock_lookup.fetch_open_library_work_details.side_effect = Exception("API down")

        metadata = BookMetadata(
            title="Test Book",
            authors=["Author"],
            drive_file_id="test-exc",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result is not None
        assert result.rejection_reason is None
        assert result.status != "rejected"

    def test_none_candidate_does_not_reject(self, mock_lookup, enrichment_service):
        """No candidates found does not reject the book."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []
        mock_lookup.fetch_open_library_work_details.return_value = {}

        metadata = BookMetadata(
            title="Unfindable Book",
            vision_title="Unfindable Book",
            vision_author="Unknown Author",
            authors=["Unknown Author"],
            drive_file_id="test-none",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result.rejection_reason is None
        assert result.status != "rejected"
        assert result.vision_title == "Unfindable Book"
        assert result.vision_title == "Unfindable Book"

    def test_empty_work_details_does_not_reject(self, mock_lookup, enrichment_service):
        """Empty OL work details does not reject the book."""
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []
        mock_lookup.fetch_open_library_work_details.return_value = {
            "subjects": [],
            "languages": [],
        }

        metadata = BookMetadata(
            title="Test Book",
            authors=["Author"],
            drive_file_id="test-empty",
            vision_used=True,
        )

        result = enrichment_service.enrich(metadata)
        assert result.rejection_reason is None


# ------------------------------------------------------------------
# ISBN-first enrichment and page count
# ------------------------------------------------------------------


class TestIsbnFirstEnrichment:
    """The selected edition's ISBN drives edition-specific enrichment."""

    def test_isbn_match_supplies_edition_fields(self, mock_lookup, enrichment_service):
        """ISBN-matched candidate enriches page count and publication data."""
        metadata = BookMetadata(
            title="Le coeur vert",
            authors=["Jean-Louis Kuffer"],
            isbn_13="9782825104699",
            drive_file_id="isbn-test",
            vision_used=True,
        )
        isbn_edition = BookMetadata(
            title="Le coeur vert",
            authors=["Jean-Louis Kuffer"],
            isbn_13="9782825104699",
            publisher="L'Age d'Homme",
            published_date="1993",
            language="fre",
            page_count=142,
            source="google_books",
        )
        mock_lookup.search_by_isbn.return_value = [isbn_edition]
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(metadata)

        mock_lookup.search_by_isbn.assert_called_once_with("9782825104699")
        assert result.publisher == "L'Age d'Homme"
        assert result.published_date == "1993"
        assert result.page_count == 142
        # The identified title is never rewritten by enrichment.
        assert result.title == "Le coeur vert"

    def test_no_isbn_skips_isbn_lookup(self, mock_lookup, enrichment_service):
        """Without an ISBN the title+author search is the only lookup."""
        metadata = BookMetadata(
            title="Le coeur vert",
            authors=["Jean-Louis Kuffer"],
            drive_file_id="no-isbn",
            vision_used=True,
        )
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        enrichment_service.enrich(metadata)

        mock_lookup.search_by_isbn.assert_not_called()

    def test_existing_page_count_not_overwritten(self, mock_lookup, enrichment_service):
        """A value already present wins over a later candidate."""
        metadata = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            page_count=200,
            drive_file_id="keeps-pages",
            vision_used=True,
        )
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            page_count=999,
            source="google_books",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(metadata)

        assert result.page_count == 200

    def test_missing_page_count_left_empty(self, mock_lookup, enrichment_service):
        """Nothing is invented when providers have no page count."""
        metadata = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            drive_file_id="no-pages",
            vision_used=True,
        )
        candidate = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            publisher="Test Publisher",
            source="google_books",
        )
        mock_lookup.search_by_title_author.return_value = [candidate]

        result = enrichment_service.enrich(metadata)

        assert result.page_count is None
        assert result.publisher == "Test Publisher"

    def test_isbn_provider_failure_is_survivable(self, mock_lookup, enrichment_service):
        """An ISBN lookup error must not abort enrichment."""
        metadata = BookMetadata(
            title="Test Title",
            authors=["Test Author"],
            isbn_13="9781234567897",
            drive_file_id="isbn-error",
            vision_used=True,
        )
        mock_lookup.search_by_isbn.side_effect = RuntimeError("provider down")
        mock_lookup.search_by_title_author.return_value = [
            BookMetadata(
                title="Test Title",
                authors=["Test Author"],
                publisher="Fallback Publisher",
                source="open_library",
            )
        ]

        result = enrichment_service.enrich(metadata)

        assert result.publisher == "Fallback Publisher"
        assert result.rejection_reason is None

    def test_isbn_candidate_does_not_replace_identified_title(
        self, mock_lookup, enrichment_service
    ):
        """A differently-titled ISBN candidate must not rewrite the title."""
        metadata = BookMetadata(
            title="Le coeur vert",
            authors=["Jean-Louis Kuffer"],
            isbn_13="9782825104699",
            drive_file_id="title-guard",
            vision_used=True,
        )
        isbn_edition = BookMetadata(
            title="Something Else Entirely",
            authors=["Another Author"],
            isbn_13="9782825104699",
            publisher="Wrong Publisher",
            source="google_books",
        )
        mock_lookup.search_by_isbn.return_value = [isbn_edition]
        mock_lookup.search_by_title_author.return_value = []
        mock_lookup.search_by_title.return_value = []

        result = enrichment_service.enrich(metadata)

        assert result.title == "Le coeur vert"
        assert result.authors == ["Jean-Louis Kuffer"]
