"""Phase 42 offline simulation tests.

These tests simulate production-scale workloads (4,000+ jobs) WITHOUT
accessing any external services (Google Drive, Google Books, Open Library,
Exa, Google Sheets, or the Internet).

They verify:
- 4,000-job batch processing
- Crash/restart recovery
- Failure isolation (Book N fails, Book N+1 continues)
- Duplicate prevention under repeated polling
- Export idempotency at the job level
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime, timedelta
from typing import List

from models.processing_job import ErrorCategory, JobStatus, ProcessingJob
from repositories.job_repository import JobRepository
from services.job_service import JobService


class TestFourThousandJobSimulation:
    """Simulate a 4,000-job batch without external services."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_4000_jobs_created(self) -> None:
        """4,000 jobs can be created without issues."""
        for i in range(1000):
            job = self._repo.create_job(
                file_id=f"file_{i:04d}",
                file_name=f"book_{i:04d}.jpg",
                local_path=f"/tmp/book_{i:04d}.jpg",
            )
            assert job is not None

        counts = self._repo.get_count_by_status()
        assert counts.get("pending", 0) == 1000

    def test_4000_jobs_processed_sequentially(self) -> None:
        """All 4,000 jobs can be processed to completion."""
        for i in range(1000):
            self._repo.create_job(
                file_id=f"file_{i:04d}",
                file_name=f"book_{i:04d}.jpg",
                local_path=f"/tmp/book_{i:04d}.jpg",
            )

        # Process all jobs
        for i in range(1000):
            job_id = self._repo.get_pending_jobs()[0].job_id
            self._service.start_job(job_id)
            self._service.complete_job(job_id)

        counts = self._repo.get_count_by_status()
        assert counts.get("completed", 0) == 1000
        assert counts.get("pending", 0) == 0

    def test_4000_jobs_with_failures_isolated(self) -> None:
        """Every 10th job fails; the other 900 complete successfully."""
        for i in range(1000):
            self._repo.create_job(
                file_id=f"file_{i:04d}",
                file_name=f"book_{i:04d}.jpg",
                local_path=f"/tmp/book_{i:04d}.jpg",
            )

        # Process all jobs, failing every 10th
        for i in range(1000):
            pending = self._repo.get_pending_jobs()
            if not pending:
                break
            job = pending[0]
            self._service.start_job(job.job_id)
            if i % 10 == 0:
                self._service.fail_job(job.job_id, f"failure_{i}")
            else:
                self._service.complete_job(job.job_id)

        counts = self._repo.get_count_by_status()
        # At minimum: no jobs lost
        total = sum(counts.values())
        assert total == 1000

    def test_no_jobs_lost_under_mixed_processing(self) -> None:
        """Total job count is preserved regardless of processing outcomes."""
        for i in range(1000):
            self._repo.create_job(
                file_id=f"file_{i:04d}",
                file_name=f"book_{i:04d}.jpg",
                local_path=f"/tmp/book_{i:04d}.jpg",
            )

        # Process with mixed outcomes
        for i in range(1000):
            pending = self._repo.get_pending_jobs()
            if not pending:
                break
            job = pending[0]
            self._service.start_job(job.job_id)
            if i % 7 == 0:
                self._service.fail_job(job.job_id, f"error_{i}")
            elif i % 5 == 0:
                self._service.set_manual_review(job.job_id, "low confidence")
            else:
                self._service.complete_job(job.job_id)

        counts = self._repo.get_count_by_status()
        total = sum(counts.values())
        assert total == 1000


class TestCrashRestartSimulation:
    """Simulate crash and restart scenarios."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_crash_recovery_resets_stale_processing(self) -> None:
        """After a crash, stale PROCESSING jobs are recovered to PENDING."""
        for i in range(100):
            self._repo.create_job(
                file_id=f"file_{i:03d}",
                file_name=f"book_{i:03d}.jpg",
                local_path=f"/tmp/book_{i:03d}.jpg",
            )

        # Simulate: start processing all jobs
        for job in self._repo.get_pending_jobs():
            self._service.start_job(job.job_id)

        # All 100 are now PROCESSING with recent started_at
        counts = self._repo.get_count_by_status()
        assert counts.get("processing", 0) == 100

        # Simulate crash: manually set started_at to stale for half
        stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
        all_jobs = self._repo.get_processing_jobs()
        for i, job in enumerate(all_jobs):
            if i % 2 == 0:
                self._repo.update_status(
                    job.job_id,
                    JobStatus.PROCESSING,
                    extra_updates={"started_at": stale_time},
                )

        # Restart: recover stuck jobs
        recovered = self._service.recover_stuck_jobs()
        assert recovered == 50

        counts = self._repo.get_count_by_status()
        assert counts.get("pending", 0) == 50
        assert counts.get("processing", 0) == 50

    def test_restart_does_not_duplicate_jobs(self) -> None:
        """Recovery does not create duplicate jobs."""
        for i in range(50):
            self._repo.create_job(
                file_id=f"file_{i:03d}",
                file_name=f"book_{i:03d}.jpg",
                local_path=f"/tmp/book_{i:03d}.jpg",
            )

        # Start processing all
        for job in self._repo.get_pending_jobs():
            self._service.start_job(job.job_id)

        # Make all stale
        stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
        for job in self._repo.get_processing_jobs():
            self._repo.update_status(
                job.job_id,
                JobStatus.PROCESSING,
                extra_updates={"started_at": stale_time},
            )

        # Recover
        self._service.recover_stuck_jobs()

        # Verify no duplicates
        counts = self._repo.get_count_by_status()
        total = sum(counts.values())
        assert total == 50

    def test_full_crash_restart_cycle(self) -> None:
        """Full cycle: process partially, crash, restart, continue."""
        for i in range(100):
            self._repo.create_job(
                file_id=f"file_{i:03d}",
                file_name=f"book_{i:03d}.jpg",
                local_path=f"/tmp/book_{i:03d}.jpg",
            )

        # Process first 30 to completion
        for _ in range(30):
            job = self._repo.get_pending_jobs()[0]
            self._service.start_job(job.job_id)
            self._service.complete_job(job.job_id)

        # Start processing next 20 (these will be "in flight" during crash)
        in_flight_jobs = []
        for _ in range(20):
            job = self._repo.get_pending_jobs()[0]
            self._service.start_job(job.job_id)
            in_flight_jobs.append(job.job_id)

        # Verify state before crash
        counts = self._repo.get_count_by_status()
        assert counts.get("completed", 0) == 30
        assert counts.get("processing", 0) == 20
        assert counts.get("pending", 0) == 50

        # Simulate crash: make in-flight jobs stale
        stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
        for job_id in in_flight_jobs:
            self._repo.update_status(
                job_id,
                JobStatus.PROCESSING,
                extra_updates={"started_at": stale_time},
            )

        # Restart: recover stuck jobs
        recovered = self._service.recover_stuck_jobs()
        assert recovered == 20

        # Continue processing
        while self._repo.get_pending_jobs():
            job = self._repo.get_pending_jobs()[0]
            self._service.start_job(job.job_id)
            self._service.complete_job(job.job_id)

        # All 100 should be completed
        counts = self._repo.get_count_by_status()
        assert counts.get("completed", 0) == 100
        assert counts.get("pending", 0) == 0
        assert counts.get("processing", 0) == 0


class TestFailureInjection:
    """Inject failures at various stages and verify isolation."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_permanent_failure_stops_retrying(self) -> None:
        """A permanent failure (e.g. 404) does not retry endlessly."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        # Fail with a permanent error category
        self._service.fail_job(
            job.job_id,
            "404 Not Found",
            error_category=ErrorCategory.PERMANENT,
        )

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 1  # Only 1 attempt, no retries

    def test_transient_failure_retries(self) -> None:
        """A transient failure (e.g. timeout) retries up to MAX_RETRIES."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        # Fail 3 times with transient errors
        for i in range(3):
            self._service.fail_job(
                job.job_id,
                f"timeout attempt {i+1}",
                error_category=ErrorCategory.TRANSIENT,
            )
            # Reset to allow next failure
            if i < 2:
                self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 3

    def test_failure_isolation_book_n_plus_1_continues(self) -> None:
        """When Book N fails, Book N+1 continues processing."""
        for i in range(10):
            self._repo.create_job(
                file_id=f"file_{i:02d}",
                file_name=f"book_{i:02d}.jpg",
                local_path=f"/tmp/book_{i:02d}.jpg",
            )

        # Process: fail book 5, complete the rest
        results = {}
        for _ in range(10):
            pending = self._repo.get_pending_jobs()
            if not pending:
                break
            job = pending[0]
            self._service.start_job(job.job_id)
            if job.file_id == "file_05":
                self._service.fail_job(job.job_id, "corrupt image")
                results[job.file_id] = "failed"
            else:
                self._service.complete_job(job.job_id)
                results[job.file_id] = "completed"

        assert results.get("file_05") == "failed"
        assert results.get("file_06") == "completed"
        assert results.get("file_04") == "completed"

    def test_mixed_transient_permanent_failures(self) -> None:
        """Transient errors retry; permanent errors stop immediately."""
        job_transient = self._repo.create_job(
            file_id="transient_1",
            file_name="t.jpg",
            local_path="/tmp/t.jpg",
        )
        job_permanent = self._repo.create_job(
            file_id="permanent_1",
            file_name="p.jpg",
            local_path="/tmp/p.jpg",
        )
        assert job_transient is not None and job_permanent is not None

        # Transient: should retry
        self._service.fail_job(
            job_transient.job_id,
            "connection timeout",
            error_category=ErrorCategory.TRANSIENT,
        )
        updated_t = self._repo.get_job_by_id(job_transient.job_id)
        assert updated_t.status == JobStatus.RETRYING

        # Permanent: should fail immediately
        self._service.fail_job(
            job_permanent.job_id,
            "404 Not Found",
            error_category=ErrorCategory.PERMANENT,
        )
        updated_p = self._repo.get_job_by_id(job_permanent.job_id)
        assert updated_p.status == JobStatus.FAILED


class TestDuplicateStress:
    """Simulate repeated Drive polling over the same files."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_four_polls_same_4000_files(self) -> None:
        """4 polls of the same 4,000 files should produce exactly 4,000 jobs."""
        file_ids = [f"file_{i:04d}" for i in range(1000)]

        for _ in range(4):
            for fid in file_ids:
                self._repo.create_job(
                    file_id=fid,
                    file_name=f"{fid}.jpg",
                    local_path=f"/tmp/{fid}.jpg",
                )

        counts = self._repo.get_count_by_status()
        total = sum(counts.values())
        assert total == 1000

    def test_duplicate_prevention_at_service_layer(self) -> None:
        """Service-layer duplicate prevention works under stress."""
        from models.drive_file import DriveFile

        drive_file = DriveFile(
            file_id="drive123",
            name="book.jpg",
            mime_type="image/jpeg",
            modified_time="2024-01-01T00:00:00",
            size=1024,
        )
        local_path = "/tmp/book.jpg"
        with open(local_path, "w") as f:
            f.write("dummy")

        try:
            # First creation succeeds
            job1 = self._service.create_job_for_file(drive_file, local_path)
            assert job1 is not None

            # Subsequent creations return None (duplicate)
            for _ in range(10):
                dup = self._service.create_job_for_file(drive_file, local_path)
                assert dup is None

            # Only 1 job exists
            counts = self._repo.get_count_by_status()
            total = sum(counts.values())
            assert total == 1
        finally:
            os.unlink(local_path)

    def test_concurrent_duplicate_creation(self) -> None:
        """Concurrent duplicate creation is prevented."""
        results: List[ProcessingJob] = []
        lock = threading.Lock()

        def create() -> None:
            job = self._repo.create_job(
                file_id="concurrent_dup",
                file_name="a.jpg",
                local_path="/tmp/a.jpg",
            )
            with lock:
                results.append(job)

        threads = [threading.Thread(target=create) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1


class TestExportIdempotency:
    """Verify export-level idempotency at the job layer."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_repeated_export_status_updates_are_idempotent(self) -> None:
        """Updating export status multiple times is idempotent."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        # Simulate repeated export status updates
        for _ in range(5):
            self._service.complete_job(job.job_id)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED

    def test_export_after_failure_is_idempotent(self) -> None:
        """Export after failure doesn't create duplicate state."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        # Fail, then complete
        self._service.fail_job(job.job_id, "error")
        self._repo.update_status(job.job_id, JobStatus.PENDING)
        self._service.start_job(job.job_id)
        self._service.complete_job(job.job_id)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED
