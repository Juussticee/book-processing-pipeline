"""Test Google Drive service."""

import tempfile
from unittest.mock import MagicMock, patch

import pytest

from models.drive_file import DriveFile
from services.drive_service import DriveService


def _make_http_error(status: int, reason: str = "Error") -> Exception:
    """Create a realistic HttpError mock with resp.status and reason."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    return HttpError(resp, b"error body")  # type: ignore[no-any-return]


class TestDriveServiceAuthentication:
    """Test DriveService authentication."""

    def test_authenticate_with_valid_credentials(self) -> None:
        """Test that authenticate returns True with valid credentials file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import json
            import os

            creds_file = os.path.join(tmpdir, "service_account.json")
            with open(creds_file, "w") as f:
                json.dump({"type": "service_account", "project_id": "test"}, f)

            config = MagicMock()
            config.google_service_account_file = creds_file
            config.google_drive_folder_id = "test_folder_id"

            with patch(
                "services.drive_service.service_account.Credentials"
            ) as mock_creds:
                with patch("services.drive_service.build") as mock_build:
                    mock_creds.from_service_account_file.return_value = MagicMock()
                    mock_build.return_value = MagicMock()
                    service = DriveService(config)
                    result = service.authenticate()
                    assert result is True

    def test_authenticate_missing_credentials_file(self) -> None:
        """Test that authenticate returns False when credentials file is missing."""
        config = MagicMock()
        config.google_service_account_file = "/nonexistent/path.json"

        service = DriveService(config)
        result = service.authenticate()
        assert result is False


class TestDriveServiceListFiles:
    """Test DriveService file listing."""

    def test_list_files_returns_drive_files(self) -> None:
        """Test that list_files returns DriveFile instances."""
        config = MagicMock()
        config.google_drive_folder_id = "test_folder_id"

        service = DriveService(config)
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.execute.return_value = {
            "files": [
                {
                    "id": "file1",
                    "name": "test.jpg",
                    "mimeType": "image/jpeg",
                    "modifiedTime": "2024-01-01T00:00:00.000Z",
                    "size": "12345",
                }
            ]
        }
        mock_service.files.return_value.list.return_value = mock_files
        service._service = mock_service  # type: ignore[assignment]

        files, next_token = service.list_files()
        assert len(files) == 1
        assert files[0].file_id == "file1"
        assert files[0].name == "test.jpg"
        assert next_token is None

    def test_list_files_with_pagination(self) -> None:
        """Test that list_files handles pagination tokens."""
        config = MagicMock()
        config.google_drive_folder_id = "test_folder_id"

        service = DriveService(config)
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.execute.return_value = {
            "files": [],
            "nextPageToken": "token123",
        }
        mock_service.files.return_value.list.return_value = mock_files
        service._service = mock_service  # type: ignore[assignment]

        files, next_token = service.list_files()
        assert next_token == "token123"

    def test_list_files_raises_when_not_authenticated(self) -> None:
        """Test that list_files raises RuntimeError when not authenticated."""
        config = MagicMock()
        config.google_drive_folder_id = "test_folder_id"

        service = DriveService(config)
        with pytest.raises(RuntimeError):
            service.list_files()


class TestDriveServiceDownload:
    """Test DriveService file download."""

    def test_download_file_success(self) -> None:
        """Test that download_file saves a file successfully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import os

            config = MagicMock()
            config.google_drive_folder_id = "test_folder_id"

            drive_file = DriveFile(
                file_id="test123",
                name="test.jpg",
                mime_type="image/jpeg",
                modified_time="2024-01-01T00:00:00.000Z",
                size=1024,
            )

            service = DriveService(config)
            mock_service = MagicMock()

            # Mock MediaIoBaseDownload.next_chunk to avoid http.request() entirely.
            mock_status = MagicMock()
            mock_status.progress.return_value = 1.0
            mock_media = MagicMock()
            mock_media.next_chunk.return_value = (mock_status, True)

            mock_request = MagicMock()
            mock_request.get_media.return_value = mock_media
            mock_service.files.return_value.get_media.return_value = mock_request
            service._service = mock_service  # type: ignore[assignment]

            with patch(
                "services.drive_service.MediaIoBaseDownload"
            ) as mock_download_cls:
                mock_download_instance = MagicMock()
                mock_download_instance.next_chunk.return_value = (mock_status, True)
                mock_download_cls.return_value = mock_download_instance

                dest = os.path.join(tmpdir, "test.jpg")
                result = service.download_file(drive_file, dest, max_retries=1)
                assert result is True
                assert os.path.exists(dest)

    def test_download_file_retries_on_transient_error(self) -> None:
        """Test that download_file retries on transient HTTP errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import os

            config = MagicMock()
            config.google_drive_folder_id = "test_folder_id"

            drive_file = DriveFile(
                file_id="test123",
                name="test.jpg",
                mime_type="image/jpeg",
                modified_time="2024-01-01T00:00:00.000Z",
                size=1024,
            )

            service = DriveService(config)
            mock_service = MagicMock()

            http_error = _make_http_error(503, "Service Unavailable")

            mock_status = MagicMock()
            mock_status.progress.return_value = 1.0
            mock_media = MagicMock()

            call_count = [0]

            def next_chunk_side_effect():
                call_count[0] += 1
                if call_count[0] < 3:
                    raise http_error
                return (mock_status, True)

            mock_media.next_chunk.side_effect = next_chunk_side_effect

            mock_request = MagicMock()
            mock_request.get_media.return_value = mock_media
            mock_service.files.return_value.get_media.return_value = mock_request
            service._service = mock_service  # type: ignore[assignment]

            with patch(
                "services.drive_service.MediaIoBaseDownload"
            ) as mock_download_cls:
                mock_download_instance = MagicMock()
                mock_download_instance.next_chunk.side_effect = next_chunk_side_effect
                mock_download_cls.return_value = mock_download_instance

                dest = os.path.join(tmpdir, "test.jpg")
                result = service.download_file(drive_file, dest, max_retries=3)
                assert result is True
                assert call_count[0] == 3  # 2 failures + 1 success
