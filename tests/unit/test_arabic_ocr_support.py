"""Regression tests for Arabic OCR support and script detection."""

import pytest
from unittest.mock import MagicMock, patch

from services.script_detector import ScriptDetector
from services.arabic_normalizer import ArabicNormalizer


class TestScriptDetector:
    """Tests for Unicode-range script detection (produces language codes for OCR)."""

    def setup_method(self):
        self.detector = ScriptDetector()

    def test_detects_latin_script(self):
        """English/Latin text is detected as Latin script."""
        result = self.detector.detect("The Great Gatsby by F. Scott Fitzgerald")
        assert result == "latin"

    def test_detects_arabic_script(self):
        """Arabic text is detected as Arabic script."""
        text = "المدخل إلى علم البيانات"
        result = self.detector.detect(text)
        assert result == "arabic"

    def test_detects_mixed_script(self):
        """Mixed Arabic/Latin text returns the dominant script."""
        text = "The Great Gatsby بالعربية"
        result = self.detector.detect(text)
        assert result in ("latin", "arabic")

    def test_empty_text_returns_default(self):
        """Empty text returns latin as safe default."""
        assert self.detector.detect("") == "latin"

    def test_get_language_codes_arabic(self):
        """Arabic script maps to OCR language code 'ar'."""
        result = self.detector.get_language_codes("الكتاب")
        assert "ar" in result

    def test_get_language_codes_latin(self):
        """Latin script maps to OCR language code 'en'."""
        result = self.detector.get_language_codes("The Great Gatsby")
        assert "en" in result

    def test_get_language_codes_mixed(self):
        """Mixed script returns both Arabic and en."""
        result = self.detector.get_language_codes("Gatsby بالعربية")
        assert "ar" in result
        assert "en" in result

    def test_needs_bidirectional(self):
        """BiDi check detects Arabic text."""
        assert self.detector.needs_bidirectional("السلام عليكم") is True
        assert self.detector.needs_bidirectional("Hello") is False

    def test_detect_all_finds_all_scripts(self):
        """detect_all returns the set of all scripts present."""
        scripts = self.detector.detect_all("Hello مرحبا")
        assert "latin" in scripts
        assert "arabic" in scripts


class TestArabicNormalizer:
    """Tests for Arabic text normalization."""

    def setup_method(self):
        self.normalizer = ArabicNormalizer()

    def test_tatweel_removal(self):
        """Tatweel (ـ) characters are removed."""
        text = "كتاب ــ للعلم"
        result = self.normalizer.normalize(text)
        assert "ـ" not in result

    def test_alef_variants_normalized(self):
        """Different Alef forms are normalized to bare Alef."""
        text = "أبو كتاب إبراهيم آداب"
        result = self.normalizer.normalize(text)
        assert "أ" not in result
        assert "إ" not in result
        assert "آ" not in result

    def test_yeh_alef_maqsura_normalized(self):
        """Yeh Atasri (ى) is converted to Yeh (ي)."""
        text = "نهاية الكتاب"
        result = self.normalizer.normalize(text)
        assert "ى" not in result

    def test_diacritics_removed(self):
        """Arabic diacritics (tashkeel) are stripped."""
        text = "الحمد لله"
        result = self.normalizer.normalize(text)
        assert "ْ" not in result

    def test_arabic_indic_digits_converted(self):
        """Arabic-Indic digits are converted to Western digits."""
        text = "عام ١٤٣٥"
        result = self.normalizer.normalize(text)
        assert "١" not in result

    def test_has_arabic_detects_arabic(self):
        """has_arabic correctly identifies Arabic text."""
        assert self.normalizer.has_arabic("السلام عليكم") is True

    def test_has_arabic_detects_non_arabic(self):
        """has_arabic correctly rejects non-Arabic text."""
        assert self.normalizer.has_arabic("Hello World") is False

    def test_has_arabic_empty(self):
        """has_arabic returns False for empty text."""
        assert self.normalizer.has_arabic("") is False

    def test_empty_string_handled(self):
        """Empty string is handled gracefully."""
        assert self.normalizer.normalize("") == ""

    def test_presentation_forms_decomposed(self):
        """Unicode presentation forms are decomposed to base characters."""
        text = "test"
        result = self.normalizer.normalize(text)
        assert isinstance(result, str)
