"""Unit tests for Phase 26 PaddleOCR-VL book-cover extraction (BookCoverExtractor).

Tests focus on:
- Title/author extraction from layout blocks with various label patterns
- Publisher text filtering (collection names, imprint lines)
- Multi-line doc_title blocks that merge title+author (splitting)
- Upside-down image handling (orientation correction by API)
- Multiple books on same cover (selection of primary book)
- Title/author position heuristics (author above title, etc.)
- Person name detection for all-caps and mixed-case text
- Title case normalization (all-caps OCR → proper case)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.paddleocr_vl_bookcover_extractor import (
    BookCoverExtractor,
    LayoutBlock,
    ExtractionOutput,
)


# --- Fixtures ---

@pytest.fixture
def extractor():
    return BookCoverExtractor(image_width=900, image_height=1600)


@pytest.fixture
def merged_title_author_block():
    """doc_title block with title+author merged into one block.

    E.g., for "Abel Sanchez" by Miguel de Unamuno, the API returns:
      doc_title: "Miguel de Unamuno\nAbel Sanchez"
      The extractor must split: title="Abel Sanchez", author="Miguel de Unamuno"
    """
    return [
        LayoutBlock(
            text="Miguel de Unamuno\nAbel Sanchez",
            bbox=[179, 97, 757, 475],
            label="doc_title",
            score=1.0,
            order=0,
        ),
        LayoutBlock(
            text="Traduction de l'espagnol\n: et postface\nde Maurice Gahait",
            bbox=[135, 475, 720, 600],
            label="text",
            score=1.0,
            order=1,
        ),
        LayoutBlock(
            text="Vent d'\'est",
            bbox=[200, 700, 400, 800],
            label="text",
            score=1.0,
            order=2,
        ),
        LayoutBlock(
            text="L'Age d'Homme",
            bbox=[200, 1000, 400, 1100],
            label="text",
            score=1.0,
            order=3,
        ),
    ]


@pytest.fixture
def separate_title_author_blocks():
    """API returns title and author as separate blocks.

    E.g., for "Le Mal Eté" by Jean Vuilleumier:
      doc_title: "Le Mal Eté" (bbox y:556-707)
      text: "Jean Vuilleumier" (bbox y:173-270)
    """
    return [
        LayoutBlock(
            text="Jean Vuilleumier",
            bbox=[150, 173, 676, 270],
            label="text",
            score=1.0,
            order=0,
        ),
        LayoutBlock(
            text="Le Mal Eté",
            bbox=[135, 556, 710, 707],
            label="doc_title",
            score=1.0,
            order=1,
        ),
        LayoutBlock(
            text="Roman",
            bbox=[300, 773, 500, 790],
            label="text",
            score=1.0,
            order=2,
        ),
        LayoutBlock(
            text="L'Age d'Homme",
            bbox=[300, 1259, 500, 1280],
            label="text",
            score=1.0,
            order=3,
        ),
    ]


@pytest.fixture
def upside_down_blocks():
    """Blocks from a 180°-rotated image.

    The API detects orientation=180 and applies doc_preprocessor correction,
    returning text in upright orientation. The extractor should produce
    correct title/author regardless of the original orientation.
    """
    return [
        LayoutBlock(
            text="Jean Vuilleumier",
            bbox=[150, 173, 676, 270],
            label="text",
            score=1.0,
            order=0,
        ),
        LayoutBlock(
            text="Le Mal Eté",
            bbox=[135, 556, 710, 707],
            label="doc_title",
            score=1.0,
            order=1,
        ),
        LayoutBlock(
            text="Roman",
            bbox=[300, 773, 500, 790],
            label="text",
            score=1.0,
            order=2,
        ),
    ]


@pytest.fixture
def multiple_books_blocks():
    """Cover with two books visible.

    Book A (the actual cover) is the primary book.
    Book B is another book visible on the same cover.
    The extractor should select only Book A's title/author.
    """
    return [
        # Book A — primary
        LayoutBlock(
            text="ABEL SANCHEZ",
            bbox=[200, 300, 600, 400],
            label="doc_title",
            score=1.0,
            order=0,
        ),
        LayoutBlock(
            text="MIGUEL DE UNAMUNO",
            bbox=[250, 450, 550, 500],
            label="text",
            score=1.0,
            order=1,
        ),
        # Book B — secondary, should be excluded (placed well below Book A)
        LayoutBlock(
            text="OTHER BOOK TITLE",
            bbox=[200, 1100, 600, 1200],
            label="doc_title",
            score=1.0,
            order=2,
        ),
        LayoutBlock(
            text="SOME OTHER AUTHOR",
            bbox=[250, 1300, 550, 1400],
            label="text",
            score=1.0,
            order=3,
        ),
        LayoutBlock(
            text="L'AGE D'HOMME",
            bbox=[300, 1500, 500, 1530],
            label="text",
            score=1.0,
            order=4,
        ),
    ]


@pytest.fixture
def publisher_text_blocks():
    """Cover with publisher/collection text that must be filtered out."""
    return [
        LayoutBlock(
            text="ÉCRITS SUR L'ART\nL'AGE D'HOMME",
            bbox=[200, 600, 500, 680],
            label="text",
            score=1.0,
            order=0,
        ),
        LayoutBlock(
            text="LA VIOLENCE\nDE L'ESPRIT ET\nLA PITIÉ DU CŒUR",
            bbox=[150, 500, 700, 800],
            label="doc_title",
            score=1.0,
            order=1,
        ),
        LayoutBlock(
            text="MARCEL PONCET",
            bbox=[300, 850, 500, 880],
            label="text",
            score=1.0,
            order=2,
        ),
    ]


@pytest.fixture
def all_caps_title_author():
    """OCR returns all-caps text — needs case normalization for matching."""
    return [
        LayoutBlock(
            text="LE MAL ETÉ",
            bbox=[135, 556, 710, 707],
            label="doc_title",
            score=1.0,
            order=0,
        ),
        LayoutBlock(
            text="JEAN VUILLEUMIER",
            bbox=[150, 173, 676, 270],
            label="text",
            score=1.0,
            order=1,
        ),
    ]


@pytest.fixture
def no_doc_title_blocks():
    """Image where API returns no doc_title label — all text blocks.

    Title is the largest text block (paragraph_title or large text).
    """
    return [
        LayoutBlock(
            text="LES DÉSASTRES\nde la guerre",
            bbox=[100, 400, 700, 626],
            label="paragraph_title",
            score=1.0,
            order=0,
        ),
        LayoutBlock(
            text="Jeanlouis Cornuz",
            bbox=[150, 173, 500, 270],
            label="text",
            score=1.0,
            order=1,
        ),
        LayoutBlock(
            text="Collection Points",
            bbox=[300, 700, 500, 730],
            label="text",
            score=1.0,
            order=2,
        ),
    ]


# --- Title/Author Extraction Tests ---

class TestTitleAuthorExtraction:
    """Test title and author extraction from various block configurations."""

    def test_extract_separate_title_author(self, extractor, separate_title_author_blocks):
        """Title and author in separate blocks should be correctly identified."""
        output = extractor.extract(separate_title_author_blocks)
        assert "Mal" in output.title
        assert "Eté" in output.title
        assert "Jean" in output.author
        assert "Vuilleumier" in output.author

    def test_extract_merged_title_author(self, extractor, merged_title_author_block):
        """When doc_title merges title+author, the extractor must split them.

        "Miguel de Unamuno\nAbel Sanchez" -> title="Abel Sanchez", author="Miguel de Unamuno"
        """
        output = extractor.extract(merged_title_author_block)
        assert "Abel" in output.title
        assert "Sanchez" in output.title
        assert "Miguel" in output.author
        assert "Unamuno" in output.author
        # Must NOT have author in title or vice versa
        assert "Unamuno" not in output.title
        assert "Abel" not in output.author

    def test_extract_with_orientation_180(self, extractor, upside_down_blocks):
        """Extract correctly when image was upside-down (orientation=180).

        The API handles rotation internally via doc_preprocessor.
        The extractor should produce same results regardless of orientation.
        """
        output = extractor.extract(upside_down_blocks, orientation=180)
        assert "Mal" in output.title
        assert "Eté" in output.title
        assert "Jean" in output.author
        assert "Vuilleumier" in output.author

    def test_extract_with_orientation_0(self, extractor, separate_title_author_blocks):
        """Extract correctly when image is upright (orientation=0)."""
        output = extractor.extract(separate_title_author_blocks, orientation=0)
        assert "Mal" in output.title
        assert "Vuilleumier" in output.author


# --- Multiple Books Tests ---

class TestMultipleBooks:
    """Tests for covers with multiple books visible."""

    def test_selects_primary_book(self, extractor, multiple_books_blocks):
        """When multiple books are visible, select the primary (largest) book."""
        output = extractor.extract(multiple_books_blocks)
        assert "ABEL" in output.title.upper()
        assert "SANCHEZ" in output.title.upper()
        # Should NOT pick the other book
        assert "OTHER BOOK" not in output.title.upper()
        assert "SOME OTHER AUTHOR" not in (output.author or "").upper()
        assert "MIGUEL" in output.author.upper()
        assert "UNAMUNO" in output.author.upper()

    def test_multiple_books_flagged(self, extractor, multiple_books_blocks):
        """Multiple books scenario should set the multiple_books_detected flag."""
        output = extractor.extract(multiple_books_blocks)
        assert output.multiple_books_detected is True


# --- Publisher Text Filtering Tests ---

class TestPublisherFiltering:
    """Tests for filtering out publisher/collection text."""

    def test_publisher_not_in_title(self, extractor, publisher_text_blocks):
        """Publisher text like "ECRITS SUR L'ART" must not appear in the title."""
        output = extractor.extract(publisher_text_blocks)
        title_upper = output.title.upper()
        assert "ECRITS SUR L'ART" not in title_upper
        assert "L'AGE D'HOMME" not in title_upper

    def test_author_not_publisher(self, extractor, publisher_text_blocks):
        """The author must be "MARCEL PONCET", not publisher text."""
        output = extractor.extract(publisher_text_blocks)
        assert "PONCET" in output.author.upper()
        assert "GALLIMARD" not in (output.author or "").upper()


# --- Case Normalization Tests ---

class TestCaseNormalization:
    """Tests for normalizing all-caps OCR output to proper case."""

    def test_normalize_all_caps_title(self, extractor, all_caps_title_author):
        """All-caps OCR text should be normalized for case-insensitive matching."""
        output = extractor.extract(all_caps_title_author)
        # The extracted title should be normalized (not all-caps)
        assert "MAL" not in output.title or "Mal" in output.title


# --- No doc_title Tests ---

class TestNoDocTitle:
    """Tests for images where the API doesn't return a doc_title label."""

    def test_extract_from_paragraph_title(self, extractor, no_doc_title_blocks):
        """When no doc_title exists, use paragraph_title as title candidate."""
        output = extractor.extract(no_doc_title_blocks)
        assert "Désastres" in output.title or "désastres" in output.title.lower()
        assert "guerre" in output.title.lower()
        assert "Jeanlouis" in output.author or "jeanlouis" in output.author.lower()
        assert "Cornuz" in output.author or "cornuz" in output.author.lower()


# --- Person Name Detection Tests ---

class TestPersonNameDetection:
    """Tests for _looks_like_person_name helper."""

    def test_single_proper_name(self, extractor):
        """Multi-word names should be detected."""
        assert extractor._looks_like_person_name("Jean Vuilleumier") is True
        assert extractor._looks_like_person_name("Miguel de Unamuno") is True

    def test_multi_word_name(self, extractor):
        """Multi-word proper name in title case."""
        assert extractor._looks_like_person_name("Jean Vuilleumier") is True
        assert extractor._looks_like_person_name("Marcel Poncet") is True

    def test_french_title_phrase_not_name(self, extractor):
        """French title phrases should NOT be detected as person names."""
        assert extractor._looks_like_person_name("La Construction") is False
        assert extractor._looks_like_person_name("Des Villes") is False
        assert extractor._looks_like_person_name("De la Guerre") is False

    def test_all_caps_name(self, extractor):
        """All-caps name."""
        assert extractor._looks_like_person_name("LE CORBUSIER") is True
        assert extractor._looks_like_person_name("ERIC KAYAYAN") is True

    def test_genre_text_not_name(self, extractor):
        """Genre text like "Roman" should not be detected as a person name."""
        assert extractor._looks_like_person_name("Roman") is False
        assert extractor._looks_like_person_name("ROMAN") is False


# --- Title Phrase Detection Tests ---

class TestTitlePhraseDetection:
    """Tests for _looks_like_title_phrase helper."""

    def test_french_title_words(self, extractor):
        """Text containing French title vocabulary -> title phrase."""
        assert extractor._looks_like_title_phrase("de la guerre") is True
        assert extractor._looks_like_title_phrase("des villes") is True
        assert extractor._looks_like_title_phrase("d'amour") is True
        assert extractor._looks_like_title_phrase("de l'esprit") is True

    def test_french_articles(self, extractor):
        """Text with French articles (de, la, le, des) -> title phrase."""
        assert extractor._looks_like_title_phrase("la") is True
        assert extractor._looks_like_title_phrase("des") is True
        assert extractor._looks_like_title_phrase("de") is True

    def test_not_title_phrase(self, extractor):
        """Real person names should NOT be detected as title phrases."""
        assert extractor._looks_like_title_phrase("Jean Vuilleumier") is False
        assert extractor._looks_like_title_phrase("Marcel Poncet") is False


# --- Title Case Normalization Tests ---

class TestNormalizeTitleCase:
    """Tests for _normalize_title_case helper."""

    def test_normalize_french_title(self, extractor):
        """All-caps French title should be lowercased except first word."""
        result = extractor._normalize_title_case("LA CONSTRUCTION DES VILLES")
        assert result.lower() == "la construction des villes"

    def test_normalize_title_with_apostrophe(self, extractor):
        """All-caps title with apostrophe handled correctly."""
        result = extractor._normalize_title_case("DE L'ESPRIT ET LA PITIÉ")
        assert "esprit" in result.lower()
        assert "pitié" in result.lower() or "pith" in result.lower()

    def test_normalize_single_word(self, extractor):
        """Single all-caps word."""
        assert extractor._normalize_title_case("ROMAN") == "Roman"
        assert extractor._normalize_title_case("MATH") == "Math"

    def test_preserve_mixed_case(self, extractor):
        """Already mixed-case text is returned unchanged."""
        assert extractor._normalize_title_case("Le Mal Eté") == "Le Mal Eté"

    def test_first_word_capitalized(self, extractor):
        """First word should be capitalized even if it's an article."""
        result = extractor._normalize_title_case("LE CHRETIEN DANS LA CITE")
        assert result.startswith("Le")


# --- Split Title Author Block Tests ---

class TestSplitTitleAuthorBlock:
    """Tests for _try_split_title_author_block helper."""

    def test_split_merged_title_author_both_names(self, extractor):
        """When both lines are names, author is first (top), title is second."""
        block = LayoutBlock(
            text="Miguel de Unamuno\nAbel Sanchez",
            bbox=[179, 97, 757, 475],
            label="doc_title",
            score=1.0,
            order=0,
        )
        result = extractor._try_split_title_author_block(block, [block])
        assert result is not None
        title, author = result
        assert "Abel" in title
        assert "Sanchez" in title
        assert "Miguel" in author
        assert "Unamuno" in author

    def test_no_split_for_single_line(self, extractor):
        """Single-line blocks should not be split."""
        block = LayoutBlock(
            text="Le Mal Eté",
            bbox=[135, 556, 710, 707],
            label="doc_title",
            score=1.0,
            order=0,
        )
        result = extractor._try_split_title_author_block(block, [block])
        assert result is None

    def test_no_split_for_two_line_title(self, extractor):
        """Two lines where neither is a name should not be split.

        e.g., "Les Désastres\nde la guerre" — both lines are title parts.
        """
        block = LayoutBlock(
            text="Les Désastres\nde la guerre",
            bbox=[100, 400, 700, 626],
            label="paragraph_title",
            score=1.0,
            order=0,
        )
        result = extractor._try_split_title_author_block(block, [block])
        assert result is None

    def test_split_one_name_one_title(self, extractor):
        """One line is a name, other is a title phrase -> split with name as author."""
        block = LayoutBlock(
            text="Jean Vuilleumier\nLe Mal Eté",
            bbox=[179, 97, 757, 475],
            label="doc_title",
            score=1.0,
            order=0,
        )
        result = extractor._try_split_title_author_block(block, [block])
        assert result is not None
        title, author = result
        assert "Mal" in title
        assert "Eté" in title
        assert "Jean" in author
        assert "Vuilleumier" in author


# --- Region Clustering Tests ---

class TestRegionClustering:
    """Tests for spatial clustering into regions."""

    def test_title_author_far_apart(self, extractor, separate_title_author_blocks):
        """Title and author should be identified even when far apart vertically."""
        output = extractor.extract(separate_title_author_blocks)
        assert output.title is not None
        assert output.author is not None
