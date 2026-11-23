"""OCR service using PaddleOCR-VL (Baidu OCR API).

OCR REQUIREMENT (enforce: EasyOCR prohibited in production pipeline):
- Only Baidu OCR API (PaddleOCR-VL) is used for OCR/vision processing.
- No EasyOCR import, initialization, or fallback.
- If PaddleOCR-VL fails, the result is OCR_FAILED — NOT routed to EasyOCR.

Extracts text from preprocessed book cover images via PaddleOCR-VL API.
Returns structured OCRResult with text, confidence, and word list.
"""

from __future__ import annotations

from typing import List, Optional

from core.logger import setup_logger
from models.ocr_result import OCRResult, OCRWord
from services.paddleocr_vl_extractor import PaddleOCRVlExtractor
from services.paddleocr_vl_service import PaddleOCRVlService

logger = setup_logger(__name__)


class OCRService:
    """Extracts text from book cover images using PaddleOCR-VL (Baidu OCR API).

    OCR REQUIREMENT: No EasyOCR. If PaddleOCR-VL fails after all retries,
    returns OCR_FAILED — NOT routed to any other OCR engine.

    Responsibilities:
    - Call PaddleOCR-VL API on preprocessed images
    - Extract: title, subtitle, author, ISBN, publisher from layout blocks
    - Return structured OCRResult with text, confidence, and word list
    """

    def __init__(
        self,
        languages: Optional[List[str]] = None,
        use_gpu: bool = False,
        confidence_threshold: float = 0.5,
    ) -> None:
        self._languages = languages or ["en"]
        self._confidence_threshold = confidence_threshold
        self._paddleocr = PaddleOCRVlService()
        self._extractor = PaddleOCRVlExtractor()

    def extract_text(self, image_path: str) -> OCRResult:
        """Run PaddleOCR-VL (Baidu OCR API) on an image.

        Args:
            image_path: Path to the preprocessed image.

        Returns:
            OCRResult with extracted text, words, and confidence.
            Words below _confidence_threshold are excluded.
        """
        logger.info("Running PaddleOCR-VL (Baidu OCR) on: %s", image_path)

        try:
            vl_result = self._paddleocr.extract(
                image_path,
                drive_id="ocr_service_" + image_path.split("/")[-1],
            )

            words: List[OCRWord] = []
            for block in vl_result.layout_blocks:
                conf = float(block.score)
                if conf < self._confidence_threshold:
                    continue
                words.append(
                    OCRWord(
                        text=block.text.strip(),
                        confidence=round(conf, 4),
                        bounding_box=self._normalise_bbox(block.bbox),
                    )
                )

            all_text = "\n".join(w.text for w in words)

            avg_confidence = (
                sum(w.confidence for w in words) / len(words) if words else 0.0
            )

            result = OCRResult(
                text=all_text,
                confidence=round(avg_confidence, 4),
                words=words,
                language="unknown",
                image_path=image_path,
            )

            logger.info(
                "PaddleOCR-VL complete: %d words, confidence=%.4f",
                len(words),
                result.confidence,
            )
            return result

        except Exception as e:
            logger.warning("PaddleOCR-VL (Baidu OCR) failed: %s — OCR_FAILED", e)
            return OCRResult(
                text="",
                confidence=0.0,
                words=[],
                language="unknown",
                image_path=image_path,
            )

    @staticmethod
    def _normalise_bbox(bbox) -> List[List[int]]:
        """Convert a provider bounding box into ``[[x, y], ...]`` pairs.

        PaddleOCR-VL returns a flat ``[x1, y1, x2, y2]`` rectangle (see
        ``LayoutBlock.bbox``); older callers and some mocks pass a list of
        ``(x, y)`` corner pairs. Both shapes are accepted, and anything
        unexpected yields an empty box so a malformed coordinate cannot turn a
        good OCR result into a total failure (the previous code assumed pairs
        and raised ``'int' object is not subscriptable`` on real responses).
        """
        if not bbox:
            return []
        try:
            values = list(bbox)
            if len(values) == 4 and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            ):
                x1, y1, x2, y2 = (int(value) for value in values)
                return [[x1, y1], [x2, y2]]
            return [[int(point[0]), int(point[1])] for point in values]
        except (TypeError, ValueError, IndexError):
            logger.warning("Unrecognised OCR bounding box shape: %r", bbox)
            return []

    def extract_fields(self, ocr_result: OCRResult) -> dict:
        """Extract structured fields from PaddleOCR-VL OCR text.

        Parses the OCR text to find title, subtitle, author, ISBN,
        and publisher using keyword-based heuristics.

        Returns:
            Dict with keys: title, subtitle, author, isbn, publisher.
            Values are strings or None.
        """
        text = ocr_result.text
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        fields: dict[str, str | None] = {
            "title": None,
            "subtitle": None,
            "author": None,
            "isbn": None,
            "publisher": None,
        }

        from services.text_normalizer import TextNormalizer

        isbns = TextNormalizer().extract_isbn(text)
        if isbns:
            fields["isbn"] = isbns[0]

        for line in lines:
            if len(line) > 3 and not self._looks_like_label(line):
                if fields["title"] is None:
                    fields["title"] = line
                elif fields["author"] is None:
                    fields["author"] = line

        if fields["title"] is None and lines:
            fields["title"] = lines[0]

        if fields["title"] and len(lines) > 1 and fields["author"] is None:
            fields["author"] = lines[-1] if len(lines) > 1 else None

        logger.debug("Extracted fields: %s", fields)
        return fields

    @staticmethod
    def _looks_like_label(line: str) -> bool:
        """Check if a line looks like a label rather than content."""
        lower = line.lower()
        return any(
            lower.startswith(kw) for kw in ("by ", "author:", "isbn:", "publisher:")
        )
