"""Export service for Milestone 5.

Orchestrates the publication and enrichment pipeline:
  BookMetadata -> Validation -> Cover Resolution -> Cover Download -> Sheets Export
"""

from __future__ import annotations

from typing import Optional

from core.logger import setup_logger
from models.export_config import ExportConfig
from models.export_record import ExportRecord, ExportStatus
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.cover_repository import CoverRepository
from repositories.export_repository import ExportRepository
from services.clean_cover_service import CleanCoverService
from services.cover_download_service import CoverDownloadService
from services.cover_resolver_service import CoverResolverService
from services.google_sheets_service import GoogleSheetsService
from services.metadata_enrichment_service import MetadataEnrichmentService
from services.metadata_validation_service import MetadataValidationService

logger = setup_logger(__name__)


class ExportService:
    """Orchestrates the full export pipeline for a single BookMetadata record.

    Pipeline:
      1. Load BookMetadata from metadata.db
      2. Validate metadata quality
      3. Resolve cover URLs (priority-based fallback)
      4. Download cover (try each URL until one passes validation)
      5. Export to Google Sheets
      6. Persist export record

    On failure: increments retry count, saves error message, marks
    export as FAILED for later retry.
    """

    def __init__(
        self,
        metadata_repo: BookMetadataRepository,
        cover_repo: CoverRepository,
        export_repo: ExportRepository,
        validation_service: MetadataValidationService,
        cover_resolver: CoverResolverService,
        cover_downloader: CoverDownloadService,
        sheets_service: GoogleSheetsService,
        config: ExportConfig,
        enrichment_service: MetadataEnrichmentService = None,
        clean_cover_service: Optional[CleanCoverService] = None,
        book_lookup_service=None,
    ) -> None:
        self._metadata_repo = metadata_repo
        self._cover_repo = cover_repo
        self._export_repo = export_repo
        self._validation_service = validation_service
        self._cover_resolver = cover_resolver
        self._cover_downloader = cover_downloader
        self._sheets_service = sheets_service
        self._config = config
        self._enrichment_service = enrichment_service
        self._clean_cover_service = clean_cover_service or CleanCoverService(
            book_lookup=book_lookup_service
            or getattr(cover_resolver, "_book_lookup_service", None)
        )

    def process_export(
        self,
        drive_file_id: str,
        job_id: str,
        max_retries: int = 3,
        force_enrichment: bool = False,
    ) -> ExportRecord:
        """Process a single export: validate, enrich, cover, export.

        Retries resume from persisted state: a record whose metadata is already
        enriched skips provider lookups, and a validated cover already on disk
        is reused without any network call.

        Args:
            drive_file_id: Google Drive file ID.
            job_id: Original processing job ID.
            max_retries: Maximum retry attempts.
            force_enrichment: Re-run enrichment even when the record already
                looks resolved (used for controlled re-verification).

        Returns:
            ExportRecord with final status.
        """
        # Step 0: Create or get export queue record
        existing = self._export_repo.get_export_by_drive_file_id(drive_file_id)
        if existing is None:
            self._export_repo.create_export_record(drive_file_id, job_id, max_retries)

        # Step 1: Load metadata
        self._export_repo.update_export_status(
            drive_file_id, ExportStatus.PROCESSING.value
        )

        metadata = self._metadata_repo.get_by_drive_file_id(drive_file_id)
        if metadata is None:
            error_msg = f"No metadata found for drive_file_id={drive_file_id}"
            self._fail_export(drive_file_id, error_msg)
            return self._get_export_record(drive_file_id)

        logger.info(
            "Export started for drive_file_id=%s (title=%s)",
            drive_file_id,
            metadata.title[:50] if metadata.title else "[no title]",
        )

        try:
            # Step 2: Validate
            validation = self._validation_service.validate(metadata)
            if not validation.is_valid:
                if validation.confidence < 0.3:
                    logger.warning(
                        "Low confidence metadata for drive_file_id=%s "
                        "(confidence=%.4f) — exporting with review status",
                        drive_file_id,
                        validation.confidence,
                    )

            # Step 2b: Enrich metadata from APIs (Google Books / Open Library)
            # Uses vision-resolved title + author as search identity.
            # Does NOT overwrite vision title/author/publisher.
            if self._enrichment_service and (
                force_enrichment or not self._enrichment_resolved(metadata)
            ):
                try:
                    self._enrichment_service.enrich(metadata)
                    # Persist enriched metadata before touching covers/Sheets
                    self._metadata_repo.update_metadata(metadata)
                except Exception as exc:
                    logger.warning(
                        "Metadata enrichment failed for drive_file_id=%s: %s",
                        drive_file_id,
                        exc,
                    )
            else:
                logger.info(
                    "Enrichment already resolved for drive_file_id=%s — "
                    "reusing persisted metadata",
                    drive_file_id,
                )

            # Step 3: Retrieve CLEAN EXTERNAL cover (Phase 35B)
            # Drive images are NEVER used as final covers.
            # CleanCoverService uses Google Books / Open Library / Exa
            # to find a clean digital cover matching the verified title + author.
            local_cover_path: Optional[str] = None
            downloaded_cover_url: Optional[str] = None
            if self._clean_cover_service:
                try:
                    cover_url, cover_source, cover_local_path = (
                        self._clean_cover_service.find_clean_cover(metadata)
                    )
                except Exception as exc:
                    # A cover failure must never fail the export: identification
                    # and metadata remain successful, and the cover is retried
                    # on a later run because the status is persisted.
                    logger.warning(
                        "Cover retrieval failed for drive_file_id=%s: %s — "
                        "exporting metadata without cover",
                        drive_file_id,
                        exc,
                    )
                    cover_url, cover_source, cover_local_path = None, "not_found", None
                    if (metadata.cover_status or "").lower() != "found":
                        metadata.cover_status = "pending_retry"
                    self._metadata_repo.update_metadata(metadata)
                if cover_local_path:
                    # Downloaded and validated (dimensions, format, placeholder
                    # checks). Only this branch counts as a successful cover.
                    local_cover_path = cover_local_path
                    downloaded_cover_url = cover_url or metadata.cover_url
                    if downloaded_cover_url:
                        metadata.cover_url = downloaded_cover_url
                    metadata.local_cover_path = cover_local_path
                    metadata.cover_source = cover_source
                    if downloaded_cover_url:
                        metadata.cover_source_url = downloaded_cover_url
                    metadata.cover_status = "found"
                    metadata.cover_source_type = "external_bibliographic"
                    self._metadata_repo.update_metadata(metadata)
                    logger.info(
                        "Clean cover retrieved: source=%s, url=%s (drive=%s)",
                        cover_source,
                        downloaded_cover_url,
                        drive_file_id,
                    )
                elif cover_url:
                    # A URL exists but the image did not pass validation.
                    # Keep it for a later retry, but do NOT present it as a
                    # successful cover to the spreadsheet.
                    metadata.cover_source = cover_source or "unknown"
                    metadata.cover_source_url = cover_url
                    metadata.cover_status = "pending_retry"
                    metadata.cover_source_type = "external_bibliographic"
                    self._metadata_repo.update_metadata(metadata)
                    logger.warning(
                        "Cover URL found but image validation failed for "
                        "drive_file_id=%s (source=%s, url=%s) — "
                        "exported without cover, retry pending",
                        drive_file_id,
                        cover_source,
                        cover_url,
                    )
                else:
                    # Persist the negative result so later retries know a cover
                    # search already ran. Existing valid cover data is kept.
                    if not metadata.cover_url:
                        metadata.cover_status = "not_found"
                    metadata.cover_source = cover_source or "not_found"
                    metadata.cover_source_type = (
                        "external_bibliographic"
                        if cover_source != "not_found"
                        else None
                    )
                    self._metadata_repo.update_metadata(metadata)
                    logger.info(
                        "No clean external cover found for drive_file_id=%s — "
                        "book exported without cover (source=%s)",
                        drive_file_id,
                        cover_source,
                    )
            else:
                logger.warning(
                    "No CleanCoverService configured for drive_file_id=%s",
                    drive_file_id,
                )

            # Step 5: Export to Sheets (always attempt, even without cover)
            exported_data = {
                "title": metadata.title,
                "authors": metadata.authors,
                "isbn_10": metadata.isbn_10,
                "isbn_13": metadata.isbn_13,
                "publisher": metadata.publisher,
                "published_date": metadata.published_date,
                "language": metadata.language,
                "categories": metadata.categories,
                "description": metadata.description,
                "cover_url": downloaded_cover_url or metadata.cover_url,
                "cover_source": metadata.cover_source,
                "cover_status": metadata.cover_status,
                "local_cover_path": local_cover_path,
                "page_count": metadata.page_count,
                "confidence": metadata.confidence,
                "source": metadata.source,
                "status": GoogleSheetsService.export_status(metadata),
                "drive_file_id": drive_file_id,
                "vision_title": metadata.vision_title,
                "vision_author": metadata.vision_author,
                "vision_publisher": metadata.vision_publisher,
                "vision_confidence": metadata.vision_confidence,
                "vision_used": metadata.vision_used,
            }

            # Update metadata cover_url with the successfully downloaded URL
            if downloaded_cover_url:
                metadata.cover_url = downloaded_cover_url

            # Reuse the persisted row id (verified by the sheet service against
            # the row's title) so a re-export updates the same row.
            sheet_row_id = self._sheets_service.get_or_create_row(
                metadata, local_cover_path, row_id=self._known_sheet_row(drive_file_id)
            )
            sheet_url = self._sheets_service._build_sheet_url(sheet_row_id)

            # Step 6: Persist export record
            self._export_repo.update_export_status(
                drive_file_id,
                ExportStatus.COMPLETED.value,
                sheet_row_id=str(sheet_row_id) if sheet_row_id else None,
            )
            self._export_repo.update_export_record(
                drive_file_id=drive_file_id,
                status=ExportStatus.COMPLETED.value,
                sheet_row_id=str(sheet_row_id) if sheet_row_id else None,
                sheet_url=sheet_url,
                exported_columns=exported_data,
            )

            logger.info(
                "Export completed for drive_file_id=%s (row=%s)",
                drive_file_id,
                sheet_row_id,
            )
            return self._get_export_record(drive_file_id)

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Export failed for drive_file_id=%s: %s",
                drive_file_id,
                error_msg,
            )
            self._fail_export(drive_file_id, error_msg)
            return self._get_export_record(drive_file_id)

    @staticmethod
    def _enrichment_resolved(metadata) -> bool:
        """True when this record has already completed the enrichment stage.

        Used to avoid re-querying providers on export retries. A record counts
        as resolved once it carries provenance and a cover outcome. Every cover
        status is written *after* enrichment in ``process_export``, so the
        presence of one proves the enrichment stage already ran — including
        ``pending_retry``, which only asks for the cover to be retried.
        """
        source = (metadata.source or "").strip()
        cover_status = (metadata.cover_status or "").strip().lower()
        return bool(source) and cover_status in (
            "found",
            "not_found",
            "pending_retry",
            "invalid",
            "rejected_mismatch",
        )

    def _known_sheet_row(self, drive_file_id: str) -> Optional[int]:
        """Return the previously persisted sheet row for a Drive file, if any."""
        try:
            record = self._export_repo.get_export_record(drive_file_id)
        except Exception:
            return None
        row_id = getattr(record, "sheet_row_id", None)
        if row_id is None:
            return None
        try:
            return int(str(row_id).strip())
        except (TypeError, ValueError):
            return None

    def _fail_export(self, drive_file_id: str, error_msg: str) -> None:
        """Mark an export as failed and increment retry count.

        If retries remain, sets status to RETRYING and schedules the next
        retry using exponential backoff (60s, 120s, 240s, ...).
        If max retries exceeded, sets status to FAILED (dead-letter).
        """
        record = self._export_repo.get_export_by_drive_file_id(drive_file_id)
        if record:
            current_retry = record["retry_count"]
            new_retry = current_retry + 1
            if new_retry <= self._config.export_max_retries:
                new_status = ExportStatus.RETRYING.value
                # Schedule next retry with exponential backoff
                # retry_count is the current count BEFORE this attempt failed
                # so delay is based on the number of retries already done
                self._export_repo.set_next_retry(
                    drive_file_id,
                    retry_count=current_retry,
                    base_delay=float(self._config.export_retry_delay),
                )
                logger.info(
                    "Retry scheduled for drive_file_id=%s (attempt %d/%d)",
                    drive_file_id,
                    new_retry,
                    self._config.export_max_retries,
                )
            else:
                new_status = ExportStatus.FAILED.value
            self._export_repo.update_export_status(
                drive_file_id, new_status, error_message=error_msg
            )
        else:
            self._export_repo.update_export_status(
                drive_file_id, ExportStatus.FAILED.value, error_message=error_msg
            )

    def _get_export_record(self, drive_file_id: str) -> ExportRecord:
        """Fetch the ExportRecord model from repository."""
        row = self._export_repo.get_export_record(drive_file_id)
        if row is None:
            # Fallback: build from queue
            queue_row = self._export_repo.get_export_by_drive_file_id(drive_file_id)
            if queue_row:
                return ExportRecord(
                    drive_file_id=drive_file_id,
                    job_id=queue_row["job_id"],
                    status=ExportStatus(queue_row["status"]),
                    retry_count=queue_row["retry_count"],
                    last_error=queue_row["last_error"],
                )
            # Ultimate fallback: create a FAILED record
            return ExportRecord(
                drive_file_id=drive_file_id,
                job_id="",
                status=ExportStatus.FAILED,
                last_error="Export record not found",
            )
        if isinstance(row, ExportRecord):
            return row
        return ExportRecord(
            drive_file_id=drive_file_id,
            job_id=row.get("job_id", ""),
            status=ExportStatus(row.get("status", ExportStatus.FAILED.value)),
            sheet_row_id=row.get("sheet_row_id"),
            sheet_url=row.get("sheet_url"),
            exported_at=row.get("exported_at"),
            retry_count=row.get("retry_count", 0),
            last_error=row.get("last_error"),
            exported_columns=row.get("exported_columns"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def get_dead_letter_exports(self) -> list:
        """Return all FAILED exports that have exhausted their retry budget.

        Delegates to ExportRepository.get_dead_letter_exports().
        """
        return self._export_repo.get_dead_letter_exports()

    def get_retrying_exports(self) -> list:
        """Return all RETRYING exports with their scheduled retry time.

        Delegates to ExportRepository.get_retrying_exports().
        """
        return self._export_repo.get_retrying_exports()
