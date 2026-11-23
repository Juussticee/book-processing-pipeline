"""Integration tests for Google Drive sync engine.

These tests mock the Google Drive API at the class level so no real API
calls are made.  Each test exercises a specific scenario in the sync loop
(app.run_sync_engine) using a temporary SQLite database and a temporary
downloads directory.
"""

from __future__ import annotations

import os
import tempfile
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from app import run_sync_engine
from core.config import Config
from models.drive_file import DriveFile
from repositories.sqlite_repository import SQLiteRepository
from services.drive_service import DriveService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_drive_file(
    file_id: str,
    name: str = "test.jpg",
    mime_type: str = "image/jpeg",
    modified_time: str = "2024-01-01T00:00:00.000Z",
    size: Optional[int] = 1024,
    md5_checksum: Optional[str] = None,
) -> DriveFile:
    """Create a DriveFile for test fixtures."""
    return DriveFile(
        file_id=file_id,
        name=name,
        mime_type=mime_type,
        modified_time=modified_time,
        size=size,
        md5_checksum=md5_checksum,
        web_view_link=None,
    )


def _make_config(
    tmpdir: str,
    folder_id: str = "test_folder_id",
) -> Config:
    """Build a Config with temp dirs."""
    return Config(
        app_name="Book Processor",
        version="0.1.0",
        environment="Development",
        log_level="INFO",
        data_directory=tmpdir,
        log_directory=os.path.join(tmpdir, "logs"),
        temp_directory=os.path.join(tmpdir, "temp"),
        google_drive_folder_id=folder_id,
        google_sheet_id="",
        google_service_account_file="",
        ocr_language="en",
        google_books_api_key="",
    )


def _run_sync_with_repo(
    config: Config,
    logger: MagicMock,
    drive_files: List[DriveFile],
    download_mock: MagicMock,
) -> SQLiteRepository:
    """Run one sync cycle and return the repository for assertions.

    run_sync_engine runs an infinite loop.  We inject a mocked DriveService
    and patch time.sleep to raise SystemExit, breaking the loop after one cycle.
    """
    db_path = os.path.join(config.data_directory, "app.db")
    os.makedirs(os.path.join(config.data_directory, "downloads"), exist_ok=True)

    service = DriveService(config)
    service.authenticate = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._service = MagicMock()  # type: ignore[assignment]

    service.list_all_files = MagicMock(return_value=drive_files)  # type: ignore[method-assign]
    service.download_file = download_mock  # type: ignore[method-assign]

    # Patch time.sleep to raise SystemExit after the first sleep, which
    # breaks the infinite loop in run_sync_engine after one cycle.
    with patch("services.drive_service.DriveService", return_value=service):
        with patch("app.time.sleep") as mock_sleep:
            mock_sleep.side_effect = SystemExit("Sync cycle complete")
            with pytest.raises(SystemExit, match="Sync cycle complete"):
                run_sync_engine(config, logger)
    # Ensure SQLite file handles are released (Windows file locking).
    # Daemon threads may still hold DB references; gc.collect forces
    # cleanup of any lingering objects.
    import gc
    import time

    for _ in range(5):
        gc.collect()
        time.sleep(0.05)

    return SQLiteRepository(db_path)


# ---------------------------------------------------------------------------
# Test: Empty folder
# ---------------------------------------------------------------------------


class TestSyncEmptyFolder:
    """Test sync when the Drive folder is empty."""

    def test_empty_folder_no_downloads(self) -> None:
        """No files should be downloaded from an empty folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            download_mock = MagicMock(return_value=True)

            repo = _run_sync_with_repo(config, MagicMock(), [], download_mock)

            assert download_mock.call_count == 0
            assert len(repo.get_all_file_ids()) == 0
            repo.close()


# ---------------------------------------------------------------------------
# Test: New file detection
# ---------------------------------------------------------------------------


class TestSyncNewFileDetection:
    """Test that new files are detected and downloaded."""

    def test_new_file_is_downloaded_and_tracked(self) -> None:
        """A new file should be downloaded and recorded in SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            drive_files = [_make_drive_file("file1", "book1.jpg")]
            download_mock = MagicMock(return_value=True)

            repo = _run_sync_with_repo(config, MagicMock(), drive_files, download_mock)

            assert download_mock.call_count == 1
            downloaded_arg = download_mock.call_args[0][0]
            assert downloaded_arg.file_id == "file1"
            assert "file1" in repo.get_all_file_ids()
            repo.close()


# ---------------------------------------------------------------------------
# Test: Multiple files
# ---------------------------------------------------------------------------


class TestSyncMultipleFiles:
    """Test sync with multiple new files."""

    def test_multiple_files_downloaded(self) -> None:
        """All new files should be downloaded and tracked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            drive_files = [
                _make_drive_file("file1", "book1.jpg"),
                _make_drive_file("file2", "book2.jpg"),
                _make_drive_file("file3", "book3.jpg"),
            ]
            download_mock = MagicMock(return_value=True)

            repo = _run_sync_with_repo(config, MagicMock(), drive_files, download_mock)

            assert download_mock.call_count == 3
            ids = repo.get_all_file_ids()
            assert "file1" in ids
            assert "file2" in ids
            assert "file3" in ids
            repo.close()


# ---------------------------------------------------------------------------
# Test: Duplicate prevention
# ---------------------------------------------------------------------------


class TestSyncDuplicatePrevention:
    """Test that already-tracked files are not re-downloaded."""

    def test_already_tracked_files_skipped(self) -> None:
        """Files already in SQLite should not be re-downloaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)

            # Pre-populate the repo with one already-tracked file
            db_path = os.path.join(config.data_directory, "app.db")
            os.makedirs(os.path.join(config.data_directory, "downloads"), exist_ok=True)
            repo = SQLiteRepository(db_path)
            existing_file = _make_drive_file("file1", "book1.jpg")
            repo.insert_file(existing_file, "file1.jpg", "2024-01-01")
            repo.close()

            # Drive returns the same file plus a new one
            drive_files = [
                _make_drive_file("file1", "book1.jpg"),
                _make_drive_file("file2", "book2.jpg"),
            ]
            download_mock = MagicMock(return_value=True)

            repo = _run_sync_with_repo(config, MagicMock(), drive_files, download_mock)

            # Only file2 should be downloaded (file1 already tracked)
            assert download_mock.call_count == 1
            downloaded_arg = download_mock.call_args[0][0]
            assert downloaded_arg.file_id == "file2"

            ids = repo.get_all_file_ids()
            assert "file1" in ids
            assert "file2" in ids
            repo.close()


# ---------------------------------------------------------------------------
# Test: Restart recovery
# ---------------------------------------------------------------------------


class TestSyncRestartRecovery:
    """Test that the sync engine recovers correctly after a restart.

    Simulates a restart by pre-populating the SQLite database with
    previously downloaded files, then running a sync cycle.
    """

    def test_restart_does_not_redownload_tracked_files(self) -> None:
        """After restart, already-downloaded files should not be re-downloaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)

            db_path = os.path.join(config.data_directory, "app.db")
            os.makedirs(os.path.join(config.data_directory, "downloads"), exist_ok=True)

            # Simulate a previous sync that downloaded 2 files
            repo = SQLiteRepository(db_path)
            repo.insert_file(
                _make_drive_file("file1", "book1.jpg"), "file1.jpg", "2024-01-01"
            )
            repo.insert_file(
                _make_drive_file("file2", "book2.jpg"), "file2.jpg", "2024-01-01"
            )
            repo.close()

            # Drive still returns both files plus a new third file
            drive_files = [
                _make_drive_file("file1", "book1.jpg"),
                _make_drive_file("file2", "book2.jpg"),
                _make_drive_file("file3", "book3.jpg"),
            ]
            download_mock = MagicMock(return_value=True)

            repo = _run_sync_with_repo(config, MagicMock(), drive_files, download_mock)

            # Only the new file3 should be downloaded
            assert download_mock.call_count == 1
            downloaded_arg = download_mock.call_args[0][0]
            assert downloaded_arg.file_id == "file3"

            ids = repo.get_all_file_ids()
            assert "file3" in ids
            repo.close()


# ---------------------------------------------------------------------------
# Test: File rename with same Drive ID
# ---------------------------------------------------------------------------


class TestSyncFileRename:
    """Test that renaming a file in Drive (same ID) does not trigger re-download."""

    def test_renamed_file_not_redownloaded(self) -> None:
        """A file renamed in Drive (same ID) should not trigger re-download."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)

            db_path = os.path.join(config.data_directory, "app.db")
            os.makedirs(os.path.join(config.data_directory, "downloads"), exist_ok=True)

            # Pre-populate with file1 under its old name
            repo = SQLiteRepository(db_path)
            repo.insert_file(
                _make_drive_file("file1", "old_name.jpg"), "file1.jpg", "2024-01-01"
            )
            repo.close()

            # Drive now returns the file with a renamed title but same ID
            drive_files = [
                _make_drive_file("file1", "renamed_book.jpg"),
            ]
            download_mock = MagicMock(return_value=True)

            repo = _run_sync_with_repo(config, MagicMock(), drive_files, download_mock)

            # Should NOT download -- same Drive ID means already tracked
            assert download_mock.call_count == 0
            repo.close()


# ---------------------------------------------------------------------------
# Test: Pagination
# ---------------------------------------------------------------------------


class TestSyncPagination:
    """Test that the sync engine handles paginated Drive API responses."""

    def test_pagination_lists_all_files(self) -> None:
        """All files across multiple pages should be detected and downloaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)

            service = DriveService(config)
            service.authenticate = MagicMock(return_value=True)  # type: ignore[method-assign]
            service._service = MagicMock()  # type: ignore[assignment]

            page1_files = [
                _make_drive_file("file1", "book1.jpg"),
                _make_drive_file("file2", "book2.jpg"),
            ]
            page2_files = [
                _make_drive_file("file3", "book3.jpg"),
                _make_drive_file("file4", "book4.jpg"),
            ]

            call_count = [0]

            def list_files_side_effect(
                page_size: int = 100, page_token: Optional[str] = None
            ) -> tuple:
                call_count[0] += 1
                if call_count[0] == 1:
                    return page1_files, "token_page2"
                elif call_count[0] == 2:
                    return page2_files, None
                raise StopIteration("pagination exhausted")

            service.list_files = MagicMock(side_effect=list_files_side_effect)  # type: ignore[method-assign]
            download_mock = MagicMock(return_value=True)
            service.download_file = download_mock  # type: ignore[method-assign]

            db_path = os.path.join(config.data_directory, "app.db")
            os.makedirs(os.path.join(config.data_directory, "downloads"), exist_ok=True)

            with patch("services.drive_service.DriveService", return_value=service):
                with patch("app.time.sleep") as mock_sleep:
                    mock_sleep.side_effect = SystemExit("done")
                    with pytest.raises(SystemExit, match="done"):
                        run_sync_engine(config, MagicMock())
            # Ensure SQLite file handles are released
            import gc
            import time

            for _ in range(5):
                gc.collect()
                time.sleep(0.05)

            repo = SQLiteRepository(db_path)
            try:
                ids = repo.get_all_file_ids()
                assert "file1" in ids
                assert "file2" in ids
                assert "file3" in ids
                assert "file4" in ids
                assert download_mock.call_count == 4
            finally:
                repo.close()

    def test_list_all_files_accumulates_paginated_results(self) -> None:
        """list_all_files should return all files across pages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            service = DriveService(config)
            service._service = MagicMock()  # type: ignore[assignment]

            page1 = [_make_drive_file("f1"), _make_drive_file("f2")]
            page2 = [_make_drive_file("f3")]

            call_count = [0]

            def list_files_side(
                page_size: int = 100, page_token: Optional[str] = None
            ) -> tuple:
                call_count[0] += 1
                if call_count[0] == 1:
                    return page1, "token2"
                return page2, None

            service.list_files = MagicMock(side_effect=list_files_side)  # type: ignore[method-assign]

            all_files = service.list_all_files()

            assert len(all_files) == 3
            assert all_files[0].file_id == "f1"
            assert all_files[1].file_id == "f2"
            assert all_files[2].file_id == "f3"


# ---------------------------------------------------------------------------
# Test: Download retry
# ---------------------------------------------------------------------------


class TestSyncDownloadRetry:
    """Test that the sync engine's download retry logic works."""

    def test_download_failure_does_not_crash_sync(self) -> None:
        """If a download fails permanently, other files should still be processed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            drive_files = [
                _make_drive_file("file1", "book1.jpg"),
                _make_drive_file("file2", "book2.jpg"),
            ]

            call_count = [0]

            def download_side_effect(drive_file: DriveFile, destination: str) -> bool:
                call_count[0] += 1
                if drive_file.file_id == "file1":
                    raise Exception("Download failed")
                return True

            download_mock = MagicMock(side_effect=download_side_effect)
            repo = _run_sync_with_repo(config, MagicMock(), drive_files, download_mock)

            # file1 download failed, file2 should still be downloaded
            assert download_mock.call_count == 2
            # file2 should be tracked, file1 should not (download failed)
            ids = repo.get_all_file_ids()
            assert "file2" in ids
            assert "file1" not in ids
            repo.close()

    def test_download_retry_on_transient_error(self) -> None:
        """DriveService.download_file should retry on transient HTTP errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            drive_file = _make_drive_file("file1", "book1.jpg")

            service = DriveService(config)
            service._service = MagicMock()  # type: ignore[assignment]

            from googleapiclient.errors import HttpError

            resp = MagicMock()
            resp.status = 503
            resp.reason = "Service Unavailable"
            http_error = HttpError(resp, b"error body")

            mock_status = MagicMock()
            mock_status.progress.return_value = 1.0

            attempt_count = [0]

            def next_chunk_side_effect():
                attempt_count[0] += 1
                if attempt_count[0] < 3:
                    raise http_error
                return (mock_status, True)

            with patch("services.drive_service.time.sleep"):
                with patch(
                    "services.drive_service.MediaIoBaseDownload"
                ) as mock_download_cls:
                    mock_download_instance = MagicMock()
                    mock_download_instance.next_chunk.side_effect = (
                        next_chunk_side_effect
                    )
                    mock_download_cls.return_value = mock_download_instance

                    dest = os.path.join(tmpdir, "file1.jpg")
                    result = service.download_file(drive_file, dest, max_retries=3)

                    assert result is True
                    assert attempt_count[0] == 3
                    assert os.path.exists(dest)
