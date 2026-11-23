"""Unit tests for Milestone 7: Production Batch Workflow & Reliability.

Tests cover:
- FIFO ordering in the pending job queue
- Thread-safe job creation and status updates
- Duplicate prevention at the job level
- Retry logic with RETRYING state (via JobService)
- Restart recovery (PROCESSING -> PENDING)
- Failure isolation per-job
- Manual review metadata persistence (metadata_json column)
- Processing state transitions (PENDING->PROCESSING->COMPLETED,
  MANUAL_REVIEW, FAILED, RETRYING)
- Network efficiency (no redundant lookups)
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta

from models.processing_job import JobStatus, ProcessingJob
from repositories.job_repository import JobRepository
from services.job_service import JobService


class TestFIFOOrdering:
    """Milestone 7: Jobs are processed in FIFO order (oldest first)."""

    def setup_method(self, method) -> None:  # noqa: ARG002
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)

    def teardown_method(self) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_pending_jobs_returned_in_fifo_order(self) -> None:
        """Jobs are ordered by created_at ASC within the same priority."""
        self._repo.create_job(file_id="f1", file_name="a.jpg", local_path="/tmp/a.jpg")
        time.sleep(0.02)
        self._repo.create_job(file_id="f2", file_name="b.jpg", local_path="/tmp/b.jpg")
        time.sleep(0.02)
        self._repo.create_job(file_id="f3", file_name="c.jpg", local_path="/tmp/c.jpg")

        pending = self._repo.get_pending_jobs()
        assert len(pending) == 3
        assert pending[0].file_id == "f1"
        assert pending[1].file_id == "f2"
        assert pending[2].file_id == "f3"

    def test_priority_orders_before_fifo(self) -> None:
        """Higher priority jobs come first; FIFO within same priority."""
        self._repo.create_job(
            file_id="low1",
            file_name="l1.jpg",
            local_path="/tmp/l1.jpg",
            priority=0,
        )
        time.sleep(0.02)
        self._repo.create_job(
            file_id="high1",
            file_name="h1.jpg",
            local_path="/tmp/h1.jpg",
            priority=5,
        )
        time.sleep(0.02)
        self._repo.create_job(
            file_id="low2",
            file_name="l2.jpg",
            local_path="/tmp/l2.jpg",
            priority=0,
        )

        pending = self._repo.get_pending_jobs()
        assert pending[0].file_id == "high1"
        assert pending[1].file_id == "low1"
        assert pending[2].file_id == "low2"

    def test_worker_processes_oldest_first(self) -> None:
        """The worker takes pending_jobs[0] (oldest = FIFO)."""
        self._repo.create_job(
            file_id="old", file_name="old.jpg", local_path="/tmp/old.jpg"
        )
        self._repo.create_job(
            file_id="new", file_name="new.jpg", local_path="/tmp/new.jpg"
        )

        pending = self._repo.get_pending_jobs()
        assert pending[0].file_id == "old"
        assert pending[-1].file_id == "new"


class TestThreadSafety:
    """Milestone 7: Thread-safe job operations under concurrent access."""

    def setup_method(self) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_concurrent_job_creation_no_crashes(self) -> None:
        """Creating jobs from multiple threads should not crash or corrupt data."""
        results: list = []
        errors: list = []

        def create_file(file_id: int) -> None:
            try:
                job = self._repo.create_job(
                    file_id=f"file_{file_id}",
                    file_name=f"book_{file_id}.jpg",
                    local_path=f"/tmp/book_{file_id}.jpg",
                )
                results.append(job)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=create_file, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        created = [r for r in results if r is not None]
        assert len(created) == 20
        # All job_ids should be unique
        job_ids = [j.job_id for j in created]
        assert len(set(job_ids)) == 20

    def test_concurrent_status_updates(self) -> None:
        """Concurrent status updates on different jobs should not conflict."""
        jobs = []
        for i in range(10):
            job = self._repo.create_job(
                file_id=f"f{i}",
                file_name=f"b{i}.jpg",
                local_path=f"/tmp/b{i}.jpg",
            )
            assert job is not None
            jobs.append(job)

        errors: list = []

        def update_job(job: ProcessingJob) -> None:
            try:
                self._repo.update_status(job.job_id, JobStatus.PROCESSING)
                self._repo.update_status(job.job_id, JobStatus.COMPLETED)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=update_job, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        for job in jobs:
            updated = self._repo.get_job_by_id(job.job_id)
            assert updated is not None
            assert updated.status == JobStatus.COMPLETED


class TestDuplicatePrevention:
    """Milestone 7: Duplicate prevention at the job level."""

    def setup_method(self) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)
        self._temp_dir = tempfile.mkdtemp()

    def teardown_method(self) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _make_drive_file(self, file_id: str, name: str):
        from models.drive_file import DriveFile

        return DriveFile(
            file_id=file_id,
            name=name,
            mime_type="image/jpeg",
            modified_time="2024-01-01T00:00:00",
            size=12345,
        )

    def _make_local_file(self, name: str) -> str:
        path = os.path.join(self._temp_dir, name)
        with open(path, "w") as f:
            f.write("dummy")
        return path

    def test_create_job_rejects_duplicate_file_id(self) -> None:
        """Two jobs for the same file_id cannot both be PENDING."""
        job1 = self._repo.create_job(
            file_id="dup_file", file_name="book.jpg", local_path="/tmp/book.jpg"
        )
        assert job1 is not None
        # Second creation for the same file_id should return None (IntegrityError)
        job2 = self._repo.create_job(
            file_id="dup_file", file_name="book2.jpg", local_path="/tmp/book2.jpg"
        )
        assert job2 is None

    def test_job_service_creates_job_for_new_file(self) -> None:
        """JobService.create_job_for_file works for new files."""
        local_path = self._make_local_file("book.jpg")
        drive_file = self._make_drive_file("new_file", "book.jpg")
        job = self._service.create_job_for_file(drive_file, local_path)
        assert job is not None
        assert job.file_id == "new_file"

    def test_job_service_prevents_duplicate_active_job(self) -> None:
        """JobService.create_job_for_file returns None if an active job exists."""
        local_path = self._make_local_file("book.jpg")
        drive_file = self._make_drive_file("dup_file", "book.jpg")
        # Create a PENDING job first
        job1 = self._service.create_job_for_file(drive_file, local_path)
        assert job1 is not None
        # Trying to create another job for the same file while PENDING
        job2 = self._service.create_job_for_file(drive_file, local_path)
        assert job2 is None

    def test_job_service_allows_new_job_after_completion(self) -> None:
        """After a job completes, a new job for the same file can be created."""
        local_path = self._make_local_file("book.jpg")
        drive_file = self._make_drive_file("file_x", "book.jpg")
        job1 = self._service.create_job_for_file(drive_file, local_path)
        assert job1 is not None

        # Complete the job
        self._repo.complete_job(job1.job_id)

        # Now a new job should be allowed
        job2 = self._service.create_job_for_file(drive_file, local_path)
        assert job2 is not None
        assert job2.job_id != job1.job_id

    def test_job_service_prevents_duplicate_for_manual_review(self) -> None:
        """A MANUAL_REVIEW job blocks new job creation for the same file."""
        local_path = self._make_local_file("book.jpg")
        drive_file = self._make_drive_file("file_mr", "book.jpg")
        job1 = self._service.create_job_for_file(drive_file, local_path)
        assert job1 is not None

        # Move the job to MANUAL_REVIEW
        self._repo.set_manual_review(job1.job_id, metadata={"candidates": []})

        # A new job for the same file should be blocked
        job2 = self._service.create_job_for_file(drive_file, local_path)
        assert job2 is None


class TestProcessingStates:
    """Milestone 7: Verify all processing states and transitions."""

    def setup_method(self) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_state_transition_pending_to_processing(self) -> None:
        """PENDING -> PROCESSING sets started_at."""
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.started_at is None

        result = self._repo.update_status(job.job_id, JobStatus.PROCESSING)
        assert result is True

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PROCESSING
        assert updated.started_at is not None

    def test_state_transition_processing_to_completed(self) -> None:
        """PROCESSING -> COMPLETED sets completed_at."""
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        self._repo.complete_job(job.job_id)
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED
        assert updated.completed_at is not None

    def test_state_transition_processing_to_failed(self) -> None:
        """Repo-level fail_job marks FAILED directly."""
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        self._repo.fail_job(job.job_id, "OCR error")
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.error_message == "OCR error"
        assert updated.completed_at is not None

    def test_state_transition_processing_to_retrying_via_service(self) -> None:
        """JobService.fail_job transitions to RETRYING on first failure."""
        service = JobService(self._repo)
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        service.fail_job(job.job_id, "Temporary error")
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 1

    def test_state_transition_to_manual_review(self) -> None:
        """PROCESSING -> MANUAL_REVIEW stores metadata_json and sets completed_at."""
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        metadata = {
            "candidates": [
                {"title": "Test Book", "confidence": 0.5, "source": "open_library"}
            ],
            "ocr_text": "extracted text",
            "confidence": 0.5,
            "rejection_reason": "Below threshold",
        }
        result = self._repo.set_manual_review(
            job.job_id,
            error_message="Low confidence identification",
            metadata=metadata,
        )
        assert result is True

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.MANUAL_REVIEW
        assert updated.completed_at is not None

        retrieved = self._repo.get_manual_review_metadata(job.job_id)
        assert retrieved is not None
        assert retrieved["candidates"][0]["title"] == "Test Book"
        assert retrieved["ocr_text"] == "extracted text"
        assert retrieved["confidence"] == 0.5
        assert retrieved["rejection_reason"] == "Below threshold"

    def test_retrying_reset_to_pending(self) -> None:
        """reset_retrying_jobs moves RETRYING back to PENDING."""
        service = JobService(self._repo)
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)
        service.fail_job(job.job_id, "Error")
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING

        count = self._repo.reset_retrying_jobs()
        assert count == 1

        pending = self._repo.get_pending_jobs()
        assert len(pending) == 1
        assert pending[0].status == JobStatus.PENDING


class TestRestartRecovery:
    """Milestone 7: Restart recovery - stuck jobs reset to PENDING."""

    def setup_method(self) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)

    def teardown_method(self) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_recover_stuck_processing_jobs(self) -> None:
        """Jobs stuck in PROCESSING after a crash are reset to PENDING."""
        job1 = self._repo.create_job(
            file_id="f1", file_name="b1.jpg", local_path="/tmp/b1.jpg"
        )
        assert job1 is not None
        job2 = self._repo.create_job(
            file_id="f2", file_name="b2.jpg", local_path="/tmp/b2.jpg"
        )
        assert job2 is not None
        job3 = self._repo.create_job(
            file_id="f3", file_name="b3.jpg", local_path="/tmp/b3.jpg"
        )
        assert job3 is not None

        # Simulate crash: jobs 1 and 2 are stuck in PROCESSING with stale started_at
        stale_time = (datetime.utcnow() - timedelta(seconds=7200)).isoformat()
        self._repo.update_status(
            job1.job_id,
            JobStatus.PROCESSING,
            extra_updates={"started_at": stale_time},
        )
        self._repo.update_status(
            job2.job_id,
            JobStatus.PROCESSING,
            extra_updates={"started_at": stale_time},
        )

        recovered = self._repo.recover_stuck_jobs()
        assert recovered == 2

        updated1 = self._repo.get_job_by_id(job1.job_id)
        updated2 = self._repo.get_job_by_id(job2.job_id)
        updated3 = self._repo.get_job_by_id(job3.job_id)
        assert updated1 is not None
        assert updated1.status == JobStatus.PENDING
        assert updated1.started_at is None
        assert updated2 is not None
        assert updated2.status == JobStatus.PENDING
        assert updated2.started_at is None
        assert updated3 is not None
        assert updated3.status == JobStatus.PENDING

    def test_recover_does_not_touch_completed_jobs(self) -> None:
        """COMPLETED jobs are not affected by recover_stuck_jobs."""
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)
        self._repo.complete_job(job.job_id)

        recovered = self._repo.recover_stuck_jobs()
        assert recovered == 0

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED

    def test_recover_does_not_touch_manual_review_jobs(self) -> None:
        """MANUAL_REVIEW jobs are not affected by recover_stuck_jobs."""
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)
        self._repo.set_manual_review(
            job.job_id,
            error_message="Low confidence",
            metadata={"candidates": []},
        )

        recovered = self._repo.recover_stuck_jobs()
        assert recovered == 0


class TestFailureIsolation:
    """Milestone 7: Failure isolation - one job failing doesn't block others."""

    def setup_method(self) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)
        self._temp_dir = tempfile.mkdtemp()

    def teardown_method(self) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_job_service_fail_marks_retrying_then_failed(self) -> None:
        """JobService.fail_job: RETRYING on first 2 failures, FAILED on 3rd."""
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        # First failure -> RETRYING
        self._service.fail_job(job.job_id, "Error attempt 1")
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 1

        # Reset to PROCESSING for next failure
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        # Second failure -> RETRYING
        self._service.fail_job(job.job_id, "Error attempt 2")
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 2

        # Reset to PROCESSING for next failure
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        # Third failure -> FAILED (MAX_RETRIES = 3)
        self._service.fail_job(job.job_id, "Error attempt 3")
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 3

    def test_cover_failure_does_not_block_identification(self) -> None:
        """Cover download failure is isolated -- metadata still persisted.

        Job completion is independent of cover download status.
        """
        job = self._repo.create_job(
            file_id="f1", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job is not None
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)
        # Job can still be completed even if cover download failed
        self._repo.complete_job(job.job_id)
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED

    def test_failed_job_does_not_block_other_jobs(self) -> None:
        """A FAILED job does not prevent other PENDING jobs from being picked up."""
        job_a = self._repo.create_job(
            file_id="fa", file_name="a.jpg", local_path="/tmp/a.jpg"
        )
        assert job_a is not None
        job_b = self._repo.create_job(
            file_id="fb", file_name="b.jpg", local_path="/tmp/b.jpg"
        )
        assert job_b is not None

        # Fail job_a
        self._repo.update_status(job_a.job_id, JobStatus.PROCESSING)
        self._repo.fail_job(job_a.job_id, "OCR failure")

        # job_b should still be PENDING and in the pending queue
        pending = self._repo.get_pending_jobs()
        file_ids = [j.file_id for j in pending]
        assert "fb" in file_ids
        assert "fa" not in file_ids

    def test_get_queue_status_reflects_all_states(self) -> None:
        """get_queue_status returns accurate counts per status."""
        # Create a PENDING job
        pjob = self._repo.create_job(
            file_id="fp", file_name="p.jpg", local_path="/tmp/p.jpg"
        )
        assert pjob is not None

        # Create and complete a COMPLETED job
        cjob = self._repo.create_job(
            file_id="fc", file_name="c.jpg", local_path="/tmp/c.jpg"
        )
        assert cjob is not None
        self._repo.update_status(cjob.job_id, JobStatus.PROCESSING)
        self._repo.complete_job(cjob.job_id)

        # Create and fail a FAILED job
        fjob = self._repo.create_job(
            file_id="ff", file_name="f.jpg", local_path="/tmp/f.jpg"
        )
        assert fjob is not None
        self._repo.update_status(fjob.job_id, JobStatus.PROCESSING)
        self._repo.fail_job(fjob.job_id, "Error")

        status = self._service.get_queue_status()
        assert status["pending"] == 1
        assert status["processing"] == 0
        assert status["completed"] == 1
        assert status["failed"] == 1
        assert status["manual_review"] == 0
        assert status["retrying"] == 0
        assert status["cancelled"] == 0
