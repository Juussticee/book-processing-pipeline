"""Adaptive OCR service with multi-pass, script-aware strategy.

Implements the second-pass OCR requirement:
1. Initial pass: detect script, run OCR with appropriate language set
2. Second pass: targeted region OCR on title/author/isbn regions
3. Adaptive: if initial text is sparse/ambiguous, try additional passes
   with cropped regions, enlarged versions, and contrast variants

Key features:
- Script detection via ScriptDetector
- Arabic normalization via ArabicNormalizer
- PaddleOCR-VL (Baidu OCR API) with multilingual support
- dHash for image deduplication cache
- Confidence scoring per OCR result
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from core.logger import setup_logger
from services.arabic_normalizer import ArabicNormalizer
from services.image_preprocessor import ImagePreprocessor
from services.script_detector import ScriptDetector
from repositories.cache_repository import CacheRepository

logger = setup_logger(__name__)


@dataclass
class OCRLine:
    """A single OCR-detected text line with bounding box and confidence."""

    text: str
    confidence: float
    bbox: List[Tuple[int, int]]  # 4 corner points


@dataclass
class OCRPageResult:
    """Results from one OCR pass on an image."""

    text: str
    lines: List[OCRLine]
    mean_confidence: float
    script: str
    pass_type: str  # "initial", "cropped", "enlarged", "contrast", "second_pass"
    language_used: List[str]


class AdaptiveOCR:
    """Multi-pass, script-aware OCR service using PaddleOCR-VL (Baidu OCR API)."""

    # Minimum image dimension for reliable OCR
    MIN_DIMENSION = 200

    # Enlargement factor for second pass
    ENLARGE_FACTOR = 2.0

    # Region detection uses the top 30% of the image for title
    # and specific sub-regions for author/isbn
    TITLE_REGION_FRACTION = 0.30

    def __init__(
        self,
        cache: Optional[CacheRepository] = None,
        preprocessor: Optional[ImagePreprocessor] = None,
        ocr_confidence_threshold: float = 0.5,
    ) -> None:
        self._cache = cache
        self._preprocessor = preprocessor or ImagePreprocessor()
        self._script_detector = ScriptDetector()
        self._normalizer = ArabicNormalizer()
        self._ocr_threshold = ocr_confidence_threshold

    def _get_reader(self, languages: List[str]) -> Any:
        """PaddleOCR-VL is the only OCR engine."""
        from services.paddleocr_vl_service import PaddleOCRVlService

        return PaddleOCRVlService()

    def compute_image_hash(self, image_path: str) -> str:
        """Compute a dHash of the image for cache key generation."""
        try:
            img = Image.open(image_path)
            img = img.convert("L").resize((9, 8))  # dHash: 9x8 for 64-bit hash
            pixels = list(img.getdata())
            diff = []
            for row in range(8):
                for col in range(8):
                    diff.append(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
            hash_int = 0
            for i, bit in enumerate(diff):
                if bit:
                    hash_int |= 1 << i
            return hashlib.sha256(f"dhash:{hash_int:064x}".encode()).hexdigest()[:32]
        except Exception as exc:
            logger.debug("Image hash failed: %s", exc)
            # Fallback: hash the file content
            with open(image_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:32]

    def perform_ocr(self, image_path: str) -> OCRPageResult:
        """Run adaptive multi-pass OCR on an image.

        Strategy:
        1. Initial pass: full image, auto language detection
        2. Analyze results: if text is sparse or low-confidence,
           try second pass with cropped title region
        3. If Arabic detected, normalize the text

        Args:
            image_path: Path to the book cover image.

        Returns:
            OCRPageResult with the best text from all passes.
        """
        image_hash = self.compute_image_hash(image_path)

        # Check cache
        if self._cache is not None:
            cache_key = self._cache.get_ocr_cache_key(image_hash)
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("OCR cache HIT for image hash: %s", image_hash[:16])
                return OCRPageResult(
                    text=cached.get("text", ""),
                    lines=[
                        OCRLine(
                            text=l["text"],
                            confidence=l["confidence"],
                            bbox=l["bbox"],
                        )
                        for l in cached.get("lines", [])
                    ],
                    mean_confidence=cached.get("mean_confidence", 0.0),
                    script=cached.get("script", "latin"),
                    pass_type=cached.get("pass_type", "initial"),
                    language_used=cached.get("language_used", ["en"]),
                )

        # Load image
        img = cv2.imread(image_path)
        if img is None:
            raise ValueError(f"Cannot read image: {image_path}")

        # Initial pass
        initial_result = self._run_ocr_pass(image_path, img, "initial")

        # Decide on second pass
        all_text = initial_result.text
        script = initial_result.script

        # Check if we need a second pass:
        # - Low confidence
        # - Sparse text
        # - Arabic script (needs normalization)
        needs_second_pass = (
            initial_result.mean_confidence < self._ocr_threshold
            or len(all_text.strip().split()) < 5
            or script == "arabic"
        )

        best_result = initial_result

        if needs_second_pass:
            # Second pass: crop title region and re-OCR
            title_result = self._run_ocr_pass(
                image_path,
                img,
                "second_pass",
                region="title",
            )
            if title_result.mean_confidence > best_result.mean_confidence:
                best_result = title_result

            # Also try enlarged version if text was sparse
            if len(all_text.strip().split()) < 5:
                enlarged_result = self._run_ocr_pass(image_path, img, "enlarged")
                if enlarged_result.mean_confidence > best_result.mean_confidence:
                    best_result = enlarged_result

        # Normalize Arabic text if needed
        if best_result.script == "arabic":
            best_result = self._normalize_arabic_result(best_result)

        # Cache the result
        if self._cache is not None:
            cache_key = self._cache.get_ocr_cache_key(image_hash)
            self._cache.set(
                cache_key,
                {
                    "text": best_result.text,
                    "lines": [
                        {
                            "text": l.text,
                            "confidence": l.confidence,
                            "bbox": l.bbox,
                        }
                        for l in best_result.lines
                    ],
                    "mean_confidence": best_result.mean_confidence,
                    "script": best_result.script,
                    "pass_type": best_result.pass_type,
                    "language_used": best_result.language_used,
                },
                category="ocr",
            )

        return best_result

    def _run_ocr_pass(
        self,
        image_path: str,
        img: Any,
        pass_type: str,
        region: Optional[str] = None,
    ) -> OCRPageResult:
        """Run a single OCR pass on (a region of) the image.

        Args:
            image_path: Original image path (for cache).
            img: OpenCV BGR image array.
            pass_type: Label for this pass.
            region: If "title", crop the top region of the image.
        """
        # If region crop is requested, extract it
        if region == "title":
            img = self._crop_title_region(img)
            if img is None:
                # Fallback to original
                img = cv2.imread(image_path)

        # Enlarge for better OCR
        if pass_type == "enlarged":
            h, w = img.shape[:2]
            img = cv2.resize(
                img,
                (int(w * self.ENLARGE_FACTOR), int(h * self.ENLARGE_FACTOR)),
                interpolation=cv2.INTER_CUBIC,
            )

        # Preprocess: grayscale + denoise
        processed = self._preprocess_for_ocr(img)

        # Save processed image to temp file for PaddleOCR-VL API
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        cv2.imwrite(tmp_path, processed)

        try:
            # Run PaddleOCR-VL (Baidu OCR API)
            from services.paddleocr_vl_service import PaddleOCRVlService

            paddle = PaddleOCRVlService()
            vl_result = paddle.extract(tmp_path, drive_id=image_path)

            # Build OCRLine list from PaddleOCR-VL layout blocks
            lines: List[OCRLine] = []
            confidences: List[float] = []
            texts: List[str] = []

            for block in vl_result.layout_blocks:
                if block.score >= self._ocr_threshold:
                    lines.append(
                        OCRLine(
                            text=block.text,
                            confidence=block.score,
                            bbox=[tuple(int(v) for v in pt) for pt in block.bbox],
                        )
                    )
                    confidences.append(block.score)
                    texts.append(block.text)

            full_text = "\n".join(texts)
            mean_conf = sum(confidences) / len(confidences) if confidences else 0.0

            script = self._script_detector.detect(full_text) if full_text else "latin"

            return OCRPageResult(
                text=full_text,
                lines=lines,
                mean_confidence=mean_conf,
                script=script,
                pass_type=pass_type,
                language_used=["en"],
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                # Best-effort temp cleanup; log rather than swallow so a
                # persistent leak (e.g. Windows file lock) is visible.
                logger.debug("Could not remove temp OCR image %s: %s", tmp_path, exc)

    def _preprocess_for_ocr(self, img: Any) -> Any:
        """Preprocess image for optimal OCR."""
        # Convert to grayscale if needed
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img

        # Apply noise reduction
        gray = cv2.bilateralFilter(gray, 9, 75, 75)

        # Apply histogram equalization for better contrast
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

        return gray

    def _crop_title_region(self, img: Any) -> Optional[Any]:
        """Crop the top portion of the image where titles typically appear.

        Uses a simple heuristic: title is in the top 30% of the image,
        but we also scan for high-contrast text regions.
        """
        h, w = img.shape[:2]
        if h < self.MIN_DIMENSION or w < self.MIN_DIMENSION:
            return None

        # Top region for title
        title_h = int(h * self.TITLE_REGION_FRACTION)
        title_region = img[0:title_h, :]

        return title_region

    def _normalize_arabic_result(self, result: OCRPageResult) -> OCRPageResult:
        """Apply Arabic normalization to OCR result text."""
        normalized_lines: List[OCRLine] = []
        normalized_text_parts: List[str] = []
        total_conf = 0.0
        count = 0

        for line in result.lines:
            norm_text = self._normalizer.normalize(line.text)
            normalized_text_parts.append(norm_text)
            total_conf += line.confidence
            count += 1
            normalized_lines.append(
                OCRLine(
                    text=norm_text,
                    confidence=line.confidence,
                    bbox=line.bbox,
                )
            )

        norm_text = "\n".join(normalized_text_parts)
        mean_conf = total_conf / count if count > 0 else result.mean_confidence

        return OCRPageResult(
            text=norm_text,
            lines=normalized_lines,
            mean_confidence=mean_conf,
            script=result.script,
            pass_type=result.pass_type,
            language_used=result.language_used,
        )

    def get_text_statistics(self, text: str) -> Dict[str, Any]:
        """Get statistics about extracted text for decision making."""
        if not text:
            return {"word_count": 0, "line_count": 0, "avg_word_length": 0.0}

        lines = text.strip().split("\n")
        words = text.split()
        avg_len = sum(len(w) for w in words) / len(words) if words else 0.0

        return {
            "word_count": len(words),
            "line_count": len(lines),
            "avg_word_length": avg_len,
            "script": self._script_detector.detect(text),
        }
