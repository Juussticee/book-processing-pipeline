"""Unit tests for CoverRepository."""

import os
import tempfile

from repositories.cover_repository import CoverRepository


class TestCoverRepository:
    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = CoverRepository(self._db_path)

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

    def test_insert_cover(self):
        self._repo.insert_cover(
            drive_file_id="drive001",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
            local_path="data/covers/cover_drive001.jpg",
            checksum="abc123",
            width=600,
            height=800,
            file_size=150000,
        )
        record = self._repo.get_cover_by_drive_file_id("drive001")
        assert record is not None
        assert record.drive_file_id == "drive001"
        assert record.width == 600
        assert record.height == 800
        assert record.checksum == "abc123"
        assert record.resolution == "600x800"

    def test_insert_duplicate_drive_file_id_replaces(self):
        """INSERT OR REPLACE should update instead of failing."""
        self._repo.insert_cover(
            drive_file_id="drive001",
            job_id="job001",
            cover_url="http://example.com/old.jpg",
            local_path="/old/path",
            checksum="old_checksum",
            width=600,
            height=800,
            file_size=150000,
        )
        self._repo.insert_cover(
            drive_file_id="drive001",
            job_id="job001",
            cover_url="http://example.com/new.jpg",
            local_path="/new/path",
            checksum="new_checksum",
            width=800,
            height=1000,
            file_size=200000,
        )
        record = self._repo.get_cover_by_drive_file_id("drive001")
        assert record.checksum == "new_checksum"
        assert record.width == 800

    def test_get_cover_by_drive_file_id_not_found(self):
        record = self._repo.get_cover_by_drive_file_id("nonexistent")
        assert record is None

    def test_update_cover_status(self):
        self._repo.insert_cover(
            drive_file_id="drive001",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
            local_path="data/covers/cover_drive001.jpg",
            checksum="abc123",
            width=600,
            height=800,
            file_size=150000,
        )
        updated = self._repo.update_cover_status("drive001", "failed", "Timeout")
        assert updated is True
        record = self._repo.get_cover_by_drive_file_id("drive001")
        assert record.status.value == "failed"
        assert record.error_message == "Timeout"

    def test_update_cover_status_not_found(self):
        updated = self._repo.update_cover_status("nonexistent", "failed")
        assert updated is False

    def test_get_by_checksum(self):
        self._repo.insert_cover(
            drive_file_id="drive001",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
            local_path="data/covers/cover_drive001.jpg",
            checksum="unique_hash_123",
            width=600,
            height=800,
            file_size=150000,
        )
        record = self._repo.get_by_checksum("unique_hash_123")
        assert record is not None
        assert record.drive_file_id == "drive001"

    def test_get_by_checksum_not_found(self):
        record = self._repo.get_by_checksum("nonexistent_hash")
        assert record is None
