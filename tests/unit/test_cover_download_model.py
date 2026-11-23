"""Unit tests for CoverDownload model."""

from models.cover_download import CoverDownload, CoverDownloadStatus


class TestCoverDownload:
    def test_create_completed(self):
        download = CoverDownload(
            drive_file_id="drive001",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
            local_path="data/covers/cover_drive001.jpg",
            checksum="abc123",
            width=600,
            height=800,
            file_size=150000,
        )
        assert download.drive_file_id == "drive001"
        assert download.status == CoverDownloadStatus.COMPLETED
        assert download.width == 600
        assert download.height == 800
        assert download.resolution == "600x800"

    def test_coerce_string_status(self):
        download = CoverDownload(
            drive_file_id="drive002",
            job_id="job002",
            cover_url="http://example.com/cover.jpg",
            local_path="data/covers/cover_drive002.jpg",
            status="failed",
        )
        assert download.status == CoverDownloadStatus.FAILED

    def test_downloaded_at_defaults(self):
        download = CoverDownload(
            drive_file_id="drive003",
            cover_url="http://example.com/cover.jpg",
            local_path="data/covers/cover_drive003.jpg",
        )
        assert download.downloaded_at is not None

    def test_skipped_status(self):
        download = CoverDownload(
            drive_file_id="drive004",
            cover_url="",
            local_path="",
            status=CoverDownloadStatus.SKIPPED,
        )
        assert download.status == CoverDownloadStatus.SKIPPED

    def test_error_message_optional(self):
        download = CoverDownload(
            drive_file_id="drive005",
            cover_url="http://example.com/cover.jpg",
            local_path="data/covers/cover_drive005.jpg",
            error_message="Timeout downloading",
            status=CoverDownloadStatus.FAILED,
        )
        assert download.error_message == "Timeout downloading"
