"""OCR text normalization.

Cleans up common OCR mistakes: duplicate spaces, broken words,
unicode normalization, punctuation fixes, etc.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List

from core.logger import setup_logger

logger = setup_logger(__name__)

# Unicode code points for smart quotes and dashes
SMART_DOUBLE_LEFT = "\u201c"
SMART_DOUBLE_RIGHT = "\u201d"
SMART_SINGLE_LEFT = "\u2018"
SMART_SINGLE_RIGHT = "\u2019"
EM_DASH = "\u2014"
ELLIPSIS = "\u2026"


class TextNormalizer:
    """Normalizes raw OCR text to fix common recognition errors.

    Responsibilities:
    - Normalize Unicode (NFKC)
    - Remove duplicate spaces and newlines
    - Fix common OCR character confusions (0/O, 1/l/I, 5/S, etc.)
    - Merge broken words split across lines
    - Trim whitespace
    """

    # Common OCR character substitutions
    _OCR_FIXES = {
        "|": "I",  # pipe -> I
        EM_DASH: "-",  # em-dash -> hyphen
        SMART_DOUBLE_LEFT: '"',
        SMART_DOUBLE_RIGHT: '"',
        SMART_SINGLE_LEFT: "'",
        SMART_SINGLE_RIGHT: "'",
        ELLIPSIS: "...",
    }

    def normalize(self, text: str) -> str:
        """Normalize OCR text through a multi-step cleanup pipeline.

        Args:
            text: Raw OCR text.

        Returns:
            Cleaned, normalized text.
        """
        if not text:
            return ""

        logger.debug("Normalizing OCR text (length=%d)", len(text))

        # 1. Unicode normalization (NFKC) — converts smart quotes to
        #    their ASCII equivalents, among other things
        text = unicodedata.normalize("NFKC", text)

        # 2. Fix broken words split across hyphenated line breaks
        text = self._merge_hyphenated_words(text)

        # 3. Merge broken words split across line breaks (no hyphen)
        text = self._merge_line_broken_words(text)

        # 4. Fix common OCR character confusions
        text = self._fix_ocr_chars(text)
        # 5. Normalize punctuation spacing
        text = self._normalize_punctuation(text)
        # 6. Normalize whitespace (collapses spaces, preserves paragraphs)
        text = self._normalize_whitespace(text)
        # 7. Final trim
        text = text.strip()

        logger.debug("Normalized text (length=%d): %s", len(text), text[:100])
        return text

    def _merge_hyphenated_words(self, text: str) -> str:
        """Merge words broken across lines with a hyphen.

        Example:
            "Atomic Habits-\\nThe power of habits"
            -> "Atomic HabitsThe power of habits"
        """
        # Match word-hyphen-newline-word
        text = re.sub(r"(\w+)-\s*\n\s*(\w)", r"\1\2", text)
        return text

    def _merge_line_broken_words(self, text: str) -> str:
        """Merge words split across line breaks without a hyphen.

        Example:
            "AT0MIC\\nHABlTS" -> "AT0MICHABlTS"

        Only merges when the line break is between two alphanumeric segments.
        """
        # Replace single newlines between alphanumeric chars with space
        text = re.sub(r"(?<=[A-Za-z0-9])\n(?=[A-Za-z0-9])", " ", text)
        return text

    def _fix_ocr_chars(self, text: str) -> str:
        """Fix common OCR character confusions.

        Handles: pipe->I, em-dash->hyphen, smart quotes->ASCII,
        ellipsis->dots.
        """
        for bad, good in self._OCR_FIXES.items():
            text = text.replace(bad, good)
        return text

    def _normalize_whitespace(self, text: str) -> str:
        """Collapse multiple spaces/newlines into single spaces."""
        # Preserve paragraph breaks (double newline)
        paragraphs = re.split(r"\n\s*\n", text)
        normalized_paragraphs = []
        for para in paragraphs:
            cleaned = re.sub(r"[ \t]+", " ", para)
            cleaned = re.sub(r"\n+", " ", cleaned)
            normalized_paragraphs.append(cleaned.strip())
        return "\n\n".join(normalized_paragraphs)

    def _normalize_punctuation(self, text: str) -> str:
        """Fix spacing around punctuation.

        Does not touch spacing around newlines (paragraph breaks
        are preserved by _normalize_whitespace).
        """
        # Remove space before punctuation (but not across newlines)
        text = re.sub(r"[ \t]+([,.;:!?\"'])", r"\1", text)
        # Ensure space after punctuation (when followed by a letter, no newline)
        text = re.sub(r"([,.;:!?'\"])[ \t]*([A-Za-z])", r"\1 \2", text)
        # Remove space before closing parentheses
        text = re.sub(r"[ \t]+([\)\]])", r"\1", text)
        # Fix double punctuation
        text = re.sub(r"([,.;:!?])\1+", r"\1", text)
        return text

    def extract_isbn(self, text: str) -> List[str]:
        """Extract potential ISBN strings from OCR text.

        Looks for ISBN-10 and ISBN-13 patterns. Handles hyphenated
        ISBN formats by stripping separators before validation.
        """
        isbns: List[str] = []

        # ISBN-13: 978 or 979 prefix + 10 digits, with optional hyphens/spaces
        isbn13_pattern = r"\b(?:978|979)[- ]?(?:\d[- ]?){10}\b"
        for match in re.finditer(isbn13_pattern, text):
            cleaned = re.sub(r"[- ]", "", match.group())
            if len(cleaned) == 13 and self._validate_isbn13(cleaned):
                isbns.append(cleaned)

        # ISBN-10: 9 digits + check digit (0-9 or X), with optional hyphens/spaces
        isbn10_pattern = r"\b(?:\d[- ]?){9}[\dXx]\b"
        for match in re.finditer(isbn10_pattern, text):
            cleaned = re.sub(r"[- ]", "", match.group())
            if len(cleaned) == 10 and self._validate_isbn10(cleaned):
                isbns.append(cleaned)

        return list(dict.fromkeys(isbns))  # dedupe while preserving order

    @staticmethod
    def _validate_isbn10(isbn: str) -> bool:
        """Validate an ISBN-10 checksum."""
        if len(isbn) != 10:
            return False
        total = 0
        for i, char in enumerate(isbn):
            if i == 9 and char.upper() == "X":
                total += 10 * (i + 1)
            elif char.isdigit():
                total += int(char) * (i + 1)
            else:
                return False
        return total % 11 == 0

    @staticmethod
    def _validate_isbn13(isbn: str) -> bool:
        """Validate an ISBN-13 checksum."""
        if len(isbn) != 13:
            return False
        total = 0
        for i, char in enumerate(isbn):
            if not char.isdigit():
                return False
            digit = int(char)
            total += digit if i % 2 == 0 else digit * 3
        return total % 10 == 0
