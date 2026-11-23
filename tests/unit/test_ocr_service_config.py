"""Unit tests for OCRService configuration — PaddleOCR-VL (Baidu OCR API) only.

OCR REQUIREMENT: No EasyOCR. Tests verify PaddleOCR-VL config and OCR_FAILED.
"""

from unittest.mock import MagicMock, patch

from services.ocr_service import OCRService


class TestOCRServiceConfig:
    """Tests for OCRService: PaddleOCR-VL client and confidence threshold."""

    def test_default_confidence_threshold(self):
        """Default confidence threshold is 0.5."""
        svc = OCRService(languages=["en"])
        assert svc._confidence_threshold == 0.5

    def test_custom_confidence_threshold(self):
        """Confidence threshold is configurable."""
        svc = OCRService(languages=["en"], confidence_threshold=0.8)
        assert svc._confidence_threshold == 0.8

    def test_paddleocr_client_created(self):
        """OCRService creates a PaddleOCRVlService client."""
        svc = OCRService(languages=["en"])
        assert svc._paddleocr is not None

    def test_paddleocr_extractor_created(self):
        """OCRService creates a PaddleOCRVlExtractor."""
        svc = OCRService(languages=["en"])
        assert svc._extractor is not None

    def test_low_confidence_words_filtered(self):
        """Words below threshold are excluded from OCRResult."""
        mock_vl = MagicMock()
        blk1 = MagicMock()
        blk1.text = "Good Book"
        blk1.score = 0.85
        blk1.bbox = [[0, 0], [100, 0], [100, 50], [0, 50]]
        blk2 = MagicMock()
        blk2.text = "noise"
        blk2.score = 0.30
        blk2.bbox = [[0, 50], [100, 50], [100, 100], [0, 100]]
        mock_vl.layout_blocks = [blk1, blk2]
        mock_vl.raw_markdown = "Good Book\nnoise"

        with patch("services.ocr_service.PaddleOCRVlService") as mock_cls:
            mock_cls.return_value.extract.return_value = mock_vl
            svc = OCRService(languages=["en"], confidence_threshold=0.7)
            result = svc.extract_text("/fake/path.jpg")

            assert result.text == "Good Book"
            # blk2 (score 0.30) filtered by confidence_threshold 0.7
            assert len(result.words) == 1
            assert result.words[0].text == "Good Book"

    def test_ocr_failed_on_paddleocr_exception(self):
        """PaddleOCR-VL exception → OCR_FAILED, no EasyOCR fallback."""
        svc = OCRService(languages=["en"])

        with patch("services.ocr_service.PaddleOCRVlService") as mock_cls:
            mock_cls.return_value.extract.side_effect = RuntimeError("API down")
            result = svc.extract_text("/fake/path.jpg")

            assert result.text == ""
            assert result.confidence == 0.0
            assert len(result.words) == 0
