"""Phase 43.6 regression tests for the targeted extraction fixes.

Covers the mechanical extraction failures identified in Phase 43.5:
1. Markdown/HTML artifact normalization
2. Two-word mixed-case authors (Angela Ghelber)
3. Genre words as titles (Roman, Noüvelles)
4. Contributor lists as titles (Yougoslavie)
5. Title/author swap (Debout)
6. Merged author+title blocks (Le Corbusier, Weiser David, Le livre de Blam)
7. Translator credits as authors (PAR MADELEINE STEVANOV, TRADUIT DU SERBE)
8. series+publisher single-book detection (Le Mal Été)
"""

import unittest

from services.enhanced_text_extractor import (
    EnhancedBookTextExtractor,
    normalize_paddleocr_markdown,
)
from services.semantic_title_matcher import SemanticTitleMatcher


class TestMarkdownHtmlNormalization(unittest.TestCase):
    """PaddleOCR-VL markdown/HTML artifacts must be stripped before extraction."""

    def test_hash_prefix_stripped(self) -> None:
        """'# PAWEL HUELLE' evaluates like 'PAWEL HUELLE'."""
        raw = "# PAWEL HUELLE WEISER DAVID"
        normalized = normalize_paddleocr_markdown(raw)
        self.assertEqual(normalized, "PAWEL HUELLE WEISER DAVID")

    def test_multi_hash_prefix(self) -> None:
        """'## Heading' and '### Heading' are stripped."""
        for prefix in ("## ", "### ", "#### "):
            raw = f"{prefix}SOME TITLE"
            self.assertEqual(normalize_paddleocr_markdown(raw), "SOME TITLE")

    def test_html_tags_removed(self) -> None:
        """'<div><img .../></div>' lines are dropped, text around kept."""
        raw = (
            '<div style="text-align: center;">'
            '<img src="x.jpg" alt="Image" width="30%" /></div>'
        )
        normalized = normalize_paddleocr_markdown(raw)
        self.assertNotIn("<", normalized)
        self.assertNotIn("img", normalized.lower())

    def test_inline_html_stripped(self) -> None:
        """Inline '<br>' and '<p>' tags are replaced with space."""
        raw = "Line one<br>Line two<p>para</p>"
        normalized = normalize_paddleocr_markdown(raw)
        self.assertNotIn("<br>", normalized)
        self.assertIn("Line one", normalized)

    def test_text_content_preserved(self) -> None:
        """Legitimate text is not removed during normalization."""
        raw = "# Le Cœur Vert Récit"
        normalized = normalize_paddleocr_markdown(raw)
        self.assertEqual(normalized, "Le Cœur Vert Récit")

    def test_heading_with_image_below(self) -> None:
        """Common PaddleOCR-VL pattern: heading + image placeholder."""
        img_tag = (
            '<div style="text-align: center;">'
            '<img src="imgs/img_in_image_box_313_713_587_1081.jpg" '
            'alt="Image" width="30%" /></div>'
        )
        raw = f"""# PAWEL HUELLE WEISER DAVID

ROMAN TRADUIT DU POLONAISE PAR FRANÇOIS ROSSET

{img_tag}"""
        normalized = normalize_paddleocr_markdown(raw)
        self.assertIn("PAWEL HUELLE WEISER DAVID", normalized)
        self.assertIn("ROMAN TRADUIT DU POLONAISE PAR FRANÇOIS ROSSET", normalized)
        self.assertNotIn("<", normalized)


class TestTwoWordAuthorHeuristic(unittest.TestCase):
    """Two-word mixed-case names must be recognized as authors."""

    def setUp(self) -> None:
        self._ext = EnhancedBookTextExtractor(languages=["fr"])

    def test_angela_ghelber(self) -> None:
        """'Angela Ghelber' -> author (the key swap case)."""
        ocr = """Angela Ghelber

# DEBOUT, DANS L'ORDRE ET L'HARMONIE DE LA CREATION

POEMES

L'AGE D'HOMME"""
        result = self._ext.extract(ocr)
        self.assertEqual(result.get("author"), "Angela Ghelber")

    def test_jean_vuilleumier(self) -> None:
        """'Jean Vuilleumier' -> author."""
        ocr = """Jean Vuilleumier

# Le Mal Eté

Roman

La Merveilleuse Collection,

Editions l'Age d'Homme"""
        result = self._ext.extract(ocr)
        self.assertEqual(result.get("author"), "Jean Vuilleumier")

    def test_alexandre_tisma(self) -> None:
        """'Alexandre Tišma' -> author (even with series header)."""
        ocr = """# CLASSIQUES SLAVES Alexandre Tišma LE LIVRE DE BLAM ROMAN

TRADUIT DU SERBE

PAR MADELEINE STEVANOV

ÉDITIONS LAGE D' HOMME"""
        result = self._ext.extract(ocr)
        self.assertIn(
            "Alexandre",
            result.get("author") or result.get("title") or "",
        )


class TestGenreWordTitleRejection(unittest.TestCase):
    """Standalone genre words must NOT be selected as titles."""

    def setUp(self) -> None:
        self._ext = EnhancedBookTextExtractor(languages=["fr"])

    def test_roman_rejected(self) -> None:
        """'Roman' alone is not a title."""
        ocr = """Jean Vuilleumier

# Le Mal Eté

Roman

Editions l'Age d'Homme"""
        result = self._ext.extract(ocr)
        self.assertNotEqual(result.get("title"), "Roman")

    def test_nouvelles_rejected(self) -> None:
        """'Noüvelles' alone is not a title."""
        ocr = """# Martine Magnaridè Entrez dans la danse

Noüvelles

L'Age d'Homme"""
        result = self._ext.extract(ocr)
        title = result.get("title") or ""
        self.assertNotEqual(title.strip(), "Noüvelles")

    def test_genre_in_multiword_title_allowed(self) -> None:
        """'Le Roman de Renart' keeps the genre word."""
        ocr = """Le Roman de Renart

par Jean Dupont"""
        result = self._ext.extract(ocr)
        title = result.get("title") or ""
        self.assertIn("Roman", title)


class TestContributorListAsTitle(unittest.TestCase):
    """Contributor names after 'par' must NOT become titles."""

    def setUp(self) -> None:
        self._ext = EnhancedBookTextExtractor(languages=["fr"])

    def test_yougoslavie_title(self) -> None:
        """'Vie et mort de la Yougoslavie' title, contributors excluded."""
        ocr = """# Analyses de : VIE ET MORT DE LA YOUGOSLAVIE de Paul Garde

par

Pavle Ivić, Nikola Samardžić.

Anne Yelen, Pierre Maurer, Slobodan Despo"""
        result = self._ext.extract(ocr)
        title = result.get("title") or ""
        # The title must contain the actual title words
        gt = "Vie et mort de la Yougoslavie"
        self.assertTrue(
            SemanticTitleMatcher.titles_equivalent(title, gt) or "VIE ET MORT" in title,
            f"Title '{title}' should contain '{gt}'",
        )

    def test_contributor_names_not_title(self) -> None:
        """Contributor names do not appear as title candidates."""
        ocr = """# Analyses de : VIE ET MORT DE LA YOUGOSLAVIE de Paul Garde

par

Pavle Ivić, Nikola Samardžić.

Anne Yelen, Pierre Maurer, Slobodan Despo"""
        result = self._ext.extract(ocr)
        title = result.get("title") or ""
        self.assertNotIn("Pavle", title)
        self.assertNotIn("Maurer", title)


class TestTitleAuthorSwap(unittest.TestCase):
    """Author at top, title below — must not swap them."""

    def setUp(self) -> None:
        self._ext = EnhancedBookTextExtractor(languages=["fr"])

    def test_debout_case(self) -> None:
        """'Angela Ghelber' = author, 'DEBOUT...' = title."""
        ocr = """Angela Ghelber

# DEBOUT, DANS L'ORDRE ET L'HARMONIE DE LA CREATION

POEMES

L'AGE D'HOMME"""
        result = self._ext.extract(ocr)
        self.assertEqual(result.get("author"), "Angela Ghelber")
        title = result.get("title") or ""
        self.assertIn("DEBOUT", title)


class TestMergedAuthorTitleBlock(unittest.TestCase):
    """PaddleOCR-VL merges author+title blocks — split them."""

    def setUp(self) -> None:
        self._ext = EnhancedBookTextExtractor(languages=["fr"])

    def test_weiser_david_split(self) -> None:
        """'PAWEL HUELLE WEISER DAVID' -> author + title."""
        ocr = """# PAWEL HUELLE WEISER DAVID

ROMAN TRADUIT DU POLONAISE PAR FRANÇOIS ROSSET"""
        result = self._ext.extract(ocr)
        author = result.get("author") or ""
        title = result.get("title") or ""
        # Author must be 'PAWEL HUELLE', title must contain 'WEISER DAVID'
        self.assertIn("PAWEL", author)
        self.assertIn("WEISER", title)

    def test_le_corbusier_split(self) -> None:
        """'LE CORBUSIER LA CONSTRUCTION DES VILLES' -> author + title."""
        ocr = """# LE CORBUSIER LA CONSTRUCTION DES VILLES

L'AGE D'HOMME"""
        result = self._ext.extract(ocr)
        author = result.get("author") or ""
        title = result.get("title") or ""
        self.assertIn("CORBUSIER", author)
        self.assertIn("CONSTRUCTION", title)


class TestTranslatorNotAuthor(unittest.TestCase):
    """Translator/contributor credits must not become authors."""

    def setUp(self) -> None:
        self._ext = EnhancedBookTextExtractor(languages=["fr"])

    def test_par_madeleine_not_author(self) -> None:
        """'PAR MADELEINE STEVANOV' is not the author."""
        ocr = """CLASSIQUES SLAVES Alexandre Tišma LE LIVRE DE BLAM ROMAN

TRADUIT DU SERBE

PAR MADELEINE STEVANOV

ÉDITIONS LAGE D' HOMME"""
        result = self._ext.extract(ocr)
        author = result.get("author") or ""
        self.assertNotIn("MADELEINE", author)

    def test_traduit_du_serbe_not_author(self) -> None:
        """'TRADUIT DU SERBE' is not the author."""
        ocr = """CLASSIQUES SLAVES Alexandre Tišma LE LIVRE DE BLAM ROMAN

TRADUIT DU SERBE

PAR MADELEINE STEVANOV

ÉDITIONS LAGE D' HOMME"""
        result = self._ext.extract(ocr)
        author = result.get("author") or ""
        self.assertNotIn("TRADUIT", author)

    def test_vampir_jan_dusay_not_author(self) -> None:
        """Translator 'JAN DUSAY' is not the author of Vampir."""
        ocr = """HANNS HEINZ EWERS

VAMPIR

ROMAN TOUT EN COULEURS

ET EN LAMBEAUX

TRADUIT DE LALLEMAND PAR

JAN DUSAY

BIBLIOTHEQUE LACE DIOMME"""
        result = self._ext.extract(ocr)
        author = result.get("author") or ""
        self.assertNotIn("DUSAY", author)


class TestSingleBookNotMultiBook(unittest.TestCase):
    """Series + publisher on the same book must NOT trigger multi-book."""

    def setUp(self) -> None:
        self._ext = EnhancedBookTextExtractor(languages=["fr"])

    def test_le_mal_ete_not_multi_book(self) -> None:
        """'La Merveilleuse Collection' + 'Editions l'Age d'Homme' = ONE book."""
        ocr = """Jean Vuilleumier

# Le Mal Eté

Roman

La Merveilleuse Collection,

Editions l'Age d'Homme"""
        result = self._ext.extract(ocr)
        # Author should be extracted (not suppressed by multi-book)
        self.assertEqual(result.get("author"), "Jean Vuilleumier")


if __name__ == "__main__":
    unittest.main()
