"""OCR result model — the output of the OCR service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class OCRWord:
    """A single OCR-recognized word with bounding box and confidence.

    Fields:
        text: The recognized word.
        confidence: OCR confidence (0.0–1.0).
        bounding_box: List of 4 (x, y) tuples for the word bounding box.
    """

    text: str
    confidence: float = 0.0
    bounding_box: List[List[int]] = field(default_factory=list)


@dataclass
class OCRResult:
    """Result of OCR processing on an image.

    Fields:
        text: Full recognized text (all lines concatenated).
        confidence: Average confidence across all words.
        words: List of recognized words with bounding boxes.
        language: Language code used for OCR.
        image_path: Path to the image that was processed.
    """

    text: str
    confidence: float = 0.0
    words: List[OCRWord] = field(default_factory=list)
    language: str = "en"
    image_path: Optional[str] = None
    orientation_angle: Optional[int] = None
    orientation_corrected: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
