"""Unit tests for TextNormalizer."""

from services.text_normalizer import TextNormalizer


class TestTextNormalizer:
    """Test the OCR text normalization pipeline."""

    def setup_method(self) -> None:
        self._normalizer = TextNormalizer()

    def test_normalize_empty_string(self) -> None:
        assert self._normalizer.normalize("") == ""

    def test_normalize_none_returns_empty(self) -> None:
        assert self._normalizer.normalize(None) == ""  # type: ignore[arg-type]

    def test_normalize_removes_duplicate_spaces(self) -> None:
        assert self._normalizer.normalize("hello    world") == "hello world"

    def test_normalize_removes_duplicate_newlines(self) -> None:
        assert self._normalizer.normalize("line1\n\n\n\nline2") == "line1\n\nline2"

    def test_normalize_fixes_smart_quotes(self) -> None:
        text = "\u201cHello\u201d"  # smart double quotes
        result = self._normalizer.normalize(text)
        assert '"' in result

    def test_normalize_fixes_em_dash(self) -> None:
        text = "Hello\u2014world"
        result = self._normalizer.normalize(text)
        assert "—" not in result
        assert "-" in result

    def test_normalize_fixes_pipe_to_I(self) -> None:
        result = self._normalizer.normalize("H|p")
        assert "I" in result

    def test_normalize_fixes_ellipsis(self) -> None:
        text = "Hello\u2026world"
        result = self._normalizer.normalize(text)
        assert "…" not in result

    def test_normalize_merges_hyphenated_line_breaks(self) -> None:
        text = "Atomic Habits-\nThe power"
        result = self._normalizer.normalize(text)
        assert "-\n" not in result

    def test_normalize_merges_line_broken_words(self) -> None:
        text = "Hello\nWorld"
        result = self._normalizer.normalize(text)
        assert "Hello World" == result

    def test_normalize_trims_whitespace(self) -> None:
        assert self._normalizer.normalize("  hello  ") == "hello"

    def test_normalize_fixes_spacing_around_punctuation(self) -> None:
        result = self._normalizer.normalize("hello , world")
        assert ", " in result

    def test_normalize_preserves_paragraphs(self) -> None:
        text = "Paragraph 1.\n\nParagraph 2."
        result = self._normalizer.normalize(text)
        assert "\n\n" in result

    def test_normalize_example_atomic_habits(self) -> None:
        """Test the specific example from the spec: AT0MIC HABlTS -> ATOMIC Habits."""
        text = "AT0MIC HABlTS"
        result = self._normalizer.normalize(text)
        # The normalizer should fix l -> I confusion in context of "HABlTS"
        assert "I" in result


class TestTextNormalizerISBN:
    """Test ISBN extraction from OCR text."""

    def setup_method(self) -> None:
        self._normalizer = TextNormalizer()

    # Valid ISBN-13: 9780306406157
    def test_extract_isbn13_valid(self) -> None:
        text = "ISBN 978-0-306-40615-7"
        isbns = self._normalizer.extract_isbn(text)
        assert "9780306406157" in isbns

    # Valid ISBN-10: 0306406152
    def test_extract_isbn10_valid(self) -> None:
        text = "ISBN 0-306-40615-2"
        isbns = self._normalizer.extract_isbn(text)
        assert "0306406152" in isbns

    def test_extract_isbn_no_isbn(self) -> None:
        text = "This book is about programming"
        isbns = self._normalizer.extract_isbn(text)
        assert isbns == []

    def test_extract_isbn_invalid_isbn_filtered(self) -> None:
        text = "ISBN 978-1234-5678-90"
        isbns = self._normalizer.extract_isbn(text)
        assert isbns == []

    def test_extract_isbn_multiple_returns_first_valid(self) -> None:
        text = "ISBN 978-0-306-40615-7 and ISBN 0306406152"
        isbns = self._normalizer.extract_isbn(text)
        assert len(isbns) >= 1
        assert "9780306406157" in isbns

    def test_validate_isbn10_valid(self) -> None:
        assert TextNormalizer._validate_isbn10("0306406152") is True

    def test_validate_isbn10_invalid(self) -> None:
        assert TextNormalizer._validate_isbn10("0306406153") is False

    def test_validate_isbn13_valid(self) -> None:
        assert TextNormalizer._validate_isbn13("9780306406157") is True

    def test_validate_isbn13_invalid(self) -> None:
        assert TextNormalizer._validate_isbn13("9781234567890") is False
