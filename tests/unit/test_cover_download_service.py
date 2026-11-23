"""Unit tests for CoverDownloadService.

Test with mocked requests and PIL/Pillow.
"""

import io
import tempfile
from unittest.mock import patch

from models.cover_download import CoverDownloadStatus
from repositories.cover_repository import CoverRepository
from services.cover_download_service import CoverDownloadService


class _FakeImage:
    def convert(self, mode):
        return self

    def __init__(self, width=400, height=600):
        self.size = (width, height)


class TestCoverDownloadService:
    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        self._repo = CoverRepository(self._db_path)
        self._covers_dir = tempfile.mkdtemp()
        self._service = CoverDownloadService(
            cover_repo=self._repo,
            covers_dir=self._covers_dir,
            timeout=5,
            min_width=200,
            min_height=200,
        )

    def teardown_method(self):
        import gc
        import os
        import shutil

        self._service.close()
        self._repo.close()
        gc.collect()
        # Windows holds file locks; retry on PermissionError
        for _ in range(5):
            try:
                if os.path.exists(self._db_path):
                    os.unlink(self._db_path)
                break
            except PermissionError:
                import time

                time.sleep(0.1)
        if os.path.exists(self._covers_dir):
            shutil.rmtree(self._covers_dir, ignore_errors=True)

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_cover_success(self, mock_fetch):
        """Cover downloads successfully and record is COMPLETED."""
        from PIL import Image

        img = Image.new("RGB", (400, 600), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        mock_fetch.return_value = raw_bytes

        result = self._service.download_cover(
            drive_file_id="drive001",
            job_id="job001",
            cover_url="http://example.com/cover.png",
        )
        assert result.status == CoverDownloadStatus.COMPLETED
        assert result.width == 400
        assert result.height == 600
        assert result.checksum is not None
        assert result.local_path is not None
        assert "cover_drive001" in result.local_path

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_cover_failure(self, mock_fetch):
        """Download failure results in FAILED status."""
        mock_fetch.side_effect = Exception("Connection timeout")
        result = self._service.download_cover(
            drive_file_id="drive002",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
        )
        assert result.status == CoverDownloadStatus.FAILED
        assert result.error_message is not None

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_invalid_image(self, mock_fetch):
        """Invalid image bytes result in FAILED status."""
        mock_fetch.return_value = b"not an image"
        result = self._service.download_cover(
            drive_file_id="drive003",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
        )
        assert result.status == CoverDownloadStatus.FAILED
        assert (
            result.error_message == "Invalid image format or dimensions below threshold"
        )

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_1x1_placeholder_rejected(self, mock_fetch):
        """A 1x1 placeholder image should be rejected (below min dimensions)."""
        from PIL import Image

        img = Image.new("RGB", (1, 1), color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        mock_fetch.return_value = raw_bytes
        result = self._service.download_cover(
            drive_file_id="drive_1x1",
            job_id="job001",
            cover_url="http://example.com/placeholder.jpg",
        )
        assert result.status == CoverDownloadStatus.FAILED

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_fallback_to_second_url(self, mock_fetch):
        """When first URL fails validation, should try second URL."""
        from PIL import Image

        # First URL: 1x1 placeholder (fails validation)
        img1 = Image.new("RGB", (1, 1), color="white")
        buf1 = io.BytesIO()
        img1.save(buf1, format="PNG")
        bad_bytes = buf1.getvalue()

        # Second URL: valid image (400x600)
        img2 = Image.new("RGB", (400, 600), color="red")
        buf2 = io.BytesIO()
        img2.save(buf2, format="PNG")
        good_bytes = buf2.getvalue()

        mock_fetch.side_effect = [bad_bytes, good_bytes]

        result = self._service.download_covers(
            drive_file_id="drive_fallback",
            job_id="job001",
            cover_urls=[
                "http://example.com/primary.jpg",
                "http://example.com/fallback.jpg",
            ],
        )
        assert result.status == CoverDownloadStatus.COMPLETED
        assert result.width == 400
        assert result.height == 600
        assert result.cover_url == "http://example.com/fallback.jpg"

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_all_fail(self, mock_fetch):
        """When all URLs fail, status is FAILED."""
        from PIL import Image

        img1 = Image.new("RGB", (1, 1), color="white")
        buf1 = io.BytesIO()
        img1.save(buf1, format="PNG")
        bad_bytes = buf1.getvalue()

        mock_fetch.side_effect = [bad_bytes, bad_bytes]

        result = self._service.download_covers(
            drive_file_id="drive_all_fail",
            job_id="job001",
            cover_urls=[
                "http://example.com/primary.jpg",
                "http://example.com/fallback.jpg",
            ],
        )
        assert result.status == CoverDownloadStatus.FAILED

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_empty_list(self, mock_fetch):
        """Empty URL list returns FAILED immediately."""
        result = self._service.download_covers(
            drive_file_id="drive_empty",
            job_id="job001",
            cover_urls=[],
        )
        assert result.status == CoverDownloadStatus.FAILED
        assert "No cover URLs provided" in result.error_message

    def test_skip_existing_completed_cover(self):
        """If a COMPLETED cover exists, reuse without re-downloading."""
        from models.cover_download import CoverDownloadStatus as CDS

        self._repo.insert_cover(
            drive_file_id="drive004",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
            local_path="/path/to/cover_drive004.jpg",
            checksum="existingchecksum",
            width=400,
            height=600,
            file_size=50000,
            status=CDS.COMPLETED.value,
        )
        result = self._service.download_cover(
            drive_file_id="drive004",
            job_id="job001",
            cover_url="http://example.com/cover.jpg",
        )
        assert result.status == CoverDownloadStatus.COMPLETED
        assert result.checksum == "existingchecksum"

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_skip_existing_checksum(self, mock_fetch):
        """If a cover with same checksum already exists, reuse its path."""
        from PIL import Image

        img = Image.new("RGB", (400, 600), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        mock_fetch.return_value = raw_bytes

        # First download
        result1 = self._service.download_cover(
            drive_file_id="driveA",
            job_id="jobA",
            cover_url="http://example.com/cover.png",
        )
        assert result1.status == CoverDownloadStatus.COMPLETED

        # Second download with same content but different drive_file_id
        result2 = self._service.download_cover(
            drive_file_id="driveB",
            job_id="jobB",
            cover_url="http://example.com/cover.png",
        )
        assert result2.status == CoverDownloadStatus.COMPLETED
        # Should reuse the same local path
        assert result2.local_path == result1.local_path

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_network_failure(self, mock_fetch):
        """Network failure on all URLs results in FAILED status."""
        mock_fetch.side_effect = ConnectionError("Connection refused")
        result = self._service.download_covers(
            drive_file_id="drive_network",
            job_id="job001",
            cover_urls=[
                "http://example.com/primary.jpg",
                "http://example.com/fallback.jpg",
            ],
        )
        assert result.status == CoverDownloadStatus.FAILED
        assert result.error_message is not None

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_empty_response(self, mock_fetch):
        """Empty response body results in FAILED status."""
        mock_fetch.return_value = b""
        result = self._service.download_covers(
            drive_file_id="drive_empty_resp",
            job_id="job001",
            cover_urls=["http://example.com/cover.jpg"],
        )
        assert result.status == CoverDownloadStatus.FAILED
        assert result.error_message is not None

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_oversized_rejected(self, mock_fetch):
        """Image exceeding max_file_size is rejected."""
        from PIL import Image

        # Create an image larger than max_file_size (1024 bytes for this test)
        img = Image.new("RGB", (400, 600), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        # Override max_file_size to test rejection
        small_service = CoverDownloadService(
            cover_repo=self._repo,
            covers_dir=self._covers_dir,
            timeout=5,
            min_width=200,
            min_height=200,
            max_file_size=1,  # 1 byte — any image will exceed this
        )

        mock_fetch.return_value = raw_bytes
        result = small_service.download_cover(
            drive_file_id="drive_oversized",
            job_id="job001",
            cover_url="http://example.com/cover.png",
        )
        assert result.status == CoverDownloadStatus.FAILED
        assert result.error_message is not None
        small_service.close()

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_below_min_dimensions(self, mock_fetch):
        """Image below minimum dimensions is rejected."""
        from PIL import Image

        # 150x150 — below the 200x200 minimum
        img = Image.new("RGB", (150, 150), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        mock_fetch.return_value = raw_bytes
        result = self._service.download_cover(
            drive_file_id="drive_too_small",
            job_id="job001",
            cover_url="http://example.com/cover.png",
        )
        assert result.status == CoverDownloadStatus.FAILED
        assert result.error_message is not None

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_multiple_urls_first_valid(self, mock_fetch):
        """When multiple URLs provided and first is valid, use it directly."""
        from PIL import Image

        img = Image.new("RGB", (400, 600), color="green")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        mock_fetch.return_value = raw_bytes
        result = self._service.download_covers(
            drive_file_id="drive_first_valid",
            job_id="job001",
            cover_urls=[
                "http://example.com/primary.jpg",
                "http://example.com/fallback.jpg",
            ],
        )
        assert result.status == CoverDownloadStatus.COMPLETED
        assert result.cover_url == "http://example.com/primary.jpg"

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_download_covers_three_url_fallback(self, mock_fetch):
        """Fallback through 3 URLs: 1x1 → invalid bytes → valid image."""
        from PIL import Image

        # URL 1: 1x1 placeholder
        img1 = Image.new("RGB", (1, 1), color="white")
        buf1 = io.BytesIO()
        img1.save(buf1, format="PNG")
        bad1 = buf1.getvalue()

        # URL 2: invalid image bytes
        bad2 = b"corrupt data not an image"

        # URL 3: valid 500x700 image
        img3 = Image.new("RGB", (500, 700), color="blue")
        buf3 = io.BytesIO()
        img3.save(buf3, format="PNG")
        good3 = buf3.getvalue()

        mock_fetch.side_effect = [bad1, bad2, good3]
        result = self._service.download_covers(
            drive_file_id="drive_three",
            job_id="job001",
            cover_urls=[
                "http://example.com/placeholder.jpg",
                "http://example.com/corrupt.jpg",
                "http://example.com/valid.jpg",
            ],
        )
        assert result.status == CoverDownloadStatus.COMPLETED
        assert result.width == 500
        assert result.height == 700
        assert result.cover_url == "http://example.com/valid.jpg"

    @patch.object(CoverDownloadService, "_fetch_image")
    def test_failed_cover_record_persisted(self, mock_fetch):
        """Failed cover download should persist a FAILED record."""
        from models.cover_download import CoverDownloadStatus as CDS

        mock_fetch.side_effect = Exception("DNS resolution failed")
        result = self._service.download_cover(
            drive_file_id="drive_persist_fail",
            job_id="job001",
            cover_url="http://example.com/nonexistent.jpg",
        )
        assert result.status == CoverDownloadStatus.FAILED

        # Verify a FAILED record was persisted
        record = self._repo.get_cover_by_drive_file_id("drive_persist_fail")
        assert record is not None
        assert record.status == CDS.FAILED
        assert record.cover_url == "http://example.com/nonexistent.jpg"
        assert record.error_message is not None


class TestCoverDownloadValidation:
    """Downloaded bytes must be a real image before a cover counts as FOUND."""

    def setup_method(self):
        import tempfile

        from repositories.cover_repository import CoverRepository
        from services.cover_download_service import CoverDownloadService

        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        self._repo = CoverRepository(self._db_path)
        self._covers_dir = tempfile.mkdtemp()
        self._service = CoverDownloadService(
            cover_repo=self._repo,
            covers_dir=self._covers_dir,
            timeout=5,
            min_width=200,
            min_height=250,
        )

    def teardown_method(self):
        import gc
        import os
        import shutil

        self._service.close()
        self._repo.close()
        gc.collect()
        for _ in range(5):
            try:
                if os.path.exists(self._db_path):
                    os.unlink(self._db_path)
                break
            except PermissionError:
                import time

                time.sleep(0.1)
        if os.path.exists(self._covers_dir):
            shutil.rmtree(self._covers_dir, ignore_errors=True)

    def test_placeholder_url_rejected_without_request(self):
        """A known placeholder URL never reaches the network."""
        import pytest

        with patch("requests.get") as mock_get:
            with pytest.raises(ValueError):
                self._service._fetch_image(
                    "https://m.media-amazon.com/images/I/41x._kindle-store-link.jpg", 5
                )
        mock_get.assert_not_called()

    def test_non_image_content_type_rejected(self):
        """An HTML error page is not accepted as a cover."""
        from unittest.mock import MagicMock

        import pytest

        response = MagicMock()
        response.headers = {"content-type": "text/html; charset=utf-8"}
        response.content = b"<html>blocked</html>"
        response.raise_for_status.return_value = None

        with patch("requests.get", return_value=response):
            with pytest.raises(ValueError):
                self._service._fetch_image("https://example.com/cover", 5)

    def test_gif_response_rejected(self):
        """Open Library's 1x1 GIF placeholder is rejected."""
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (400, 600), color="white").save(buf, format="GIF")

        result = self._service._validate_image(buf.getvalue(), "https://x/cover.gif")
        assert result is None

    def test_tiny_image_rejected(self):
        """Images below the minimum cover size are rejected."""
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (50, 50), color="white").save(buf, format="PNG")

        result = self._service._validate_image(buf.getvalue(), "https://x/cover.png")
        assert result is None

    def test_real_image_accepted(self):
        """A real sized image passes validation and reports its dimensions."""
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (400, 600), color="white").save(buf, format="PNG")

        result = self._service._validate_image(buf.getvalue(), "https://x/cover.png")
        assert result is not None
        assert result[0] == 400
        assert result[1] == 600
