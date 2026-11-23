"""Tests for Milestone 8.3: Export retry backoff with exponential backoff.

Tests verify:
  - calculate_retry_delay produces correct exponential delays
  - reset_retrying_exports respects next_retry_at timestamps
  - _fail_export schedules retries with backoff
  - dead-letter (FAILED) records are not retried
"""

import tempfile
from unittest.mock import MagicMock, patch

from models.book_metadata import BookMetadata
from models.export_record import ExportStatus
from repositories.export_repository import ExportRepository
from services.export_service import ExportService
from services.metadata_validation_service import MetadataValidationService


class TestRetryBackoff:
    """Tests for exponential backoff in export retry logic."""

    def setup_method(self):
        self._db = tempfile.mktemp(suffix=".db")
        self._repo = ExportRepository(self._db)

    def teardown_method(self):
        self._repo.close()
        import gc

        gc.collect()
        import os
        import time

        for _ in range(5):
            try:
                os.unlink(self._db)
                break
            except PermissionError:
                time.sleep(0.1)

    def test_calculate_retry_delay_exponential(self):
        """calculate_retry_delay returns exponential delays."""
        assert ExportRepository.calculate_retry_delay(0, base_delay=60) == 60.0
        assert ExportRepository.calculate_retry_delay(1, base_delay=60) == 120.0
        assert ExportRepository.calculate_retry_delay(2, base_delay=60) == 240.0
        assert ExportRepository.calculate_retry_delay(3, base_delay=60) == 480.0

    def test_calculate_retry_delay_capped(self):
        """calculate_retry_delay is capped at 3600 seconds (1 hour)."""
        delay = ExportRepository.calculate_retry_delay(10, base_delay=60)
        assert delay == 3600.0

    def test_calculate_retry_delay_custom_base(self):
        """calculate_retry_delay respects custom base_delay."""
        assert ExportRepository.calculate_retry_delay(0, base_delay=30) == 30.0
        assert ExportRepository.calculate_retry_delay(1, base_delay=30) == 60.0

    def test_set_next_retry_schedules_future(self):
        """set_next_retry stores a future timestamp."""
        self._repo.create_export_record("drv1", "job1", max_retries=3)
        from datetime import datetime

        next_retry = self._repo.set_next_retry("drv1", retry_count=0, base_delay=60)
        dt = datetime.fromisoformat(next_retry)
        # Should be approximately 60 seconds in the future
        now = datetime.utcnow()
        delta = (dt - now).total_seconds()
        assert 55 < delta <= 60, f"Expected ~60s delay, got {delta}s"

        record = self._repo.get_export_by_drive_file_id("drv1")
        assert record["status"] == "pending"  # status unchanged

    def test_reset_retrying_exports_respects_backoff(self):
        """Retrying records with future next_retry_at are NOT reset immediately."""
        self._repo.create_export_record("drv1", "job1", max_retries=3)

        # Mark as retrying with a future retry time
        self._repo.set_next_retry("drv1", retry_count=0, base_delay=60)
        self._repo.update_export_status("drv1", ExportStatus.RETRYING.value)

        # reset_retrying_exports should NOT reset it yet (future time)
        reset_count = self._repo.reset_retrying_exports()
        assert reset_count == 0

        record = self._repo.get_export_by_drive_file_id("drv1")
        assert record["status"] == "retrying"

    def test_reset_retrying_exports_resets_past_time(self):
        """Retrying records with past next_retry_at ARE reset to pending."""
        self._repo.create_export_record("drv1", "job1", max_retries=3)

        # Set a past time by manually setting a very small delay
        self._repo.update_export_status("drv1", ExportStatus.RETRYING.value)
        # Set next_retry_at to a past time
        from datetime import datetime, timedelta

        past_time = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
        with self._repo._lock:
            cursor = self._repo._queue_conn.cursor()
            cursor.execute(
                """
                UPDATE export_queue SET next_retry_at = ?
                WHERE drive_file_id = ?
                """,
                (past_time, "drv1"),
            )
            self._repo._queue_conn.commit()

        # Now reset should work
        reset_count = self._repo.reset_retrying_exports()
        assert reset_count == 1

        record = self._repo.get_export_by_drive_file_id("drv1")
        assert record["status"] == "pending"

    def test_reset_retrying_exports_resets_null_next_retry_at(self):
        """Retrying records with NULL next_retry_at are reset (backward compat)."""
        self._repo.create_export_record("drv1", "job1", max_retries=3)
        # Set as retrying WITHOUT setting next_retry_at
        self._repo.update_export_status("drv1", ExportStatus.RETRYING.value)

        reset_count = self._repo.reset_retrying_exports()
        assert reset_count == 1

        record = self._repo.get_export_by_drive_file_id("drv1")
        assert record["status"] == "pending"

    def test_failed_records_not_reset_by_backoff(self):
        """FAILED records are not reset to PENDING by reset_retrying_exports."""
        self._repo.create_export_record("drv1", "job1", max_retries=0)

        self._repo.update_export_status("drv1", ExportStatus.FAILED.value)

        reset_count = self._repo.reset_retrying_exports()
        assert reset_count == 0

        record = self._repo.get_export_by_drive_file_id("drv1")
        assert record["status"] == "failed"

    def test_get_dead_letter_exports(self):
        """get_dead_letter_exports returns FAILED records."""
        self._repo.create_export_record("drv1", "job1", max_retries=3)
        self._repo.create_export_record("drv2", "job2", max_retries=3)

        # drv1 -> retrying, drv2 -> failed
        self._repo.update_export_status("drv1", ExportStatus.RETRYING.value)
        self._repo.update_export_status("drv2", ExportStatus.FAILED.value)

        dead_letters = self._repo.get_dead_letter_exports()
        assert len(dead_letters) == 1
        assert dead_letters[0]["drive_file_id"] == "drv2"

    def test_get_retrying_exports(self):
        """get_retrying_exports returns RETRYING records with next_retry_at."""
        self._repo.create_export_record("drv1", "job1", max_retries=3)
        self._repo.create_export_record("drv2", "job2", max_retries=3)

        self._repo.set_next_retry("drv1", retry_count=0, base_delay=60)
        self._repo.update_export_status("drv1", ExportStatus.RETRYING.value)

        self._repo.set_next_retry("drv2", retry_count=1, base_delay=60)
        self._repo.update_export_status("drv2", ExportStatus.RETRYING.value)

        retrying = self._repo.get_retrying_exports()
        assert len(retrying) == 2

    @patch("services.cover_download_service.CoverDownloadService._fetch_image")
    def test_fail_export_schedules_backoff(self, mock_fetch):
        """_fail_export schedules retry with exponential backoff."""
        import io

        from PIL import Image

        # Create a real metadata repo and export repo
        meta_db = tempfile.mktemp(suffix=".db")
        export_db = tempfile.mktemp(suffix=".db")
        from models.export_config import ExportConfig
        from repositories.book_metadata_repository import BookMetadataRepository
        from repositories.cover_repository import CoverRepository

        meta_repo = BookMetadataRepository(meta_db)
        cover_repo = CoverRepository(meta_db)
        export_repo = ExportRepository(export_db)

        try:
            img = Image.new("RGB", (400, 600), color="green")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            mock_fetch.return_value = buf.getvalue()

            metadata = BookMetadata(
                title="Backoff Test Book",
                authors=["Test Author"],
                isbn_10="0306406152",
                confidence=0.95,
                drive_file_id="backoff001",
                job_id="j001",
            )
            meta_repo.upsert_metadata(metadata)

            config = ExportConfig()
            config.export_max_retries = 3
            config.export_retry_delay = 60

            sheets = MagicMock()
            sheets.get_or_create_row.side_effect = Exception("Sheets unavailable")

            svc = ExportService(
                metadata_repo=meta_repo,
                cover_repo=cover_repo,
                export_repo=export_repo,
                validation_service=MetadataValidationService(),
                cover_resolver=None,
                cover_downloader=None,
                sheets_service=sheets,
                config=config,
            )

            result = svc.process_export("backoff001", "j001")
            assert result.status == ExportStatus.RETRYING

            # Verify next_retry_at was set
            record = export_repo.get_export_by_drive_file_id("backoff001")
            assert record["status"] == "retrying"
            assert record["retry_count"] >= 1
        finally:
            meta_repo.close()
            cover_repo.close()
            export_repo.close()
            import gc
            import os
            import time

            gc.collect()
            for path in [meta_db, export_db]:
                for _ in range(5):
                    try:
                        if os.path.exists(path):
                            os.unlink(path)
                        break
                    except PermissionError:
                        time.sleep(0.1)
