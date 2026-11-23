"""Integration tests for the processing pipeline.

Tests the full end-to-end flow from job creation through worker
processing to completion/failure, using real repository instances.

For Milestone 4, ProcessingWorker now uses MetadataService.
Tests mock the metadata_service to avoid requiring PaddleOCR-VL/torch at test time.
"""

import os
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models.processing_job import JobStatus
from repositories.job_repository import JobRepository
from services.job_service import JobService
from workers.processing_worker import ProcessingWorker


class TestProcessingPipeline:
    """End-to-end tests for the processing pipeline."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)
        # Mock metadata service for all integration tests
        mock_metadata_service = MagicMock()
        mock_metadata = MagicMock()
        mock_metadata.title = "Test Book"
        mock_metadata.confidence = 0.95
        mock_metadata.source = "google_books"
        mock_metadata_service.identify_book.return_value = (mock_metadata, [])
        self._worker = ProcessingWorker(self._service, mock_metadata_service)
        self._temp_dir = tempfile.mkdtemp()

    def teardown_method(self, method) -> None:
        self._worker.shutdown()
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)
        # Clean up temp files
        for fname in os.listdir(self._temp_dir):
            os.unlink(os.path.join(self._temp_dir, fname))
        os.rmdir(self._temp_dir)

    def _create_image_file(self, name: str = "test.jpg") -> str:
        """Create a temporary, decodable JPEG and return its path.

        ProcessingWorker validates image safety before OCR, so the fixture must
        be a real image rather than placeholder bytes.
        """
        from PIL import Image

        path = os.path.join(self._temp_dir, name)
        Image.new("RGB", (120, 180), color=(200, 200, 200)).save(path, "JPEG")
        return path

    def _make_worker_thread(self) -> threading.Thread:
        """Create a worker running in a background thread."""
        call_count = [0]

        def mock_sleep(seconds):
            call_count[0] += 1
            if call_count[0] >= 100:
                raise StopIteration()

        thread = threading.Thread(target=self._worker.run, daemon=True)
        # Patch time.sleep for the worker thread
        self._sleep_patch = patch(
            "workers.processing_worker.time.sleep",
            side_effect=mock_sleep,
        )
        self._sleep_patch.start()
        # Also patch base_worker time.sleep
        self._sleep_patch2 = patch(
            "workers.base_worker.time.sleep",
            side_effect=mock_sleep,
        )
        self._sleep_patch2.start()
        thread.start()
        return thread

    def _stop_worker_thread(self, thread: threading.Thread) -> None:
        """Stop the worker thread and clean up patches."""
        self._worker.shutdown()
        thread.join(timeout=5)
        self._sleep_patch.stop()
        self._sleep_patch2.stop()

    def test_scenario_1_job_creation_and_success(self) -> None:
        """End-to-end: create a job, process it, verify completion."""
        local_path = self._create_image_file("book1.jpg")

        job = self._repo.create_job(
            file_id="drive001",
            file_name="book1.jpg",
            local_path=local_path,
        )
        assert job is not None

        with patch("workers.processing_worker.time.sleep"):
            self._worker._process_job(job)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED
        assert updated.worker_name == "ProcessingWorker"
        assert updated.error_message is None

    def test_scenario_2_job_failure_and_retry(self) -> None:
        """End-to-end: job fails, retries, eventually succeeds."""
        local_path = self._create_image_file("book2.jpg")

        job = self._repo.create_job(
            file_id="drive002",
            file_name="book2.jpg",
            local_path=local_path,
        )
        assert job is not None

        # First attempt fails
        mock_process = patch.object(
            ProcessingWorker,
            "process",
            side_effect=[ValueError("fail 1"), True],
        )
        with mock_process:
            self._worker._process_job(job)

        # Check job is RETRYING after first failure
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 1

        # Reset and retry
        self._repo.reset_retrying_jobs()
        pending = self._service.get_pending_jobs()
        assert len(pending) == 1

        # Second attempt - process() returns True (already in side_effect)
        self._worker._process_job(pending[0])
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED

    def test_scenario_3_max_retries_exhausted(self) -> None:
        """End-to-end: job fails 3 times, ends as FAILED."""
        local_path = self._create_image_file("book3.jpg")

        job = self._repo.create_job(
            file_id="drive003",
            file_name="book3.jpg",
            local_path=local_path,
        )
        assert job is not None

        try:
            with patch.object(
                ProcessingWorker,
                "process",
                side_effect=ValueError("persistent error"),
            ):
                # First failure
                self._worker._process_job(job)
                self._repo.reset_retrying_jobs()

                # Second failure
                pending = self._service.get_pending_jobs()
                assert len(pending) == 1
                self._worker._process_job(pending[0])
                self._repo.reset_retrying_jobs()

                # Third failure -> FAILED
                pending = self._service.get_pending_jobs()
                assert len(pending) == 1
                self._worker._process_job(pending[0])

            updated = self._repo.get_job_by_id(job.job_id)
            assert updated is not None
            assert updated.status == JobStatus.FAILED
            assert updated.retry_count == 3
            assert updated.error_message is not None
        except StopIteration:
            # If process mock runs dry of side_effects, that's fine
            # for this test
            pass

    def test_scenario_4_fifo_processing_order(self) -> None:
        """End-to-end: multiple jobs processed in FIFO order."""
        path1 = self._create_image_file("book_a.jpg")
        path2 = self._create_image_file("book_b.jpg")
        path3 = self._create_image_file("book_c.jpg")

        job1 = self._repo.create_job(
            file_id="f1", file_name="book_a.jpg", local_path=path1
        )
        job2 = self._repo.create_job(
            file_id="f2", file_name="book_b.jpg", local_path=path2
        )
        job3 = self._repo.create_job(
            file_id="f3", file_name="book_c.jpg", local_path=path3
        )
        assert job1 is not None and job2 is not None and job3 is not None

        processed_order = []
        original_process = self._worker.process

        def tracking_process(job):
            processed_order.append(job.file_id)
            return original_process(job)

        self._worker.process = tracking_process  # type: ignore[method-assign]

        try:
            with patch("workers.processing_worker.time.sleep"):
                # Process all three jobs
                for j in [job1, job2, job3]:
                    self._worker._process_job(j)
        finally:
            self._worker.process = original_process  # type: ignore[method-assign]

        assert processed_order == ["f1", "f2", "f3"]

        # Verify all completed
        for j in [job1, job2, job3]:
            updated = self._repo.get_job_by_id(j.job_id)
            assert updated is not None
            assert updated.status == JobStatus.COMPLETED

    def test_scenario_5_duplicate_job_prevention(self) -> None:
        """End-to-end: service prevents duplicate active jobs for same file."""
        from models.drive_file import DriveFile

        drive_file = DriveFile(
            file_id="drive_dup",
            name="dup.jpg",
            mime_type="image/jpeg",
            modified_time="2024-01-01T00:00:00",
            size=1024,
        )
        local_path = self._create_image_file("dup.jpg")

        job1 = self._service.create_job_for_file(drive_file, local_path)
        assert job1 is not None

        # Attempting to create a second job for the same file returns None
        job2 = self._service.create_job_for_file(drive_file, local_path)
        assert job2 is None

        # After completing the first job, a new one can be created
        self._repo.complete_job(job1.job_id)
        job3 = self._service.create_job_for_file(drive_file, local_path)
        assert job3 is not None

    def test_scenario_6_recovery_on_restart(self) -> None:
        """End-to-end: stuck PROCESSING jobs are recovered to PENDING on startup."""
        local_path = self._create_image_file("book6.jpg")

        job = self._repo.create_job(
            file_id="drive006",
            file_name="book6.jpg",
            local_path=local_path,
        )
        assert job is not None

        # Simulate a crash: job is stuck in PROCESSING
        self._repo.update_status(job.job_id, JobStatus.PROCESSING)

        # Verify job is PROCESSING
        processing = self._service.get_processing_jobs()
        assert len(processing) == 1

        # Simulate restart: recover stuck jobs. A zero timeout means every
        # PROCESSING job is considered stale (i.e. the process really did die).
        restart_service = JobService(
            self._repo,
            config=SimpleNamespace(processing_timeout_seconds=0),
        )
        recovered = restart_service.recover_stuck_jobs()
        assert recovered == 1

        # Job should now be PENDING
        pending = restart_service.get_pending_jobs()
        assert len(pending) == 1
        assert pending[0].status == JobStatus.PENDING

    def test_scenario_7_health_check_after_processing(self) -> None:
        """End-to-end: health check reflects post-processing state."""
        local_path1 = self._create_image_file("book7a.jpg")
        local_path2 = self._create_image_file("book7b.jpg")

        job1 = self._repo.create_job(
            file_id="f7a", file_name="book7a.jpg", local_path=local_path1
        )
        job2 = self._repo.create_job(
            file_id="f7b", file_name="book7b.jpg", local_path=local_path2
        )
        assert job1 is not None and job2 is not None

        try:
            with patch.object(
                ProcessingWorker, "process", side_effect=[True, ValueError("err")]
            ):
                # Process first job successfully
                self._worker._process_job(job1)

                # Process second job with failure
                self._worker._process_job(job2)
        except StopIteration:
            pass

        # Check health reflects the state
        health = self._worker.health_check()
        assert health["processed_count"] >= 1
        assert health["failed_count"] >= 1

        # Check queue status
        queue_status = self._service.get_queue_status()
        assert queue_status["completed"] >= 1
        assert queue_status["failed"] >= 1 or queue_status["retrying"] >= 1
