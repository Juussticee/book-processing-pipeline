"""Base worker framework for processing jobs."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

from core.logger import setup_logger
from models.processing_job import ErrorCategory, ProcessingJob
from services.job_service import JobService
from utils.http_errors import classify_error

logger = setup_logger(__name__)


class _LeaseHeartbeat:
    """Renews a job's heartbeat while a worker is processing it.

    Without this, a job that legitimately runs longer than the lease timeout
    would look abandoned and could be picked up by a second worker.
    """

    def __init__(self, job_service: JobService, job_id: str, interval: float) -> None:
        self._job_service = job_service
        self._job_id = job_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._interval <= 0:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"lease-{self._job_id[:8]}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._job_service.heartbeat(self._job_id):
                    return  # job finished or no longer ours
            except Exception as exc:  # never let the lease thread kill a job
                logger.debug("[Job %s] Heartbeat failed: %s", self._job_id, exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


class BaseWorker(ABC):
    """Abstract base class for all processing workers.

    Implements the core worker lifecycle:
    - initialize() / shutdown() for setup and cleanup
    - health_check() for monitoring
    - process(job) for processing a single job (implemented by subclasses)

    The worker is designed to be thread-safe -- it can be started in a
    background thread alongside the main sync engine.
    """

    WORKER_NAME: str = "BaseWorker"
    POLL_INTERVAL: int = 5  # seconds between poll cycles
    MAX_CONSECUTIVE_FAILURES: int = 10  # stop after this many loop failures
    # Must stay well below processing_timeout_seconds (default 900).
    HEARTBEAT_INTERVAL: float = 60.0

    def __init__(
        self,
        job_service: JobService,
        worker_name: Optional[str] = None,
    ) -> None:
        self._job_service = job_service
        self._worker_name = worker_name or self.WORKER_NAME
        self._running: bool = False
        self._consecutive_failures: int = 0
        self._processed_count: int = 0
        self._failed_count: int = 0
        self._lock = threading.Lock()

    @property
    def worker_name(self) -> str:
        return self._worker_name

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
        """Initialize the worker. Called once before the polling loop starts."""
        with self._lock:
            self._running = True
            self._consecutive_failures = 0
        logger.info("[Worker: %s] Initialized", self._worker_name)

    def shutdown(self) -> None:
        """Shutdown the worker. Called when stopping the polling loop."""
        with self._lock:
            self._running = False
        logger.info("[Worker: %s] Shutdown", self._worker_name)

    def health_check(self) -> dict:
        """Return the worker's health status."""
        with self._lock:
            return {
                "worker_name": self._worker_name,
                "running": self._running,
                "processed_count": self._processed_count,
                "failed_count": self._failed_count,
                "consecutive_failures": self._consecutive_failures,
                "poll_interval": self.POLL_INTERVAL,
            }

    @abstractmethod
    def process(self, job: ProcessingJob) -> Optional[bool]:
        """Process a single job.

        Returns:
        - True: job processed successfully (status set to COMPLETED).
        - False/None: worker handled status transition itself (e.g.
          MANUAL_REVIEW) — BaseWorker will not override the status.
        - Raises: exception indicates failure (status set to FAILED/retrying).
        """
        ...

    def run(self) -> None:
        """Main polling loop. Runs until ``shutdown()`` is called.

        Polls the job queue for pending jobs in FIFO order.
        Processes one job at a time (single-worker model).
        Designed to run in a background thread.
        """
        self.initialize()
        logger.info("[Worker: %s] Starting polling loop", self._worker_name)

        try:
            while self._running:
                try:
                    pending_jobs = self._job_service.get_pending_jobs()
                    if pending_jobs:
                        job = pending_jobs[0]  # FIFO - take oldest
                        self._process_job(job)
                    else:
                        # No jobs -- check for stuck jobs and sleep
                        self._job_service.recover_stuck_jobs()
                        time.sleep(self.POLL_INTERVAL)
                except (StopIteration, SystemExit):
                    # Used by tests to break the loop
                    break
                except Exception as exc:
                    with self._lock:
                        self._consecutive_failures += 1
                    logger.error(
                        "[Worker: %s] Error in polling loop "
                        "(consecutive failures: %d): %s",
                        self._worker_name,
                        self._consecutive_failures,
                        exc,
                    )
                    if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        logger.critical(
                            "[Worker: %s] Too many consecutive failures "
                            "(%d) -- stopping worker",
                            self._worker_name,
                            self._consecutive_failures,
                        )
                        self.shutdown()
                        break
                    time.sleep(self.POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info(
                "[Worker: %s] Received keyboard interrupt -- shutting down",
                self._worker_name,
            )
        finally:
            self.shutdown()

    def _process_job(self, job: ProcessingJob) -> None:
        """Process a single job with full lifecycle management."""
        logger.info(
            "[Job %s] Worker %s starting processing " "(Drive ID: %s, file: %s)",
            job.job_id,
            self._worker_name,
            job.file_id,
            job.file_name,
        )

        # Claim the job atomically: only one conditional UPDATE can win, so a
        # second worker polling the same PENDING row cannot process it too.
        if not self._job_service.claim_job(job.job_id, self._worker_name):
            still_owned = any(
                active.job_id == job.job_id
                and active.worker_name in (None, self._worker_name)
                for active in self._job_service.get_processing_jobs()
            )
            if not still_owned:
                logger.info(
                    "[Job %s] Already claimed or finished -- skipping",
                    job.job_id,
                )
                return

        heartbeat = _LeaseHeartbeat(
            self._job_service, job.job_id, self.HEARTBEAT_INTERVAL
        )
        heartbeat.start()
        try:
            result = self.process(job)
            if result is True:
                self._job_service.complete_job(job.job_id, self._worker_name)
                with self._lock:
                    self._processed_count += 1
                    self._consecutive_failures = 0
                logger.info(
                    "[Job %s] Processing completed successfully " "(Drive ID: %s)",
                    job.job_id,
                    job.file_id,
                )
            elif result is None:
                # None means the worker already handled the status
                # transition (e.g. MANUAL_REVIEW). Don't update further.
                logger.info(
                    "[Job %s] Worker handled final status (Drive ID: %s)",
                    job.job_id,
                    job.file_id,
                )
            else:
                self._handle_failure(job, "Process returned False")

        except Exception as exc:
            import traceback

            tb_str = traceback.format_exc()
            logger.error(
                "[Job %s] Exception in _process_job: %s\n%s",
                job.job_id,
                str(exc),
                tb_str,
            )
            self._handle_failure(job, str(exc), exc=exc)
        finally:
            heartbeat.stop()

    def _handle_failure(
        self,
        job: ProcessingJob,
        error_message: str,
        exc: Optional[BaseException] = None,
    ) -> None:
        """Handle a job processing failure with retry logic.

        Classifies the failure using ``utils.http_errors.classify_error`` and
        passes the resulting ``ErrorCategory`` to ``JobService.fail_job`` so
        permanent errors are not retried endlessly.
        """
        with self._lock:
            self._failed_count += 1

        category = classify_error(exc) if exc is not None else "unknown"
        error_category: Optional[ErrorCategory] = None
        if category == "transient":
            error_category = ErrorCategory.TRANSIENT
        elif category == "permanent":
            error_category = ErrorCategory.PERMANENT

        self._job_service.fail_job(
            job.job_id,
            error_message,
            self._worker_name,
            error_category=error_category,
        )
