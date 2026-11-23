"""Export worker for Milestone 5.

Polls the export_queue table for pending export records and
processes them through the ExportService pipeline.

Runs in a background thread, similar to ProcessingWorker.
"""

from __future__ import annotations

import time

from core.logger import setup_logger
from models.export_record import ExportRecord, ExportStatus
from repositories.export_repository import ExportRepository
from services.export_service import ExportService

logger = setup_logger(__name__)


class ExportWorker:
    """Worker that processes export queue records.

    Unlike ProcessingWorker which extends BaseWorker, ExportWorker
    has its own polling loop that operates on the export_queue table
    rather than the jobs table.

    States:
      PENDING → PROCESSING → COMPLETED
      FAILED → RETRYING → PENDING (via reset_retrying_exports)
    """

    WORKER_NAME = "ExportWorker"
    POLL_INTERVAL = 10  # seconds between poll cycles

    def __init__(
        self,
        export_service: ExportService,
        export_repo: ExportRepository,
        poll_interval: int = POLL_INTERVAL,
    ) -> None:
        self._export_service = export_service
        self._export_repo = export_repo
        self._poll_interval = poll_interval
        self._running: bool = False
        self._processed_count: int = 0
        self._failed_count: int = 0
        self._consecutive_failures: int = 0
        self.MAX_CONSECUTIVE_FAILURES = 10

    @property
    def running(self) -> bool:
        return self._running

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def failed_count(self) -> int:
        return self._failed_count

    def initialize(self) -> None:
        """Initialize the worker before starting the polling loop.

        Recovers exports left in PROCESSING by a crash so they resume from the
        persisted metadata/cover state instead of being stranded.
        """
        self._running = True
        self._consecutive_failures = 0
        try:
            recovered = self._export_repo.recover_stuck_exports()
            if recovered > 0:
                logger.info(
                    "[ExportWorker] Recovered %d stuck export(s) "
                    "PROCESSING -> PENDING",
                    recovered,
                )
        except Exception as exc:
            logger.warning("[ExportWorker] Stuck-export recovery failed: %s", exc)
        logger.info("[ExportWorker] Initialized")

    def shutdown(self) -> None:
        """Shutdown the worker."""
        self._running = False
        logger.info("[ExportWorker] Shutdown")

    def health_check(self) -> dict:
        """Return the worker's health status."""
        return {
            "worker_name": self.WORKER_NAME,
            "running": self._running,
            "processed_count": self._processed_count,
            "failed_count": self._failed_count,
            "consecutive_failures": self._consecutive_failures,
            "poll_interval": self._poll_interval,
        }

    def run(self) -> None:
        """Main polling loop. Runs until ``shutdown()`` is called.

        Polls the export_queue for PENDING and RETRYING records,
        processes one at a time, and updates status accordingly.
        """
        self.initialize()
        logger.info("[ExportWorker] Starting polling loop")

        try:
            while self._running:
                try:
                    # Reset retrying records back to pending
                    reset_count = self._export_repo.reset_retrying_exports()
                    if reset_count > 0:
                        logger.info(
                            "[ExportWorker] Reset %d retrying export(s) to PENDING",
                            reset_count,
                        )

                    pending = self._export_repo.get_pending_exports(limit=10)
                    if not pending:
                        time.sleep(self._poll_interval)
                        continue

                    for record in pending:
                        if not self._running:
                            break
                        try:
                            self._process_export(
                                drive_file_id=record["drive_file_id"],
                                job_id=record["job_id"],
                            )
                        except Exception as exc:
                            # One record's failure must not skip the rest of the
                            # batch: _process_export already marked that export
                            # FAILED and counted it, so the next record is
                            # independent and still runs in this cycle.
                            logger.error(
                                "[ExportWorker] Export failed for "
                                "drive_file_id=%s — continuing with the next "
                                "record: %s",
                                record["drive_file_id"],
                                type(exc).__name__,
                            )

                    # All pending records processed — sleep
                    if self._running:
                        time.sleep(self._poll_interval)

                except StopIteration:
                    break
                except Exception as exc:
                    self._consecutive_failures += 1
                    logger.error(
                        "[ExportWorker] Error in polling loop "
                        "(consecutive failures: %d): %s",
                        self._consecutive_failures,
                        exc,
                    )
                    if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        logger.critical(
                            "[ExportWorker] Too many consecutive failures "
                            "(%d) — stopping worker",
                            self._consecutive_failures,
                        )
                        self.shutdown()
                        break
                    time.sleep(self._poll_interval)
        except KeyboardInterrupt:
            logger.info("[ExportWorker] Received keyboard interrupt — shutting down")
        finally:
            self.shutdown()

    def _process_export(self, drive_file_id: str, job_id: str) -> ExportRecord:
        """Process a single export record.

        Delegates to ExportService.process_export and handles
        status transitions and retry logic.
        """
        logger.info(
            "[ExportWorker] Picked up export for drive_file_id=%s (job_id=%s)",
            drive_file_id,
            job_id[:8] + "...",
        )
        logger.info(
            "[ExportWorker] Processing export for drive_file_id=%s (job_id=%s)",
            drive_file_id,
            job_id[:8] + "...",
        )

        # Mark as processing
        self._export_repo.update_export_status(
            drive_file_id, ExportStatus.PROCESSING.value
        )

        try:
            result = self._export_service.process_export(
                drive_file_id=drive_file_id,
                job_id=job_id,
                max_retries=self._config_max_retries,
            )
            self._processed_count += 1
            self._consecutive_failures = 0
            return result
        except Exception as exc:
            self._failed_count += 1
            logger.error(
                "[ExportWorker] Export failed for drive_file_id=%s: %s",
                drive_file_id,
                type(exc).__name__,
            )
            # _fail_export is called inside process_export, but if
            # an unhandled exception escapes, handle it here too
            self._export_repo.update_export_status(
                drive_file_id,
                ExportStatus.FAILED.value,
                error_message=type(exc).__name__,
            )
            raise

    @property
    def _config_max_retries(self) -> int:
        """Get max_retries from the config, with fallback."""
        config = getattr(self._export_service, "_config", None)
        if config:
            return getattr(config, "export_max_retries", 3)
        return 3
