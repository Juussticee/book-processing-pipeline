"""Unit tests for ValidationResult model."""

from models.validation_result import QualityScore, ValidationResult


class TestValidationResult:
    def test_create_valid(self):
        result = ValidationResult(
            drive_file_id="drive001",
            job_id="job001",
            is_valid=True,
            confidence=0.95,
        )
        assert result.is_valid is True
        assert result.confidence == 0.95
        assert result.issues == []
        assert result.missing_fields == []

    def test_create_invalid_with_issues(self):
        result = ValidationResult(
            drive_file_id="drive002",
            job_id="job002",
            is_valid=False,
            confidence=0.2,
            issues=["Missing ISBN", "Low confidence"],
            missing_fields=["isbn_10", "publisher"],
        )
        assert result.is_valid is False
        assert len(result.issues) == 2
        assert len(result.missing_fields) == 2

    def test_quality_score_defaults(self):
        result = ValidationResult(drive_file_id="drive003", job_id="job003")
        assert result.title_quality == QualityScore.POOR
        assert result.author_quality == QualityScore.POOR
        assert result.publisher_quality == QualityScore.POOR

    def test_quality_score_good(self):
        result = ValidationResult(
            drive_file_id="drive004",
            job_id="job004",
            title_quality=QualityScore.GOOD,
            author_quality=QualityScore.GOOD,
            publisher_quality=QualityScore.GOOD,
        )
        assert result.title_quality == QualityScore.GOOD
        assert result.author_quality == QualityScore.GOOD
        assert result.publisher_quality == QualityScore.GOOD
