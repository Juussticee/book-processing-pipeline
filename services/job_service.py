"""Job service for managing the processing job lifecycle."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import List, Optional

from core.logger import setup_logger
from models.drive_file import DriveFile
from models.processing_job import ErrorCategory, JobStatus, ProcessingJob
from repositories.job_repository import JobRepository

logger = setup_logger(__name__)


class JobService:
    """Service for managing processing jobs.

    Responsibilities:
    - Verify a downloaded file exists on disk.
    - Prevent duplicate jobs for the same Drive file ID.
    - Create processing jobs.
    - Manage job status transitions (retry, complete, fail).
    """

    MAX_RETRIES = 3

    def __init__(self, job_repo: JobRepository, config=None) -> None:
        self._job_repo = job_repo
        self._config = config

    def _get_config_value(self, name, default):
        if self._config is not None:
            return getattr(self._config, name, default)
        return default

    def requeue_failed_jobs(self, drive_file_id: Optional[str] = None) -> List[str]:
        """Requeue jobs that exhausted their retries (status ``failed``).

        Returns the requeued job ids. Completed books and skipped duplicates are
        never touched; each requeued job re-runs the normal pipeline from
        preprocessing, which costs one OCR call per photo.
        """
        job_ids = self._job_repo.requeue_failed_jobs(drive_file_id)
        for job_id in job_ids:
            logger.info("[Job %s] Requeued after exhausting its retries", job_id)
        return job_ids

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_job_for_file(
        self,
        drive_file: DriveFile,
        local_path: str,
        priority: int = 0,
        worker_name: Optional[str] = None,
    ) -> Optional[ProcessingJob]:
        """Create a processing job for a downloaded Drive file.

        Verifies:
        - The local file exists on disk.
        - No existing active job for this Drive file ID.

        Returns the created job, or None if:
        - The file doesn't exist on disk (logged as warning).
        - A job for this file already exists (logged as info).
        """
        if not os.path.exists(local_path):
            logger.warning(
                "[Drive ID: %s] Local file does not exist at %s",
                drive_file.file_id,
                local_path,
            )
            return None

        existing_job = self._job_repo.get_job_by_drive_file_id(drive_file.file_id)
        if existing_job and existing_job.status in (
            JobStatus.PENDING,
            JobStatus.PROCESSING,
            JobStatus.RETRYING,
            JobStatus.MANUAL_REVIEW,
        ):
            logger.info(
                "[Job %s] Job already exists for Drive ID %s "
                "(status: %s) -- skipping creation",
                existing_job.job_id,
                drive_file.file_id,
                existing_job.status.value,
            )
            return None

        job = self._job_repo.create_job(
            file_id=drive_file.file_id,
            file_name=drive_file.name,
            local_path=local_path,
            priority=priority,
            worker_name=worker_name,
        )

        if job is None:
            logger.warning(
                "Failed to create job for Drive ID %s -- "
                "possible unique constraint violation",
                drive_file.file_id,
            )
            return None

        logger.info(
            "[Job %s] Created processing job for Drive ID %s, "
            "file: %s, priority: %d",
            job.job_id,
            job.file_id,
            job.file_name,
            job.priority,
        )
        return job

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def start_job(self, job_id: str) -> bool:
        """Mark a job as PROCESSING. Returns True if updated."""
        return self._job_repo.update_status(job_id, JobStatus.PROCESSING)

    def claim_job(self, job_id: str, worker_name: Optional[str] = None) -> bool:
        """Atomically claim a PENDING job for this worker.

        Returns False when another worker got there first, so the caller can
        move on instead of processing the same job twice.
        """
        return self._job_repo.claim_job(job_id, worker_name)

    def heartbeat(self, job_id: str, worker_name: Optional[str] = None) -> bool:
        """Renew the lease of a job that is still being processed."""
        return self._job_repo.heartbeat(job_id, worker_name)

    def complete_job(self, job_id: str, worker_name: Optional[str] = None) -> bool:
        """Mark a job as COMPLETED."""
        logger.info("[Job %s] Processing completed", job_id)
        return self._job_repo.complete_job(job_id, worker_name)

    def set_manual_review(
        self,
        job_id: str,
        error_message: Optional[str] = None,
        metadata: Optional[dict] = None,
        worker_name: Optional[str] = None,
    ) -> bool:
        """Mark a job as MANUAL_REVIEW (low confidence, needs review)."""
        logger.info("[Job %s] Marked for manual review", job_id)
        return self._job_repo.set_manual_review(
            job_id, error_message, metadata, worker_name
        )

    def mark_duplicate(
        self, job_id: str, reason: str, worker_name: Optional[str] = None
    ) -> bool:
        """Close a job as CANCELLED because the file duplicates known work.

        Duplicates are not failures: the job must not retry, and the reason
        records which original file it duplicates.
        """
        logger.info("[Job %s] Skipped as duplicate (%s)", job_id, reason)
        return self._job_repo.update_status(
            job_id,
            JobStatus.CANCELLED,
            error_message=reason,
            worker_name=worker_name,
        )

    def fail_job(
        self,
        job_id: str,
        error_message: str,
        worker_name: Optional[str] = None,
        error_category: Optional[ErrorCategory] = None,
    ) -> bool:
        """Mark a job as FAILED and increment retry count.

        If retry_count < MAX_RETRIES, the job is set to RETRYING
        with exponential backoff. If retry_count >= MAX_RETRIES,
        the job is marked FAILED permanently.

        Returns True if the job was updated.
        """
        job = self._job_repo.get_job_by_id(job_id)
        if job is None:
            logger.warning("[Job %s] Cannot fail -- job not found", job_id)
            return False

        new_retry_count = self._job_repo.increment_retry(job_id)
        logger.error(
            "[Job %s] Processing failed (attempt %d/%d): %s",
            job_id,
            new_retry_count,
            self.MAX_RETRIES,
            error_message,
        )

        # Classify error if not provided
        if error_category is None:
            error_category = self._classify_error(error_message)

        # Permanent errors are not retried
        if error_category == ErrorCategory.PERMANENT:
            self._job_repo.fail_job(
                job_id, error_message, worker_name,
                error_category=error_category.value,
            )
            logger.error(
                "[Job %s] Permanent failure -- job marked as FAILED",
                job_id,
            )
            return True

        # Retry path
        if new_retry_count >= self.MAX_RETRIES:
            self._job_repo.fail_job(
                job_id, error_message, worker_name,
                error_category=error_category.value,
            )
            logger.error(
                "[Job %s] Max retries (%d) exceeded -- "
                "job marked as FAILED permanently",
                job_id,
                self.MAX_RETRIES,
            )
            return True

        # Calculate backoff delay
        delay = self._calculate_retry_delay(new_retry_count)
        next_retry = (datetime.utcnow() + timedelta(seconds=delay)).isoformat()

        # Transition to RETRYING with backoff
        self._job_repo.update_status(
            job_id,
            JobStatus.RETRYING,
            error_message,
            worker_name,
            extra_updates={
                "error_category": error_category.value,
                "next_retry_at": next_retry,
                "last_error_at": datetime.utcnow().isoformat(),
            },
        )
        logger.info(
            "[Job %s] Job marked for retry (attempt %d/%d, delay=%ds, category=%s)",
            job_id,
            new_retry_count,
            self.MAX_RETRIES,
            delay,
            error_category.value,
        )
        return True

    def _classify_error(self, error_message: str) -> ErrorCategory:
        """Classify an error message as transient or permanent.

        Transient errors: network timeouts, temporary service unavailability,
        rate limits, temporary file locks, etc.
        Permanent errors: file not found, unsupported format, data corruption,
        validation failures, etc.

        Delegates to ``utils.http_errors.classify_error`` so the service-layer
        classifier stays consistent with the HTTP-layer classifier.
        """
        from utils.http_errors import classify_error

        category = classify_error(Exception(error_message) if error_message else None)
        if category == "permanent":
            return ErrorCategory.PERMANENT
        # Default to TRANSIENT for unknown errors -- better to retry once
        # than to fail permanently on an unrecognized issue.
        return ErrorCategory.TRANSIENT

    def _calculate_retry_delay(self, retry_count: int) -> float:
        """Calculate exponential backoff delay for a retry.

        Uses configurable parameters from Config, falling back to defaults.
        Formula: min(initial_delay * (backoff_factor ** (retry_count - 1)), max_delay)
        """
        initial_delay = float(
            self._get_config_value("retry_initial_delay_seconds", 5.0)
        )
        max_delay = float(self._get_config_value("retry_max_delay_seconds", 300.0))
        backoff_factor = float(self._get_config_value("retry_backoff_factor", 2.0))

        delay = initial_delay * (backoff_factor ** (retry_count - 1))
        return min(delay, max_delay)

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    def get_retryable_jobs(self) -> List[ProcessingJob]:
        """Return RETRYING jobs whose backoff has expired.

        Only jobs with next_retry_at <= now are eligible for retry.
        """
        return self._job_repo.get_retryable_jobs()

    def recover_stuck_jobs(self) -> int:
        """Recover PROCESSING jobs whose lease has expired.

        "Expired" means no heartbeat (and no start time) within the configured
        ``processing_timeout_seconds``; a job whose worker is still renewing
        its heartbeat is never stolen. Jobs that already exhausted their
        retries are failed rather than requeued indefinitely.

        Called on application startup and whenever the queue is empty.

        Returns the number of jobs whose status changed.
        """
        timeout_seconds = self._get_config_value("processing_timeout_seconds", 900)
        count = self._job_repo.recover_stuck_jobs(
            timeout_seconds, max_retries=self.MAX_RETRIES
        )
        if count > 0:
            logger.info(
                "Recovered %d expired job lease(s) -- requeued or failed",
                count,
            )
        return count

    def get_pending_jobs(self) -> List[ProcessingJob]:
        """Return all pending jobs in FIFO order.

        Retries jobs that were marked RETRYING but haven't exceeded
        the retry limit. These are moved back to PENDING before
        fetching the queue.
        """
        self._job_repo.reset_retrying_jobs()
        return self._job_repo.get_pending_jobs()

    def get_processing_jobs(self) -> List[ProcessingJob]:
        """Return all jobs currently in PROCESSING status."""
        return self._job_repo.get_processing_jobs()

    def get_failed_jobs(self) -> List[ProcessingJob]:
        """Return all FAILED jobs."""
        return self._job_repo.get_failed_jobs()

    def get_queue_status(self) -> dict:
        """Return the current queue status for health checks."""
        counts = self._job_repo.get_count_by_status()
        return {
            "pending": counts.get(JobStatus.PENDING.value, 0),
            "processing": counts.get(JobStatus.PROCESSING.value, 0),
            "completed": counts.get(JobStatus.COMPLETED.value, 0),
            "failed": counts.get(JobStatus.FAILED.value, 0),
            "retrying": counts.get(JobStatus.RETRYING.value, 0),
            "cancelled": counts.get(JobStatus.CANCELLED.value, 0),
            "manual_review": counts.get(JobStatus.MANUAL_REVIEW.value, 0),
        }
