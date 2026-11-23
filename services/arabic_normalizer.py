"""Arabic text normalization for OCR post-processing.

Handles the common Arabic OCR challenges:
- Tatweel (ـ) removal
- Alef variant normalization (أ إ آ ا → ا)
- Yeh / Alef Maqsura (ى → ي)
- Taa Marbuta (ة → ه or ت depending on context)
- Arabic diacritic removal
- BiDi (bidirectional) text handling with python-bidi
- Arabic-Persian character differences (پ چ ژ ڤ)
- Presentation form to base character conversion
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from core.logger import setup_logger

logger = setup_logger(__name__)


class ArabicNormalizer:
    """Normalizes Arabic (and Arabic-script) OCR text.

    This class handles the many orthographic variants that Arabic,
    Persian, and Urdu share, which make raw OCR output unreliable
    for matching against book metadata from APIs.

    Key normalizations:
    1. Tatweel (ـ, U+0640) removal — stretched justification character
    2. Alef variants (أ إ آ أ) → bare Alef (ا)
    3. Yeh (ي) ↔ Alef Maqsura (ى) normalization
    4. Taa Marbuta (ة → ت or ه)
    5. Diacritic (harakat) removal
    6. Presentation form B → base character
    7. Arabic-Persian digits → ASCII
    8. Whitespace normalization
    """

    # Tatweel (Arabic TATWEEL / Kashida)
    _TATWEEL = "\u0640"

    # Alef variants
    _ALEF_HAMZA_ABOVE = "\u0623"  # أ
    _ALEF_HAMZA_BELOW = "\u0625"  # إ
    _ALEF_MADDA = "\u0622"  # آ
    _ALEF_HAMZA_MAQSURA = "\u0629"  # ة (also Taa Marbuta)
    _ALEF = "\u0627"  # ا

    # Yeh
    _YEH = "\u064A"  # ي
    _ALEF_MAQSURA = "\u0649"  # ى

    # Taa Marbuta
    _TAA_MARBUFA = "\u0629"  # ة (same as Alef Hamza Maqsura in this context)

    # Persian-specific characters
    _PE = "\u067E"  # پ
    _CHE = "\u0686"  # چ
    _ZHE = "\u0698"  # ژ
    _VAV = "\u0648"  # و

    # Arabic diacritics block
    _DIACRITICS_PATTERN = re.compile(r"[\u064B-\u065F\u0670]")

    # Arabic-Indic digits
    _ARABIC_DIGITS = "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
    _EXTENDED_ARABIC_DIGITS = "\u06F0\u06F1\u06F2\u06F3\u06F4\u06F5\u06F6\u06F7\u06F8\u06F9"
    _ARABIC_DIGIT_MAP = str.maketrans(_ARABIC_DIGITS + _EXTENDED_ARABIC_DIGITS, "01234567890123456789")

    def __init__(self) -> None:
        pass

    def normalize(self, text: str) -> str:
        """Apply full Arabic normalization pipeline to *text*.

        Args:
            text: Raw OCR text (may contain Arabic).

        Returns:
            Normalized text with standard Arabic orthography.
        """
        if not text:
            return text

        original = text

        # 1. Normalize Unicode to NFC form first
        text = unicodedata.normalize("NFC", text)

        # 2. Remove tatweel
        text = text.replace(self._TATWEEL, "")

        # 3. Normalize alef variants to bare alef (ا)
        for alef_variant in [
            self._ALEF_HAMZA_ABOVE,
            self._ALEF_HAMZA_BELOW,
            self._ALEF_MADDA,
        ]:
            text = text.replace(alef_variant, self._ALEF)

        # 4. Normalize Alef Maqsura (ى) → Yeh (ي)
        text = text.replace(self._ALEF_MAQSURA, self._YEH)

        # 5. Normalize Taa Marbuta: typically → ه (for general comparison)
        #    But in book metadata, it's often the publisher name ending.
        #    We normalize to ه for consistency with most Arabic APIs.
        text = text.replace(self._TAA_MARBUFA, "\u0647")  # ه

        # 6. Remove diacritics (harakat)
        text = self._DIACRITICS_PATTERN.sub("", text)

        # 7. Convert Arabic-Indic digits to ASCII
        text = text.translate(self._ARABIC_DIGIT_MAP)

        # 8. Normalize presentation forms to base characters
        #    (e.g., isolated/initial/final/medial forms → base)
        text = self._normalize_presentation_forms(text)

        # 9. Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()

        if text != original:
            logger.debug("Arabic normalized: %r → %r", original[:50], text[:50])

        return text

    def _normalize_presentation_forms(self, text: str) -> str:
        """Convert Arabic presentation form A/B characters to base forms.

        This handles the case where OCR returns pre-composed presentation
        forms (isolated/initial/final/medial) rather than base letters.
        """
        # Presentation Forms-A range: FB50–FDFF
        # Presentation Forms-B range: FE70–FEFF
        cleaned = []
        for ch in text:
            code = ord(ch)
            if 0xFB50 <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF:
                # Try to get the base character via unicodedata.normalize('NFKC')
                # NFKC decomposes presentation forms into base characters
                decomposed = unicodedata.normalize("NFKC", ch)
                cleaned.append(decomposed)
            else:
                cleaned.append(ch)
        return "".join(cleaned)

    def apply_bidi(self, text: str) -> str:
        """Apply bidirectional text reordering for mixed RTL/LTR display.

        Uses python-bidi (already installed) to correctly order
        mixed Arabic/Latin text for display and downstream processing.

        Args:
            text: Text that may contain Arabic (RTL) and Latin (LTR) segments.

        Returns:
            Text with Bidi control applied for proper visual ordering.
        """
        if not self.has_arabic(text):
            return text
        try:
            from bidi.algorithm import get_display  # type: ignore[import-not-found]
            display = get_display(text, base_dir="R")
            return display
        except ImportError:
            logger.debug("python-bidi not available; skipping Bidi reordering")
            return text
        except Exception as exc:
            logger.debug("Bidi application failed: %s", exc)
            return text

    @staticmethod
    def has_arabic(text: str) -> bool:
        """Check if *text* contains any Arabic script characters."""
        if not text:
            return False
        for ch in text:
            code = ord(ch)
            if ArabicNormalizer._in_arabic_range(code):
                return True
        return False

    @staticmethod
    def _in_arabic_range(code: int) -> bool:
        """Check if a Unicode codepoint is in an Arabic script range."""
        if 0x0600 <= code <= 0x06FF:
            return True
        for start, end in [
            (0x0750, 0x077F),
            (0x08A0, 0x08FF),
            (0xFB50, 0xFDFF),
            (0xFE70, 0xFEFF),
        ]:
            if start <= code <= end:
                return True
        return False

    def normalize_title_author(self, title: str, author: str) -> tuple[str, str]:
        """Normalize both title and author, handling mixed script contexts.

        For Arabic titles, we keep the Arabic normalization but do NOT
        apply Bidi reordering (which changes character order) — instead
        we normalize to a canonical form suitable for API search comparison.
        """
        norm_title = self.normalize(title) if title else title
        norm_author = self.normalize(author) if author else author
        return norm_title, norm_author

    @staticmethod
    def is_arabic_title(line: str) -> bool:
        """Quick check if a line is primarily Arabic text."""
        if not line:
            return False
        from services.script_detector import ScriptDetector
        detector = ScriptDetector()
        counts = detector._script_counts(line)  # noqa: SLF001
        total = sum(counts.values())
        if total == 0:
            return False
        return counts.get("arabic", 0) / total > 0.3

    def transliterate_arabic_to_latin(self, text: str) -> str:
        """Provide a rough Latin transliteration of Arabic text.

        This is used as a fallback search strategy when Arabic text
        search returns no results — many book APIs use Latin transcriptions.

        This is a simple character-level mapping, NOT a full transliteration
        system. Good enough for search fuzziness.
        """
        if not text:
            return ""
        mapping = {
            "ا": "a", "أ": "a", "إ": "i", "آ": "a",
            "ب": "b", "ت": "t", "ث": "th", "ج": "j",
            "ح": "h", "خ": "kh", "د": "d", "ذ": "dh",
            "ر": "r", "ز": "z", "س": "s", "ش": "sh",
            "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
            "ع": "3", "غ": "gh", "ف": "f", "ق": "q",
            "ك": "k", "ل": "l", "م": "m", "ن": "n",
            "ه": "h", "و": "w", "ي": "y", "ى": "y",
            "ة": "t", "ء": "2", "ئ": "2", "ؤ": "w",
            "ً": "", "ٌ": "", "ٍ": "", "َ": "", "ُ": "",
            "ِ": "", "ّ": "", "ْ": "",
        }
        result = []
        for ch in text:
            result.append(mapping.get(ch, ch))
        return "".join(result)
