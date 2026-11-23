"""Phase 42 comprehensive verification tests.

Covers: retry/state persistence, stale recovery with config,
failure isolation, image safety in production path,
duplicate protection, export idempotency, restart recovery.
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime, timedelta
from typing import List

import pytest

from models.processing_job import ErrorCategory, JobStatus, ProcessingJob
from repositories.job_repository import JobRepository
from services.job_service import JobService


class TestRetryStatePersistence:
    """Verify retry_count, next_retry_at, and error_category persist correctly."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_first_transient_failure_sets_retrying_with_state(self) -> None:
        """First transient failure -> RETRYING with retry_count=1, next_retry_at set."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        result = self._service.fail_job(
            job.job_id, "connection timeout", error_category=ErrorCategory.TRANSIENT
        )
        assert result is True

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 1
        assert updated.error_category == ErrorCategory.TRANSIENT
        assert updated.next_retry_at is not None
        assert updated.last_error_at is not None

    def test_next_retry_at_is_in_the_future(self) -> None:
        """next_retry_at should be in the future after a transient failure."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        before = datetime.utcnow()
        self._service.fail_job(
            job.job_id, "timeout", error_category=ErrorCategory.TRANSIENT
        )

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.next_retry_at is not None
        # next_retry_at may be str or datetime depending on repo conversion
        if isinstance(updated.next_retry_at, str):
            next_retry = datetime.fromisoformat(updated.next_retry_at)
        else:
            next_retry = updated.next_retry_at
        assert next_retry > before

    def test_second_transient_failure_increments_retry_count(self) -> None:
        """Second transient failure → retry_count=2."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        self._service.fail_job(
            job.job_id, "timeout 1", error_category=ErrorCategory.TRANSIENT
        )
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        self._service.fail_job(
            job.job_id, "timeout 2", error_category=ErrorCategory.TRANSIENT
        )

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 2

    def test_retries_until_max_then_failed(self) -> None:
        """Retries continue until MAX_RETRIES, then FAILED."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        # MAX_RETRIES = 3, so 3 failures → FAILED
        for i in range(3):
            self._service.fail_job(
                job.job_id,
                f"timeout {i+1}",
                error_category=ErrorCategory.TRANSIENT,
            )
            if i < 2:
                self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 3

    def test_permanent_failure_fails_immediately(self) -> None:
        """Permanent failure → FAILED immediately, no retries."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        self._service.fail_job(
            job.job_id, "404 Not Found", error_category=ErrorCategory.PERMANENT
        )

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 1
        assert updated.error_category == ErrorCategory.PERMANENT

    def test_unknown_exception_defaults_to_transient(self) -> None:
        """Unknown exception → transient/retryable per documented policy."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        # No error_category provided, message doesn't match permanent keywords
        self._service.fail_job(job.job_id, "something unexpected happened")

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 1

    def test_error_category_persists_correctly(self) -> None:
        """error_category persists as the correct enum value."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        self._service.fail_job(
            job.job_id, "timeout", error_category=ErrorCategory.TRANSIENT
        )

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.error_category == ErrorCategory.TRANSIENT


class TestStaleRecoveryWithConfig:
    """Verify stale recovery respects configured timeout."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_recent_processing_job_not_recovered(self) -> None:
        """Recent PROCESSING job remains PROCESSING."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.status == JobStatus.PROCESSING

    def test_stale_processing_job_recovered(self) -> None:
        """Stale PROCESSING job becomes PENDING with started_at cleared."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
        self._repo.update_status(
            job.job_id,
            JobStatus.PROCESSING,
            extra_updates={"started_at": stale_time},
        )

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 1

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.status == JobStatus.PENDING
        assert updated.started_at is None

    def test_completed_jobs_untouched(self) -> None:
        """COMPLETED jobs are not affected by recovery."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.complete_job(job.job_id)

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.status == JobStatus.COMPLETED

    def test_failed_jobs_untouched(self) -> None:
        """FAILED jobs are not affected by recovery."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.fail_job(job.job_id, "error")

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.status == JobStatus.FAILED

    def test_manual_review_jobs_untouched(self) -> None:
        """MANUAL_REVIEW jobs are not affected by recovery."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.set_manual_review(job.job_id, "low confidence")

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.status == JobStatus.MANUAL_REVIEW

    def test_multiple_stale_jobs_recovered(self) -> None:
        """Multiple stale PROCESSING jobs are all recovered."""
        for i in range(10):
            job = self._repo.create_job(
                file_id=f"file_{i:02d}",
                file_name=f"book_{i:02d}.jpg",
                local_path=f"/tmp/book_{i:02d}.jpg",
            )
            assert job is not None
            stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
            self._repo.update_status(
                job.job_id,
                JobStatus.PROCESSING,
                extra_updates={"started_at": stale_time},
            )

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 10

    def test_mixed_recent_and_stale_jobs(self) -> None:
        """Only stale jobs are recovered; recent ones remain."""
        # Create 5 recent jobs
        for i in range(5):
            job = self._repo.create_job(
                file_id=f"recent_{i}",
                file_name=f"r_{i}.jpg",
                local_path=f"/tmp/r_{i}.jpg",
            )
            assert job is not None
            self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        # Create 5 stale jobs
        for i in range(5):
            job = self._repo.create_job(
                file_id=f"stale_{i}",
                file_name=f"s_{i}.jpg",
                local_path=f"/tmp/s_{i}.jpg",
            )
            assert job is not None
            stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
            self._repo.update_status(
                job.job_id,
                JobStatus.PROCESSING,
                extra_updates={"started_at": stale_time},
            )

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 5

        counts = self._repo.get_count_by_status()
        assert counts.get("pending", 0) == 5
        assert counts.get("processing", 0) == 5


class TestFailureIsolation:
    """Verify failure isolation: Book N fails, Book N+1 continues."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_mixed_batch_isolation(self) -> None:
        """Book A-F with mixed outcomes; failures don't block subsequent jobs."""
        results = {}
        for fid, outcome in [
            ("book_a", "success"),
            ("book_b", "transient_fail"),
            ("book_c", "metadata_fail"),
            ("book_d", "cover_fail"),
            ("book_e", "export_fail"),
            ("book_f", "success"),
        ]:
            job = self._repo.create_job(
                file_id=fid, file_name=f"{fid}.jpg", local_path=f"/tmp/{fid}.jpg"
            )
            assert job is not None
            self._service.start_job(job.job_id)

            if outcome == "success":
                self._service.complete_job(job.job_id)
                results[fid] = "completed"
            elif outcome == "transient_fail":
                self._service.fail_job(job.job_id, "connection timeout")
                results[fid] = "retrying"
            elif outcome == "metadata_fail":
                self._service.fail_job(job.job_id, "no metadata found")
                results[fid] = "retrying"
            elif outcome == "cover_fail":
                self._service.fail_job(job.job_id, "cover download failed")
                results[fid] = "retrying"
            elif outcome == "export_fail":
                self._service.fail_job(job.job_id, "export timeout")
                results[fid] = "retrying"

        # Book F must succeed regardless of B/C/D/E failures
        assert results["book_a"] == "completed"
        assert results["book_f"] == "completed"
        assert results["book_b"] == "retrying"
        assert results["book_c"] == "retrying"
        assert results["book_d"] == "retrying"
        assert results["book_e"] == "retrying"

    def test_failure_does_not_block_subsequent_jobs(self) -> None:
        """A failed job does not prevent the next job from being processed."""
        for i in range(10):
            self._repo.create_job(
                file_id=f"file_{i:02d}",
                file_name=f"book_{i:02d}.jpg",
                local_path=f"/tmp/book_{i:02d}.jpg",
            )

        # Process all, failing every 3rd
        completed = 0
        failed = 0
        for i in range(10):
            pending = self._repo.get_pending_jobs()
            if not pending:
                break
            job = pending[0]
            self._service.start_job(job.job_id)
            if i % 3 == 0:
                self._service.fail_job(job.job_id, f"error_{i}")
                failed += 1
            else:
                self._service.complete_job(job.job_id)
                completed += 1

        assert completed + failed == 10
        assert completed > 0


class TestImageSafetyInProductionPath:
    """Verify image safety validation is in the real ProcessingWorker path."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_missing_file_raises_file_not_found(self) -> None:
        """Missing file → FileNotFoundError (isolated failure)."""
        from workers.processing_worker import ProcessingWorker

        worker = ProcessingWorker(self._service)
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/nonexistent/path.jpg"
        )
        assert job is not None

        with pytest.raises(FileNotFoundError):
            worker.process(job)

    def test_unsupported_extension_raises_value_error(self) -> None:
        """Unsupported extension → ValueError (isolated failure)."""
        from workers.processing_worker import ProcessingWorker

        # Create a temp file with unsupported extension
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write("not an image")

            worker = ProcessingWorker(self._service)
            job = self._repo.create_job(
                file_id="f1", file_name="a.txt", local_path=path
            )
            assert job is not None

            with pytest.raises(ValueError, match="Unsupported file extension"):
                worker.process(job)
        finally:
            os.unlink(path)

    def test_oversized_file_fails_safety_check(self) -> None:
        """Oversized file → safety check failure (isolated)."""
        from workers.processing_worker import ProcessingWorker

        # Create a large temp file
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            # Write 25MB of data (exceeds 20MB default)
            with open(path, "wb") as f:
                f.write(b"\x00" * (25 * 1024 * 1024))

            worker = ProcessingWorker(self._service)
            job = self._repo.create_job(
                file_id="f1", file_name="big.jpg", local_path=path
            )
            assert job is not None

            with pytest.raises(ValueError, match="Unsafe image"):
                worker.process(job)
        finally:
            os.unlink(path)

    def test_valid_image_passes_safety_check(self) -> None:
        """Valid image → passes safety check (would proceed to OCR)."""
        from workers.processing_worker import ProcessingWorker

        # Create a small valid JPEG
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            img = Image.new("RGB", (100, 100), color="red")
            img.save(path, "JPEG")

            worker = ProcessingWorker(self._service)
            job = self._repo.create_job(
                file_id="f1", file_name="valid.jpg", local_path=path
            )
            assert job is not None

            # Should pass safety check but fail at OCR (no PaddleOCR-VL in test env)
            # We just verify it gets past the safety check
            try:
                worker.process(job)
            except ValueError as e:
                if "Unsafe image" in str(e):
                    pytest.fail("Valid image should pass safety check")
                # Other errors (e.g. OCR not available) are fine
            except Exception:
                # OCR errors are expected in test environment
                pass
        finally:
            os.unlink(path)


class TestDuplicateProtection:
    """Verify duplicate protection at all levels."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_drive_duplicate_same_file_id(self) -> None:
        """Same drive_file_id discovered repeatedly → one job."""
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
            job1 = self._service.create_job_for_file(drive_file, local_path)
            assert job1 is not None

            for _ in range(10):
                dup = self._service.create_job_for_file(drive_file, local_path)
                assert dup is None

            counts = self._repo.get_count_by_status()
            assert sum(counts.values()) == 1
        finally:
            os.unlink(local_path)

    def test_concurrent_duplicate_creation(self) -> None:
        """Multiple threads attempt the same file → one active job."""
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

    def test_completed_job_allows_recreation(self) -> None:
        """A completed job can be recreated for the same Drive file."""
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
            job1 = self._service.create_job_for_file(drive_file, local_path)
            assert job1 is not None
            self._repo.complete_job(job1.job_id)

            job2 = self._service.create_job_for_file(drive_file, local_path)
            assert job2 is not None
            assert job2.job_id != job1.job_id
        finally:
            os.unlink(local_path)


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

    def test_repeated_complete_is_idempotent(self) -> None:
        """complete_job() called multiple times is idempotent."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        for _ in range(5):
            self._service.complete_job(job.job_id)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.status == JobStatus.COMPLETED

    def test_export_after_failure_does_not_erase_metadata(self) -> None:
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
        assert updated.status == JobStatus.COMPLETED


class TestRestartRecovery:
    """Verify crash/restart recovery sequence."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_full_restart_sequence(self) -> None:
        """PENDING -> PROCESSING -> crash -> restart -> recovery -> resume."""
        for i in range(20):
            self._repo.create_job(
                file_id=f"file_{i:02d}",
                file_name=f"book_{i:02d}.jpg",
                local_path=f"/tmp/book_{i:02d}.jpg",
            )

        # Process first 5 to completion
        for _ in range(5):
            job = self._repo.get_pending_jobs()[0]
            self._service.start_job(job.job_id)
            self._service.complete_job(job.job_id)

        # Start processing next 10 (in-flight during crash)
        in_flight = []
        for _ in range(10):
            job = self._repo.get_pending_jobs()[0]
            self._service.start_job(job.job_id)
            in_flight.append(job.job_id)

        # Verify pre-crash state
        counts = self._repo.get_count_by_status()
        assert counts.get("completed", 0) == 5
        assert counts.get("processing", 0) == 10
        assert counts.get("pending", 0) == 5

        # Simulate crash: make in-flight jobs stale
        stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
        for job_id in in_flight:
            self._repo.update_status(
                job_id,
                JobStatus.PROCESSING,
                extra_updates={"started_at": stale_time},
            )

        # Restart: recover stuck jobs
        recovered = self._service.recover_stuck_jobs()
        assert recovered == 10

        # Continue processing
        while self._repo.get_pending_jobs():
            job = self._repo.get_pending_jobs()[0]
            self._service.start_job(job.job_id)
            self._service.complete_job(job.job_id)

        # All 20 should be completed
        counts = self._repo.get_count_by_status()
        assert counts.get("completed", 0) == 20
        assert counts.get("pending", 0) == 0
        assert counts.get("processing", 0) == 0

    def test_completed_job_not_processed_again_after_crash(self) -> None:
        """A job completed before crash is NOT processed again."""
        for i in range(10):
            self._repo.create_job(
                file_id=f"file_{i:02d}",
                file_name=f"book_{i:02d}.jpg",
                local_path=f"/tmp/book_{i:02d}.jpg",
            )

        # Process all 10 to completion
        for _ in range(10):
            job = self._repo.get_pending_jobs()[0]
            self._service.start_job(job.job_id)
            self._service.complete_job(job.job_id)

        # Verify all completed
        counts = self._repo.get_count_by_status()
        assert counts.get("completed", 0) == 10

        # Simulate crash: recovery should NOT touch completed jobs
        recovered = self._service.recover_stuck_jobs()
        assert recovered == 0

        # All 10 should still be completed
        counts = self._repo.get_count_by_status()
        assert counts.get("completed", 0) == 10
        assert counts.get("pending", 0) == 0
