"""Local VLM recovery orchestrator.

Mirrors GeminiRecoveryOrchestrator but uses LocalVisionService instead of
GeminiRecoveryService. The recovery flow is identical:

  Google Sheet (rows with confidence < 0.70)
    → Drive image download
    → LocalVisionService (title/author extraction via Ollama VLM)
    → MetadataService.identify_book() with VLM's title/author
    → Google Sheets row update (existing row, not duplicate)

The local VVM is NOT called for books with confidence >= 0.70.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from core.logger import setup_logger
from models.book_metadata import BookMetadata
from services.local_vision_service import (
    LocalVisionService,
    VLMRecoveryResult,
    LocalVLMNotAvailableError,
    RECOVERY_CONFIDENCE_THRESHOLD,
)
from services.google_sheets_service import GoogleSheetsService

logger = setup_logger(__name__)


class LocalVLMRecoveryStats:
    """Aggregate statistics for a local VVM recovery session."""

    def __init__(self):
        self.total_rows: int = 0
        self.processed: int = 0
        self.low_confidence_rows: int = 0
        self.vvm_calls: int = 0
        self.vvm_successes: int = 0
        self.re_identified: int = 0
        self.unresolved: int = 0
        self.retries: int = 0
        self.api_errors: list = []

    @property
    def gemini_calls(self) -> int:
        """Alias for backward compatibility."""
        return self.vvm_calls

    @property
    def gemini_successes(self) -> int:
        """Alias for backward compatibility."""
        return self.vvm_successes


class LocalVisionRecoveryOrchestrator:
    """Orchestrates the local VVM low-confidence recovery flow.

    Args:
        vvm_service: The LocalVisionService (handles Ollama communication,
            retries, memory safety).
        metadata_service: The existing MetadataService used for re-identifying
            books with the VVM's extracted title/author.
        sheets_service: The existing GoogleSheetsService for reading rows
            and updating them.
        drive_service: The existing DriveService for downloading images.
        downloads_dir: Where to temporarily save downloaded images.
    """

    def __init__(
        self,
        vvm_service: LocalVisionService,
        metadata_service,
        sheets_service: GoogleSheetsService,
        drive_service,
        downloads_dir: str = "data/downloads",
    ) -> None:
        self._vvm = vvm_service
        self._metadata = metadata_service
        self._sheets = sheets_service
        self._drive = drive_service
        self._downloads_dir = downloads_dir
        self._stats = LocalVLMRecoveryStats()

    def run(self, rows: Optional[List[Dict[str, str]]] = None) -> LocalVLMRecoveryStats:
        """Run the full recovery flow against the existing Google Sheet.

        1. Read all rows from the Sheet (or use provided rows for testing).
        2. Filter rows with confidence < 0.70.
        3. For each, download the image and send to local VVM.
        4. Re-run identify_book() with VVM's title/author.
        5. Update the existing Sheet row (no duplication).

        Args:
            rows: Optional pre-loaded rows (for testing). If None,
                reads from the Sheet.

        Returns:
            LocalVLMRecoveryStats with counts.
        """
        os.makedirs(self._downloads_dir, exist_ok=True)

        # Step 1: Read existing Sheet rows (or use provided)
        if rows is not None:
            sheet_rows = rows
        else:
            sheet_rows = self._sheets.read_all_rows()
        self._stats.total_rows = len(sheet_rows)
        logger.info(
            "Local VVM recovery: read %d total rows from Sheet",
            len(sheet_rows),
        )

        # Step 2: Filter for confidence < 0.70
        low_confidence_rows = []
        for row in sheet_rows:
            conf_str = row.get("Confidence", "")
            if not conf_str:
                continue
            try:
                conf = float(conf_str)
            except ValueError:
                continue
            if conf < RECOVERY_CONFIDENCE_THRESHOLD:
                low_confidence_rows.append(row)

        self._stats.low_confidence_rows = len(low_confidence_rows)
        logger.info(
            "Local VVM recovery: %d rows have confidence < %.2f",
            len(low_confidence_rows),
            RECOVERY_CONFIDENCE_THRESHOLD,
        )

        # Step 3: Process each low-confidence row
        for row in low_confidence_rows:
            row_num = row.get("_row_num", 0)
            drive_file_id = row.get("Drive File ID", "") or row.get("Drive ID", "")
            if not drive_file_id:
                logger.warning(
                    "Row %d has no Drive File ID — skipping", row_num
                )
                continue

            self._stats.processed += 1

            try:
                result = self._process_single_row(row, drive_file_id)
                if result is None:
                    self._stats.unresolved += 1
            except Exception as exc:
                # Don't crash the whole recovery on a single failure
                logger.error(
                    "Local VVM recovery failed for drive_file_id=%s: %s",
                    drive_file_id,
                    exc,
                )
                self._stats.api_errors.append(str(exc))
                self._stats.unresolved += 1

        return self._stats

    def process_rows(
        self, rows: List[Dict[str, str]]
    ) -> LocalVLMRecoveryStats:
        """Process pre-loaded rows (test-friendly alias for run)."""
        return self.run(rows=rows)

    def _process_single_row(
        self, row: Dict[str, str], drive_file_id: str
    ) -> Optional[BookMetadata]:
        """Process a single low-confidence Sheet row.

        Returns:
            BookMetadata if re-identified, None if unresolved.
        """
        # Download the Drive image
        image_path = self._download_image(drive_file_id)
        if image_path is None:
            self._stats.unresolved += 1
            return None

        # Get current confidence from the Sheet row
        current_conf = self._parse_confidence(row.get("Confidence", ""))

        # Step: Send to local VVM
        vvm_result = self._vvm.extract_from_image(
            image_path=image_path,
            current_confidence=current_conf,
            recovery_threshold=RECOVERY_CONFIDENCE_THRESHOLD,
        )

        self._stats.vvm_calls += 1
        if vvm_result.retry_count > 0:
            self._stats.retries += vvm_result.retry_count

        if not vvm_result.vlm_called:
            # Skipped (high confidence or memory safety)
            return None

        self._stats.vvm_successes += 1

        # Step: If VVM couldn't extract, preserve existing result
        if not vvm_result.title and not vvm_result.author:
            logger.warning(
                "VVM returned no title/author for %s — preserving existing",
                drive_file_id,
            )
            return None

        # Step: Re-run existing identify_book with VVM's title/author
        vvm_title = vvm_result.title or ""
        vvm_author = vvm_result.author or ""
        combined_text = f"{vvm_title}\n{vvm_author}"

        logger.info(
            "VVM extracted: title='%s', author='%s', conf=%.2f, latency=%.2fs",
            vvm_title,
            vvm_author,
            vvm_result.confidence,
            vvm_result.latency,
        )

        best_metadata, candidates = self._metadata.identify_book(
            image_path=image_path,
            raw_ocr_text=combined_text,
            drive_file_id=drive_file_id,
        )

        if best_metadata and best_metadata.confidence > current_conf:
            # Improvement — update the Sheet row
            self._stats.re_identified += 1
            self._update_sheet_row(row, best_metadata)
            logger.info(
                "VVM recovery: RE-IDENTIFIED %s — "
                "confidence %.4f -> %.4f (new title: %s)",
                drive_file_id,
                current_conf,
                best_metadata.confidence,
                best_metadata.title,
            )
            return best_metadata
        elif best_metadata:
            # Same or lower confidence — keep existing result
            logger.info(
                "VVM recovery: no improvement for %s "
                "(old=%.4f, new=%.4f) — preserving existing",
                drive_file_id,
                current_conf,
                best_metadata.confidence,
            )
            return None
        else:
            # No candidates found — preserve existing
            self._stats.unresolved += 1
            return None

    def _download_image(self, drive_file_id: str) -> Optional[str]:
        """Download the image for a Drive file ID."""
        local_filename = f"{drive_file_id}.jpg"
        local_path = os.path.join(self._downloads_dir, local_filename)

        # Check if already downloaded
        if os.path.exists(local_path):
            return local_path

        # Get the full Drive metadata from Drive, then download
        drive_file = self._drive.get_file_metadata(drive_file_id)
        if drive_file is None:
            logger.error("Could not fetch metadata for Drive file %s", drive_file_id)
            return None
        try:
            downloaded = self._drive.download_file(drive_file, local_path)
            if not downloaded:
                logger.error(
                    "Could not download Drive file %s for VVM recovery",
                    drive_file_id,
                )
                return None
            return local_path
        except Exception as exc:
            logger.error("Download failed for %s: %s", drive_file_id, exc)
            return None

    def _parse_confidence(self, conf_str: str) -> float:
        """Parse a confidence value from a string (Sheet format)."""
        try:
            return float(conf_str)
        except (ValueError, TypeError):
            return 0.0

    def _update_sheet_row(
        self, row: Dict[str, str], metadata: BookMetadata
    ) -> None:
        """Update an existing Sheet row with improved metadata.

        Uses get_or_create_row to ensure no duplication — if the row
        already exists (matched by Drive File ID), it is updated in place.
        """
        self._sheets.get_or_create_row(metadata)

    @property
    def stats(self) -> LocalVLMRecoveryStats:
        return self._stats
