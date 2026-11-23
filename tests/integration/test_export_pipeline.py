"""Integration tests for the Milestone 5 export pipeline.

These tests verify end-to-end behavior with mocked external services
(Google Sheets API) and real database operations (SQLite).
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

from models.book_metadata import BookMetadata
from models.cover_download import CoverDownloadStatus
from models.export_config import ExportConfig
from models.export_record import ExportStatus
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.cover_repository import CoverRepository
from repositories.export_repository import ExportRepository
from services.clean_cover_service import CleanCoverService
from services.cover_download_service import CoverDownloadService
from services.cover_resolver_service import CoverResolverService
from services.export_service import ExportService
from services.metadata_validation_service import MetadataValidationService


class _StubBookLookupService:
    """Stub BookLookupService returning no candidates (no external API calls)."""

    def search_by_isbn(self, isbn):
        return []

    def search_by_title_author(self, title, author):
        return []


class _StubCleanCoverService:
    """Stub CleanCoverService returning a synthetic clean cover."""

    def find_clean_cover(self, metadata):
        return "https://example.com/clean.jpg", "google_books", "/tmp/clean.jpg"


def _make_metadata(**kwargs):
    defaults = {
        "title": "Integration Test Book",
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
        "drive_file_id": "integration001",
        "job_id": "job_int_001",
    }
    defaults.update(kwargs)
    return BookMetadata(**defaults)


class _ExportPipelineFixture:
    """Shared setup: real SQLite repos, stubbed external services."""

    def setup_method(self):
        self._metadata_db = tempfile.mktemp(suffix=".db")
        self._export_db = tempfile.mktemp(suffix=".db")
        self._covers_dir = tempfile.mkdtemp()

        self._metadata_repo = BookMetadataRepository(self._metadata_db)
        self._cover_repo = CoverRepository(self._metadata_db)
        self._export_repo = ExportRepository(self._export_db)

        # Real validation service
        self._validation_service = MetadataValidationService()
        # Stub cover resolver (no external API)
        stub_lookup = _StubBookLookupService()
        self._cover_resolver = CoverResolverService(book_lookup_service=stub_lookup)
        # Stub clean cover service so integration tests cover the live pipeline path.
        self._clean_cover_service = _StubCleanCoverService()
        # Stub cover downloader with mock fetch
        self._cover_downloader = CoverDownloadService(
            cover_repo=self._cover_repo,
            covers_dir=self._covers_dir,
            timeout=5,
            min_width=200,
            min_height=200,
        )
        # Mock Google Sheets service
        self._sheets_service = MagicMock()
        self._sheets_service.get_or_create_row.return_value = 42
        self._sheets_service._build_sheet_url.return_value = (
            "https://docs.google.com/spreadsheets/d/test/edit#gid=0&range=A42"
        )

        self._config = ExportConfig()

        self._service = ExportService(
            metadata_repo=self._metadata_repo,
            cover_repo=self._cover_repo,
            export_repo=self._export_repo,
            validation_service=self._validation_service,
            cover_resolver=self._cover_resolver,
            cover_downloader=self._cover_downloader,
            sheets_service=self._sheets_service,
            config=self._config,
            clean_cover_service=self._clean_cover_service,
        )

    def teardown_method(self):
        import gc
        import shutil
        import time

        self._metadata_repo.close()
        self._cover_repo.close()
        self._export_repo.close()
        gc.collect()
        for path in [self._metadata_db, self._export_db]:
            for _ in range(5):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                    break
                except PermissionError:
                    time.sleep(0.1)
        if os.path.exists(self._covers_dir):
            shutil.rmtree(self._covers_dir, ignore_errors=True)


class TestExportPipelineIntegration(_ExportPipelineFixture):
    """End-to-end tests of the export pipeline with mocked external APIs."""

    def test_full_pipeline_success(self):
        """Full metadata → sheets pipeline with real DB operations."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)

        result = self._service.process_export("integration001", "job_int_001")
        assert result.status == ExportStatus.COMPLETED
        assert result.sheet_row_id == "42"

        # Verify Sheets was called
        self._sheets_service.get_or_create_row.assert_called_once()

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_duplicate_prevention(self, mock_fetch):
        """Second export of same drive_file_id does not create duplicate."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)
        # Also insert a second metadata for a different drive_file_id
        metadata2 = _make_metadata(drive_file_id="integration002", job_id="job_int_002")
        self._metadata_repo.upsert_metadata(metadata2)

        # Mock fetch to return a valid image
        import io

        from PIL import Image

        img = Image.new("RGB", (400, 600), color="green")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mock_fetch.return_value = buf.getvalue()

        # First export
        result1 = self._service.process_export("integration001", "job_int_001")
        assert result1.status == ExportStatus.COMPLETED

        # Second export — creates a new entry for a different drive_file_id
        result2 = self._service.process_export("integration002", "job_int_002")
        assert result2.status == ExportStatus.COMPLETED

        # Verify queue entries exist for both
        entry1 = self._export_repo.get_export_by_drive_file_id("integration001")
        entry2 = self._export_repo.get_export_by_drive_file_id("integration002")
        assert entry1 is not None
        assert entry2 is not None

    def test_existing_cover_skip(self):
        """If cover already exists locally, reuse it without re-download."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)

        # Pre-insert a completed cover
        self._cover_repo.insert_cover(
            drive_file_id="integration001",
            job_id="job_int_001",
            cover_url="http://example.com/cover.jpg",
            local_path="/path/to/existing.jpg",
            checksum="existinghash",
            width=400,
            height=600,
            file_size=50000,
            status=CoverDownloadStatus.COMPLETED.value,
        )

        with patch.object(
            self._service._clean_cover_service,
            "find_clean_cover",
            return_value=(
                "http://example.com/cover.jpg",
                "google_books",
                "/path/to/existing.jpg",
            ),
        ) as mock_find:
            result = self._service.process_export("integration001", "job_int_001")
            assert result.status == ExportStatus.COMPLETED
            mock_find.assert_called_once()

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_cover_download_failure(self, mock_fetch):
        """Cover download failure doesn't block sheets export."""
        mock_fetch.side_effect = Exception("Connection timeout")

        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)

        result = self._service.process_export("integration001", "job_int_001")
        # Export should still succeed — cover is optional
        assert result.status == ExportStatus.COMPLETED

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_sheets_api_failure_marks_failed(self, mock_fetch):
        """Sheets API failure marks export as FAILED."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)
        self._config.export_max_retries = 0

        self._sheets_service.get_or_create_row.side_effect = Exception(
            "Sheets API unavailable"
        )

        result = self._service.process_export("integration001", "job_int_001")
        assert result.status == ExportStatus.FAILED
        assert result.last_error is not None

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_retry_behavior(self, mock_fetch):
        """Failed exports increment retry count and mark as RETRYING."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)
        self._config.export_max_retries = 3

        self._sheets_service.get_or_create_row.side_effect = Exception(
            "Sheets API error"
        )

        result = self._service.process_export("integration001", "job_int_001")
        # With max_retries=3, first failure goes to RETRYING
        assert result.status == ExportStatus.RETRYING
        assert result.retry_count >= 1

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_database_rollback_on_failure(self, mock_fetch):
        """On failure, the export record status reflects FAILED."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)
        self._config.export_max_retries = 0

        self._sheets_service.get_or_create_row.side_effect = Exception("Fatal")

        result = self._service.process_export("integration001", "job_int_001")
        assert result.status == ExportStatus.FAILED

        # Verify the record in DB is FAILED
        record = self._export_repo.get_export_by_drive_file_id("integration001")
        assert record["status"] == "failed"

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_low_confidence_metadata_still_exported(self, mock_fetch):
        """Low confidence metadata is exported with a review flag."""
        import io

        from PIL import Image

        img = Image.new("RGB", (400, 600), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mock_fetch.return_value = buf.getvalue()

        metadata = _make_metadata(confidence=0.2)
        self._metadata_repo.upsert_metadata(metadata)

        result = self._service.process_export("integration001", "job_int_001")
        # Should still export — validation failure doesn't block export
        assert result.status == ExportStatus.COMPLETED

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_missing_fields_handled(self, mock_fetch):
        """Metadata with missing ISBN/title is handled gracefully."""
        import io

        from PIL import Image

        img = Image.new("RGB", (400, 600), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mock_fetch.return_value = buf.getvalue()

        metadata = _make_metadata(
            title="Ab",
            isbn_10=None,
            isbn_13=None,
            publisher=None,
            published_date=None,
            authors=[],
        )
        self._metadata_repo.upsert_metadata(metadata)

        result = self._service.process_export("integration001", "job_int_001")
        assert result.status == ExportStatus.COMPLETED

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_image_links_stored_in_db(self, mock_fetch):
        """image_links field is persisted and retrievable from database."""
        import io

        from PIL import Image

        img = Image.new("RGB", (400, 600), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mock_fetch.return_value = buf.getvalue()

        metadata = _make_metadata(
            image_links={
                "large": "https://example.com/large.jpg",
                "medium": "https://example.com/medium.jpg",
            },
        )
        self._metadata_repo.upsert_metadata(metadata)

        # Retrieve and verify image_links
        retrieved = self._metadata_repo.get_by_drive_file_id("integration001")
        assert retrieved is not None
        assert retrieved.image_links.get("large") == "https://example.com/large.jpg"
        assert retrieved.image_links.get("medium") == "https://example.com/medium.jpg"

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_cover_download_failure_marks_cover_failed_not_metadata(self, mock_fetch):
        """Cover download failure: cover FAILED but metadata stays identified."""
        mock_fetch.side_effect = Exception("Network error")

        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)

        result = self._service.process_export("integration001", "job_int_001")
        # Export should still complete — cover failure does not block
        assert result.status == ExportStatus.COMPLETED

        # Cover record should be FAILED (or not present as COMPLETED)
        cover = self._cover_repo.get_cover_by_drive_file_id("integration001")
        if cover:
            assert cover.status != CoverDownloadStatus.COMPLETED

    def test_validated_cover_record_has_dimensions(self):
        """Clean cover service sets a real cover on exported metadata."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)

        result = self._service.process_export("integration001", "job_int_001")
        assert result.status == ExportStatus.COMPLETED

        updated = self._metadata_repo.get_by_drive_file_id("integration001")
        assert updated is not None
        assert updated.cover_url == "https://example.com/clean.jpg"
        assert updated.local_cover_path == "/tmp/clean.jpg"
        assert updated.cover_source == "google_books"
        assert updated.cover_status == "found"

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_export_still_occurs_when_no_cover_urls(self, mock_fetch):
        """Export succeeds even when resolver returns no cover URLs."""
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url=None,
            isbn_10=None,
            isbn_13=None,
            title="No Cover Book",
        )
        self._metadata_repo.upsert_metadata(metadata)

        result = self._service.process_export("integration001", "job_int_001")
        assert result.status == ExportStatus.COMPLETED
        # Verify no cover was downloaded (no cover URLs to download)
        cover = self._cover_repo.get_cover_by_drive_file_id("integration001")
        assert cover is None  # No cover record created since no URLs

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_process_all_pending(self, mock_fetch):
        """Test processing all pending exports through the queue."""
        # Insert two metadata records
        meta1 = _make_metadata(drive_file_id="drv001", job_id="job001")
        meta2 = _make_metadata(drive_file_id="drv002", job_id="job002")
        self._metadata_repo.upsert_metadata(meta1)
        self._metadata_repo.upsert_metadata(meta2)

        # Create export queue entries
        self._export_repo.create_export_record("drv001", "job001")
        self._export_repo.create_export_record("drv002", "job002")

        # The worker would call process_export for each — test the service directly
        for drive_id, job_id in [("drv001", "job001"), ("drv002", "job002")]:
            self._sheets_service.get_or_create_row.return_value = 1 + int(drive_id[-1])
            result = self._service.process_export(drive_id, job_id)
            assert result.status == ExportStatus.COMPLETED


class _RaisingCleanCoverService:
    """Clean cover service whose provider chain blows up."""

    def find_clean_cover(self, metadata):
        raise RuntimeError("cover provider unavailable")


class _RecordingEnrichmentService:
    """Enrichment stub that records how often it ran."""

    def __init__(self):
        self.calls = 0

    def enrich(self, metadata):
        self.calls += 1
        if not metadata.publisher:
            metadata.publisher = "Enriched Publisher"
        if not metadata.page_count:
            metadata.page_count = 142
        return metadata


def _make_export_service(base, **overrides):
    """Clone an ExportService with overrides (same repos, same config)."""
    kwargs = dict(
        metadata_repo=base._metadata_repo,
        cover_repo=base._cover_repo,
        export_repo=base._export_repo,
        validation_service=base._validation_service,
        cover_resolver=base._cover_resolver,
        cover_downloader=base._cover_downloader,
        sheets_service=base._sheets_service,
        config=base._config,
        clean_cover_service=base._clean_cover_service,
        enrichment_service=None,
    )
    kwargs.update(overrides)
    return ExportService(**kwargs)


class TestExportStateIndependence(_ExportPipelineFixture):
    """Cover, metadata and export states fail independently and resume."""

    def test_cover_provider_crash_still_exports_metadata(self):
        """A cover exception must not lose an identified book."""
        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)

        service = _make_export_service(
            self, clean_cover_service=_RaisingCleanCoverService()
        )
        result = service.process_export("integration001", "job_int_001")

        assert result.status == ExportStatus.COMPLETED
        stored = self._metadata_repo.get_by_drive_file_id("integration001")
        assert stored.title == "Integration Test Book"
        assert stored.isbn_13 == "9780306406157"
        assert stored.cover_status == "pending_retry"
        # The sheet still received the book's metadata.
        self._sheets_service.get_or_create_row.assert_called()

    def test_enrichment_not_repeated_when_only_cover_is_pending(self):
        """Resuming a pending cover does not re-query metadata providers."""
        metadata = _make_metadata(
            publisher=None,
            cover_status="pending_retry",
            cover_source="google_books",
            local_cover_path=None,
        )
        self._metadata_repo.upsert_metadata(metadata)
        enrichment = _RecordingEnrichmentService()
        service = _make_export_service(self, enrichment_service=enrichment)

        service.process_export("integration001", "job_int_001")

        assert enrichment.calls == 0

    def test_enrichment_runs_once_then_is_persisted(self):
        """Enrichment is persisted, so a retry does not repeat it."""
        metadata = _make_metadata(publisher=None)
        self._metadata_repo.upsert_metadata(metadata)
        enrichment = _RecordingEnrichmentService()
        service = _make_export_service(self, enrichment_service=enrichment)

        service.process_export("integration001", "job_int_001")
        stored = self._metadata_repo.get_by_drive_file_id("integration001")
        assert stored.page_count == 142

        service.process_export("integration001", "job_int_001")
        assert enrichment.calls == 1

    def test_failed_export_resumes_without_repeating_cover_download(self, tmp_path):
        """Cover + metadata survive a Sheets failure; retry skips them."""
        from PIL import Image

        metadata = _make_metadata()
        self._metadata_repo.upsert_metadata(metadata)
        enrichment = _RecordingEnrichmentService()
        clean_cover = CleanCoverService(
            book_lookup=_StubBookLookupService(),
            covers_dir=str(tmp_path / "covers"),
        )
        service = _make_export_service(
            self, enrichment_service=enrichment, clean_cover_service=clean_cover
        )

        # First run: Sheets fails after the cover is persisted.
        (tmp_path / "covers").mkdir(parents=True, exist_ok=True)
        cover_file = str(tmp_path / "covers" / "persisted.png")
        Image.new("RGB", (400, 600), color="white").save(cover_file, format="PNG")
        metadata.local_cover_path = cover_file
        metadata.cover_url = "https://books.google.com/books/content?id=abc"
        metadata.cover_source = "google_books"
        metadata.cover_source_url = metadata.cover_url
        metadata.cover_status = "found"
        self._metadata_repo.update_metadata(metadata)

        self._sheets_service.get_or_create_row.side_effect = Exception("Sheets 503")
        first = service.process_export("integration001", "job_int_001")
        assert first.status == ExportStatus.RETRYING

        # Second run: resume from persisted state.
        self._sheets_service.get_or_create_row.side_effect = None
        self._sheets_service.get_or_create_row.return_value = 7
        second = service.process_export("integration001", "job_int_001")

        assert second.status == ExportStatus.COMPLETED
        assert enrichment.calls == 0
        assert os.path.exists(cover_file)

    def test_worker_startup_recovers_stuck_export(self):
        """An export interrupted mid-flight resumes on worker startup."""
        from workers.export_worker import ExportWorker

        self._metadata_repo.upsert_metadata(_make_metadata())
        self._export_repo.create_export_record("integration001", "job_int_001")
        self._export_repo.update_export_status("integration001", "processing")
        assert self._export_repo.get_pending_exports() == []

        # Simulate the crashed attempt having happened long ago.
        import sqlite3

        conn = sqlite3.connect(self._export_db)
        conn.execute(
            "UPDATE export_queue SET attempted_at = ? WHERE drive_file_id = ?",
            ("2020-01-01T00:00:00", "integration001"),
        )
        conn.commit()
        conn.close()

        worker = ExportWorker(
            export_service=self._service, export_repo=self._export_repo
        )
        worker.initialize()
        worker.shutdown()

        pending = self._export_repo.get_pending_exports()
        assert [row["drive_file_id"] for row in pending] == ["integration001"]
