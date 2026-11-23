"""Script detection for OCR text.

Public API (used by adaptive_ocr.py, arabic_normalizer.py):
    detector = ScriptDetector()
    script = detector.detect(text)              # dominant script name
    langs = detector.get_language_codes(text)
    detector.needs_bidirectional(text)          # True for RTL scripts
    detector.detect_all(text)                   # set of all scripts present
    detector._script_counts(text)               # per-script char counts

Static helpers (used by enhanced_text_extractor.py):
    ScriptDetector.detect_scripts(text) -> Set[str]
    ScriptDetector.is_cyrillic(text) -> bool
    ScriptDetector.is_arabic(text) -> bool
    ScriptDetector.is_latin(text) -> bool
"""

from __future__ import annotations

from typing import Dict, Set

from core.logger import setup_logger

logger = setup_logger(__name__)


class ScriptDetector:
    """Detects the script/writing system of OCR text via Unicode ranges."""

    # Unicode script ranges (inclusive)
    _RANGES: Dict[str, tuple] = {
        "latin": (0x0041, 0x024F),  # Basic Latin + Latin Extended
        "greek": (0x0370, 0x03FF),
        "cyrillic": (0x0400, 0x04FF),
        "hebrew": (0x0590, 0x05FF),
        "arabic": (0x0600, 0x06FF),
        "devanagari": (0x0900, 0x097F),
        "thai": (0x0E00, 0x0E7F),
        "cjk": (0x4E00, 0x9FFF),
    }

    # OCR language codes per script
    _OCR_LANGS: Dict[str, list] = {
        "latin": ["en", "fr"],
        "greek": ["el"],
        "cyrillic": ["ru"],
        "hebrew": [],
        "arabic": ["ar"],
        "devanagari": ["hi"],
        "thai": ["th"],
        "cjk": ["ch_sim"],
    }

    # Right-to-left scripts
    _RTL_SCRIPTS = {"arabic", "hebrew"}

    def __init__(self) -> None:
        pass

    # --- Instance API (original production interface) ---

    def detect(self, text: str) -> str:
        """Return the dominant script of the text.

        Empty text defaults to 'latin' (safe default for the pipeline).
        """
        if not text:
            return "latin"
        counts = self._script_counts(text)
        if not counts:
            return "latin"
        # Dominant script by character count
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def detect_all(self, text: str) -> Set[str]:
        """Return the set of all scripts present in the text."""
        if not text:
            return set()
        return set(self._script_counts(text).keys())

    def get_language_codes(self, text: str) -> list:
        """Map detected scripts to language codes for OCR.

        Uses the script-to-language mapping to return the appropriate
        language codes for the detected scripts in `text`.
        """
        scripts = self.detect_all(text)
        if not scripts:
            return ["en"]
        langs = []
        for script in scripts:
            langs.extend(self._OCR_LANGS.get(script, []))
        if not langs:
            langs = ["en"]
        return list(dict.fromkeys(langs))

    def needs_bidirectional(self, text: str) -> bool:
        """True when the text contains RTL scripts (needs BiDi handling)."""
        return bool(self.detect_all(text) & self._RTL_SCRIPTS)

    def _script_counts(self, text: str) -> Dict[str, int]:
        """Count characters per script (alphabetic only)."""
        counts: Dict[str, int] = {}
        for ch in text:
            if not ch.isalpha():
                continue
            cp = ord(ch)
            for script, (lo, hi) in self._RANGES.items():
                if lo <= cp <= hi:
                    counts[script] = counts.get(script, 0) + 1
                    break
        return counts

    # --- Static helpers (Phase 43.5) ---

    @staticmethod
    def detect_scripts(text: str) -> Set[str]:
        """Detect which scripts are present in the text.

        Returns:
            Set of script names: 'latin', 'cyrillic', 'arabic', 'unknown'
        """
        if not text:
            return {"unknown"}

        detector = ScriptDetector()
        scripts = detector.detect_all(text)
        if not scripts:
            return {"unknown"}
        return scripts

    @staticmethod
    def is_cyrillic(text: str) -> bool:
        """Check if text contains significant Cyrillic characters."""
        if not text:
            return False
        counts = ScriptDetector()._script_counts(text)
        total = sum(counts.values())
        if total == 0:
            return False
        return counts.get("cyrillic", 0) / total > 0.3

    @staticmethod
    def is_arabic(text: str) -> bool:
        """Check if text contains significant Arabic characters."""
        if not text:
            return False
        counts = ScriptDetector()._script_counts(text)
        total = sum(counts.values())
        if total == 0:
            return False
        return counts.get("arabic", 0) / total > 0.3

    @staticmethod
    def is_latin(text: str) -> bool:
        """Check if text is primarily Latin script."""
        if not text:
            return False
        counts = ScriptDetector()._script_counts(text)
        total = sum(counts.values())
        if total == 0:
            return False
        return counts.get("latin", 0) / total > 0.7
