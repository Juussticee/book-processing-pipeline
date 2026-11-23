"""Unit tests for ExportRepository."""

import os
import tempfile

from models.book_metadata import BookMetadata
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.export_repository import ExportRepository


class TestExportRepository:
    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = ExportRepository(self._db_path)

    def teardown_method(self):
        import gc
        import time

        self._repo.close()
        gc.collect()
        for _ in range(5):
            try:
                if os.path.exists(self._db_path):
                    os.unlink(self._db_path)
                break
            except PermissionError:
                time.sleep(0.1)

    def test_create_export_record(self):
        record = self._repo.create_export_record("drive001", "job001")
        assert record is not None
        assert record.drive_file_id == "drive001"
        assert record.job_id == "job001"
        assert record.status.value == "pending"

    def test_create_duplicate_drive_file_id_returns_none(self):
        """Duplicate drive_file_id should return None (duplicate prevention)."""
        first = self._repo.create_export_record("drive001", "job001")
        assert first is not None
        second = self._repo.create_export_record("drive001", "job002")
        assert second is None

    def test_create_duplicate_job_id_returns_none(self):
        """Duplicate job_id should return None (duplicate prevention)."""
        first = self._repo.create_export_record("drive001", "job001")
        assert first is not None
        second = self._repo.create_export_record("drive002", "job001")
        assert second is None

    def test_get_pending_exports(self):
        self._repo.create_export_record("drive001", "job001")
        self._repo.create_export_record("drive002", "job002")
        pending = self._repo.get_pending_exports(limit=10)
        assert len(pending) == 2

    def test_get_pending_exports_empty(self):
        pending = self._repo.get_pending_exports()
        assert pending == []

    def test_get_export_by_drive_file_id(self):
        self._repo.create_export_record("driveABC", "jobABC")
        record = self._repo.get_export_by_drive_file_id("driveABC")
        assert record is not None
        assert record["drive_file_id"] == "driveABC"
        assert record["job_id"] == "jobABC"

    def test_get_export_by_drive_file_id_not_found(self):
        record = self._repo.get_export_by_drive_file_id("nonexistent")
        assert record is None

    def test_update_export_status(self):
        self._repo.create_export_record("drive001", "job001")
        updated = self._repo.update_export_status(
            "drive001", "completed", sheet_row_id="42"
        )
        assert updated is True
        record = self._repo.get_export_by_drive_file_id("drive001")
        assert record["status"] == "completed"

    def test_update_export_status_not_found(self):
        updated = self._repo.update_export_status("nonexistent", "failed")
        assert updated is False

    def test_increment_retry(self):
        self._repo.create_export_record("drive001", "job001")
        new_count = self._repo.increment_retry("drive001")
        assert new_count == 1

    def test_increment_retry_not_found(self):
        new_count = self._repo.increment_retry("nonexistent")
        assert new_count == -1

    def test_reset_retrying_exports(self):
        self._repo.create_export_record("drive001", "job001")
        self._repo.update_export_status("drive001", "retrying")
        count = self._repo.reset_retrying_exports()
        assert count == 1
        record = self._repo.get_export_by_drive_file_id("drive001")
        assert record["status"] == "pending"

    def test_empty_drive_file_id_raises(self):
        try:
            self._repo.create_export_record("", "job001")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_empty_job_id_raises(self):
        try:
            self._repo.create_export_record("drive001", "")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    # --- Crash recovery ---

    def test_recover_stuck_exports_resets_processing(self):
        """An export left PROCESSING by a crash returns to PENDING."""
        self._repo.create_export_record("drive001", "job001")
        self._repo.update_export_status("drive001", "processing")
        assert self._repo.get_pending_exports() == []

        recovered = self._repo.recover_stuck_exports(timeout_seconds=0)
        assert recovered == 1

        pending = self._repo.get_pending_exports()
        assert [row["drive_file_id"] for row in pending] == ["drive001"]

    def test_recover_stuck_exports_leaves_recent_jobs(self):
        """A recently started export (default timeout) is not stolen."""
        self._repo.create_export_record("drive002", "job002")
        self._repo.update_export_status("drive002", "processing")

        recovered = self._repo.recover_stuck_exports()
        assert recovered == 0
        assert self._repo.get_pending_exports() == []

    def test_recover_stuck_exports_ignores_completed(self):
        """Completed exports are never re-queued."""
        self._repo.create_export_record("drive003", "job003")
        self._repo.update_export_status("drive003", "completed", sheet_row_id="7")

        recovered = self._repo.recover_stuck_exports(timeout_seconds=0)
        assert recovered == 0
        assert self._repo.get_pending_exports() == []

    def test_recover_stuck_exports_leaves_pending_untouched(self):
        """A record queued but never started survives a restart unchanged."""
        self._repo.create_export_record("drive004", "job004")

        assert self._repo.recover_stuck_exports(timeout_seconds=0) == 0
        pending = self._repo.get_pending_exports()
        assert [row["drive_file_id"] for row in pending] == ["drive004"]

    def test_repeated_requeue_keeps_a_single_queue_row(self):
        """Requeueing the same book twice must not create a second job."""
        self._repo.create_export_record("drive006", "job006")
        self._repo.update_export_status("drive006", "completed", sheet_row_id="2")

        assert self._repo.requeue_export("drive006") is True
        assert self._repo.requeue_export("drive006") is True

        rows = self._repo._queue_conn.execute(
            "select count(*) from export_queue where drive_file_id = ?",
            ("drive006",),
        ).fetchone()[0]
        assert rows == 1
        assert self._repo.get_export_by_drive_file_id("drive006")["status"] == (
            "pending"
        )


class TestRequeueExports:
    """Explicit requeue path for records that finished incomplete (RC-8)."""

    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        # Production keeps export_queue in jobs.db and book_metadata in
        # metadata.db; a single file exercises the same code path.
        self._repo = ExportRepository(self._db_path)
        self._metadata_repo = BookMetadataRepository(self._db_path)

    def teardown_method(self):
        import gc
        import time

        self._metadata_repo.close()
        self._repo.close()
        gc.collect()
        for _ in range(5):
            try:
                if os.path.exists(self._db_path):
                    os.unlink(self._db_path)
                break
            except PermissionError:
                time.sleep(0.1)

    def _add_metadata(self, drive_file_id: str, job_id: str, **kwargs) -> None:
        self._metadata_repo.upsert_metadata(
            BookMetadata(
                title="Requeue Test Book",
                drive_file_id=drive_file_id,
                job_id=job_id,
                **kwargs,
            )
        )

    def _complete_export(self, drive_file_id: str, job_id: str) -> None:
        self._repo.create_export_record(drive_file_id, job_id)
        self._repo.update_export_status(drive_file_id, "completed", sheet_row_id="2")

    def test_requeue_export_resets_completed_record(self):
        """A completed export can be put back on the queue explicitly."""
        self._complete_export("drive001", "job001")
        assert self._repo.get_pending_exports() == []

        assert self._repo.requeue_export("drive001") is True

        pending = self._repo.get_pending_exports()
        assert [row["drive_file_id"] for row in pending] == ["drive001"]
        record = self._repo.get_export_by_drive_file_id("drive001")
        assert record["status"] == "pending"
        assert record["retry_count"] == 0
        assert record["last_error"] is None

    def test_requeue_export_unknown_id_returns_false(self):
        assert self._repo.requeue_export("missing") is False
        assert self._repo.requeue_export("") is False

    def test_requeue_incomplete_picks_missing_cover(self):
        """A finished export without a validated cover is resumable."""
        self._add_metadata(
            "drive001", "job001", isbn_13="9780306406157", cover_status="not_found"
        )
        self._complete_export("drive001", "job001")

        assert self._repo.requeue_incomplete_exports() == ["drive001"]
        pending = self._repo.get_pending_exports()
        assert [row["drive_file_id"] for row in pending] == ["drive001"]

    def test_resolved_export_is_not_requeued(self):
        """A book with a validated cover and an ISBN stays exported."""
        self._add_metadata(
            "drive002", "job002", isbn_13="9780306406157", cover_status="found"
        )
        self._complete_export("drive002", "job002")

        assert self._repo.requeue_incomplete_exports() == []
        assert self._repo.get_pending_exports() == []

    def test_missing_isbn_makes_record_incomplete(self):
        """The cover alone does not make a record complete."""
        self._add_metadata("drive005", "job005", cover_status="found")
        self._complete_export("drive005", "job005")

        assert self._repo.requeue_incomplete_exports() == ["drive005"]

    def test_in_flight_export_is_not_disturbed(self):
        """A record currently PROCESSING is left to the worker."""
        self._add_metadata("drive003", "job003", cover_status="pending_retry")
        self._repo.create_export_record("drive003", "job003")
        self._repo.update_export_status("drive003", "processing")

        assert self._repo.requeue_incomplete_exports() == []
        record = self._repo.get_export_by_drive_file_id("drive003")
        assert record["status"] == "processing"

    def test_metadata_without_queue_row_is_queued(self):
        """An identified book that never reached the queue is exported later."""
        self._add_metadata("drive004", "job004", cover_status="pending_retry")

        assert self._repo.requeue_incomplete_exports() == ["drive004"]
        record = self._repo.get_export_by_drive_file_id("drive004")
        assert record is not None
        assert record["status"] == "pending"

    def test_jobs_only_database_has_nothing_to_requeue(self):
        """Without a book_metadata table the requeue is a no-op."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        repo = ExportRepository(db_path)
        try:
            assert repo.requeue_incomplete_exports() == []
        finally:
            repo.close()
            os.unlink(db_path)
