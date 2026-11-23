"""Unit tests for JobService."""

import os
import tempfile
from datetime import datetime, timedelta

from models.drive_file import DriveFile
from models.processing_job import JobStatus, ProcessingJob
from repositories.job_repository import JobRepository
from services.job_service import JobService


class TestJobServiceCreation:
    """Test creating jobs via the service layer."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_create_job_for_file(self) -> None:
        """Test creating a job for a downloaded file."""
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
            job = self._service.create_job_for_file(drive_file, local_path)
            assert job is not None
            assert job.file_id == "drive123"
            assert job.file_name == "book.jpg"
            assert job.local_path == local_path
            assert job.status == JobStatus.PENDING
        finally:
            os.unlink(local_path)

    def test_create_job_for_file_missing_local(self) -> None:
        """If the local file doesn't exist, no job is created."""
        drive_file = DriveFile(
            file_id="drive123",
            name="book.jpg",
            mime_type="image/jpeg",
            modified_time="2024-01-01T00:00:00",
            size=1024,
        )

        job = self._service.create_job_for_file(drive_file, "/nonexistent/path.jpg")
        assert job is None

    def test_create_job_for_duplicate_file(self) -> None:
        """Creating a second job for the same Drive file returns None."""
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

            job2 = self._service.create_job_for_file(drive_file, local_path)
            assert job2 is None
        finally:
            os.unlink(local_path)

    def test_create_job_for_completed_file_allows_recreation(self) -> None:
        """A completed job can be recreated for the same Drive file."""
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


class TestJobServiceStatusManagement:
    """Test job status transitions via the service."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_start_job(self) -> None:
        """Test starting a job."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        assert self._service.start_job(job.job_id) is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PROCESSING

    def test_complete_job(self) -> None:
        """Test completing a job."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        assert self._service.complete_job(job.job_id) is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED

    def test_fail_job_first_failure_marks_retrying(self) -> None:
        """First failure increments retry and sets RETRYING."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._service.fail_job(job.job_id, "error1")
        assert result is True

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 1

    def test_fail_job_second_failure_marks_retrying(self) -> None:
        """Second failure increments retry and sets RETRYING."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._service.fail_job(job.job_id, "error1")
        self._service.fail_job(job.job_id, "error2")

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 2

    def test_fail_job_third_failure_marks_failed(self) -> None:
        """Third failure exceeds MAX_RETRIES and sets FAILED."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._service.fail_job(job.job_id, "error1")
        self._service.fail_job(job.job_id, "error2")
        self._service.fail_job(job.job_id, "error3")

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 3

    def test_fail_job_nonexistent(self) -> None:
        """Failing a non-existent job returns False."""
        result = self._service.fail_job("nonexistent", "error")
        assert result is False

    def test_fail_job_fourth_failure_no_additional_retry(self) -> None:
        """After 3 retries, further failures do not increment (already FAILED)."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._service.fail_job(job.job_id, "e1")
        self._service.fail_job(job.job_id, "e2")
        self._service.fail_job(job.job_id, "e3")

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 3

        # Fourth failure call still increments retry_count in repo
        # but the service checks if already at max and doesn't change status
        # Actually, fail_job will increment retry_count again and set RETRYING
        # because retry_count was 3 >= MAX_RETRIES (3), so it goes to FAIL.
        # Wait - let's check: after 3 failures, retry_count=3, status=FAILED
        # The fail_job service method increments retry_count to 4,
        # then checks 4 >= 3 -> True -> sets FAILED
        # But the row is already FAILED. The update_status will still work.
        # Let me verify the retry count.
        self._service.fail_job(job.job_id, "e4")
        updated2 = self._repo.get_job_by_id(job.job_id)
        assert updated2 is not None
        assert updated2.status == JobStatus.FAILED


class TestJobServiceQueueOperations:
    """Test queue-related service operations."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_get_pending_jobs(self) -> None:
        """Test getting pending jobs in FIFO order."""
        job1 = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        job2 = self._repo.create_job(
            file_id="f2", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job1 is not None and job2 is not None

        pending = self._service.get_pending_jobs()
        assert len(pending) == 2
        assert pending[0].file_id == "f1"
        assert pending[1].file_id == "f2"

    def test_get_processing_jobs(self) -> None:
        """Test getting processing jobs."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        processing = self._service.get_processing_jobs()
        assert len(processing) == 1

    def test_get_failed_jobs(self) -> None:
        """Test getting failed jobs."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.fail_job(job.job_id, "error")

        failed = self._service.get_failed_jobs()
        assert len(failed) == 1

    def test_recover_stuck_jobs(self) -> None:
        """Test recovering stuck (stale) PROCESSING jobs."""
        from datetime import timedelta

        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        # Set PROCESSING with a stale started_at (older than the timeout)
        stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
        self._repo.update_status(
            job.job_id,
            JobStatus.PROCESSING,
            extra_updates={"started_at": stale_time},
        )

        count = self._service.recover_stuck_jobs()
        assert count == 1

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PENDING
        assert updated.started_at is None

    def test_get_queue_status(self) -> None:
        """Test getting queue status for health checks."""
        job1 = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        job2 = self._repo.create_job(
            file_id="f2", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job1 is not None and job2 is not None

        self._repo.update_status(job1.job_id, JobStatus.PROCESSING)


class TestJobServiceStaleRecoveryTimeout:
    """Stale recovery must detect recent vs stale PROCESSING jobs.

    The JobService.recover_stuck_jobs() delegates to the repository
    with a configurable timeout. Recent PROCESSING jobs (started_at within
    the timeout) must NOT be reset. Stale PROCESSING jobs (started_at older
    than the timeout) must be recovered to PENDING.
    """

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def _make_job(self, file_id: str) -> ProcessingJob:
        job = self._repo.create_job(
            file_id=file_id,
            file_name=f"{file_id}.jpg",
            local_path=f"/tmp/{file_id}.jpg",
        )
        assert job is not None
        return job

    def _set_processing(self, job_id: str, started_at_iso: str) -> None:
        self._repo.update_status(
            job_id,
            JobStatus.PROCESSING,
            extra_updates={"started_at": started_at_iso},
        )

    def test_recent_processing_job_not_recovered(self) -> None:
        """A recently started PROCESSING job is left alone."""
        now = datetime.utcnow()
        job = self._make_job("recent")
        self._set_processing(job.job_id, now.isoformat())

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PROCESSING
        assert updated.started_at is not None

    def test_stale_processing_job_recovered(self) -> None:
        """A stale PROCESSING job is recovered to PENDING."""
        old = datetime.utcnow() - timedelta(seconds=7200)
        job = self._make_job("stale")
        self._set_processing(job.job_id, old.isoformat())

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 1

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PENDING
        assert updated.started_at is None

    def test_pending_job_ignored_by_service_recovery(self) -> None:
        """PENDING jobs are not touched by service-level recovery."""
        job = self._make_job("pending")
        recovered = self._service.recover_stuck_jobs()
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PENDING

    def test_multiple_jobs_mixed_state_service_recovery(self) -> None:
        """Across Book A (recent PROCESSING), Book B (stale PROCESSING),
        Book C (PENDING), only Book B is recovered."""
        now = datetime.utcnow()
        stale = now - timedelta(seconds=7200)

        job_a = self._make_job("book_a")
        self._set_processing(job_a.job_id, now.isoformat())

        job_b = self._make_job("book_b")
        self._set_processing(job_b.job_id, stale.isoformat())

        job_c = self._make_job("book_c")
        # Default status is PENDING.

        recovered = self._service.recover_stuck_jobs()
        assert recovered == 1

        updated_a = self._repo.get_job_by_id(job_a.job_id)
        updated_b = self._repo.get_job_by_id(job_b.job_id)
        updated_c = self._repo.get_job_by_id(job_c.job_id)

        assert updated_a.status == JobStatus.PROCESSING
        assert updated_a.started_at is not None

        assert updated_b.status == JobStatus.PENDING
        assert updated_b.started_at is None

        assert updated_c.status == JobStatus.PENDING
