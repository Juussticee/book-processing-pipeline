"""Unit tests for ProcessingWorker and BaseWorker.

Updated for Milestone 4: ProcessingWorker now uses MetadataService
for book identification. Tests mock the metadata_service to avoid
requiring PaddleOCR-VL/torch at test time.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from models.processing_job import JobStatus, ProcessingJob
from repositories.job_repository import JobRepository
from services.job_service import JobService
from workers.processing_worker import SUPPORTED_EXTENSIONS, ProcessingWorker


def _make_dummy_image(path: str, extension: str) -> None:
    """Create a minimal valid image file at path using PIL if available."""
    try:
        from PIL import Image  # noqa: WPS433

        ext = extension.lower()
        if ext in {".bmp"}:
            img = Image.new("RGB", (10, 10), color="white")
            img.save(path, "BMP")
        elif ext in {".tiff", ".tif"}:
            img = Image.new("RGB", (10, 10), color="white")
            img.save(path, "TIFF")
        else:
            img = Image.new("RGB", (10, 10), color="white")
            img.save(path)
    except Exception:
        # Fallback: write a dummy file; safety validation may reject this
        # in future, but it preserves old behavior for environments
        # without PIL.
        with open(path, "wb") as f:
            f.write(b"dummy image data")


class TestProcessingWorkerProcess:
    """Test the ProcessingWorker.process() method."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)
        self._temp_dir = tempfile.mkdtemp()
        self._local_path = os.path.join(self._temp_dir, "test.jpg")

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)
        if os.path.exists(self._local_path):
            os.unlink(self._local_path)
        os.rmdir(self._temp_dir)

    def _make_worker_with_mock_metadata(self) -> ProcessingWorker:
        """Create a ProcessingWorker with a mocked MetadataService."""
        mock_metadata_service = MagicMock()
        mock_metadata = MagicMock()
        mock_metadata.title = "Test Book"
        mock_metadata.confidence = 0.95
        mock_metadata.source = "google_books"
        mock_metadata_service.identify_book.return_value = (mock_metadata, [])
        return ProcessingWorker(self._service, mock_metadata_service)

    def test_process_valid_image(self) -> None:
        """Test processing a valid image file."""
        _make_dummy_image(self._local_path, ".jpg")

        job = ProcessingJob(
            job_id="job1",
            file_id="file123",
            file_name="test.jpg",
            local_path=self._local_path,
        )

        worker = self._make_worker_with_mock_metadata()
        result = worker.process(job)

        assert result is True

    def test_process_missing_file_raises(self) -> None:
        """Processing a missing file raises FileNotFoundError."""
        job = ProcessingJob(
            job_id="job1",
            file_id="file123",
            file_name="test.jpg",
            local_path="/nonexistent/file.jpg",
        )

        worker = self._make_worker_with_mock_metadata()
        with pytest.raises(FileNotFoundError, match="does not exist"):
            worker.process(job)

    def test_process_unsupported_extension_raises(self) -> None:
        """Processing a non-image file raises ValueError."""
        bad_path = os.path.join(self._temp_dir, "test.txt")
        with open(bad_path, "w") as f:
            f.write("text data")

        try:
            job = ProcessingJob(
                job_id="job1",
                file_id="file123",
                file_name="test.txt",
                local_path=bad_path,
            )

            worker = self._make_worker_with_mock_metadata()
            with pytest.raises(ValueError, match="Unsupported file extension"):
                worker.process(job)
        finally:
            if os.path.exists(bad_path):
                os.unlink(bad_path)

    def test_process_supported_extensions(self) -> None:
        """Test that all supported extensions are accepted."""
        worker = self._make_worker_with_mock_metadata()
        for ext in SUPPORTED_EXTENSIONS:
            filename = f"test{ext}"
            filepath = os.path.join(self._temp_dir, filename)
            _make_dummy_image(filepath, ext)

            try:
                job = ProcessingJob(
                    job_id=f"job_{ext}",
                    file_id=f"file_{ext}",
                    file_name=filename,
                    local_path=filepath,
                )
                result = worker.process(job)
                assert result is True
            finally:
                if os.path.exists(filepath):
                    os.unlink(filepath)

    def test_process_manual_review_returns_none(self) -> None:
        """When confidence is low, process returns None (worker handles status)."""
        _make_dummy_image(self._local_path, ".jpg")

        job = ProcessingJob(
            job_id="job1",
            file_id="file123",
            file_name="test.jpg",
            local_path=self._local_path,
        )

        mock_metadata_service = MagicMock()
        mock_metadata_service.identify_book.return_value = (None, [MagicMock()])
        worker = ProcessingWorker(self._service, mock_metadata_service)

        result = worker.process(job)
        assert result is None  # Worker handled MANUAL_REVIEW

    def test_process_no_candidates_creates_minimal_metadata(self) -> None:
        """When no candidates found, process creates minimal metadata for review.

        Per the non-negotiable requirement, no discovered book may be rejected.
        Even when identify_book returns (None, None), the worker must persist
        a minimal BookMetadata entry with status=needs_review.
        """
        from models.book_metadata import BookMetadata
        from repositories.book_metadata_repository import BookMetadataRepository

        _make_dummy_image(self._local_path, ".jpg")

        job = ProcessingJob(
            job_id="job1",
            file_id="file123",
            file_name="test.jpg",
            local_path=self._local_path,
        )

        mock_metadata_service = MagicMock()
        mock_metadata_service.identify_book.return_value = (None, None)
        # Mock the metadata_repo attribute
        mock_repo = MagicMock()
        mock_metadata_service._metadata_repo = mock_repo
        worker = ProcessingWorker(self._service, mock_metadata_service)

        result = worker.process(job)
        # No ValueError raised -- book is persisted, not rejected
        assert result is None  # Worker handled status transition
        # Verify minimal metadata was created and persisted
        mock_repo.upsert_metadata.assert_called_once()
        saved_meta = mock_repo.upsert_metadata.call_args[0][0]
        assert saved_meta.title == "(unreadable)"
        assert saved_meta.status == "needs_review"
        assert saved_meta.rejection_reason == "no_candidates_no_vision"

    def test_manual_review_candidate_summary_has_full_evidence(self) -> None:
        """Milestone 7: _build_candidate_summary includes all evidence fields."""
        from models.book_candidate import BookCandidate
        from models.book_metadata import BookMetadata

        metadata = BookMetadata(
            title="Le cœur vert",
            authors=["Jean-Louis Kuffer"],
            confidence=0.5,
            source="open_library",
            isbn_10=None,
            isbn_13=None,
            publisher=None,
            ocr_text="cœur vert",
            raw_ocr="cœur vert",
            extracted_title="Le cœur vert",
            extracted_author="Jean-Louis Kuffer",
            extracted_isbn=None,
            extracted_publisher=None,
            match_score=0.5,
            rejection_reason="Below confidence threshold",
        )
        candidate = BookCandidate(
            metadata=metadata,
            total_score=0.5,
            title_score=0.9,
            author_score=0.8,
            isbn_score=0.0,
            publisher_score=0.0,
            isbn_state="UNAVAILABLE",
            title_available=True,
            author_available=True,
            publisher_available=False,
        )
        worker = ProcessingWorker(self._service, MagicMock())
        summary = worker._build_candidate_summary([candidate])

        assert "candidates" in summary
        c = summary["candidates"][0]
        assert c["title"] == "Le cœur vert"
        assert c["authors"] == ["Jean-Louis Kuffer"]
        assert c["source"] == "open_library"
        assert c["total_score"] == 0.5
        # Evidence fields
        assert c["title_score"] == 0.9
        assert c["author_score"] == 0.8
        assert c["isbn_score"] == 0.0
        assert c["publisher_score"] == 0.0
        assert c["isbn_state"] == "UNAVAILABLE"
        assert c["title_available"] is True
        assert c["author_available"] is True
        assert c["publisher_available"] is False
        assert c["confidence"] == 0.5

    def test_manual_review_empty_candidates(self) -> None:
        """Milestone 7: _build_candidate_summary handles no candidates."""
        worker = ProcessingWorker(self._service, MagicMock())
        summary = worker._build_candidate_summary([])
        assert summary == {"candidates": []}


class TestBaseWorkerLifecycle:
    """Test BaseWorker lifecycle methods."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)
        self._worker = ProcessingWorker(self._service)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_initialize(self) -> None:
        """Test worker initialization."""
        self._worker.initialize()
        assert self._worker.running is True

    def test_shutdown(self) -> None:
        """Test worker shutdown."""
        self._worker.initialize()
        self._worker.shutdown()
        assert self._worker.running is False

    def test_health_check(self) -> None:
        """Test health check returns correct status."""
        self._worker.initialize()
        health = self._worker.health_check()
        assert health["worker_name"] == "ProcessingWorker"
        assert health["running"] is True
        assert health["processed_count"] == 0
        assert health["failed_count"] == 0


class TestBaseWorkerProcessJob:
    """Test the _process_job and _handle_failure methods.

    Uses a real ProcessingWorker with a mocked ``process`` method
    so that inherited methods like ``_process_job`` and
    ``_handle_failure`` are not intercepted by MagicMock.
    """

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)
        self._temp_dir = tempfile.mkdtemp()
        self._local_path = os.path.join(self._temp_dir, "test.jpg")

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)
        if os.path.exists(self._local_path):
            os.unlink(self._local_path)
        os.rmdir(self._temp_dir)

    def _make_worker_with_process_mock(
        self, return_value=True, side_effect=None
    ) -> ProcessingWorker:
        """Create a real ProcessingWorker with process() mocked."""
        mock_metadata_service = MagicMock()
        worker = ProcessingWorker(self._service, mock_metadata_service)
        worker.process = MagicMock(  # type: ignore[method-assign]
            return_value=return_value,
            side_effect=side_effect,
        )
        return worker

    def test_process_job_success(self) -> None:
        """Test that a successful job is marked COMPLETED."""
        _make_dummy_image(self._local_path, ".jpg")

        worker = self._make_worker_with_process_mock(return_value=True)

        job = self._repo.create_job(
            file_id="f1", file_name="test.jpg", local_path=self._local_path
        )
        assert job is not None

        worker._process_job(job)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED

    def test_process_job_failure_marks_retrying(self) -> None:
        """Test that a failed job is marked RETRYING (first transient failure)."""
        _make_dummy_image(self._local_path, ".jpg")

        worker = self._make_worker_with_process_mock(
            side_effect=ValueError("temporary network timeout")
        )

        job = self._repo.create_job(
            file_id="f1", file_name="test.jpg", local_path=self._local_path
        )
        assert job is not None

        worker._process_job(job)

        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.RETRYING
        assert updated.retry_count == 1

    def test_process_job_three_failures_marks_failed(self) -> None:
        """Test that 3 transient failures result in FAILED status."""
        _make_dummy_image(self._local_path, ".jpg")

        worker = self._make_worker_with_process_mock(
            side_effect=ValueError("temporary timeout")
        )

        job = self._repo.create_job(
            file_id="f1", file_name="test.jpg", local_path=self._local_path
        )
        assert job is not None

        # First failure
        worker._process_job(job)
        self._repo.reset_retrying_jobs()
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PENDING
        assert updated.retry_count == 1

        # Second failure
        worker._process_job(job)
        self._repo.reset_retrying_jobs()
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.PENDING
        assert updated.retry_count == 2

        # Third failure -> FAILED
        worker._process_job(job)
        updated = self._repo.get_job_by_id(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.FAILED
        assert updated.retry_count == 3


class TestBaseWorkerPolling:
    """Test the worker polling loop."""

    def setup_method(self, method) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = JobRepository(self._db_path)
        self._service = JobService(self._repo)
        mock_metadata_service = MagicMock()
        mock_metadata = MagicMock()
        mock_metadata.title = "Test"
        mock_metadata.confidence = 0.95
        mock_metadata_service.identify_book.return_value = (mock_metadata, [])
        self._worker = ProcessingWorker(self._service, mock_metadata_service)

    def teardown_method(self, method) -> None:
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_run_processes_one_job_then_stops(self) -> None:
        """Test that the worker processes one job and then stops."""
        temp_dir = tempfile.mkdtemp()
        local_path = os.path.join(temp_dir, "test.jpg")
        _make_dummy_image(local_path, ".jpg")

        try:
            job = self._repo.create_job(
                file_id="f1", file_name="test.jpg", local_path=local_path
            )
            assert job is not None

            call_count = [0]

            def mock_sleep(seconds):
                call_count[0] += 1
                if call_count[0] >= 2:
                    raise StopIteration()

            # Both patches target the same global time.sleep;
            # innermost patch wins, so use the same side_effect
            with patch("workers.processing_worker.time.sleep", side_effect=mock_sleep):
                with patch("workers.base_worker.time.sleep", side_effect=mock_sleep):
                    self._worker.run()

            updated = self._repo.get_job_by_id(job.job_id)
            assert updated is not None
            assert updated.status == JobStatus.COMPLETED
        finally:
            if os.path.exists(local_path):
                os.unlink(local_path)
            os.rmdir(temp_dir)

    def test_run_no_jobs_sleeps(self) -> None:
        """Test that the worker sleeps when there are no pending jobs."""
        call_count = [0]

        def mock_sleep(seconds):
            call_count[0] += 1
            if call_count[0] >= 1:
                raise StopIteration()

        with patch("workers.processing_worker.time.sleep", side_effect=mock_sleep):
            with patch("workers.base_worker.time.sleep", side_effect=mock_sleep):
                self._worker.run()

        assert call_count[0] == 1

    def test_run_fifo_order(self) -> None:
        """Test that jobs are processed in FIFO order."""
        temp_dir = tempfile.mkdtemp()

        paths = []
        for i in range(3):
            p = os.path.join(temp_dir, f"test{i}.jpg")
            _make_dummy_image(p, ".jpg")
            paths.append(p)

        try:
            job1 = self._repo.create_job(
                file_id="f1", file_name="test0.jpg", local_path=paths[0]
            )
            job2 = self._repo.create_job(
                file_id="f2", file_name="test1.jpg", local_path=paths[1]
            )
            job3 = self._repo.create_job(
                file_id="f3", file_name="test2.jpg", local_path=paths[2]
            )
            assert job1 is not None and job2 is not None and job3 is not None

            processed_order = []
            original_process = self._worker.process

            def tracking_process(job):
                processed_order.append(job.file_id)
                return original_process(job)

            self._worker.process = tracking_process  # type: ignore[method-assign]

            call_count = [0]

            def mock_sleep(seconds):
                call_count[0] += 1
                if call_count[0] >= 4:
                    raise StopIteration()

            with patch("workers.processing_worker.time.sleep", side_effect=mock_sleep):
                with patch("workers.base_worker.time.sleep", side_effect=mock_sleep):
                    self._worker.run()

            # Should process jobs in order: f1, f2, f3
            assert processed_order == ["f1", "f2", "f3"]
        finally:
            for p in paths:
                if os.path.exists(p):
                    os.unlink(p)
            os.rmdir(temp_dir)


class TestJobStatusEnum:
    """Test the JobStatus enum."""

    def test_all_statuses_exist(self) -> None:
        """All statuses should be present."""
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.PROCESSING.value == "processing"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.RETRYING.value == "retrying"
        assert JobStatus.CANCELLED.value == "cancelled"
        assert JobStatus.MANUAL_REVIEW.value == "manual_review"

    def test_status_from_string(self) -> None:
        """Test creating JobStatus from string value."""
        assert JobStatus("pending") == JobStatus.PENDING
        assert JobStatus("processing") == JobStatus.PROCESSING
        assert JobStatus("completed") == JobStatus.COMPLETED
        assert JobStatus("failed") == JobStatus.FAILED
        assert JobStatus("retrying") == JobStatus.RETRYING
        assert JobStatus("cancelled") == JobStatus.CANCELLED
        assert JobStatus("manual_review") == JobStatus.MANUAL_REVIEW

    def test_status_is_string(self) -> None:
        """JobStatus should be a string enum."""
        assert isinstance(JobStatus.PENDING.value, str)
        assert JobStatus.PENDING == "pending"