"""Unit tests for ExportService.

Tests the export pipeline: metadata -> validation -> cover resolution ->
cover download -> Google Sheets export -> record persistence.
"""

import os
import tempfile
from unittest.mock import MagicMock

from models.book_metadata import BookMetadata
from models.export_config import ExportConfig
from models.export_record import ExportRecord, ExportStatus
from repositories.cover_repository import CoverRepository
from repositories.export_repository import ExportRepository
from services.export_service import ExportService


def _make_metadata(**kwargs):
    defaults = {
        "title": "Test Book Title",
        "authors": ["Test Author"],
        "isbn_10": "0306406152",
        "isbn_13": "9780306406157",
        "publisher": "Test Publisher",
        "published_date": "2024-01-01",
        "language": "en",
        "categories": ["Fiction"],
        "description": "A test book description.",
        "cover_url": "http://example.com/cover.jpg",
        "thumbnail_url": "http://example.com/thumb.jpg",
        "confidence": 0.95,
        "source": "google_books",
        "drive_file_id": "drive001",
        "job_id": "job001",
    }
    defaults.update(kwargs)
    return BookMetadata(**defaults)


class TestExportService:
    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._export_db = tempfile.mktemp(suffix=".db")
        self._covers_dir = tempfile.mkdtemp()

        self._metadata_repo = MagicMock()
        self._cover_repo = CoverRepository(self._db_path)
        self._export_repo = ExportRepository(self._export_db)
        self._validation_service = MagicMock()
        self._cover_resolver = MagicMock()
        self._cover_downloader = MagicMock()
        self._sheets_service = MagicMock()
        self._sheets_service._build_sheet_url.return_value = (
            "https://docs.google.com/spreadsheets/d/test/edit#gid=0&range=A1"
        )
        self._config = ExportConfig()
        self._config.export_max_retries = 0

        self._service = ExportService(
            metadata_repo=self._metadata_repo,
            cover_repo=self._cover_repo,
            export_repo=self._export_repo,
            validation_service=self._validation_service,
            cover_resolver=self._cover_resolver,
            cover_downloader=self._cover_downloader,
            sheets_service=self._sheets_service,
            config=self._config,
        )

    def teardown_method(self):
        import gc
        import shutil
        import time

        self._cover_repo.close()
        self._export_repo.close()
        gc.collect()
        for _ in range(5):
            try:
                if os.path.exists(self._db_path):
                    os.unlink(self._db_path)
                break
            except PermissionError:
                time.sleep(0.1)
        for _ in range(5):
            try:
                if os.path.exists(self._export_db):
                    os.unlink(self._export_db)
                break
            except PermissionError:
                time.sleep(0.1)
        if os.path.exists(self._covers_dir):
            shutil.rmtree(self._covers_dir, ignore_errors=True)

    def test_process_export_success(self):
        """Full pipeline: validate -> resolve -> download -> sheets -> record."""
        metadata = _make_metadata()
        self._metadata_repo.get_by_drive_file_id.return_value = metadata

        from models.validation_result import ValidationResult

        self._validation_service.validate.return_value = ValidationResult(
            drive_file_id="drive001", job_id="job001", is_valid=True, confidence=0.95
        )
        self._cover_resolver.resolve_cover_urls.return_value = [
            "http://example.com/cover.jpg"
        ]
        mock_cover = MagicMock()
        mock_cover.local_path = "data/covers/cover_drive001.jpg"
        mock_cover.cover_url = "http://example.com/cover.jpg"
        self._cover_downloader.download_covers.return_value = mock_cover
        self._sheets_service.get_or_create_row.return_value = 42

        result = self._service.process_export("drive001", "job001")
        assert result.status == ExportStatus.COMPLETED
        assert result.sheet_row_id == "42"

    def test_process_export_no_metadata(self):
        """Export fails when metadata not found."""
        self._metadata_repo.get_by_drive_file_id.return_value = None
        self._config.export_max_retries = 0
        result = self._service.process_export("nonexistent", "job001")
        assert result.status == ExportStatus.FAILED
        assert result.last_error is not None

    def test_process_export_validation_failure_still_exports(self):
        """Low confidence is logged but export still proceeds."""
        from models.validation_result import ValidationResult

        metadata = _make_metadata()
        self._metadata_repo.get_by_drive_file_id.return_value = metadata
        self._validation_service.validate.return_value = ValidationResult(
            drive_file_id="drive001", job_id="job001", is_valid=True, confidence=0.4
        )
        self._cover_resolver.resolve_cover_urls.return_value = []
        mock_cover = MagicMock()
        mock_cover.local_path = None
        self._cover_downloader.download_covers.return_value = mock_cover
        self._sheets_service.get_or_create_row.return_value = 7

        result = self._service.process_export("drive001", "job001")
        assert result.status == ExportStatus.COMPLETED

    def test_process_export_sheets_failure_marks_failed(self):
        """Sheets API failure marks export as FAILED."""
        metadata = _make_metadata()
        self._metadata_repo.get_by_drive_file_id.return_value = metadata
        from models.validation_result import ValidationResult

        self._validation_service.validate.return_value = ValidationResult(
            drive_file_id="drive001", job_id="job001", is_valid=True, confidence=0.95
        )
        self._cover_resolver.resolve_cover_urls.return_value = [
            "http://example.com/cover.jpg"
        ]
        mock_cover = MagicMock()
        mock_cover.local_path = "data/covers/cover_drive001.jpg"
        self._cover_downloader.download_covers.return_value = mock_cover
        self._sheets_service.get_or_create_row.side_effect = Exception(
            "Sheets API error"
        )
        self._config.export_max_retries = 0

        result = self._service.process_export("drive001", "job001")
        assert result.status == ExportStatus.FAILED
        assert result.last_error is not None

    def test_process_export_no_cover_url(self):
        """Export proceeds without a cover when URL is None."""
        metadata = _make_metadata()
        self._metadata_repo.get_by_drive_file_id.return_value = metadata
        from models.validation_result import ValidationResult

        self._validation_service.validate.return_value = ValidationResult(
            drive_file_id="drive001", job_id="job001", is_valid=True, confidence=0.95
        )
        self._cover_resolver.resolve_cover_urls.return_value = []
        self._sheets_service.get_or_create_row.return_value = 99

        result = self._service.process_export("drive001", "job001")
        # Cover download should not be called when no URLs
        self._cover_downloader.download_covers.assert_not_called()
        assert result.status == ExportStatus.COMPLETED

    def test_process_export_returns_export_record(self):
        """Result is always an ExportRecord."""
        metadata = _make_metadata()
        self._metadata_repo.get_by_drive_file_id.return_value = metadata
        from models.validation_result import ValidationResult

        self._validation_service.validate.return_value = ValidationResult(
            drive_file_id="drive001", job_id="job001", is_valid=True, confidence=0.95
        )
        self._cover_resolver.resolve_cover_urls.return_value = [
            "http://example.com/cover.jpg"
        ]
        mock_cover = MagicMock()
        mock_cover.local_path = "data/covers/cover_drive001.jpg"
        mock_cover.cover_url = "http://example.com/cover.jpg"
        self._cover_downloader.download_covers.return_value = mock_cover
        self._sheets_service.get_or_create_row.return_value = 1

        result = self._service.process_export("drive001", "job001")
        assert isinstance(result, ExportRecord)
        assert result.drive_file_id == "drive001"

    def test_process_export_creates_queue_entry(self):
        """If no export queue entry exists, one is created."""
        metadata = _make_metadata()
        self._metadata_repo.get_by_drive_file_id.return_value = metadata
        from models.validation_result import ValidationResult

        self._validation_service.validate.return_value = ValidationResult(
            drive_file_id="drive001", job_id="job001", is_valid=True, confidence=0.95
        )
        self._cover_resolver.resolve_cover_urls.return_value = []
        self._sheets_service.get_or_create_row.return_value = 1

        # Verify a queue entry was created
        existing = self._export_repo.get_export_by_drive_file_id("drive001")
        assert existing is None

        self._service.process_export("drive001", "job001")

        existing = self._export_repo.get_export_by_drive_file_id("drive001")
        assert existing is not None
        assert existing["status"] == "completed"
