"""Validation result model for Milestone 5 metadata validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class QualityScore(str, Enum):
    """Quality rating for a metadata field."""

    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


@dataclass
class ValidationResult:
    """Result of validating a single BookMetadata record.

    Fields:
        drive_file_id: Google Drive file ID.
        job_id: Original processing job ID.
        is_valid: Whether the metadata passes all validation checks.
        confidence: The metadata confidence score (0.0–1.0).
        issues: List of human-readable validation issues.
        missing_fields: List of missing required field names.
        title_quality: Quality rating of the title.
        author_quality: Quality rating of the author field.
        publisher_quality: Quality rating of the publisher field.
    """

    drive_file_id: str = ""
    job_id: str = ""
    is_valid: bool = False
    confidence: float = 0.0
    issues: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    title_quality: QualityScore = QualityScore.POOR
    author_quality: QualityScore = QualityScore.POOR
    publisher_quality: QualityScore = QualityScore.POOR
    title_overlap: Optional[float] = None
    author_overlap: Optional[float] = None
    id: Optional[int] = None
