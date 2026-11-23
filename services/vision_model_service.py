"""Vision-model book-cover extraction service.

Wraps VisionBookCoverExtractor to provide vision-based extraction
as a fallback when PaddleOCR-VL (Baidu OCR API) produces insufficient results.

The vision model is the authoritative extraction path when:
- PaddleOCR-VL produces no text
- PaddleOCR-VL confidence is below threshold
- Title extraction is suspicious
- Author extraction is missing
- Metadata lookup produces no reliable candidate
- OCR text conflicts with visual evidence

Usage:
    VisionModelService can be used standalone or injected into
    MetadataService as the vision extraction layer.

The service attempts extraction in this order:
1. Cached vision response (from data/vision_model_responses/)
2. Local VVM (Ollama-based, if available)
3. Gemini (if API key and quota available)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logger import setup_logger
from services.vision_bookcover_extractor import (
    VisionBookCoverExtractor,
    VisionMetadata,
)

logger = setup_logger(__name__)

# Cache directory for pre-cached vision model responses
VISION_CACHE_DIR = os.getenv(
    "VISION_CACHE_DIR",
    str(Path(__file__).resolve().parent.parent / "data" / "vision_model_responses"),
)

# Drive file IDs are at least this long to avoid false cache matches
MIN_DRIVE_FILE_ID_LENGTH = 10


class VisionModelService:
    """Service for vision-model book-cover extraction.

    Provides vision-based extraction as a fallback/override when PaddleOCR-VL
    (Baidu OCR API) produces insufficient results.
    Uses the VisionBookCoverExtractor from Phase 27, which sends images
    to a vision model and parses structured JSON responses.

    The vision model receives the RAW image and performs visual reasoning
    to extract title, author, publisher, orientation, etc.
    It does NOT rely on OCR output, heuristics, or candidate ranking.
    """

    def __init__(self, cache_dir: str = VISION_CACHE_DIR) -> None:
        self._extractor = VisionBookCoverExtractor()
        self._cache_dir = Path(cache_dir)
        # Backend availability is probed at most once per service instance: a
        # missing Ollama or disabled Gemini must not cost a probe (or an
        # exception) on every single book.
        self._local_vvm_available: Optional[bool] = None
        self._gemini_unavailable = False
        # Track vision model calls for metrics
        self._call_count = 0
        self._cache_hits = 0
        self._actual_calls = 0

    @property
    def stats(self) -> Dict:
        """Return usage statistics."""
        return {
            "total_calls": self._call_count,
            "cache_hits": self._cache_hits,
            "actual_model_calls": self._actual_calls,
        }

    def needs_vision(
        self,
        ocr_title: Optional[str],
        ocr_author: Optional[str],
        ocr_result_text: str,
        ocr_confidence: float,
        ocr_available: bool,
        candidates: List,
        confidence: Optional[float] = None,
    ) -> bool:
        """Determine whether the vision model should be invoked.

        Returns True (need vision) when:
        - OCR produced no usable text
        - OCR confidence is below threshold
        - Title extraction is missing
        - Author extraction is missing
        - No metadata candidates were found
        - OCR and metadata results disagree (low confidence despite candidates)

        Args:
            ocr_title: Extracted title from OCR (may be None).
            ocr_author: Extracted author from OCR (may be None).
            ocr_result_text: Full OCR text.
            ocr_confidence: OCR confidence score (0.0-1.0).
            ocr_available: Whether OCR produced any text.
            candidates: Metadata lookup candidates.
            confidence: Final confidence score (if computed).

        Returns:
            True if vision model extraction should be attempted.
        """
        # Case 1: No OCR text at all -> vision model needed
        if not ocr_available or not ocr_result_text.strip():
            logger.info("Vision needed: no OCR text extracted")
            return True

        # Case 2: OCR confidence too low -> vision model needed
        ocr_threshold = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.5"))
        if ocr_confidence < ocr_threshold:
            logger.info(
                "Vision needed: OCR confidence %.4f < threshold %.2f",
                ocr_confidence,
                ocr_threshold,
            )
            return True

        # Case 3: Title missing -> vision model needed
        if not ocr_title:
            logger.info("Vision needed: no title extracted from OCR")
            return True

        # Case 4: Author missing -> vision model needed
        if not ocr_author:
            logger.info("Vision needed: no author extracted from OCR")
            return True

        # Case 5: No candidates found -> vision model needed
        if not candidates:
            logger.info("Vision needed: no metadata candidates found")
            return True

        # Case 6: Low confidence despite candidates -> results disagree
        if confidence is not None and confidence < 0.50:
            logger.info(
                "Vision needed: confidence %.4f < 0.50 (OCR/metadata disagreement)",
                confidence,
            )
            return True

        # OCR is usable and produced reasonable results -> no vision needed
        return False

    def extract_vision_metadata(
        self,
        image_path: str,
        drive_file_id: str = "",
        current_confidence: Optional[float] = None,
    ) -> Optional[VisionMetadata]:
        """Extract book metadata via vision model.

        Tries cached responses first, then falls back to live vision model call.

        Args:
            image_path: Absolute path to the book-cover image.
            drive_file_id: Drive file ID (for cache lookup).
            current_confidence: The pipeline's current confidence for this book,
                if it already has one. Only consulted by the opt-in Gemini
                layer, which is limited to books below its recovery threshold
                so it can never spend quota on a book the pipeline already
                accepted.

        Returns:
            VisionMetadata if extraction succeeded, None otherwise.
        """
        self._call_count += 1

        # Strategy 1: Try cached vision model response
        vision_meta = self._try_cache(drive_file_id)
        if vision_meta is not None:
            logger.info("Vision extraction: using cached response")
            self._cache_hits += 1
            return vision_meta

        # Strategy 2: Try local VVM (if available)
        vision_meta = self._try_local_vvm(image_path)
        if vision_meta is not None:
            logger.info("Vision extraction: using local VVM")
            self._actual_calls += 1
            return vision_meta

        # Strategy 3: Try Gemini (if available)
        vision_meta = self._try_gemini(image_path, current_confidence)
        if vision_meta is not None:
            logger.info("Vision extraction: using Gemini")
            self._actual_calls += 1
            return vision_meta

        logger.warning("Vision extraction: all backends unavailable")
        return None

    def _try_cache(self, drive_file_id: str) -> Optional[VisionMetadata]:
        """Try to load a cached vision model response.

        Cached responses are stored in data/vision_model_responses/
        with filenames matching the Drive file ID as a prefix.
        e.g., drive_file_id="1AvSHrA5dQE2wR" matches
        "1AvSHrA5dQE2wRl_V6FLX_LGB8nxgj4FQ.json"

        Only matches when drive_file_id is a prefix of the cache filename.
        """
        if not drive_file_id or len(drive_file_id) < MIN_DRIVE_FILE_ID_LENGTH:
            return None

        if not self._cache_dir.exists():
            return None

        for f in self._cache_dir.iterdir():
            if not (f.is_file() and f.suffix == ".json"):
                continue
            if not f.stem.startswith(drive_file_id):
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    response_text = fh.read()
                vision_meta = VisionBookCoverExtractor.extract_from_response(
                    response_text
                )
                if vision_meta is not None:
                    return vision_meta
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                logger.debug("Cache parse error for %s: %s", f.name, exc)

        return None

    def _try_local_vvm(self, image_path: str) -> Optional[VisionMetadata]:
        """Try extraction via local VVM (Ollama).

        Uses moondream:latest via Ollama's HTTP API for local vision
        extraction. No external API key required.

        The availability probe is cached for the lifetime of the instance, so a
        host without Ollama pays it once instead of once per book.
        """
        if self._local_vvm_available is False:
            return None
        try:
            from services.local_vision_service import LocalVisionService

            vvm = LocalVisionService()
            if self._local_vvm_available is None:
                self._local_vvm_available = vvm._check_ollama_available()
            if not self._local_vvm_available:
                logger.debug("Local VVM not available (Ollama not running)")
                return None

            result = vvm.extract_from_image(image_path)
            if not result.vlm_called or result.error:
                logger.debug("Local VVM extraction not called or failed")
                return None

            return VisionMetadata(
                title=result.title,
                title_confidence=result.confidence,
                author=result.author,
                author_confidence=result.confidence,
                publisher=None,
                publisher_confidence=0.0,
                orientation="unknown",
                target_book_identified=bool(result.title),
            )
        except Exception as exc:
            logger.debug("Local VVM extraction failed: %s", exc)
            return None

    def _try_gemini(
        self, image_path: str, current_confidence: Optional[float] = None
    ) -> Optional[VisionMetadata]:
        """Try extraction via Gemini, but only when it is switched on.

        Gemini recovery is an opt-in layer (``GEMINI_RECOVERY_ENABLED``) and is
        never part of the OCR path. When it is disabled or has no key, this
        returns ``None`` immediately instead of constructing a service that
        cannot work — the previous code raised ``TypeError`` on every book and
        swallowed it, which made "disabled" indistinguishable from "broken".
        """
        if self._gemini_unavailable:
            return None
        if os.getenv("GEMINI_RECOVERY_ENABLED", "false").strip().lower() != "true":
            self._gemini_unavailable = True
            return None
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            logger.debug("Gemini recovery enabled but GEMINI_API_KEY is empty")
            self._gemini_unavailable = True
            return None
        if current_confidence is None:
            # Without a pipeline confidence the quota guard cannot be honoured,
            # so this layer stays out of the way.
            return None

        try:
            from services.gemini_recovery_service import (
                RECOVERY_CONFIDENCE_THRESHOLD,
                GeminiQuotaExhausted,
                GeminiRecoveryService,
            )

            if current_confidence >= RECOVERY_CONFIDENCE_THRESHOLD:
                return None

            gemini = GeminiRecoveryService(api_key=api_key)
            result = gemini.extract_from_image(
                image_path, current_confidence=current_confidence
            )
            if result is None or not result.gemini_called or result.parsing_error:
                return None
            if not result.title:
                return None

            # Map Gemini result to VisionMetadata
            return VisionMetadata(
                title=result.title,
                title_confidence=result.confidence,
                author=result.author,
                author_confidence=result.confidence,
                publisher=None,
                publisher_confidence=0.0,
                orientation="unknown",
                target_book_identified=True,
            )
        except GeminiQuotaExhausted as exc:  # pragma: no cover - opt-in layer
            logger.warning("Gemini recovery stopped: %s", exc)
            self._gemini_unavailable = True
            return None
        except Exception as exc:
            logger.debug("Gemini extraction failed: %s", exc)
            return None

    def vision_to_candidate_data(
        self, vision_meta: VisionMetadata
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Convert VisionMetadata to (title, author, publisher) tuple.

        Used to feed vision results into the existing book lookup service.
        """
        title = vision_meta.title
        author = vision_meta.author
        publisher = vision_meta.publisher

        logger.info(
            "Vision extraction result: title=%s, author=%s, publisher=%s "
            "(confidences: title=%.2f, author=%.2f, publisher=%.2f, "
            "orientation=%s, multiple_books=%s)",
            title,
            author,
            publisher,
            vision_meta.title_confidence,
            vision_meta.author_confidence,
            vision_meta.publisher_confidence,
            vision_meta.orientation,
            vision_meta.multiple_books_detected,
        )

        return title, author, publisher
