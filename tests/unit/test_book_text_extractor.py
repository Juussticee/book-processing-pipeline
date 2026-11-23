"""Unit tests for BookTextExtractor.

Tests structured field extraction from OCR text including:
- French title extraction
- Author extraction with \"par\"/\"de\"/\"by\" patterns
- ISBN-10 and ISBN-13 extraction and validation
- Noisy OCR cleanup
- Publisher detection
- Edition extraction
- Language detection
"""

from services.book_text_extractor import BookTextExtractor


class TestBookTextExtractor:
    """Test the structured text extractor."""

    def setup_method(self):
        self._extractor = BookTextExtractor(languages=["en", "fr"])

    # --- Title extraction ---

    def test_extract_french_title(self):
        """Should extract 'Le cœur vert' from French OCR text."""
        text = (
            "Le cœur vert\n"
            "par Jean-Louis Kuffer\n"
            "ISBN 9780441013593\n"
            "Éditeur: Gallimard"
        )
        result = self._extractor.extract(text)
        assert result["title"] == "Le cœur vert"

    def test_extract_english_title(self):
        """Should extract an English title."""
        text = "Dune\nby Frank Herbert\nISBN 9780441013593\nPublished by Ace Books"
        result = self._extractor.extract(text)
        assert result["title"] == "Dune"

    def test_extract_title_cleaned_of_noise(self):
        """Title should not include ISBN or publisher noise."""
        text = "  D u n e  \n\n\nby F r a n k   H e r b e r t  \nISBN  9780441013593  "
        result = self._extractor.extract(text)
        assert result["title"] is not None
        assert "isbn" not in result["title"].lower()
        assert "published" not in result["title"].lower()

    def test_extract_title_empty_input(self):
        """Empty input returns all-None fields."""
        result = self._extractor.extract("")
        assert result["title"] is None

    # --- Author extraction ---

    def test_extract_author_par_pattern(self):
        """French 'par <name>' pattern."""
        text = "Le cœur vert\npar Jean-Louis Kuffer"
        result = self._extractor.extract(text)
        assert result["author"] == "Jean-Louis Kuffer"

    def test_extract_author_de_pattern(self):
        """French 'de <name>' pattern."""
        text = "Le Petit Prince\nde Antoine de Saint-Exupéry"
        result = self._extractor.extract(text)
        assert result["author"] == "Antoine de Saint-Exupéry"

    def test_extract_author_by_pattern(self):
        """English 'by <name>' pattern."""
        text = "Dune\nby Frank Herbert"
        result = self._extractor.extract(text)
        assert result["author"] == "Frank Herbert"

    def test_extract_author_published_by_pattern(self):
        """'Published by <name>' pattern."""
        text = "Some Book\nPublished by John Smith"
        result = self._extractor.extract(text)
        assert result["author"] == "John Smith"

    def test_extract_author_comma_format(self):
        """Author in 'Surname, First' format."""
        text = "Some Book\nKuffer, Jean-Louis"
        result = self._extractor.extract(text)
        assert result["author"] == "Kuffer, Jean-Louis"

    # --- ISBN extraction ---

    def test_extract_isbn_10(self):
        """ISBN-10 should be extracted (normalized without hyphens)."""
        text = "The Hobbit ISBN 0-395-07122-4"
        result = self._extractor.extract(text)
        assert result["isbn"] == "0395071224"

    def test_extract_isbn_13(self):
        """ISBN-13 should be extracted (normalized without hyphens)."""
        text = "Dune ISBN 9780441013593"
        result = self._extractor.extract(text)
        assert result["isbn"] == "9780441013593"

    def test_extract_no_isbn(self):
        """No ISBN in text returns None."""
        text = "Some Book by Some Author"
        result = self._extractor.extract(text)
        assert result["isbn"] is None

    def test_extract_multiple_isbns_first_wins(self):
        """When multiple ISBNs present, first valid one wins."""
        text = "ISBN 978-0-441-01359-3 ISBN 0-261-10233-0"
        result = self._extractor.extract(text)
        assert "978" in result["isbn"]

    # --- Publisher extraction ---

    def test_extract_publisher_keyword(self):
        """Publisher after keyword."""
        text = "Some Book\nPublisher: Random House"
        result = self._extractor.extract(text)
        assert result["publisher"] == "Random House"

    def test_extract_publisher_french(self):
        """French 'éditeur' keyword."""
        text = "Le cœur vert\nÉditeur: Gallimard"
        result = self._extractor.extract(text)
        assert result["publisher"] == "Gallimard"

    def test_extract_no_publisher(self):
        """No publisher keyword returns None."""
        text = "Some Book by Author"
        result = self._extractor.extract(text)
        assert result["publisher"] is None

    # --- Edition extraction ---

    def test_extract_edition_english(self):
        """English edition pattern."""
        text = "Second edition\nSome Book"
        result = self._extractor.extract(text)
        assert result["edition"] is not None

    def test_extract_edition_french(self):
        """French edition pattern."""
        text = "3e édition\nSome Book"
        result = self._extractor.extract(text)
        assert result["edition"] is not None

    def test_extract_no_edition(self):
        """No edition returns None."""
        text = "Some Book"
        result = self._extractor.extract(text)
        assert result["edition"] is None

    # --- Language detection ---

    def test_detect_french_language(self):
        """Should detect French language."""
        text = "Le cœur vert par Jean-Louis Kuffer"
        result = self._extractor.extract(text)
        assert result["language"] == "fr"

    def test_detect_english_language(self):
        """Should detect English language."""
        text = "The Heart of Darkness by Joseph Conrad"
        result = self._extractor.extract(text)
        assert result["language"] == "en"

    # --- OCR noise cleanup ---

    def test_clean_ocr_noise(self):
        """Noisy OCR text should still extract title."""
        text = (
            "  D u n e  \n\n\nby F r a n k   H e r b e r t  \nISBN  978 0 441 01359 3  "
        )
        result = self._extractor.extract(text)
        assert result["title"] is not None
        assert result["author"] is not None

    def test_extract_complete_metadata(self):
        """Full metadata extraction from well-structured OCR."""
        text = (
            "Le cœur vert\n"
            "par Jean-Louis Kuffer\n"
            "ISBN 9780441013593\n"
            "Éditeur: Gallimard\n"
            "2e édition"
        )
        result = self._extractor.extract(text)
        assert result["title"] == "Le cœur vert"
        assert result["author"] == "Jean-Louis Kuffer"
        assert "978" in result["isbn"]
        assert result["publisher"] == "Gallimard"
        assert result["edition"] is not None
        assert result["language"] == "fr"

        # --- Regression tests for forensic report failure modes ---

    def test_extract_with_actual_newlines(self):
        """Text with actual newline characters should split correctly."""
        text = "LE CHRETIEN\nDANS LA CITE\nERIC KAYAYAN"
        result = self._extractor.extract(text)
        assert result["title"] == "LE CHRETIEN DANS LA CITE"
        assert result["author"] == "ERIC KAYAYAN"

    def test_extract_with_literal_backslash_n(self):
        """Text with literal backslash-n (OCR escaped newlines) should split correctly."""
        text = (
            "LE CHRETIEN"
            + chr(92)
            + "n"
            + "DANS LA CITE"
            + chr(92)
            + "n"
            + "ERIC KAYAYAN"
        )
        result = self._extractor.extract(text)
        assert "LE CHRETIEN" in (result["title"] or "")
        assert "DANS LA CITE" in (result["title"] or "")
        assert result["author"] == "ERIC KAYAYAN"

    def test_extract_with_mixed_newline_types(self):
        """Text with mixed actual newlines and literal backslash-n should split correctly."""
        text = "LE CHRETIEN" + chr(92) + "n" + "DANS LA CITE" + chr(10) + "ERIC KAYAYAN"
        result = self._extractor.extract(text)
        assert "LE CHRETIEN" in (result["title"] or "")
        assert "DANS LA CITE" in (result["title"] or "")
        assert result["author"] == "ERIC KAYAYAN"

    def test_extract_publisher_directional_not_title(self):
        """Directional words from publisher logos should not become the title."""
        text = (
            "LE CHRETIEN DANS LA CITE"
            + chr(92)
            + "n"
            + "ERIC KAYAYAN"
            + chr(92)
            + "n"
            + "VENT DEST VENT D'OU OUEST"
            + chr(92)
            + "n"
            + "GALLIMARD"
        )
        result = self._extractor.extract(text)
        assert result["author"] == "ERIC KAYAYAN"
        assert "VENT" not in (result["title"] or "")
        assert "DEST" not in (result["title"] or "")

    def test_extract_fragmented_apostrophe_title(self):
        """OCR-split fragments like L' + individu should recombine correctly."""
        text = (
            "L'"
            + chr(92)
            + "n"
            + "individu"
            + chr(92)
            + "n"
            + "ERIC KAYAYAN"
            + chr(92)
            + "n"
            + "ISBN 9780441013593"
        )
        result = self._extractor.extract(text)
        assert result["title"] is not None
        assert "L'" in (result["title"] or "")
        assert "individu" in (result["title"] or "")

    def test_extract_single_word_title_not_rejected(self):
        """Single-word titles should not be rejected unless they are function words."""
        text = (
            "DUNE"
            + chr(92)
            + "n"
            + "by Frank Herbert"
            + chr(92)
            + "n"
            + "ISBN 9780441013593"
        )
        result = self._extractor.extract(text)
        assert result["title"] == "DUNE"
        assert result["author"] == "Frank Herbert"

    def test_extract_cyrillic_text_supported(self):
        """Extractor should handle Cyrillic characters in title and author."""
        # Using chr() to avoid encoding issues in source file
        voyna = chr(0x0412) + chr(0x041E) + chr(0x0419) + chr(0x041D) + chr(0x0410)
        mir = chr(0x041C) + chr(0x0418) + chr(0x0420)
        lev = chr(0x041B) + chr(0x0415) + chr(0x0412)
        tolstoy = (
            chr(0x0422)
            + chr(0x041E)
            + chr(0x041B)
            + chr(0x0421)
            + chr(0x0422)
            + chr(0x041E)
            + chr(0x0419)
        )
        text = (
            voyna
            + " "
            + mir
            + chr(92)
            + "n"
            + lev
            + " "
            + tolstoy
            + chr(92)
            + "n"
            + "ISBN 9780441013593"
        )
        result = self._extractor.extract(text)
        assert result["title"] is not None
        assert result["author"] is not None

    def test_extract_title_not_split_across_lines(self):
        """Multi-line title should recombine but author should still be separated."""
        text = (
            "LE CHRETIEN"
            + chr(92)
            + "n"
            + "DANS LA CITE"
            + chr(92)
            + "n"
            + "ERIC KAYAYAN"
            + chr(92)
            + "n"
            + "PARIS 1952"
            + chr(92)
            + "n"
            + "GALLIMARD"
        )
        result = self._extractor.extract(text)
        assert result["title"] == "LE CHRETIEN DANS LA CITE"
        assert result["author"] == "ERIC KAYAYAN"

    def test_extract_contributor_phrase_with_newline_split(self):
        """Contributor phrase split across lines should be handled correctly."""
        text = (
            "LE GRAND MEGALOPOLIS"
            + chr(92)
            + "n"
            + "par Georges Biz"
            + chr(92)
            + "n"
            + "ERIC KAYAYAN"
        )
        result = self._extractor.extract(text)
        assert "LE GRAND MEGALOPOLIS" in (result["title"] or "")
        assert result["author"] is not None

    def test_title_not_penalized_by_short_substring_match(self):
        """A short excluded line that is a substring of a title candidate
        must not drag down the candidate's score.

        Regression: when scoring "LA VIOLENCE", the substring "LA" (a
        short excluded line with score 0.0) matched inside it, producing a
        false average of 0.25 instead of 0.50, causing "VIULEIYCE" (0.40)
        to outrank "LA VIOLENCE".
        """
        text = "MARCEL PONCET\n" "LA VIOLENCE\n" "DE L'ESPRIT ET\n" "LA\n" "VIULEIYCE\n"
        result = self._extractor.extract(text)
        # The correct title is "LA VIOLENCE DE L'ESPRIT ET" (part of the
        # full title "La violence de l'esprit et la pitié du cœur"), not the
        # garbled last line "VIULEIYCE". The best-line floor in get_candidate_score
        # ensures the multi-line candidate (containing "LA VIOLENCE" at 0.50)
        # outscores the single garbled line "VIULEIYCE" (0.40).
        assert result["title"] == "LA VIOLENCE DE L'ESPRIT ET"
        assert result["title"] != "VIULEIYCE"

    def test_author_not_merged_into_title(self):
        """Author name must not be blindly concatenated into the title field."""
        text = (
            "LE CHRETIEN DANS LA CITE\n"
            "ERIC KAYAYAN\n"
            "avec la collaboration de\n"
            "AARON R KAYAYAN\n"
        )
        result = self._extractor.extract(text)
        assert result["title"] == "LE CHRETIEN DANS LA CITE"
        assert result["author"] == "ERIC KAYAYAN"

    def test_title_candidate_ranking_by_score_not_position(self):
        """Among multiple title candidates, the one with the highest
        line-score must win, not simply the last or first line."""
        text = "GABRIELLE\n" "LAGE D'HOMME\n" "UNE VIE\n" "GABRIELLE\n"
        result = self._extractor.extract(text)
        # Title should be a real title candidate, not the author line
        # "LAGE D'HOMME" must not be selected as the title.
        assert result["title"] is not None
        assert "LAGE" not in (result["title"] or "")
        assert "HOMME" not in (result["title"] or "")

    # --- Regression tests for 2026-08-15 forensic pipeline fixes ---

    def test_score_title_candidate_strips_cover_art_lines(self):
        """Cover-art description lines like 'ÉCRITS SUR L'ART' must score 0.0.

        Regression: these lines describe printing/artwork on the cover,
        not the book title. They must not contaminate title reconstruction.
        """
        extractor = BookTextExtractor()
        # "ÉCRITS SUR L'ART" contains "écrits sur" — a cover-art indicator
        score = extractor._score_title_candidate("ÉCRITS SUR L'ART", None)
        assert score == 0.0

    def test_score_title_candidate_exempts_title_function_words(self):
        """Short French title function words ('de', 'la', 'et') must not be
        penalized, so they survive the 0.10 minimum and can merge into
        multi-line titles (e.g. 'de la' in 'Les Désastres de la guerre').

        Regression: 'de la' previously scored 0.0 (both words <= 2 chars,
        -0.15 each), breaking the grouping of multi-line titles.
        """
        extractor = BookTextExtractor()
        score = extractor._score_title_candidate("de la", "Jeanlouis Cornuz")
        assert score > 0.10, f"'de la' should score > 0.10, got {score}"

    def test_score_title_candidate_author_name_not_title_when_matched(self):
        """An all-caps 2-word line that matches the extracted author should
        score 0.0 (it IS the author). When it doesn't match the author:
        - Lines with apostrophes in the first word (e.g. "LE' CORBUSIER")
          are almost certainly names → score 0.0
        - Lines without apostrophes (e.g. "DANS LORDRE", "HISTOIRES DAMOUR")
          get a -0.30 penalty but still score > 0 (they might be title fragments).

        Regression: this prevents 'LE' CORBUSIER' from being merged into
        the title when author extraction assigned 'LACE D'HOMME' instead.
        """
        extractor = BookTextExtractor()
        # MARCEL PONCET matches the author → 0.0
        assert extractor._score_title_candidate("MARCEL PONCET", "MARCEL PONCET") == 0.0
        # LE' CORBUSIER — has apostrophe in first word, doesn't match author → 0.0
        assert extractor._score_title_candidate("LE' CORBUSIER", "LACE D'HOMME") == 0.0
        # DANS LORDRE — no apostrophe, doesn't match author → penalty but > 0
        score = extractor._score_title_candidate("DANS LORDRE", "Angela Ghelber")
        assert score > 0.0, f"'DANS LORDRE' should score > 0, got {score}"

    def test_score_title_candidate_author_name_when_no_author_extracted(self):
        """When no author is extracted (author=None), all-caps 2-word lines
        that look like author names should still score 0.0 (conservative —
        avoid including name-like lines as titles without confirmed author).

        Regression: this conservatism prevents false title extraction when
        author extraction fails entirely.
        """
        extractor = BookTextExtractor()
        # HISTOIRES DAMOUR looks like an author name (2 caps words, no period)
        # With no extracted author, it should score 0.0 (conservative)
        assert extractor._score_title_candidate("HISTOIRES DAMOUR", None) == 0.0
        # But with an extracted author, it should score > 0 (title fragment)
        score = extractor._score_title_candidate("HISTOIRES DAMOUR", "Yvan Lecomte")
        assert score > 0.10, f"'HISTOIRES DAMOUR' should score > 0.10 with different author, got {score}"

    def test_extract_title_merges_french_function_words(self):
        """Multi-line French title with function-word fragments should merge.

        Regression: 'Les Désastres' + 'de la' must form 'Les Désastres de la',
        not leave them as separate candidates. Previously 'de la' scored 0.0
        (both words <= 2 chars, -0.15 each), breaking the grouping.
        """
        text = "Jeanlouis Cornuz\nLes Désastres\nde la\nRoman\nguerre"
        result = self._extractor.extract(text)
        assert result["author"] == "Jeanlouis Cornuz"
        # "de la" should be merged with "Les Désastres" — the full merged
        # title "Les Désastres de la" gives token_sort_ratio=0.8444 with
        # the API title "Les désastres de la guerre", enough to cross 0.85
        # confidence threshold.
        title = result["title"] or ""
        assert "DÉSASTRES" in title.upper()
        assert "DE" in title.upper() or "de" in title
        # "de la" must be in the same candidate (not a separate one)
        assert "de la" in title.lower() or "DE LA" in title.upper() or "déla" in title.lower().replace(" ", "")


    # --- Phase 14 regression tests: 4 title extraction failure modes ---

    def test_phase14_case_a_author_name_not_selected_as_title(self):
        """CASE A: When all OCR lines are short all-caps 2-word fragments and
        no author is extracted via patterns, the first name-like line should
        not be selected as the title when other lines form a stronger title.
        Regression: 'LE' CORBUSIER' was selected as title when 'LA CONSTRUCTION
        DES VILLES' was available as the actual title.
        """
        text = (
            "LE' CORBUSIER\n"
            "LA CONSTRUCTION\n"
            "DES VILLES\n"
            "LACE D'HOMME"
        )
        result = self._extractor.extract(text)
        assert result["title"] is not None
        title_upper = (result["title"] or "").upper()
        assert "CONSTRUCTION" in title_upper
        assert "VILLES" in title_upper

    def test_phase14_case_a_correct_title_and_author(self):
        """CASE A: Full extraction should identify both title and author correctly
        from fragmented all-caps OCR lines where author is a known pseudonym.
        """
        text = (
            "LE' CORBUSIER\n"
            "LA CONSTRUCTION\n"
            "DES VILLES\n"
            "LACE D'HOMME"
        )
        result = self._extractor.extract(text)
        assert result["author"] is not None
        title_upper = (result["title"] or "").upper()
        assert "CONSTRUCTION" in title_upper

    def test_phase14_case_b_prefix_line_merged_into_multiline_title(self):
        """CASE B: A short prefix line before a multi-line title should be
        merged into the title candidate, not left as a separate candidate.
        Regression: 'Analyse' was a separate candidate and 'VIE ET MORT DE
        LA YOUCOSLAVIE' was selected without the 'Analyse de' prefix.
        """
        text = (
            "Analyse\n"
            "de\n"
            "VIE ET MORT\n"
            "DE LA YOUCOSLAVIE\n"
            "de\n"
            "Paul\n"
            "Garde\n"
            "par\n"
            "Anne Yelev. Pierre Maurer, Slobodan Despot"
        )
        result = self._extractor.extract(text)
        title_upper = (result["title"] or "").upper()
        assert "VIE ET MORT" in title_upper
        assert "YOUCOSLAVIE" in title_upper or "YOUGOSLAVIE" in title_upper
        assert "ANALYSE" in title_upper or "Analyse" in (result["title"] or "")

    def test_phase14_case_c_postface_contributor_not_selected_as_title(self):
        """CASE C: 'postface de <name>' is a contributor credit and must not
        be selected as the book title.
        Regression: 'postface de Maurice Gahail' was selected as the title
        instead of 'Abel Sanchez'.
        """
        text = (
            "Miguel de Unamuno\n"
            "Abel Sanchez\n"
            "Traduction de Fespagnol\n"
            "et\n"
            "postface\n"
            "de Maurice Gahail\n"
            "Vent dest\n"
            "Vent d'ouest\n"
            "dHomme"
        )
        result = self._extractor.extract(text)
        title_upper = (result["title"] or "").upper()
        assert "POSTFACE" not in title_upper
        assert "ABEL" in title_upper
        assert "SANCHEZ" in title_upper

    def test_phase14_case_d_publisher_fragment_not_title(self):
        """CASE D: Garbled publisher fragments like 'dHomme INIVS' must not
        be selected as the title when a real title candidate exists.
        Regression: 'dHomme INIVS' was selected because the publisher
        fragment detection had a reversed substring check.
        """
        text = (
            "Theodore Litt\n"
            "Lindividu et\n"
            "la communauté\n"
            "Raison dialectique\n"
            "dHomme\n"
            "INIVS\n"
            "LAge"
        )
        result = self._extractor.extract(text)
        title = result["title"] or ""
        title_upper = title.upper()
        assert "INDIVIDU" in title_upper
        assert "INIVS" not in title_upper.split()
        assert "dHomme INIVS" != title
        assert "INIVS" != title

    def test_phase14_case_d_publisher_fragment_scoring(self):
        """CASE D (unit): The publisher fragment check should correctly detect
        'dHomme' as containing 'homme' (fragment-in-word, not word-in-fragment).
        """
        extractor = BookTextExtractor()
        assert extractor._looks_like_publisher_fragment("dHomme") is True
        assert extractor._looks_like_publisher_fragment("INIVS") is False
        assert extractor._looks_like_publisher_fragment("LAge") is True

    def test_phase14_case_c_postface_contributor_pattern(self):
        """CASE C (unit): 'postface de' should be detected as a contributor phrase.

        Note: 'postface' alone is NOT a contributor phrase — it's a French word
        meaning 'afterword'. Only 'postface de <NAME>' is a contributor phrase.
        When OCR splits the phrase across lines, the split detection in
        _get_contributor_line_indices and _score_title_candidate handles it.
        """
        extractor = self._extractor
        assert extractor._is_contributor_phrase("postface de Maurice Gahail") is True
        # 'postface' alone is a valid French word, not a contributor phrase
        assert extractor._is_contributor_phrase("postface") is False
        assert extractor._is_contributor_phrase("Abel Sanchez") is False

    # --- Phase 16 regression tests: title extraction merge fix ---

    def test_title_single_word_not_merged_after_title_article(self):
        """Regression: single-word OCR fragments should not merge into a
        title that starts with a French title article (le, la, les, l').

        OCR often splits a title from adjacent text (publisher, imprint,
        or noise) across lines. When the title starts with a title article
        like "Le", the merge logic was too permissive — it fell through to
        an unconditional merge for single-word fragments like "Romar".

        Real case: "Le Mal Eté" + "Romar" (not part of the title) should
        stay as just "Le Mal Eté", not become "Le Mal Été Romar".
        """
        text = (
            "Jean Vuilleumier\n"
            "Le Mal Eté\n"
            "Romar\n"
            "La Merveilleuse Collection\n"
            "Editiong\n"
            "dHomme"
        )
        result = self._extractor.extract(text)
        title = (result["title"] or "").upper().strip()
        assert "MAL ETÉ" in title.upper().replace("ÉTÉ", "ETE") or "MAL" in title
        assert "ROMAR" not in title, (
            f"Single-word fragment 'Romar' should not be merged into title, "
            f"got: {result['title']}"
        )

    def test_title_single_word_not_merged_after_title_article_bridge(self):
        """Regression: single-word fragments separated by bridge words from a
        title-article-starting title should not merge via the bridge path.

        Real case: "Les Désastres" + "de la" (bridge) + "Roman" (noise)
        should produce "Les Désastres de la", NOT "Les Désastres de la Roman".
        The word "Roman" is an OCR misread — the actual final word "guerre"
        appears later on the cover.
        """
        text = (
            "Jeanlouis Cornuz\n"
            "Les Désastres\n"
            "de la\n"
            "Roman\n"
            "LAge dHomme\n"
            "guerre"
        )
        result = self._extractor.extract(text)
        title = (result["title"] or "").upper().strip()
        assert "DÉSASTRES" in title or "DESASTRES" in title
        assert "DE LA" in title.upper() or "DELA" in title.upper().replace(" ", "")
        assert "ROMAN" not in title, (
            f"Single-word 'Roman' should not merge via bridge into title, "
            f"got: {result['title']}"
        )

    def test_multi_word_fragment_still_merges_after_title_article(self):
        """Ensure fix for single-word fragments does NOT break multi-word
        title continuation. 'La violence de l'esprit' should still merge
        'de l'esprit' even though it starts with title article 'de'.
        """
        text = (
            "Marcel Poncet\n"
            "LA VIOLENCE\n"
            "DE L'ESPRIT ET\n"
            "LA PITIÉ DU CŒUR"
        )
        result = self._extractor.extract(text)
        title = (result["title"] or "").upper()
        assert "VIOLENCE" in title
        assert "ESPRIT" in title, (
            f"Multi-word fragment should merge into title, "
            f"got: {result['title']}"
        )
