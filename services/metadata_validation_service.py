"""Metadata validation service for book identification results.

Validates that a BookMetadata object has sufficient evidence to be considered
a reliable identification. The design distinguishes between:

REQUIRED evidence (missing -> validation failure):
  - sufficiently high confidence (>= confidence_threshold)
  - valid title (non-empty, at least 3 characters)
  - valid author (at least one non-empty author)

OPTIONAL evidence (missing -> warning, not failure):
  - ISBN
  - publisher
  - published_date
  - categories
  - description

Contradictory evidence (can cause validation failure):
  - contradictory ISBN (OCR ISBN != candidate ISBN when both present)

Many legitimate books have no ISBN, incomplete publisher metadata, or
incomplete publication dates. Validation is designed around evidence that
actually exists rather than requiring all metadata fields to be present.
"""

from __future__ import annotations

from core.logger import setup_logger
from models.book_metadata import BookMetadata
from models.validation_result import QualityScore, ValidationResult

logger = setup_logger(__name__)


class MetadataValidationService:
    """Validates book metadata identification results.

    The validation strategy distinguishes between required fields (title,
    author, confidence) and optional fields (ISBN, publisher, published_date,
    categories, description). Missing optional fields produce warnings but
    do not fail validation.

    Args:
        confidence_threshold: Minimum confidence for a valid result.
            Defaults to 0.85.
    """

    # Minimum title length for GOOD quality
    MIN_TITLE_LENGTH = 3

    def __init__(self, confidence_threshold: float = 0.85) -> None:
        self._confidence_threshold = confidence_threshold

    @property
    def confidence_threshold(self) -> float:
        return self._confidence_threshold

    def _assess_title_quality(self, title: str) -> QualityScore:
        """Assess the quality of a title string."""
        if not title or not title.strip():
            return QualityScore.POOR
        title = title.strip()
        if len(title) < 2:
            return QualityScore.POOR
        if len(title) < self.MIN_TITLE_LENGTH:
            return QualityScore.POOR
        return QualityScore.GOOD

    def _assess_author_quality(self, authors) -> QualityScore:
        """Assess the quality of the authors list."""
        if not authors:
            return QualityScore.POOR
        non_empty = [a for a in authors if a and a.strip()]
        if not non_empty:
            return QualityScore.POOR
        if len(non_empty) == 1 and len(non_empty[0].strip()) < 2:
            return QualityScore.POOR
        return QualityScore.GOOD

    def _assess_publisher_quality(self, publisher) -> QualityScore:
        """Assess the quality of the publisher field."""
        if not publisher or not publisher.strip():
            return QualityScore.POOR
        return QualityScore.GOOD

    def validate(self, metadata: BookMetadata) -> ValidationResult:
        """Validate a BookMetadata object.

        Required (missing -> is_valid=False):
          - confidence >= confidence_threshold
          - valid title (non-empty, at least 3 characters)
          - valid author (at least one non-empty author name)

        Optional (missing -> warning, is_valid still True):
          - ISBN (isbn_10 or isbn_13)
          - publisher
          - published_date
          - categories
          - description

        Args:
            metadata: The BookMetadata to validate.

        Returns:
            A ValidationResult with is_valid, issues, missing_fields, etc.
        """
        result = ValidationResult(
            drive_file_id=metadata.drive_file_id,
            job_id=metadata.job_id,
            confidence=metadata.confidence or 0.0,
        )

        issues: list = []
        missing_fields: list = []

        # --- Required: confidence ---
        confidence = metadata.confidence or 0.0
        if confidence < self._confidence_threshold:
            issues.append(
                f"Confidence {confidence:.4f} is below required threshold "
                f"{self._confidence_threshold}"
            )
            missing_fields.append("confidence")

        # --- Required: title ---
        title_quality = self._assess_title_quality(metadata.title or "")
        result.title_quality = title_quality
        if title_quality == QualityScore.POOR:
            issues.append("Title is missing or too short")
            missing_fields.append("title")

        # --- Required: author ---
        author_quality = self._assess_author_quality(metadata.authors)
        result.author_quality = author_quality
        if author_quality == QualityScore.POOR:
            issues.append("Author is missing or invalid")
            missing_fields.append("authors")

        # --- Optional: ISBN (warning, not failure) ---
        if not (metadata.isbn_10 or metadata.isbn_13):
            issues.append("ISBN is not available (optional)")
            result.title_quality = title_quality  # keep consistent

        # --- Optional: publisher (warning, not failure) ---
        publisher_quality = self._assess_publisher_quality(metadata.publisher)
        result.publisher_quality = publisher_quality
        if publisher_quality == QualityScore.POOR:
            issues.append("Publisher is not available (optional)")

        # --- Optional: published_date (warning, not failure) ---
        if not metadata.published_date:
            issues.append("Published date is not available (optional)")

        # --- Optional: categories (warning, not failure) ---
        if not metadata.categories:
            issues.append("Categories are not available (optional)")

        # --- Optional: description (warning, not failure) ---
        if not metadata.description:
            issues.append("Description is not available (optional)")

        # --- Contradictory ISBN check ---
        # If both OCR ISBN and candidate ISBN exist and they differ,
        # this is a validation failure (contradictory evidence).
        # Note: ISBN absence is treated as neutral (not contradictory).
        if metadata.extracted_isbn and (metadata.isbn_10 or metadata.isbn_13):
            candidate_isbns = []
            if metadata.isbn_10:
                candidate_isbns.append(metadata.isbn_10.replace("-", ""))
            if metadata.isbn_13:
                candidate_isbns.append(metadata.isbn_13.replace("-", ""))
            ocr_isbn_norm = (metadata.extracted_isbn or "").replace("-", "")
            if ocr_isbn_norm and ocr_isbn_norm not in candidate_isbns:
                issues.append(
                    f"ISBN conflict: OCR ISBN '{ocr_isbn_norm}' does not match "
                    f"candidate ISBN(s) {candidate_isbns}"
                )
                missing_fields.append("isbn_conflict")

        # Validation is valid if all REQUIRED fields pass AND no contradictory
        # metadata is found. Warnings for missing optional fields do not
        # affect validity, but contradictory metadata does.
        is_valid = len(missing_fields) == 0

        result.is_valid = is_valid
        result.issues = issues
        # missing_fields tracks REQUIRED missing fields + conflicts
        result.missing_fields = missing_fields

        if not result.is_valid:
            logger.warning(
                "Metadata validation FAILED for '%s': missing required fields %s",
                metadata.title or "?",
                missing_fields,
            )
        else:
            logger.info(
                "Metadata validation PASSED for '%s' " "(confidence=%.4f, issues=%d)",
                metadata.title,
                confidence,
                len(issues),
            )

        return result
