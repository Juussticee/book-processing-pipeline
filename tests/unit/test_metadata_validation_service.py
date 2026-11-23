"""Unit tests for MetadataValidationService.

Tests the ISBN-optional validation logic introduced in Milestone 8.2.
Validation distinguishes between required fields (title, author, confidence)
and optional fields (ISBN, publisher, published_date, categories, description).
Missing optional fields produce warnings but do not fail validation.
"""

from models.book_metadata import BookMetadata
from models.validation_result import QualityScore, ValidationResult
from services.metadata_validation_service import MetadataValidationService


def _make_metadata(
    drive_file_id="drive001",
    job_id="job001",
    title="Test Book Title",
    authors=["Test Author"],
    isbn_10="0306406152",
    isbn_13="9780306406157",
    publisher="Test Publisher",
    published_date="2024-01-01",
    confidence=0.95,
    categories=["Fiction"],
    description="A test book.",
    image_links=None,
):
    return BookMetadata(
        title=title,
        authors=authors,
        isbn_10=isbn_10,
        isbn_13=isbn_13,
        publisher=publisher,
        published_date=published_date,
        language="en",
        categories=categories,
        description=description,
        thumbnail_url="http://example.com/thumb.jpg",
        cover_url="http://example.com/cover.jpg",
        confidence=confidence,
        source="google_books",
        drive_file_id=drive_file_id,
        job_id=job_id,
    )


class TestMetadataValidationService:
    def setup_method(self):
        self.service = MetadataValidationService()

    def test_validate_valid_metadata(self):
        """Valid metadata with all fields should pass validation."""
        metadata = _make_metadata()
        result = self.service.validate(metadata)
        assert result.is_valid is True
        assert result.confidence == 0.95
        assert result.missing_fields == []
        # Optional fields that are present should not generate warnings
        assert (
            "isbn" not in " ".join(result.issues).lower()
            or "optional" in " ".join(result.issues).lower()
        )

    def test_validate_valid_metadata_no_isbn(self):
        """Valid metadata WITHOUT ISBN should still pass validation.

        This is the core fix for Milestone 8.2: many legitimate books
        have no ISBN. Missing ISBN is a warning, not a failure.
        """
        metadata = _make_metadata(isbn_10=None, isbn_13=None)
        result = self.service.validate(metadata)
        assert result.is_valid is True
        # ISBN should be in issues as a warning, not in missing_fields
        assert not any("isbn" in f for f in result.missing_fields)

    def test_validate_valid_metadata_no_publisher(self):
        """Missing publisher is optional — validation should still pass."""
        metadata = _make_metadata(publisher=None)
        result = self.service.validate(metadata)
        assert result.is_valid is True
        assert "publisher" not in result.missing_fields
        assert result.publisher_quality == QualityScore.POOR

    def test_validate_valid_metadata_no_published_date(self):
        """Missing published_date is optional — validation should still pass."""
        metadata = _make_metadata(published_date=None)
        result = self.service.validate(metadata)
        assert result.is_valid is True
        assert "published_date" not in result.missing_fields

    def test_validate_valid_metadata_only_title_author(self):
        """A book with only title + author + high confidence is valid.

        All optional fields missing, but the book still passes validation
        because title, author, and confidence are sufficient.
        """
        metadata = _make_metadata(
            isbn_10=None,
            isbn_13=None,
            publisher=None,
            published_date=None,
            categories=[],
            description=None,
            confidence=0.95,
        )
        result = self.service.validate(metadata)
        assert result.is_valid is True
        assert result.missing_fields == []
        assert result.title_quality == QualityScore.GOOD
        assert result.author_quality == QualityScore.GOOD

    def test_validate_contradictory_isbn(self):
        """Contradictory ISBN (OCR != candidate) should fail validation."""
        metadata = _make_metadata(isbn_10="0306406152", isbn_13="9780306406157")
        metadata.extracted_isbn = "9999999999999"
        result = self.service.validate(metadata)
        assert result.is_valid is False
        assert "isbn_conflict" in result.missing_fields

    def test_validate_contradictory_title_author(self):
        """Contradictory title/author (confidence too low) should fail.

        If confidence is below threshold, validation fails regardless.
        """
        metadata = _make_metadata(
            title="Wrong Title",
            authors=["Wrong Author"],
            confidence=0.3,
        )
        result = self.service.validate(metadata)
        assert result.is_valid is False
        assert "confidence" in result.missing_fields

    def test_validate_low_confidence(self):
        """Confidence below threshold should fail validation."""
        metadata = _make_metadata(confidence=0.3)
        result = self.service.validate(metadata)
        assert result.is_valid is False
        assert any("confidence" in issue.lower() for issue in result.issues)

    def test_validate_missing_title(self):
        """Missing title should fail validation.

        BookMetadata requires a non-empty title in __post_init__, so we
        test with a very short title (1 char) which is too short to be valid.
        """
        metadata = _make_metadata(title="A")
        result = self.service.validate(metadata)
        assert result.is_valid is False
        assert "title" in result.missing_fields

    def test_validate_short_title(self):
        """Short title (fewer than 3 chars) should fail validation."""
        metadata = _make_metadata(title="Ab")
        result = self.service.validate(metadata)
        assert result.is_valid is False
        assert "title" in result.missing_fields

    def test_validate_missing_authors(self):
        """Missing authors should fail validation."""
        metadata = _make_metadata(authors=[])
        result = self.service.validate(metadata)
        assert result.is_valid is False
        assert "authors" in result.missing_fields

    def test_validate_custom_threshold(self):
        """Custom confidence threshold should be respected."""
        service = MetadataValidationService(confidence_threshold=0.9)
        metadata = _make_metadata(confidence=0.85)
        result = service.validate(metadata)
        assert result.is_valid is False
        assert any("confidence" in issue.lower() for issue in result.issues)

    def test_validate_returns_validation_result(self):
        """Validate should return a ValidationResult object."""
        metadata = _make_metadata()
        result = self.service.validate(metadata)
        assert isinstance(result, ValidationResult)
        assert result.drive_file_id == "drive001"

    def test_le_coeur_vert_case_passes(self):
        """The 'Le cœur vert' test case must pass validation even without ISBN.

        This is the current important book test case. With confidence ~0.991
        and title/author matching, the absence of ISBN must NOT cause failure.
        """
        metadata = BookMetadata(
            title="Le cœur vert",
            authors=["Jean-Louis Kuffer"],
            isbn_10=None,
            isbn_13=None,
            publisher=None,
            published_date=None,
            language="fr",
            categories=[],
            description=None,
            confidence=0.991,
            source="open_library",
            drive_file_id="test_drive_file",
            job_id="test_job",
        )
        result = self.service.validate(metadata)
        assert result.is_valid is True
        assert "isbn_conflict" not in result.missing_fields
        assert result.missing_fields == []
