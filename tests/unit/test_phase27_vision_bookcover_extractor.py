"""Unit tests for Phase 27 vision-model book-cover extractor.

Tests the VisionBookCoverExtractor's JSON parsing and comparison utilities.
The actual vision model calls are tested via integration benchmark.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.vision_bookcover_extractor import (
    VisionBookCoverExtractor,
    VisionMetadata,
    normalize_for_comparison,
    fuzzy_match,
    compare_title,
    compare_author,
    _normalize_orientation,
)


class TestJsonParsing:
    """Test extract_from_response handles various response formats."""

    def test_extract_clean_json(self):
        extractor = VisionBookCoverExtractor()
        data = {
            "title": "Le Mal Eté",
            "title_confidence": 0.95,
            "author": "Jean Vuilleumier",
            "author_confidence": 0.95,
            "publisher": "Editions l'Age d'Homme",
            "publisher_confidence": 0.95,
            "subtitle": None,
            "orientation": "normal",
            "multiple_books_detected": False,
            "target_book_identified": True,
            "other_text": ["Roman"],
            "uncertainties": [],
        }
        response = json.dumps(data)
        meta = extractor.extract_from_response(response)
        assert meta.title == "Le Mal Eté"
        assert meta.title_confidence == 0.95
        assert meta.author == "Jean Vuilleumier"
        assert meta.author_confidence == 0.95
        assert meta.publisher == "Editions l'Age d'Homme"
        assert meta.publisher_confidence == 0.95
        assert meta.orientation == "normal"
        assert meta.multiple_books_detected is False
        assert meta.target_book_identified is True
        assert meta.other_text == ["Roman"]
        assert meta.uncertainties == []

    def test_extract_markdown_fenced_json(self):
        extractor = VisionBookCoverExtractor()
        data = {
            "title": "Abel Sanchez",
            "title_confidence": 1.0,
            "author": "Miguel de Unamuno",
            "author_confidence": 1.0,
            "publisher": "L'Age d'Homme",
            "publisher_confidence": 1.0,
            "orientation": "normal",
            "multiple_books_detected": False,
            "target_book_identified": True,
            "other_text": [],
            "uncertainties": [],
        }
        response = "```json\n" + json.dumps(data) + "\n```"
        meta = extractor.extract_from_response(response)
        assert meta.title == "Abel Sanchez"
        assert meta.author == "Miguel de Unamuno"

    def test_extract_with_extra_text(self):
        """Model response may have explanatory text before/after JSON."""
        extractor = VisionBookCoverExtractor()
        data = {
            "title": "Histoires d'amour",
            "title_confidence": 1.0,
            "author": "Jean-Marie Agier",
            "author_confidence": 1.0,
            "publisher": "L'Age d'Homme",
            "publisher_confidence": 1.0,
            "orientation": "normal",
            "multiple_books_detected": False,
            "target_book_identified": True,
            "other_text": [],
            "uncertainties": [],
        }
        response = "Here is the analysis:\n```json\n" + json.dumps(data) + "\n```\nThat is the result."
        meta = extractor.extract_from_response(response)
        assert meta.title == "Histoires d'amour"
        assert meta.author == "Jean-Marie Agier"

    def test_extract_null_fields(self):
        extractor = VisionBookCoverExtractor()
        data = {
            "title": None,
            "title_confidence": 0.0,
            "author": None,
            "author_confidence": 0.0,
            "publisher": None,
            "publisher_confidence": 0.0,
            "orientation": "unknown",
            "multiple_books_detected": False,
            "target_book_identified": False,
            "other_text": [],
            "uncertainties": ["Author not visible on cover"],
        }
        response = json.dumps(data)
        meta = extractor.extract_from_response(response)
        assert meta.title is None
        assert meta.author is None
        assert meta.publisher is None
        assert meta.orientation == "unknown"
        assert meta.target_book_identified is False
        assert meta.uncertainties == ["Author not visible on cover"]

    def test_extract_with_uncertainties(self):
        extractor = VisionBookCoverExtractor()
        data = {
            "title": "Partial Title",
            "title_confidence": 0.60,
            "author": None,
            "author_confidence": 0.0,
            "publisher": None,
            "publisher_confidence": 0.0,
            "orientation": "rotated_180",
            "multiple_books_detected": True,
            "target_book_identified": True,
            "other_text": [],
            "uncertainties": [
                "Target book is upside down",
                "Author partially obscured by another book",
                "Publisher text not visible",
            ],
        }
        response = json.dumps(data)
        meta = extractor.extract_from_response(response)
        assert meta.title == "Partial Title"
        assert meta.title_confidence == 0.60
        assert meta.author is None
        assert len(meta.uncertainties) == 3
        assert "Target book is upside down" in meta.uncertainties

    def test_extract_uptight_orientation(self):
        """Vision model may return 'upright' — should be normalized to 'normal'."""
        extractor = VisionBookCoverExtractor()
        data = {
            "title": "Test",
            "title_confidence": 0.95,
            "author": "Author",
            "author_confidence": 0.95,
            "publisher": None,
            "publisher_confidence": 0.0,
            "orientation": "upright",
            "multiple_books_detected": False,
            "target_book_identified": True,
            "other_text": [],
            "uncertainties": [],
        }
        meta = extractor.extract_from_response(json.dumps(data))
        assert meta.orientation == "normal"


class TestNormalizeComparison:
    """Test text normalization for fuzzy matching."""

    def test_case_insensitive(self):
        assert normalize_for_comparison("LE MAL ETÉ") == normalize_for_comparison("le mal eté")

    def test_accent_insensitive(self):
        assert normalize_for_comparison("CRÉATION") == normalize_for_comparison("creation")

    def test_punctuation_stripped(self):
        assert normalize_for_comparison("Love, Life!") == normalize_for_comparison("Love Life")

    def test_cyrillic_normalized(self):
        assert normalize_for_comparison("Философия") == normalize_for_comparison("философия")


class TestFuzzyMatch:
    def test_fuzzy_match_exact(self):
        assert fuzzy_match("Le Mal Eté", "Le Mal Eté") is True

    def test_fuzzy_match_case(self):
        assert fuzzy_match("LE MAL ETÉ", "le mal eté") is True

    def test_fuzzy_match_accent(self):
        assert fuzzy_match("création", "creation") is True

    def test_fuzzy_match_partial(self):
        assert fuzzy_match("Histoires d'amour mais petites-bourgeoises", "Histoires d'amour", 0.5) is True

    def test_fuzzy_match_different(self):
        assert fuzzy_match("Viols", "Le Mal Eté") is False

    def test_fuzzy_match_none(self):
        assert fuzzy_match(None, "title") is False
        assert fuzzy_match("title", None) is False
        assert fuzzy_match(None, None) is True


class TestCompareFunctions:
    """Test compare_title and compare_author helpers."""

    def test_compare_title_exact(self):
        assert compare_title("Le Mal Eté", "Le Mal Eté") is True

    def test_compare_title_case_insensitive(self):
        assert compare_title("LE MAL ETÉ", "Le mal eté") is True

    def test_compare_title_accent_insensitive(self):
        assert compare_title("création", "Creation") is True

    def test_compare_title_partial_overlap(self):
        assert compare_title("Histoires d'amour mais petites-bourgeoises", "Histoires d'amour") is True

    def test_compare_author_exact(self):
        assert compare_author("Jean Vuilleumier", "Jean Vuilleumier") is True

    def test_compare_author_case(self):
        assert compare_author("LE CORBUSIER", "Le Corbusier") is True

    def test_compare_author_list_first_match(self):
        assert compare_author("Pavle Ivić, Nikola Samardžić", "Pavle Ivic") is True
        assert compare_author("Pavle Ivić, Nikola Samardžić", "Nikola Samardžić") is True


class TestVisionMetadata:

    def test_to_dict(self):
        meta = VisionMetadata(
            title="Test Title",
            title_confidence=0.95,
            author="Test Author",
            author_confidence=0.95,
            publisher="Test Publisher",
            publisher_confidence=0.90,
            subtitle=None,
            orientation="rotated_180",
            multiple_books_detected=True,
            target_book_identified=True,
            other_text=["series"],
            uncertainties=[],
        )
        d = meta.to_dict()
        assert d["title"] == "Test Title"
        assert d["title_confidence"] == 0.95
        assert d["author"] == "Test Author"
        assert d["author_confidence"] == 0.95
        assert d["publisher"] == "Test Publisher"
        assert d["publisher_confidence"] == 0.90
        assert d["orientation"] == "rotated_180"
        assert d["multiple_books_detected"] is True
        assert d["target_book_identified"] is True
        assert d["uncertainties"] == []

    def test_defaults(self):
        meta = VisionMetadata()
        assert meta.title is None
        assert meta.author is None
        assert meta.publisher is None
        assert meta.subtitle is None
        assert meta.orientation == "normal"
        assert meta.multiple_books_detected is False
        assert meta.target_book_identified is True
        assert meta.other_text == []
        assert meta.uncertainties == []
        assert meta.title_confidence == 0.0
        assert meta.author_confidence == 0.0
        assert meta.publisher_confidence == 0.0


class TestOrientationHandling:

    def test_rotated_180(self):
        meta = VisionMetadata(
            title="Le Mal Eté",
            author="Jean Vuilleumier",
            orientation="rotated_180",
        )
        assert meta.orientation == "rotated_180"

    def test_valid_orientations(self):
        for orient in ["normal", "rotated_90_cw", "rotated_90_ccw", "rotated_180", "unknown"]:
            meta = VisionMetadata(orientation=orient)
            assert meta.orientation == orient


class TestOrientationNormalization:

    def test_upright_to_normal(self):
        assert _normalize_orientation("upright") == "normal"

    def test_180_variants(self):
        assert _normalize_orientation("rotated_180") == "rotated_180"
        assert _normalize_orientation("180") == "rotated_180"
        assert _normalize_orientation("upside_down") == "rotated_180"
        assert _normalize_orientation("upside-down") == "rotated_180"

    def test_90_variants(self):
        assert _normalize_orientation("rotated_90_cw") == "rotated_90_cw"
        assert _normalize_orientation("rotated_90") == "rotated_90_cw"
        assert _normalize_orientation("90") == "rotated_90_cw"

    def test_270_variants(self):
        assert _normalize_orientation("rotated_90_ccw") == "rotated_90_ccw"
        assert _normalize_orientation("rotated_270") == "rotated_90_ccw"
        assert _normalize_orientation("270") == "rotated_90_ccw"

    def test_unknown_variants(self):
        assert _normalize_orientation("unknown") == "unknown"
        assert _normalize_orientation("none") == "unknown"
        assert _normalize_orientation("") == "unknown"
        assert _normalize_orientation(None) == "unknown"
