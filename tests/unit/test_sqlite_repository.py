"""Test SQLite repository."""

import os
import tempfile

from models.drive_file import DriveFile
from repositories.sqlite_repository import SQLiteRepository


class TestSQLiteRepository:
    """Test SQLiteRepository operations."""

    def test_create_database(self) -> None:
        """Test that the repository creates the database and tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            repo = SQLiteRepository(db_path)
            assert os.path.exists(db_path)
            repo.close()

    def test_insert_and_check_file_exists(self) -> None:
        """Test inserting a file and checking if it exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            repo = SQLiteRepository(db_path)

            drive_file = DriveFile(
                file_id="file123",
                name="test.jpg",
                mime_type="image/jpeg",
                modified_time="2024-01-01T00:00:00.000Z",
                size=1024,
            )

            result = repo.insert_file(drive_file, "file123.jpg", "2024-01-01")
            assert result is True
            assert repo.file_exists("file123") is True
            repo.close()

    def test_insert_duplicate_file_returns_false(self) -> None:
        """Test that inserting a duplicate file returns False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            repo = SQLiteRepository(db_path)

            drive_file = DriveFile(
                file_id="file123",
                name="test.jpg",
                mime_type="image/jpeg",
                modified_time="2024-01-01T00:00:00.000Z",
                size=1024,
            )

            repo.insert_file(drive_file, "file123.jpg", "2024-01-01")
            result = repo.insert_file(drive_file, "file123.jpg", "2024-01-01")
            assert result is False
            repo.close()

    def test_get_all_file_ids(self) -> None:
        """Test retrieving all tracked file IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            repo = SQLiteRepository(db_path)

            drive_file = DriveFile(
                file_id="file123",
                name="test.jpg",
                mime_type="image/jpeg",
                modified_time="2024-01-01T00:00:00.000Z",
                size=1024,
            )

            repo.insert_file(drive_file, "file123.jpg", "2024-01-01")
            ids = repo.get_all_file_ids()
            assert "file123" in ids
            repo.close()

    def test_update_file_status(self) -> None:
        """Test updating a file's status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            repo = SQLiteRepository(db_path)

            drive_file = DriveFile(
                file_id="file123",
                name="test.jpg",
                mime_type="image/jpeg",
                modified_time="2024-01-01T00:00:00.000Z",
                size=1024,
            )

            repo.insert_file(drive_file, "file123.jpg", "2024-01-01")
            result = repo.update_file_status("file123", "processed")
            assert result is True
            repo.close()
