"""Processing worker for book cover identification.

This worker implements the book identification pipeline:
1. Preprocess the image (auto-rotate, resize, grayscale, denoise).
2. Run OCR (PaddleOCR-VL / Baidu OCR API) to extract text.
3. Normalize OCR text (fix common mistakes, extract ISBN).
4. Look up books via Google Books and Open Library.
5. Rank candidates using RapidFuzz similarity.
6. Calculate confidence score.
7. If confidence >= threshold: mark COMPLETED.
8. If confidence < threshold: mark MANUAL_REVIEW (never FAILED/rejected).
9. On exception: mark FAILED and handle retry (but no book is ever
   permanently rejected -- all books reach needs_review for inspection).
10. Non-negotiable: NO DISCOVERED BOOK MAY BE REJECTED.
"""

from __future__ import annotations

import os
import time  # noqa: F401 — re-exported for test patching of polling loop sleep
from typing import Optional, Set

from core.logger import setup_logger
from models.processing_job import ProcessingJob
from services.book_lookup_service import BookLookupService
from services.confidence_service import ConfidenceService
from services.image_preprocessor import ImagePreprocessor
from services.metadata_service import MetadataService
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer
from workers.base_worker import BaseWorker

logger = setup_logger(__name__)

# Supported image extensions for book cover processing
SUPPORTED_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"}


def _extension_from_magic(path: str) -> Optional[str]:
    """Best-effort image extension from file magic bytes."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(12)
    except OSError:
        return None

    header_hex = header.hex()
    if header_hex.startswith("ffd8ff"):
        return ".jpeg"
    if header_hex.startswith("89504e47"):
        return ".png"
    if (
        header_hex.startswith("52494646")
        and len(header_hex) >= 16
        and header[8:12].hex() == "57454250"
    ):
        return ".webp"
    if header.startswith(b"BM"):
        return ".bmp"
    if header_hex.startswith("47494638"):
        return ".png"
    return None


class ProcessingWorker(BaseWorker):
    """Worker that processes book cover images through the identification pipeline.

    Uses MetadataService to run: preprocess -> OCR -> normalize -> lookup ->
    rank -> confidence, then marks the job as COMPLETED or MANUAL_REVIEW.
    """

    WORKER_NAME = "ProcessingWorker"
    PROCESSING_TIMEOUT = 120  # seconds max per job

    def __init__(
        self,
        job_service,
        metadata_service: Optional[MetadataService] = None,
        metadata_repo=None,
    ) -> None:
        super().__init__(job_service, self.WORKER_NAME)
        self._metadata_service = metadata_service
        self._image_preprocessor = ImagePreprocessor()
        if metadata_repo is not None and self._metadata_service is not None:
            self._metadata_service.set_metadata_repo(metadata_repo)

    def initialize(self) -> None:
        """Initialize OCR and other heavy resources lazily."""
        super().initialize()
        if self._metadata_service is None:
            logger.info("Initializing MetadataService with default services")
        else:
            logger.info("Using provided MetadataService instance")
        logger.info("[Worker %s] Initialization complete", self._worker_name)

    def shutdown(self) -> None:
        """Release resources."""
        super().shutdown()
        logger.info("[Worker %s] Shutdown complete", self._worker_name)

    def process(self, job: ProcessingJob) -> Optional[bool]:
        """Process a single book cover identification job.

        Steps:
        1. Verify the local file exists.
        2. Verify the file has a supported image extension.
        3. Run the MetadataService identification pipeline.
        4. Return True on success (job marked COMPLETED by caller).

        This method does NOT update job status — the caller (BaseWorker)
        handles status transitions. It raises on any failure.
        """
        # Step 1: Verify file exists
        if not os.path.exists(job.local_path):
            raise FileNotFoundError(f"Local file does not exist: {job.local_path}")

        # Step 2: Verify image extension
        _, ext = os.path.splitext(job.local_path)
        ext_lower = ext.lower()
        if not ext_lower:
            ext_lower = _extension_from_magic(job.local_path) or ""
        if ext_lower not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file extension '{ext_lower}' for file "
                f"{job.file_name}. Supported: {SUPPORTED_EXTENSIONS}"
            )

        # Step 3: Verify image safety before OCR/vision
        is_safe, error_message = self._image_preprocessor.validate_image_safety(
            job.local_path
        )
        if not is_safe:
            raise ValueError(f"Unsafe image: {error_message}")

        logger.info(
            "[Job %s] Starting book identification (Drive ID: %s)",
            job.job_id,
            job.file_id,
        )
        logger.info(
            "[Job %s] Local file: %s",
            job.job_id,
            job.local_path,
        )

        # Step 4: Run identification pipeline
        metadata_service = (
            self._metadata_service or self._get_default_metadata_service()
        )
        metadata, candidates = metadata_service.identify_book(
            job.local_path,
            drive_file_id=job.file_id,
            job_id=job.job_id,
        )

        # Step 4b: Duplicate (same image, or a different photo of a book that
        # is already stored) -- close the job without OCR side effects.
        if metadata is not None and getattr(metadata, "status", None) == "duplicate":
            reason = metadata.rejection_reason or "duplicate"
            self._job_service.mark_duplicate(
                job.job_id, reason, worker_name=self._worker_name
            )
            logger.info(
                "[Job %s] Skipped duplicate (%s) for Drive ID: %s",
                job.job_id,
                reason,
                job.file_id,
            )
            return None

        # Step 5: Handle result
        if metadata is None and candidates is None:
            # Fallback: NO discovery at all. Per non-negotiable requirement,
            # no discovered book may be rejected. Create a minimal metadata
            # entry so the book is persisted and reaches needs_review.
            logger.warning(
                "[Job %s] No candidates and no metadata returned — "
                "creating minimal entry for review",
                job.job_id,
            )
            from models.book_metadata import BookMetadata

            minimal_meta = BookMetadata(
                title="(unreadable)",
                confidence=0.0,
                source="fallback",
                status="needs_review",
                drive_file_id=job.file_id,
                job_id=job.job_id,
                rejection_reason="no_candidates_no_vision",
                ocr_text="",
                raw_ocr="",
            )
            # Persist via metadata repo if available
            if self._metadata_service and self._metadata_service._metadata_repo:
                self._metadata_service._metadata_repo.upsert_metadata(minimal_meta)
            metadata = minimal_meta

        if metadata is None:
            # Low confidence or rejection_reason -- still persisted by
            # MetadataService (never truly rejected per non-negotiable req)
            logger.warning(
                "[Job %s] Metadata needs review — persisting for inspection",
                job.job_id,
            )
            # Store candidate info for review
            review_metadata = self._build_candidate_summary(candidates)
            # Include OCR text and extracted fields from the best candidate
            if candidates:
                best = candidates[0]  # Already ranked by confidence
                review_metadata["ocr_text"] = best.metadata.ocr_text
                review_metadata["raw_ocr"] = best.metadata.raw_ocr
                review_metadata["extracted_title"] = best.metadata.extracted_title
                review_metadata["extracted_author"] = best.metadata.extracted_author
                review_metadata["extracted_isbn"] = best.metadata.extracted_isbn
                review_metadata["extracted_publisher"] = (
                    best.metadata.extracted_publisher
                )
                review_metadata["match_score"] = best.metadata.match_score
                review_metadata["confidence"] = best.metadata.confidence
                review_metadata["rejection_reason"] = best.metadata.rejection_reason
            self._job_service.set_manual_review(
                job.job_id,
                error_message="Low confidence identification",
                metadata=review_metadata,
                worker_name=self._worker_name,
            )
            logger.info(
                "[Job %s] Marked for MANUAL_REVIEW (Drive ID: %s)",
                job.job_id,
                job.file_id,
            )
            # Return None to signal worker handled the status transition
            return None

        # Success — high confidence identification
        if metadata.status == "needs_review":
            # Vision-only preservation or other needs_review path:
            # metadata is valid but cannot be auto-verified.
            logger.info(
                "[Job %s] Metadata needs review: '%s' " "(confidence=%.4f, source=%s)",
                job.job_id,
                metadata.title,
                metadata.confidence,
                metadata.source,
            )
            review_metadata = {
                "title": metadata.title,
                "authors": metadata.authors,
                "publisher": metadata.publisher,
                "confidence": metadata.confidence,
                "ocr_text": metadata.ocr_text,
                "raw_ocr": metadata.raw_ocr,
                "extracted_title": metadata.extracted_title,
                "extracted_author": metadata.extracted_author,
                "match_score": metadata.match_score,
                "rejection_reason": metadata.rejection_reason,
                "vision_title": metadata.vision_title,
                "vision_author": metadata.vision_author,
                "vision_publisher": metadata.vision_publisher,
                "vision_confidence": metadata.vision_confidence,
                "vision_used": metadata.vision_used,
            }
            self._job_service.set_manual_review(
                job.job_id,
                error_message=(
                    "Vision extraction preserved — no metadata match, "
                    "needs human verification"
                    if metadata.vision_used
                    else "Low confidence identification"
                ),
                metadata=review_metadata,
                worker_name=self._worker_name,
            )
            logger.info(
                "[Job %s] Marked for MANUAL_REVIEW (Drive ID: %s)",
                job.job_id,
                job.file_id,
            )
            return None

        logger.info(
            "[Job %s] Book identified: '%s' (confidence=%.4f, source=%s)",
            job.job_id,
            metadata.title,
            metadata.confidence,
            metadata.source,
        )
        logger.info(
            "[Job %s] Identification complete (Drive ID: %s)",
            job.job_id,
            job.file_id,
        )
        return True

    def _get_default_metadata_service(self) -> MetadataService:
        """Create a default MetadataService with standard dependency chain."""
        return MetadataService(
            image_preprocessor=ImagePreprocessor(),
            ocr_service=OCRService(),
            text_normalizer=TextNormalizer(),
            book_lookup=BookLookupService(),
            candidate_ranker=None,
            confidence_service=ConfidenceService(),
        )

    @staticmethod
    def _build_candidate_summary(candidates) -> dict:
        """Build a comprehensive summary dict of candidates for manual review.

        Includes all evidence needed for human review:
        - Candidate metadata (title, authors, ISBNs, publisher, source)
        - Match scores (title, author, isbn, publisher, total)
        - ISBN state (MATCH, CONFLICT, UNAVAILABLE)
        - Availability flags for title/author/publisher
        """
        return {
            "candidates": (
                [
                    {
                        "title": c.metadata.title,
                        "authors": c.metadata.authors,
                        "isbn_10": c.metadata.isbn_10,
                        "isbn_13": c.metadata.isbn_13,
                        "publisher": c.metadata.publisher,
                        "source": c.metadata.source,
                        "total_score": c.total_score,
                        "title_score": c.title_score,
                        "author_score": c.author_score,
                        "isbn_score": c.isbn_score,
                        "publisher_score": c.publisher_score,
                        "isbn_state": c.isbn_state,
                        "title_available": c.title_available,
                        "author_available": c.author_available,
                        "publisher_available": c.publisher_available,
                        "confidence": c.metadata.confidence,
                    }
                    for c in candidates
                ]
                if candidates
                else []
            )
        }
