"""Unit tests for ImagePreprocessor."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from services.image_preprocessor import SUPPORTED_EXTENSIONS, ImagePreprocessor


def make_mock_image(size=(50, 50)):
    """Create a mock PIL Image that returns itself for chainable methods."""
    mock = MagicMock()
    mock.size = size
    # Each conversion/processing step returns the same mock for chaining
    mock.convert.return_value = mock
    mock.rotate.return_value = mock
    mock.resize.return_value = mock
    mock.filter.return_value = mock
    mock._getexif.return_value = None  # No EXIF data
    return mock


class TestImagePreprocessor:
    """Test image preprocessing methods."""

    def setup_method(self) -> None:
        self._temp_dir = tempfile.mkdtemp()
        self._preprocessor = ImagePreprocessor(temp_dir=self._temp_dir)

    def teardown_method(self) -> None:
        for fname in os.listdir(self._temp_dir):
            os.unlink(os.path.join(self._temp_dir, fname))
        if os.path.exists(self._temp_dir):
            os.rmdir(self._temp_dir)

    def test_supported_extensions(self) -> None:
        """Supported extensions should include jpg, jpeg, png, webp."""
        assert ".jpg" in SUPPORTED_EXTENSIONS
        assert ".jpeg" in SUPPORTED_EXTENSIONS
        assert ".png" in SUPPORTED_EXTENSIONS
        assert ".webp" in SUPPORTED_EXTENSIONS

    def test_preprocess_file_not_found(self) -> None:
        """Preprocessing a non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="not found"):
            self._preprocessor.preprocess("/nonexistent/image.jpg")

    def test_preprocess_unsupported_format(self) -> None:
        """Preprocessing a non-image file raises ValueError."""
        bad_path = os.path.join(self._temp_dir, "test.txt")
        with open(bad_path, "w") as f:
            f.write("text")

        with pytest.raises(ValueError, match="Unsupported"):
            self._preprocessor.preprocess(bad_path)

    @patch("services.image_preprocessor.PIL")
    def test_preprocess_jpg(self, mock_pil) -> None:
        """Test preprocessing a JPG image."""
        mock_img = make_mock_image()
        mock_pil.Image.open.return_value = mock_img
        # Make autocontrast and filter return the same mock for chaining
        mock_pil.ImageOps.autocontrast.return_value = mock_img
        mock_pil.ImageFilter.SMOOTH = "SMOOTH"

        img_path = os.path.join(self._temp_dir, "test.jpg")
        with open(img_path, "w") as f:
            f.write("dummy")

        result_path = self._preprocessor.preprocess(img_path)

        assert result_path != img_path
        mock_pil.Image.open.assert_called_once_with(img_path)
        mock_img.save.assert_called_once()

    @patch("services.image_preprocessor.PIL")
    def test_preprocess_does_not_modify_original(self, mock_pil) -> None:
        """Original file must never be overwritten."""
        mock_img = make_mock_image()
        mock_pil.Image.open.return_value = mock_img
        mock_pil.ImageOps.autocontrast.return_value = mock_img

        img_path = os.path.join(self._temp_dir, "test.png")
        with open(img_path, "wb") as f:
            f.write(b"\x89PNG\r\n")

        original_content = open(img_path, "rb").read()
        result_path = self._preprocessor.preprocess(img_path)

        after_content = open(img_path, "rb").read()
        assert original_content == after_content
        assert result_path != img_path

    @patch("services.image_preprocessor.PIL")
    def test_preprocess_pipeline_steps_called(self, mock_pil) -> None:
        """Verify all preprocessing steps are called."""
        mock_img = make_mock_image()
        mock_pil.Image.open.return_value = mock_img
        mock_pil.ImageOps.autocontrast.return_value = mock_img

        img_path = os.path.join(self._temp_dir, "test.jpg")
        with open(img_path, "w") as f:
            f.write("dummy")

        self._preprocessor.preprocess(img_path)

        mock_pil.Image.open.assert_called_once()
        mock_img.convert.assert_called()
        mock_img.save.assert_called_once()

    @patch("services.image_preprocessor.PIL")
    def test_preprocess_converts_to_grayscale(self, mock_pil) -> None:
        """Image should be converted to grayscale (L mode)."""
        mock_img = make_mock_image()
        mock_pil.Image.open.return_value = mock_img
        mock_pil.ImageOps.autocontrast.return_value = mock_img

        img_path = os.path.join(self._temp_dir, "test.jpg")
        with open(img_path, "w") as f:
            f.write("dummy")

        self._preprocessor.preprocess(img_path)

        convert_calls = [call.args[0] for call in mock_img.convert.call_args_list]
        assert "RGB" in convert_calls
        assert "L" in convert_calls

    @patch("services.image_preprocessor.PIL")
    def test_preprocess_auto_rotate_no_exif(self, mock_pil) -> None:
        """Auto-rotate should handle missing EXIF gracefully."""
        mock_img = make_mock_image()
        mock_pil.Image.open.return_value = mock_img

        result = self._preprocessor._auto_rotate(mock_img)
        assert result is mock_img

    @patch("services.image_preprocessor.PIL")
    def test_resize_oversized_image(self, mock_pil) -> None:
        """Image exceeding max dimensions should be resized."""
        mock_img = make_mock_image(size=(2000, 1500))
        mock_pil.Image.Resampling.LANCZOS = "LANCZOS"
        mock_pil.ImageOps.autocontrast.return_value = mock_img

        self._preprocessor._resize(mock_img)
        mock_img.resize.assert_called_once()

    @patch("services.image_preprocessor.PIL")
    def test_resize_small_image_no_change(self, mock_pil) -> None:
        """Image within dimensions should not be resized."""
        mock_img = make_mock_image(size=(800, 600))
        mock_pil.Image.Resampling.LANCZOS = "LANCZOS"

        result = self._preprocessor._resize(mock_img)
        assert result is mock_img
        mock_img.resize.assert_not_called()

    def test_cleanup_removes_temp_file(self) -> None:
        """Cleanup should remove temp files."""
        temp_path = os.path.join(self._temp_dir, "temp_img.jpg")
        with open(temp_path, "w") as f:
            f.write("dummy")

        assert os.path.exists(temp_path)
        self._preprocessor.cleanup(temp_path)
        assert not os.path.exists(temp_path)

    def test_cleanup_nonexistent_file(self) -> None:
        """Cleanup should not raise for non-existent files."""
        self._preprocessor.cleanup("/nonexistent/file.jpg")

    def test_rotate_image_0_degrees_returns_same_image(self) -> None:
        """_rotate_image(0) should return the same image object (no copy)."""
        mock_img = make_mock_image()
        result = self._preprocessor._rotate_image(mock_img, 0)
        assert result is mock_img

    @patch("services.image_preprocessor.PIL")
    def test_rotate_image_180_degrees_calls_rotate(self, mock_pil) -> None:
        """_rotate_image(180) must call img.rotate(180), not overwrite img."""
        mock_img = make_mock_image()
        result = self._preprocessor._rotate_image(mock_img, 180)
        # The result should be the mocked rotate return value, not the PIL module
        assert result is mock_img
        mock_img.rotate.assert_called_once_with(180, expand=True)

    @patch("services.image_preprocessor.PIL")
    def test_rotate_image_90_cw_calls_rotate(self, mock_pil) -> None:
        """_rotate_image(90) must call img.rotate(90), not overwrite img."""
        mock_img = make_mock_image()
        result = self._preprocessor._rotate_image(mock_img, 90)
        assert result is mock_img
        mock_img.rotate.assert_called_once_with(90, expand=True)

    @patch("services.image_preprocessor.PIL")
    def test_rotate_image_270_cw_calls_rotate(self, mock_pil) -> None:
        """_rotate_image(270) must call img.rotate(270), not overwrite img."""
        mock_img = make_mock_image()
        result = self._preprocessor._rotate_image(mock_img, 270)
        assert result is mock_img
        mock_img.rotate.assert_called_once_with(270, expand=True)

    @patch("services.image_preprocessor.PIL")
    def test_detect_orientation_does_not_crash_on_valid_image(self, mock_pil) -> None:
        """detect_orientation must not crash with AttributeError on the PIL module."""
        mock_img = make_mock_image(size=(200, 300))
        mock_pil.Image.fromarray.return_value = mock_img
        # _score_orientation uses numpy; mock np.array to return a small array
        # so the scoring path works without real image data
        result = self._preprocessor.detect_orientation(mock_img)
        assert hasattr(result, "angle")
        assert hasattr(result, "was_corrected")

    @patch("services.image_preprocessor.PIL")
    def test_correct_orientation_no_correction_returns_original(self, mock_pil) -> None:
        """correct_orientation with was_corrected=False should return the image as-is."""
        from models.image_orientation import ImageOrientation

        mock_img = make_mock_image()
        orient = ImageOrientation(angle=0, confidence=0.9, was_corrected=False)
        result = self._preprocessor.correct_orientation(mock_img, orient)
        assert result is mock_img
        mock_img.rotate.assert_not_called()

    @patch("services.image_preprocessor.PIL")
    def test_correct_orientation_with_correction_calls_rotate(self, mock_pil) -> None:
        """correct_orientation with was_corrected=True should rotate the image."""
        from models.image_orientation import ImageOrientation

        mock_img = make_mock_image()
        orient = ImageOrientation(angle=180, confidence=0.9, was_corrected=True)
        self._preprocessor.correct_orientation(mock_img, orient)
        mock_img.rotate.assert_called_once_with(180, expand=True)
