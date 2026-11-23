"""Unit tests for JobRepository."""

import os
import tempfile
import threading
from datetime import datetime, timedelta
from typing import List

from models.processing_job import ErrorCategory, JobStatus, ProcessingJob
from repositories.job_repository import JobRepository


class TestJobRepositoryCreation:
    """Test creating jobs."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_create_job(self) -> None:
        """Test creating a job."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        assert job.file_id == "f1"
        assert job.file_name == "a.jpg"
        assert job.local_path == "/tmp/a.jpg"
        assert job.status == JobStatus.PENDING
        assert job.priority == 0
        assert job.retry_count == 0

    def test_create_job_with_priority(self) -> None:
        """Test creating a job with custom priority."""
        job = self._repo.create_job(
            file_id="f1",
            file_name="a.jpg",
            local_path="/tmp/a.jpg",
            priority=5,
        )
        assert job is not None
        assert job.priority == 5

    def test_create_job_with_worker_name(self) -> None:
        """Test creating a job with worker name."""
        job = self._repo.create_job(
            file_id="f1",
            file_name="a.jpg",
            local_path="/tmp/a.jpg",
            worker_name="worker1",
        )
        assert job is not None
        assert job.worker_name == "worker1"

    def test_create_duplicate_job_same_file_id(self) -> None:
        """Creating a job with the same file_id fails (unique constraint)."""
        job1 = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job1 is not None
        job2 = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job2 is None

    def test_create_jobs_different_file_ids(self) -> None:
        """Creating jobs with different file_ids succeeds."""
        job1 = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        job2 = self._repo.create_job(
            file_id="f2", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job1 is not None
        assert job2 is not None
        assert job1.job_id != job2.job_id


class TestJobRepositoryStatus:
    """Test job status transitions."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_update_status_to_processing(self) -> None:
        """Test updating job status to PROCESSING."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._repo.update_status(job.job_id, JobStatus.PROCESSING)
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PROCESSING

    def test_update_status_to_completed(self) -> None:
        """Test updating job status to COMPLETED."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._repo.update_status(job.job_id, JobStatus.COMPLETED)
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED

    def test_update_status_to_failed(self) -> None:
        """Test updating job status to FAILED."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._repo.update_status(job.job_id, JobStatus.FAILED, "error")
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.error_message == "error"

    def test_update_status_nonexistent_job(self) -> None:
        """Updating non-existent job returns False."""
        result = self._repo.update_status("nonexistent", JobStatus.PROCESSING)
        assert result is False

    def test_update_status_with_started_at(self) -> None:
        """Test setting started_at when transitioning to PROCESSING."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        started_at = datetime.utcnow()
        result = self._repo.update_status(
            job.job_id,
            JobStatus.PROCESSING,
            started_at=started_at.isoformat(),
        )
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PROCESSING
        assert updated.started_at is not None

    def test_update_status_with_worker_name(self) -> None:
        """Test setting worker_name in status update."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._repo.update_status(
            job.job_id,
            JobStatus.PROCESSING,
            worker_name="worker1",
        )
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.worker_name == "worker1"

    def test_update_status_with_error_message(self) -> None:
        """Test setting error_message in status update."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._repo.update_status(
            job.job_id,
            JobStatus.FAILED,
            error_message="Something went wrong",
        )
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.error_message == "Something went wrong"

    def test_update_status_with_error_category(self) -> None:
        """Test setting error_category in status update."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._repo.update_status(
            job.job_id,
            JobStatus.FAILED,
            error_message="error",
            extra_updates={"error_category": ErrorCategory.PERMANENT.value},
        )
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.error_category == ErrorCategory.PERMANENT

    def test_update_status_with_next_retry_at(self) -> None:
        """Test setting next_retry_at in status update."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        next_retry = datetime.utcnow().isoformat()
        result = self._repo.update_status(
            job.job_id,
            JobStatus.RETRYING,
            extra_updates={"next_retry_at": next_retry},
        )
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.next_retry_at is not None

    def test_update_status_with_last_error_at(self) -> None:
        """Test setting last_error_at in status update."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        result = self._repo.update_status(
            job.job_id,
            JobStatus.FAILED,
            extra_updates={"last_error_at": datetime.utcnow().isoformat()},
        )
        assert result is True
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.last_error_at is not None

    def test_complete_job(self) -> None:
        """Test complete_job sets status to COMPLETED."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        assert self._repo.complete_job(job.job_id) is True

    def test_fail_job(self) -> None:
        """Test fail_job sets status to FAILED."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        assert self._repo.fail_job(job.job_id, "error") is True


class TestJobRepositoryRetrieval:
    """Test job retrieval methods."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_get_job_by_id(self) -> None:
        """Test retrieving a job by its UUID."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        retrieved = self._repo.get_job_by_id(job.job_id)
        assert retrieved is not None
        assert retrieved.job_id == job.job_id
        assert retrieved.file_id == "f1"

    def test_get_job_by_id_nonexistent(self) -> None:
        """Retrieving a non-existent job returns None."""
        result = self._repo.get_job_by_id("nonexistent")
        assert result is None

    def test_get_job_by_drive_file_id(self) -> None:
        """Test retrieving a job by Drive file ID."""
        job = self._repo.create_job(
            file_id="drive123",
            file_name="a.jpg",
            local_path="/tmp/a.jpg",
        )
        assert job is not None
        retrieved = self._repo.get_job_by_drive_file_id("drive123")
        assert retrieved is not None
        assert retrieved.file_id == "drive123"

    def test_get_job_by_drive_file_id_nonexistent(self) -> None:
        """Retrieving a job for a non-existent file returns None."""
        result = self._repo.get_job_by_drive_file_id("nonexistent")
        assert result is None

    def test_get_pending_jobs(self) -> None:
        """Test retrieving pending jobs."""
        job1 = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        job2 = self._repo.create_job(
            file_id="f2", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job1 is not None and job2 is not None
        pending = self._repo.get_pending_jobs()
        assert len(pending) == 2

    def test_get_processing_jobs(self) -> None:
        """Test retrieving processing jobs."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)
        processing = self._repo.get_processing_jobs()
        assert len(processing) == 1
        assert processing[0].status == JobStatus.PROCESSING

    def test_get_failed_jobs(self) -> None:
        """Test retrieving failed jobs."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.fail_job(job.job_id, "error")
        failed = self._repo.get_failed_jobs()
        assert len(failed) == 1


class TestJobRepositoryConcurrency:
    """Test concurrent job creation."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_concurrent_create_same_file(self) -> None:
        """Two threads creating a job for the same file -- only one succeeds."""
        results: List[ProcessingJob] = []
        lock = threading.Lock()

        def create() -> None:
            job = self._repo.create_job(
                file_id="concurrent1",
                file_name="a.jpg",
                local_path="/tmp/a.jpg",
            )
            with lock:
                results.append(job)

        t1 = threading.Thread(target=create)
        t2 = threading.Thread(target=create)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1  # Only one should succeed


class TestJobRepositoryRecovery:
    """Test restart recovery operations."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_recover_stuck_jobs(self) -> None:
        """PROCESSING jobs with stale started_at are recovered to PENDING."""
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
        count = self._repo.recover_stuck_jobs()
        assert count == 1

        recovered = self._repo.get_job_by_id(job.job_id)
        assert recovered is not None
        assert recovered.status == JobStatus.PENDING
        assert recovered.started_at is None

    def test_recover_stuck_jobs_no_processing(self) -> None:
        """No PROCESSING jobs -- recovery returns 0."""
        count = self._repo.recover_stuck_jobs()
        assert count == 0

    def test_reset_retrying_jobs(self) -> None:
        """RETRYING jobs are reset to PENDING."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.RETRYING)
        count = self._repo.reset_retrying_jobs()
        assert count == 1

        reset = self._repo.get_job_by_id(job.job_id)
        assert reset is not None
        assert reset.status == JobStatus.PENDING


class TestJobRepositoryCount:
    """Test the count_by_status method."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_get_count_by_status(self) -> None:
        """Test getting counts grouped by status."""
        job1 = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        job2 = self._repo.create_job(
            file_id="f2", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        job3 = self._repo.create_job(
            file_id="f3", file_name="c.jpg", local_path="/tmp/c.jpg"
        )
        assert job1 is not None and job2 is not None and job3 is not None

        self._repo.update_status(job1.job_id, JobStatus.PROCESSING)
        self._repo.complete_job(job2.job_id)

        counts = self._repo.get_count_by_status()
        assert counts["processing"] == 1
        assert counts["completed"] == 1
        assert counts["pending"] == 1


class TestJobRepositoryManualReview:
    """Milestone 7: Tests for manual review metadata persistence."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_set_manual_review_stores_metadata(self) -> None:
        """Test that set_manual_review stores metadata JSON."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        metadata = {"key": "value", "number": 42}
        result = self._repo.set_manual_review(job.job_id, "needs review", metadata)
        assert result is True

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.MANUAL_REVIEW
        assert updated.error_message == "needs review"

        stored = self._repo.get_manual_review_metadata(job.job_id)
        assert stored is not None
        assert stored["key"] == "value"
        assert stored["number"] == 42

    def test_set_manual_review_without_metadata(self) -> None:
        """Test set_manual_review without metadata."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        result = self._repo.set_manual_review(job.job_id, "no metadata")
        assert result is True

        stored = self._repo.get_manual_review_metadata(job.job_id)
        assert stored is None  # No metadata stored

    def test_get_manual_review_metadata_nonexistent(self) -> None:
        """Test getting metadata for non-existent job."""
        result = self._repo.get_manual_review_metadata("nonexistent")
        assert result is None

    def test_get_manual_review_metadata_no_metadata(self) -> None:
        """Test getting metadata for job without metadata."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        # Set manual review without metadata
        self._repo.set_manual_review(job.job_id)
        result = self._repo.get_manual_review_metadata(job.job_id)
        assert result is None


class TestJobRepositoryRetry:
    """Test retry-related operations."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_increment_retry(self) -> None:
        """Test incrementing retry count."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None
        assert job.retry_count == 0

        new_count = self._repo.increment_retry(job.job_id)
        assert new_count == 1

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.retry_count == 1

    def test_increment_retry_multiple_times(self) -> None:
        """Test incrementing retry count multiple times."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        self._repo.increment_retry(job.job_id)
        self._repo.increment_retry(job.job_id)
        self._repo.increment_retry(job.job_id)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated.retry_count == 3

    def test_increment_retry_nonexistent_job(self) -> None:
        """Test incrementing retry for non-existent job."""
        result = self._repo.increment_retry("nonexistent")
        assert result == -1

    def test_get_retryable_jobs(self) -> None:
        """Test getting retryable jobs."""
        # Create a job and set it to RETRYING with a past next_retry_at
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        past_time = (datetime.utcnow() - timedelta(seconds=3600)).isoformat()
        self._repo.update_status(
            job.job_id,
            JobStatus.RETRYING,
            error_message="error",
            extra_updates={
                "next_retry_at": past_time,
                "error_category": ErrorCategory.TRANSIENT.value,
                "last_error_at": datetime.utcnow().isoformat(),
            },
        )

        retryable = self._repo.get_retryable_jobs()
        assert len(retryable) == 1
        assert retryable[0].job_id == job.job_id

    def test_get_retryable_jobs_not_yet_ready(self) -> None:
        """Test that jobs with future next_retry_at are not returned."""
        job = self._repo.create_job(
            file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job is not None

        future_time = (datetime.utcnow() + timedelta(seconds=3600)).isoformat()
        self._repo.update_status(
            job.job_id,
            JobStatus.RETRYING,
            extra_updates={
                "next_retry_at": future_time,
                "error_category": ErrorCategory.TRANSIENT.value,
                "last_error_at": datetime.utcnow().isoformat(),
            },
        )

        retryable = self._repo.get_retryable_jobs()
        assert len(retryable) == 0

    def test_get_retryable_jobs_empty(self) -> None:
        """Test getting retryable jobs when none exist."""
        retryable = self._repo.get_retryable_jobs()
        assert len(retryable) == 0


class TestJobRepositoryStaleRecoveryTimeout:
    """Stale recovery must respect a configurable timeout.

    Recent PROCESSING jobs (started_at within the timeout) must NOT be
    reset. Stale PROCESSING jobs (started_at older than the timeout)
    must be recovered to PENDING with started_at cleared.
    """

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

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

        recovered = self._repo.recover_stuck_jobs(timeout_seconds=3600)
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

        recovered = self._repo.recover_stuck_jobs(timeout_seconds=3600)
        assert recovered == 1

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PENDING
        assert updated.started_at is None

    def test_pending_job_ignored_by_recovery(self) -> None:
        """PENDING jobs are not touched by recovery."""
        job = self._make_job("pending")
        recovered = self._repo.recover_stuck_jobs(timeout_seconds=3600)
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PENDING

    def test_multiple_jobs_mixed_state_recovery(self) -> None:
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

        recovered = self._repo.recover_stuck_jobs(timeout_seconds=3600)
        assert recovered == 1

        updated_a = self._repo.get_job_by_id(job_a.job_id)
        updated_b = self._repo.get_job_by_id(job_b.job_id)
        updated_c = self._repo.get_job_by_id(job_c.job_id)

        assert updated_a.status == JobStatus.PROCESSING
        assert updated_a.started_at is not None

        assert updated_b.status == JobStatus.PENDING
        assert updated_b.started_at is None

        assert updated_c.status == JobStatus.PENDING


class TestRequeueFailedJobs:
    """Requeueing must revive failed work only, never finished or skipped work."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def _job(self, file_id: str, status: JobStatus) -> str:
        job = self._repo.create_job(
            file_id=file_id,
            file_name=f"{file_id}.jpg",
            local_path=f"/tmp/{file_id}.jpg",
        )
        assert job is not None
        if status != JobStatus.PENDING:
            self._repo.update_status(job.job_id, status, error_message="boom")
        return job.job_id

    def test_requeues_only_failed_jobs_with_a_fresh_budget(self) -> None:
        failed = self._job("f1", JobStatus.FAILED)
        completed = self._job("f2", JobStatus.COMPLETED)
        cancelled = self._job("f3", JobStatus.CANCELLED)
        review = self._job("f4", JobStatus.MANUAL_REVIEW)
        pending = self._job("f5", JobStatus.PENDING)
        for _ in range(3):
            self._repo.increment_retry(failed)

        requeued = self._repo.requeue_failed_jobs()

        assert requeued == [failed]
        revived = self._repo.get_job_by_id(failed)
        assert revived is not None
        assert revived.status == JobStatus.PENDING
        assert revived.retry_count == 0
        assert revived.error_message is None
        assert revived.started_at is None
        assert revived.completed_at is None
        assert self._repo.get_job_by_id(completed).status == JobStatus.COMPLETED
        assert self._repo.get_job_by_id(cancelled).status == JobStatus.CANCELLED
        assert self._repo.get_job_by_id(review).status == JobStatus.MANUAL_REVIEW
        assert self._repo.get_job_by_id(pending).status == JobStatus.PENDING

    def test_filter_by_drive_file_id(self) -> None:
        first = self._job("f1", JobStatus.FAILED)
        second = self._job("f2", JobStatus.FAILED)

        requeued = self._repo.requeue_failed_jobs(drive_file_id="f2")

        assert requeued == [second]
        assert self._repo.get_job_by_id(first).status == JobStatus.FAILED
        assert self._repo.get_job_by_id(second).status == JobStatus.PENDING

    def test_no_failed_jobs_is_a_no_op(self) -> None:
        self._job("f1", JobStatus.COMPLETED)

        assert self._repo.requeue_failed_jobs() == []
