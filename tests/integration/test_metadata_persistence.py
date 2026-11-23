"""Integration tests for metadata persistence through the full pipeline.

Tests: MetadataService.identify_book() -> BookMetadataRepository,
verifying metadata is persisted, duplicates prevented, updates work,
and persistence failures propagate.
"""

import os
import tempfile
from unittest.mock import MagicMock

import pytest

from models.book_metadata import BookMetadata
from models.ocr_result import OCRResult
from models.processing_job import JobStatus
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.job_repository import JobRepository
from services.book_lookup_service import BookLookupService
from services.confidence_service import ConfidenceService
from services.job_service import JobService
from services.metadata_service import MetadataService
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer
from workers.processing_worker import ProcessingWorker


def _make_metadata(
    drive_file_id: str = "drive001",
    job_id: str = "job001",
    title: str = "Test Book",
    confidence: float = 0.95,
    status: str = "identified",
) -> BookMetadata:
    """Create a BookMetadata instance for testing."""
    return BookMetadata(
        title=title,
        authors=["Test Author"],
        isbn_10="0306406152",
        isbn_13="9780306406157",
        publisher="Test Publisher",
        published_date="2024-01-01",
        language="en",
        categories=["Fiction"],
        description="A test book description.",
        thumbnail_url="http://example.com/thumb.jpg",
        cover_url="http://example.com/cover.jpg",
        confidence=confidence,
        source="google_books",
        raw_ocr="Test Book by Test Author ISBN 9780306406157",
        drive_file_id=drive_file_id,
        job_id=job_id,
        status=status,
    )


class TestMetadataRepository:
    """Tests for BookMetadataRepository CRUD operations."""

    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = BookMetadataRepository(self._db_path)

    def teardown_method(self):
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_insert_and_retrieve(self):
        """Can insert and retrieve metadata."""
        metadata = _make_metadata()
        self._repo.insert_metadata(metadata)
        result = self._repo.get_by_drive_file_id("drive001")
        assert result is not None
        assert result.title == "Test Book"

    def test_upsert_inserts_new(self):
        """Upsert on a new drive file ID inserts a new row."""
        metadata = _make_metadata(drive_file_id="up001", job_id="uj001")
        metadata_id = self._repo.upsert_metadata(metadata)
        assert metadata_id > 0
        result = self._repo.get_by_drive_file_id("up001")
        assert result is not None

    def test_upsert_updates_existing(self):
        """Upsert on existing drive file ID updates, no duplicate."""
        meta1 = _make_metadata(drive_file_id="dup001", job_id="jobA", title="First")
        self._repo.upsert_metadata(meta1)
        assert len(self._repo.list()) == 1
        meta2 = _make_metadata(drive_file_id="dup001", job_id="jobB", title="Second")
        self._repo.upsert_metadata(meta2)
        results = self._repo.list()
        assert len(results) == 1
        result = self._repo.get_by_drive_file_id("dup001")
        assert result.title == "Second"
        assert result.job_id == "jobB"

    def test_get_by_job_id(self):
        """Can retrieve metadata by job ID."""
        metadata = _make_metadata(job_id="special_job_123")
        self._repo.insert_metadata(metadata)
        result = self._repo.get_by_job_id("special_job_123")
        assert result is not None

    def test_exists(self):
        """Exists returns True/False correctly."""
        metadata = _make_metadata(drive_file_id="exists001")
        self._repo.insert_metadata(metadata)
        assert self._repo.exists("exists001") is True
        assert self._repo.exists("nope") is False

    def test_delete(self):
        """Delete removes the record."""
        metadata = _make_metadata(drive_file_id="del001")
        self._repo.insert_metadata(metadata)
        assert self._repo.delete("del001") is True
        assert self._repo.get_by_drive_file_id("del001") is None

    def test_list_with_status_filter(self):
        """List can filter by status."""
        meta1 = _make_metadata(drive_file_id="s1", job_id="j1", status="identified")
        meta2 = _make_metadata(drive_file_id="s2", job_id="j2", status="needs_review")
        self._repo.insert_metadata(meta1)
        self._repo.insert_metadata(meta2)
        identified = self._repo.list(status="identified")
        assert len(identified) == 1
        manual = self._repo.list(status="needs_review")
        assert len(manual) == 1


class TestMetadataServicePersistence:
    """Tests that MetadataService.identify_book persists via BookMetadataRepository."""

    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._meta_repo = BookMetadataRepository(self._db_path)

    def teardown_method(self):
        self._meta_repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def _make_service(self, book_lookup_result=None):
        """Build a MetadataService with mocked dependencies."""
        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()

        ocr_service = MagicMock(spec=OCRService)
        ocr_service.extract_text.return_value = OCRResult(
            text="Test Book ISBN 9780306406157",
            confidence=0.9,
            words=[],
            language="en",
            image_path="/fake/image.jpg",
        )
        ocr_service.extract_fields.return_value = {
            "title": "Test Book",
            "author": "Test Author",
        }

        text_normalizer = MagicMock(spec=TextNormalizer)
        text_normalizer.normalize.return_value = "Test Book"
        text_normalizer.extract_isbn.return_value = ["9780306406157"]

        book_lookup = MagicMock(spec=BookLookupService)
        if book_lookup_result:
            book_lookup.search_by_isbn.return_value = book_lookup_result
        else:
            book_lookup.search_by_isbn.return_value = [
                BookMetadata(title="Test Book", confidence=0.95, source="google_books")
            ]

        # Use real CandidateRanker and real ConfidenceService
        # (they don't need I/O — just scoring logic)
        from services.candidate_ranker import CandidateRanker

        confidence_svc = ConfidenceService(threshold=0.20)

        return MetadataService(
            image_preprocessor=preprocessor,
            ocr_service=ocr_service,
            text_normalizer=text_normalizer,
            book_lookup=book_lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=confidence_svc,
            metadata_repo=self._meta_repo,
        )

    def test_identify_book_persists_metadata(self):
        """identify_book persists metadata to the repository."""
        service = self._make_service()

        result, candidates = service.identify_book(
            "/fake/image.jpg",
            drive_file_id="persist001",
            job_id="persist_job_001",
        )

        assert result is not None
        persisted = self._meta_repo.get_by_drive_file_id("persist001")
        assert persisted is not None
        assert persisted.title == "Test Book"
        assert persisted.drive_file_id == "persist001"
        assert persisted.job_id == "persist_job_001"

    def test_reprocess_updates_existing(self):
        """Reprocessing with same drive_file_id updates, no duplicate."""
        service = self._make_service()

        # First call
        service.identify_book(
            "/fake/image.jpg", drive_file_id="replay001", job_id="rj1"
        )
        result = self._meta_repo.get_by_drive_file_id("replay001")
        assert result is not None
        assert result.job_id == "rj1"

        # Second call with same drive_file_id, different job_id
        service.identify_book(
            "/fake/image.jpg", drive_file_id="replay001", job_id="rj2"
        )

        # Only one record (upsert, not insert)
        results = self._meta_repo.list()
        assert len(results) == 1
        result = self._meta_repo.get_by_drive_file_id("replay001")
        assert result.job_id == "rj2"

    def test_persistence_failure_raises(self):
        """If _persist_metadata raises, the exception propagates."""
        service = self._make_service()

        def failing_persist(metadata: BookMetadata):
            raise RuntimeError("DB connection lost")

        service._persist_metadata = failing_persist

        with pytest.raises(RuntimeError, match="DB connection lost"):
            service.identify_book(
                "/fake/image.jpg",
                drive_file_id="fail001",
                job_id="fail_job_001",
            )


class TestProcessingWorkerIntegration:
    """Tests that ProcessingWorker passes drive_file_id and job_id correctly."""

    def setup_method(self):
        self._fd, self._jobs_db = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._fd2, self._meta_db = tempfile.mkstemp(suffix=".db")
        os.close(self._fd2)
        self._temp_dir = tempfile.mkdtemp()
        self._job_repo = JobRepository(self._jobs_db)
        self._job_service = JobService(self._job_repo)
        self._meta_repo = BookMetadataRepository(self._meta_db)

    def teardown_method(self):
        self._job_repo.close()
        self._meta_repo.close()
        for path in [self._jobs_db, self._meta_db]:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except PermissionError:
                    pass
        for fname in os.listdir(self._temp_dir):
            try:
                os.unlink(os.path.join(self._temp_dir, fname))
            except PermissionError:
                pass
        try:
            os.rmdir(self._temp_dir)
        except OSError:
            pass

    def _create_image_file(self, name: str = "test.jpg") -> str:
        """Create a temporary, decodable JPEG (ProcessingWorker validates it)."""
        from PIL import Image

        path = os.path.join(self._temp_dir, name)
        Image.new("RGB", (120, 180), color=(200, 200, 200)).save(path, "JPEG")
        return path

    def test_worker_calls_identify_book_with_correct_args(self):
        """ProcessingWorker passes drive_file_id and job_id to identify_book."""
        local_path = self._create_image_file("worker1.jpg")
        job = self._job_repo.create_job(
            file_id="worker_drive_001",
            file_name="worker1.jpg",
            local_path=local_path,
            worker_name="ProcessingWorker",
        )
        assert job is not None
        self._job_repo.update_status(job.job_id, JobStatus.PROCESSING)

        metadata = BookMetadata(
            title="Worker Test Book", confidence=0.95, source="google_books"
        )

        service = MetadataService(
            image_preprocessor=MagicMock(),
            ocr_service=MagicMock(spec=OCRService),
            text_normalizer=MagicMock(spec=TextNormalizer),
            book_lookup=MagicMock(spec=BookLookupService),
            candidate_ranker=MagicMock(),
            confidence_service=ConfidenceService(threshold=0.85),
            metadata_repo=self._meta_repo,
        )
        service.identify_book = MagicMock(return_value=(metadata, []))

        worker = ProcessingWorker(
            self._job_service, service, metadata_repo=self._meta_repo
        )
        result = worker.process(job)

        assert result is True
        call_kwargs = service.identify_book.call_args
        assert call_kwargs.kwargs.get("drive_file_id") == job.file_id
        assert call_kwargs.kwargs.get("job_id") == job.job_id


class TestSafeFilename:
    """Tests for safe_local_filename helper."""

    def test_drive_file_id_as_filename(self):
        """Downloaded files use Drive file ID as filename, not original name."""
        from app import safe_local_filename

        result = safe_local_filename("1AbCdEfGh", "My Book Cover!.jpg")
        assert result == "1AbCdEfGh.jpg"

    def test_path_traversal_blocked(self):
        from app import safe_local_filename

        result = safe_local_filename("safe123", "../../../etc/passwd")
        assert ".." not in result
        assert "/" not in result

    def test_special_chars_in_drive_id_stripped(self):
        from app import safe_local_filename

        result = safe_local_filename("abc123!@#", "photo.png")
        assert result == "abc123.png"

    def test_no_extension(self):
        from app import safe_local_filename

        result = safe_local_filename("file007", "noextension")
        assert result == "file007"

    def test_empty_drive_file_id(self):
        from app import safe_local_filename

        result = safe_local_filename("", "test.jpg")
        assert result == "unknown.jpg"
