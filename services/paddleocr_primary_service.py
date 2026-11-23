"""Phase 43: PaddleOCR-VL primary OCR — Baidu OCR ONLY, no EasyOCR fallback.

OCR REQUIREMENT (enforce: EasyOCR prohibited in production pipeline):
- Only Baidu OCR API (PaddleOCR-VL) is used for OCR/vision processing.
- No EasyOCR import, initialization, or fallback anywhere in this service.
- If PaddleOCR-VL fails, the result is OCR_FAILED — NOT routed to EasyOCR.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from core.logger import setup_logger
from models.ocr_result import OCRResult, OCRWord
from services.paddleocr_vl_service import PaddleOCRVlService
from services.paddleocr_vl_extractor import PaddleOCRVlExtractor

logger = setup_logger(__name__)


class PaddleOCRPrimaryService:
    """Primary OCR service using PaddleOCR-VL (Baidu OCR API) only.

    OCR REQUIREMENT: No EasyOCR. If PaddleOCR-VL fails after all retries,
    returns OCR_FAILED — NOT routed to any other OCR engine.
    """
    
    def __init__(
        self,
        paddleocr_token: Optional[str] = None,
        paddleocr_base_url: str = "https://paddleocr.aistudio-app.com",
    ) -> None:
        self._paddleocr = PaddleOCRVlService(
            token=paddleocr_token,
            base_url=paddleocr_base_url,
        )
        self._extractor = PaddleOCRVlExtractor()

    def extract_text(self, image_path: str) -> Tuple[OCRResult, str]:
        """Extract text from image using PaddleOCR-VL (Baidu OCR API) only.

        OCR REQUIREMENT: No EasyOCR fallback. If PaddleOCR-VL fails after
        all retries, returns an OCR_FAILED result — NOT routed to EasyOCR.

        Returns:
            Tuple of (OCRResult, engine_used) where engine_used is
            'paddleocr_vl' on success or 'ocr_failed' on failure.
        """
        if not self._paddleocr.is_configured():
            logger.error("PaddleOCR-VL not configured — OCR_FAILED")
            return self._make_failed_result(image_path, "PaddleOCR-VL not configured"), "ocr_failed"

        try:
            result = self._extract_with_paddleocr(image_path)
            return result, "paddleocr_vl"
        except Exception as e:
            logger.warning("PaddleOCR-VL failed after all retries: %s — OCR_FAILED", e)
            return self._make_failed_result(image_path, str(e)), "ocr_failed"

    def _make_failed_result(self, image_path: str, reason: str) -> OCRResult:
        """Build an OCR_FAILED result — no EasyOCR fallback."""
        return OCRResult(
            text="",
            confidence=0.0,
            words=[],
            language="unknown",
            image_path=image_path,
        )

    def _extract_with_paddleocr(self, image_path: str) -> OCRResult:
        """Extract text using PaddleOCR-VL with retry logic for queue limits."""
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                # Get layout result
                vl_result = self._paddleocr.extract(image_path, drive_id=os.path.basename(image_path))
                
                # Extract title and author using spatial layout
                extraction = self._extractor.extract(
                    layout_blocks=vl_result.layout_blocks,
                    raw_markdown=vl_result.raw_markdown,
                )
                
                # Build OCRResult
                all_text = vl_result.raw_markdown or "\n".join(b.text for b in vl_result.layout_blocks)
                words = [OCRWord(text=b.text, confidence=b.score, bounding_box=b.bbox) for b in vl_result.layout_blocks]
                
                ocr_result = OCRResult(
                    text=all_text,
                    confidence=0.9 if vl_result.layout_blocks else 0.5,
                    words=words,
                    language="unknown",
                    image_path=image_path,
                )
                
                # Add metadata
                ocr_result.metadata = {
                    "engine": "paddleocr_vl",
                    "layout_blocks": len(vl_result.layout_blocks),
                    "orientation": vl_result.orientation,
                    "extracted_title": extraction.selected_title,
                    "extracted_author": extraction.selected_author,
                    "title_candidates": [c.text for c in extraction.title_candidates],
                    "author_candidates": [c.text for c in extraction.author_candidates],
                }
                
                return ocr_result
                
            except Exception as e:
                if '10010' in str(e) or '队列已满' in str(e):
                    wait = 60 * (attempt + 1)
                    logger.warning("PaddleOCR-VL queue full, waiting %ds (attempt %d/%d)", wait, attempt + 1, max_attempts)
                    time.sleep(wait)
                else:
                    raise
        
        raise Exception(f"PaddleOCR-VL failed after {max_attempts} attempts")
    
    def is_paddleocr_configured(self) -> bool:
        """Check if PaddleOCR-VL is properly configured."""
        return self._paddleocr.is_configured()
