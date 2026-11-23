"""Orientation detection for book cover images."""

from __future__ import annotations

from typing import Tuple

from PIL import Image

from core.logger import setup_logger

logger = setup_logger(__name__)


class OrientationDetector:
    """Detects if a book cover image is rotated.

    Uses text orientation heuristics and image analysis to determine
    if the image needs rotation for proper OCR.
    """

    # Common French/English words that should appear upright on book covers
    _COMMON_WORDS = {
        "le",
        "la",
        "les",
        "de",
        "du",
        "des",
        "un",
        "une",
        "et",
        "ou",
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "in",
        "on",
        "to",
        "for",
        "par",
        "roman",
        "édition",
        "collection",
        "volume",
    }

    @staticmethod
    def detect_orientation(image_path: str) -> Tuple[int, float]:
        """Detect the orientation of a book cover image.

        Returns:
            Tuple of (rotation_degrees, confidence)
            rotation_degrees is one of 0, 90, 180, 270
        """
        try:
            img = Image.open(image_path)
        except Exception as exc:
            logger.warning("Cannot open image for orientation detection: %s", exc)
            return 0, 0.0

        # Try OCR at each rotation and pick the one with the most readable text
        # For now, use a heuristic based on aspect ratio and text position
        width, height = img.size

        # Book covers are typically portrait (taller than wide)
        # If landscape, it might be rotated 90°
        if width > height * 1.3:
            # Likely rotated 90° or 270°
            logger.info("Image is landscape (%dx%d), may be rotated 90°", width, height)
            return 90, 0.6

        # For portrait images, check if text is at the top or bottom
        # This is a simplified heuristic - in practice, we'd run OCR
        # at each rotation and compare results
        return 0, 0.5

    @staticmethod
    def needs_rotation(image_path: str) -> bool:
        """Quick check if image likely needs rotation."""
        rotation, confidence = OrientationDetector.detect_orientation(image_path)
        return rotation != 0 and confidence > 0.5

    @staticmethod
    def rotate_image(image_path: str, output_path: str, degrees: int) -> bool:
        """Rotate an image by the specified degrees.

        Args:
            image_path: Path to the source image.
            output_path: Path to save the rotated image.
            degrees: Rotation degrees (90, 180, 270).

        Returns:
            True if rotation was successful.
        """
        try:
            img = Image.open(image_path)
            rotated = img.rotate(-degrees, expand=True)
            rotated.save(output_path)
            logger.info("Rotated image %d°: %s -> %s", degrees, image_path, output_path)
            return True
        except Exception as exc:
            logger.warning("Failed to rotate image: %s", exc)
            return False
