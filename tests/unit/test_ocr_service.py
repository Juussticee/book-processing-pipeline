"""Unit tests for OCRService — PaddleOCR-VL (Baidu OCR API) only.

OCR REQUIREMENT: No EasyOCR. Tests mock PaddleOCRVlService, never EasyOCR.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

from models.ocr_result import OCRResult, OCRWord
from services.ocr_service import OCRService


class TestOCRService:
    """Test the OCR service text extraction via PaddleOCR-VL."""

    def setup_method(self) -> None:
        self._temp_dir = tempfile.mkdtemp()

    def teardown_method(self) -> None:
        for fname in os.listdir(self._temp_dir):
            os.unlink(os.path.join(self._temp_dir, fname))
        os.rmdir(self._temp_dir)

    def _make_vl_result(
        self,
        blocks: list[tuple[str, float, list[list[int]]]],
    ) -> MagicMock:
        """Build a mock PaddleOCR-VL result from (text, score, bbox) tuples."""
        mock_vl = MagicMock()
        mock_blocks = []
        for text, score, bbox in blocks:
            mb = MagicMock()
            mb.text = text
            mb.score = score
            mb.bbox = bbox
            mock_blocks.append(mb)
        mock_vl.layout_blocks = mock_blocks
        mock_vl.raw_markdown = "\n".join(t for t, _, _ in blocks)
        mock_vl.orientation = "normal"
        return mock_vl

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_extract_text_returns_ocr_result(self, mock_pvls_class) -> None:
        """Test that extract_text returns a properly structured OCRResult."""
        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.return_value = self._make_vl_result(
            [
                ("Atomic Habits", 0.95, [[10, 10], [200, 10], [200, 50], [10, 50]]),
                ("James Clear", 0.92, [[10, 60], [150, 60], [150, 90], [10, 90]]),
            ]
        )

        temp_image = os.path.join(self._temp_dir, "test.jpg")
        with open(temp_image, "w") as f:
            f.write("dummy")

        service = OCRService(languages=["en"])
        result = service.extract_text(temp_image)

        assert isinstance(result, OCRResult)
        assert "Atomic Habits" in result.text
        assert "James Clear" in result.text
        assert result.confidence > 0
        assert len(result.words) == 2
        assert result.language == "unknown"
        assert result.image_path == temp_image

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_extract_text_filters_low_confidence(self, mock_pvls_class) -> None:
        """Low confidence results should be filtered out."""
        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.return_value = self._make_vl_result(
            [
                ("Title", 0.95, [[10, 10], [200, 10], [200, 50], [10, 50]]),
                ("Garbage", 0.05, [[10, 60], [150, 60], [150, 90], [10, 90]]),
            ]
        )

        temp_image = os.path.join(self._temp_dir, "test.jpg")
        with open(temp_image, "w") as f:
            f.write("dummy")

        service = OCRService()
        result = service.extract_text(temp_image)

        assert len(result.words) == 1
        assert result.words[0].text == "Title"

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_extract_text_empty_result(self, mock_pvls_class) -> None:
        """Empty OCR result returns empty text."""
        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.return_value = self._make_vl_result([])

        temp_image = os.path.join(self._temp_dir, "test.jpg")
        with open(temp_image, "w") as f:
            f.write("dummy")

        service = OCRService()
        result = service.extract_text(temp_image)

        assert result.text == ""
        assert result.confidence == 0.0
        assert len(result.words) == 0

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_extract_text_paddleocr_failure_returns_ocr_failed(
        self, mock_pvls_class
    ) -> None:
        """PaddleOCR-VL failure returns OCR_FAILED — no EasyOCR fallback."""
        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.side_effect = RuntimeError("API error")

        temp_image = os.path.join(self._temp_dir, "test.jpg")
        with open(temp_image, "w") as f:
            f.write("dummy")

        service = OCRService()
        result = service.extract_text(temp_image)

        # OCR_FAILED: empty text, zero confidence, no words
        assert result.text == ""
        assert result.confidence == 0.0
        assert len(result.words) == 0
        assert result.image_path == temp_image

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_extract_fields_finds_title(self, mock_pvls_class) -> None:
        """Test field extraction from PaddleOCR-VL OCR text."""
        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.return_value = self._make_vl_result(
            [
                (
                    "The Pragmatic Programmer",
                    0.95,
                    [[10, 10], [200, 10], [200, 50], [10, 50]],
                ),
                ("Andrew Hunt", 0.90, [[10, 60], [150, 60], [150, 90], [10, 90]]),
            ]
        )

        temp_image = os.path.join(self._temp_dir, "test.jpg")
        with open(temp_image, "w") as f:
            f.write("dummy")

        service = OCRService()
        ocr_result = service.extract_text(temp_image)
        fields = service.extract_fields(ocr_result)

        assert fields["title"] is not None
        assert "Pragmatic" in fields["title"]

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_extract_fields_finds_isbn(self, mock_pvls_class) -> None:
        """Test ISBN extraction from PaddleOCR-VL OCR text."""
        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.return_value = self._make_vl_result(
            [
                (
                    "ISBN 978-0-306-40615-7",
                    0.95,
                    [[10, 10], [200, 10], [200, 50], [10, 50]],
                ),
            ]
        )

        temp_image = os.path.join(self._temp_dir, "test.jpg")
        with open(temp_image, "w") as f:
            f.write("dummy")

        service = OCRService()
        ocr_result = service.extract_text(temp_image)
        fields = service.extract_fields(ocr_result)

        assert fields["isbn"] is not None
        assert "9780306406157" in fields["isbn"]

    def test_ocr_word_dataclass(self) -> None:
        """Test OCRWord dataclass structure."""
        word = OCRWord(
            text="Test",
            confidence=0.95,
            bounding_box=[[0, 0], [10, 0], [10, 10], [0, 10]],
        )
        assert word.text == "Test"
        assert word.confidence == 0.95
        assert len(word.bounding_box) == 4

    def test_ocr_result_defaults(self) -> None:
        """Test OCRResult default values."""
        result = OCRResult(text="", confidence=0.0, words=[])
        assert result.text == ""
        assert result.confidence == 0.0
        assert result.words == []

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_extract_text_no_easyocr_import(self, mock_pvls_class) -> None:
        """OCR REQUIREMENT: EasyOCR must not be imported or referenced."""
        import services.ocr_service as mod

        # The module must not import easyocr
        assert not hasattr(mod, "easyocr"), "OCRService must not import easyocr"

        # PaddleOCRVlService must be the only OCR client
        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.return_value = self._make_vl_result(
            [
                ("Test Book", 0.9, [[0, 0], [100, 0], [100, 50], [0, 50]]),
            ]
        )

        service = OCRService()
        result = service.extract_text("/tmp/test.jpg")
        assert result.text == "Test Book"
        mock_pvls.extract.assert_called_once()

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_provider_boundary_baidu_only(self, mock_pvls_class) -> None:
        """Prove OCRService uses Baidu API client and no other OCR provider.

        Mocks PaddleOCRVlService (Baidu API client) and verifies:
        - OCRService calls PaddleOCRVlService.extract() exactly once
        - OCRService does NOT import or call easyocr
        - OCRService does NOT import or call local paddleocr library
        - OCRService does NOT call Gemini
        - OCRService does NOT call any other OCR engine
        """
        import sys

        mock_pvls = MagicMock()
        mock_pvls_class.return_value = mock_pvls
        mock_pvls.extract.return_value = self._make_vl_result(
            [
                ("Baidu Test", 0.95, [[0, 0], [100, 0], [100, 50], [0, 50]]),
            ]
        )

        temp_image = os.path.join(self._temp_dir, "baidu_test.jpg")
        with open(temp_image, "w") as f:
            f.write("dummy")

        service = OCRService()
        result = service.extract_text(temp_image)

        # Baidu API client was called exactly once
        mock_pvls.extract.assert_called_once()

        # Result came from Baidu API
        assert result.text == "Baidu Test"
        assert result.confidence > 0

        # No easyocr in module namespace
        import services.ocr_service as mod

        assert not hasattr(mod, "easyocr"), "easyocr must not be imported"

        # No local paddleocr library in sys.modules
        assert "paddleocr" not in sys.modules, "local paddleocr must not be imported"

        # No Gemini in module namespace
        assert not hasattr(mod, "gemini"), "Gemini must not be referenced"

        # No other OCR engines in module namespace
        for engine in ["tesseract", "tesseract_ocr", "easyocr", "paddleocr"]:
            assert not hasattr(mod, engine), f"{engine} must not be referenced"


class TestProviderBoundingBoxes:
    """The live provider returns a flat rectangle, not corner pairs.

    Regression for the live acceptance failure where every real PaddleOCR-VL
    response raised ``'int' object is not subscriptable`` while building
    ``OCRWord.bounding_box``, which turned real OCR output into empty text.
    """

    def _service(self) -> OCRService:
        return OCRService(languages=["en"])

    def _blocks(self, bbox):
        block = MagicMock()
        block.text = "LE COEUR VERT"
        block.score = 0.93
        block.bbox = bbox
        vl = MagicMock()
        vl.layout_blocks = [block]
        return vl

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_flat_rectangle_keeps_word_text(self, mock_pvls_class) -> None:
        mock_pvls_class.return_value.extract.return_value = self._blocks(
            [10, 20, 210, 90]
        )

        result = self._service().extract_text("image.png")

        assert result.text == "LE COEUR VERT"
        assert len(result.words) == 1
        assert result.words[0].bounding_box == [[10, 20], [210, 90]]
        assert result.confidence == 0.93

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_corner_pairs_still_work(self, mock_pvls_class) -> None:
        mock_pvls_class.return_value.extract.return_value = self._blocks(
            [[10, 20], [210, 20], [210, 90], [10, 90]]
        )

        result = self._service().extract_text("image.png")

        assert result.text == "LE COEUR VERT"
        assert result.words[0].bounding_box == [
            [10, 20],
            [210, 20],
            [210, 90],
            [10, 90],
        ]

    @patch("services.ocr_service.PaddleOCRVlService")
    def test_degenerate_boxes_do_not_lose_the_word(self, mock_pvls_class) -> None:
        for bbox in (None, [], [1, 2], ["a", "b", "c", "d"], 42):
            mock_pvls_class.return_value.extract.return_value = self._blocks(bbox)

            result = self._service().extract_text("image.png")

            assert result.text == "LE COEUR VERT", bbox
            assert result.words[0].bounding_box == [], bbox
