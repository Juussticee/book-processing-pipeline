"""Image preprocessing for book cover OCR.

Provides:
- Targeted preprocessing: contrast, brightness, sharpening
- Orientation detection and correction
- Auto-rotation via EXIF
- Image resizing

Public API (used by app.py, drive_batch.py, adaptive_ocr.py):
    preprocessor = ImagePreprocessor(temp_dir="data/temp")
    output_path = preprocessor.preprocessor("input.jpg")
    preprocessor.cleanup(temp_path)
    preprocessor.detect_orientation(img) -> ImageOrientation
    preprocessor.correct_orientation(img, orient) -> Image
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator, Optional, Set, Tuple

import cv2
import numpy as np
import PIL
import PIL.Image
import PIL.ImageFilter
import PIL.ImageOps
import PIL.ImageStat
import PIL.PngImagePlugin

from core.logger import setup_logger
from models.image_orientation import ImageOrientation

logger = setup_logger(__name__)

# Marker stored in the normalized PNG so a second pass is a no-op instead of
# re-applying autocontrast/smoothing (which would change pixels).
NORMALIZED_MARKER = "book-normalized"

SUPPORTED_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}


class ImagePreprocessor:
    """Image preprocessor for book cover OCR.

    Provides preprocessing pipeline:
    1. Auto-rotate via EXIF
    2. Resize oversized images
    3. Contrast enhancement
    4. Sharpening
    5. Grayscale conversion

    Also provides orientation detection and correction.
    """

    def __init__(
        self,
        temp_dir: Optional[str] = None,
        max_dimension: int = 1600,
    ) -> None:
        self._temp_dir = temp_dir or tempfile.mkdtemp()
        self._max_dimension = max_dimension

    def preprocess(self, image_path: str) -> str:
        """Preprocess an image for OCR.

        Args:
            image_path: Path to the source image.

        Returns:
            Path to the preprocessed image.

        Raises:
            FileNotFoundError: If image_path does not exist.
            ValueError: If file format is unsupported.
        """
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")

        ext = Path(image_path).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported image format: {ext}. "
                f"Supported: {SUPPORTED_EXTENSIONS}"
            )

        img = PIL.Image.open(image_path)

        # Already-normalized artifacts carry a marker; re-running the filter
        # chain would re-sharpen them and change pixels. Normalization must be
        # idempotent (a retried job produces byte-identical input), so a marked
        # copy is passed through unchanged.
        already_normalized = img.info.get(NORMALIZED_MARKER) == "1"
        if already_normalized:
            img = img.convert("L")
            temp_path = os.path.join(
                self._temp_dir,
                f"preprocessed_{os.getpid()}_{Path(image_path).stem}.png",
            )
            img.save(temp_path)
            return temp_path

        # Step 1: Auto-rotate via EXIF
        img = self._auto_rotate(img)

        # Step 2: Resize if needed
        img = self._resize(img)

        # Step 3: Contrast enhancement
        img = PIL.ImageOps.autocontrast(img)

        # Step 4: Sharpen
        img = img.filter(PIL.ImageFilter.SMOOTH)

        # Step 5: Convert to grayscale
        # First convert to RGB (handles RGBA, P modes), then to L
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.mode != "L":
            img = img.convert("L")

        # Save to temp file
        temp_path = os.path.join(
            self._temp_dir,
            f"preprocessed_{os.getpid()}_{Path(image_path).stem}.png",
        )
        metadata = PIL.PngImagePlugin.PngInfo()
        metadata.add_text(NORMALIZED_MARKER, "1")
        img.save(temp_path, pnginfo=metadata)
        return temp_path

    def cleanup(self, temp_path: str) -> None:
        """Remove a temporary file.

        Args:
            temp_path: Path to the file to remove.
        """
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    # Default safety limits (used by ProcessingWorker.validate_image_safety)
    MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
    MIN_FILE_SIZE_BYTES = 1  # bytes — only rejects truly empty files

    def validate_image_safety(self, image_path: str) -> tuple:
        """Validate an image is safe to process (size limits, decodable).

        Returns:
            Tuple of (is_safe, error_message). error_message is empty
            when safe.
        """
        # Existence
        if not os.path.exists(image_path):
            return False, f"File not found: {image_path}"

        # Size limits
        try:
            size = os.path.getsize(image_path)
        except OSError as exc:
            return False, f"Cannot stat file: {exc}"

        if size > self.MAX_FILE_SIZE_BYTES:
            return False, (
                f"File too large: {size} bytes " f"(max {self.MAX_FILE_SIZE_BYTES})"
            )
        if size < self.MIN_FILE_SIZE_BYTES:
            return False, (
                f"File too small: {size} bytes " f"(min {self.MIN_FILE_SIZE_BYTES})"
            )

        # Decodability (cheap verification — does not load full image)
        try:
            with PIL.Image.open(image_path) as img:
                img.verify()
        except Exception as exc:
            return False, f"Cannot decode image: {exc}"

        return True, ""

    def _auto_rotate(self, img: PIL.Image.Image) -> PIL.Image.Image:
        """Apply the EXIF orientation tag (values 1-8) to a processing copy.

        ``ImageOps.exif_transpose`` implements the whole EXIF orientation
        table: 1 = identity, 2 = mirror, 3 = rotate 180, 4 = mirror + 180,
        5/7 = transpose variants, 6 = rotate 90, 8 = rotate 270. It also
        strips the tag from the returned copy, so processing an image twice
        cannot rotate it twice (normalization is idempotent).

        The source file is never modified; only the in-memory copy changes.
        """
        try:
            orientation = img.getexif().get(0x0112)
        except (AttributeError, KeyError, IndexError, ValueError):
            return img
        # Values 2-8 need a transform; anything else (missing tag, value 1,
        # or a non-integer from a malformed file) is left untouched.
        if not isinstance(orientation, int) or orientation <= 1 or orientation > 8:
            return img
        try:
            return PIL.ImageOps.exif_transpose(img)
        except Exception as exc:  # malformed EXIF must not stop processing
            logger.warning(
                "EXIF orientation %s could not be applied: %s", orientation, exc
            )
            return img

    def _resize(self, img: PIL.Image.Image) -> PIL.Image.Image:
        """Resize image if it exceeds max dimensions."""
        width, height = img.size
        if max(width, height) > self._max_dimension:
            ratio = self._max_dimension / max(width, height)
            new_size = (int(width * ratio), int(height * ratio))
            img = img.resize(new_size, PIL.Image.Resampling.LANCZOS)
        return img

    def _rotate_image(self, img: PIL.Image.Image, degrees: int) -> PIL.Image.Image:
        """Rotate image by specified degrees."""
        if degrees == 0:
            return img
        return img.rotate(degrees, expand=True)

    def detect_orientation(self, img: PIL.Image.Image) -> ImageOrientation:
        """Detect a sideways (90 degree) cover using line orientation.

        Book covers carry mostly horizontal text lines. When the stored pixels
        are rotated by 90/270 degrees those lines become vertical, so a strong
        vertical majority is a cheap signal that the image must be turned.

        This heuristic cannot distinguish upright from 180 degrees (both keep
        horizontal lines); 180 degrees is only handled by the OCR-quality
        fallback in ``MetadataService._try_orientation_correction``.

        Returns:
            ImageOrientation with angle, confidence, was_corrected
        """
        # Try to detect orientation using line detection
        try:
            img_array = np.array(img.convert("L"))
            edges = cv2.Canny(img_array, 50, 150)
            lines = cv2.HoughLinesP(
                edges,
                1,
                np.pi / 180,
                100,
                minLineLength=100,
                maxLineGap=10,
            )

            # Count horizontal vs vertical lines. The segment iterator accepts
            # every output shape OpenCV has returned so a geometry heuristic
            # can never break processing.
            horizontal = 0
            vertical = 0
            for x1, y1, x2, y2 in self._iter_hough_segments(lines):
                angle = abs(np.arctan2(y2 - y1, x2 - x1) * 180 / np.pi)
                if angle < 30 or angle > 150:
                    horizontal += 1
                elif 60 < angle < 120:
                    vertical += 1

            # Book covers should have more horizontal text lines
            if vertical > horizontal * 1.5:
                return ImageOrientation(angle=90, confidence=0.7, was_corrected=True)

            return ImageOrientation(angle=0, confidence=0.5, was_corrected=False)
        except Exception as exc:
            logger.warning("Orientation detection failed: %s", exc)
            return ImageOrientation(angle=0, confidence=0.0, was_corrected=False)

    @staticmethod
    def _iter_hough_segments(lines) -> Iterator[Tuple[int, int, int, int]]:
        """Yield ``(x1, y1, x2, y2)`` from any ``cv2.HoughLinesP`` result.

        OpenCV returns ``(N, 1, 4)`` in older releases and ``(N, 4)`` in newer
        ones, and an empty result may arrive as an empty array rather than
        ``None``. Unrecognised shapes yield nothing (the caller then keeps the
        image as-is) instead of raising.
        """
        if lines is None:
            return
        try:
            segments = np.asarray(lines, dtype=np.float64).reshape(-1, 4)
        except (TypeError, ValueError):
            logger.debug(
                "Unrecognised HoughLinesP output shape: %s",
                getattr(lines, "shape", type(lines).__name__),
            )
            return
        for x1, y1, x2, y2 in segments:
            yield int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))

    def correct_orientation(
        self, img: PIL.Image.Image, orient: ImageOrientation
    ) -> PIL.Image.Image:
        """Apply orientation correction to an image.

        Args:
            img: PIL Image to correct.
            orient: ImageOrientation from detect_orientation.

        Returns:
            Corrected image.
        """
        if orient.was_corrected and orient.angle != 0:
            return self._rotate_image(img, orient.angle)
        return img

    @staticmethod
    def analyze_image(image_path: str) -> dict:
        """Analyze image quality metrics.

        Returns:
            Dict with keys: mean_brightness, contrast, width, height, mode
        """
        try:
            img = PIL.Image.open(image_path).convert("L")  # Grayscale
            stat = PIL.ImageStat.Stat(img)
            mean_brightness = stat.mean[0]
            contrast = stat.stddev[0]
            width, height = img.size

            return {
                "mean_brightness": mean_brightness,
                "contrast": contrast,
                "width": width,
                "height": height,
                "is_low_contrast": contrast < 30,
                "is_bright": mean_brightness > 200,
                "is_dark": mean_brightness < 50,
            }
        except Exception as exc:
            logger.warning("Failed to analyze image %s: %s", image_path, exc)
            return {
                "mean_brightness": 128,
                "contrast": 50,
                "width": 0,
                "height": 0,
                "is_low_contrast": False,
                "is_bright": False,
                "is_dark": False,
            }
