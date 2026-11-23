"""Unit tests for CandidateRanker.

Tests the ranking logic including ISBN MATCH/CONFLICT/UNAVAILABLE detection.
"""

from typing import Optional

from models.book_metadata import BookMetadata
from services.candidate_ranker import CandidateRanker


class TestCandidateRanker:
    """Test the candidate ranking service."""

    def setup_method(self) -> None:
        self._ranker = CandidateRanker()

    def _make_candidate(
        self,
        title: str = "Test Book",
        authors=None,
        isbn_10: Optional[str] = None,
        isbn_13: Optional[str] = None,
        publisher: Optional[str] = None,
        source: str = "google_books",
    ) -> BookMetadata:
        return BookMetadata(
            title=title,
            subtitle=None,
            authors=authors or [],
            isbn_10=isbn_10,
            isbn_13=isbn_13,
            publisher=publisher,
            published_date=None,
            language=None,
            page_count=None,
            categories=[],
            description=None,
            thumbnail_url=None,
            cover_url=None,
            confidence=0.0,
            source=source,
            raw_ocr="",
        )

    def test_rank_returns_sorted_list(self) -> None:
        """Candidates should be sorted by total_score descending."""
        candidates = [
            self._make_candidate(title="Book A", authors=["Author A"]),
            self._make_candidate(title="Book B", authors="Author B".split()),
        ]

        ranked = self._ranker.rank(
            candidates,
            ocr_title="Book A",
            ocr_author="Author A",
        )

        assert ranked[0].metadata.title == "Book A"
        assert ranked[0].total_score > ranked[1].total_score

    def test_rank_isbn_exact_match_gets_high_score(self) -> None:
        """ISBN exact match should boost the score significantly."""
        candidates = [
            self._make_candidate(
                title="Different Book",
                isbn_10="0306406152",
            ),
            self._make_candidate(
                title="Matching Book",
                isbn_10="1234567890",
            ),
        ]

        ranked = self._ranker.rank(
            candidates,
            ocr_title="Some Title",
            ocr_isbn="0306406152",
        )

        assert ranked[0].metadata.title == "Different Book"
        assert ranked[0].isbn_score == 1.0
        assert ranked[0].isbn_state == "MATCH"

    def test_rank_empty_candidates(self) -> None:
        """Ranking an empty list returns empty list."""
        ranked = self._ranker.rank([], ocr_title="Test")
        assert ranked == []

    def test_rank_title_similarity(self) -> None:
        """Title with partial match gets proportional score."""
        candidates = [
            self._make_candidate(title="Atomic Habits", authors=[]),
            self._make_candidate(title="Some Other Book", authors=[]),
        ]

        ranked = self._ranker.rank(
            candidates,
            ocr_title="Atomic Habits",
        )

        assert ranked[0].metadata.title == "Atomic Habits"
        assert ranked[0].title_score > 0.9
        assert ranked[1].title_score < 0.5

    def test_rank_author_similarity(self) -> None:
        """Author similarity is scored correctly."""
        candidates = [
            self._make_candidate(title="Book", authors=["James Clear"]),
            self._make_candidate(title="Book", authors=["Some Other Author"]),
        ]

        ranked = self._ranker.rank(
            candidates,
            ocr_author="James Clear",
        )

        assert ranked[0].metadata.authors[0] == "James Clear"
        assert ranked[0].author_score > ranked[1].author_score

    def test_rank_publisher_similarity(self) -> None:
        """Publisher similarity is scored correctly."""
        candidates = [
            self._make_candidate(title="Book", publisher="HarperCollins"),
            self._make_candidate(title="Book", publisher="Random House"),
        ]

        ranked = self._ranker.rank(
            candidates,
            ocr_publisher="HarperCollins",
        )

        assert ranked[0].publisher_score > ranked[1].publisher_score

    def test_rank_no_ocr_fields(self) -> None:
        """Ranking without OCR fields still works (returns list)."""
        candidates = [self._make_candidate(title="Test Book")]
        ranked = self._ranker.rank(candidates)
        assert len(ranked) == 1
        assert ranked[0].total_score == 0.0  # No matches
        assert ranked[0].isbn_state == "UNAVAILABLE"
        assert ranked[0].title_available is False
        assert ranked[0].author_available is False

    def test_rank_candidate_scores_documented(self) -> None:
        """CandidateRanker should document the weight formula."""
        candidates = [
            self._make_candidate(
                title="Atomic Habits",
                authors=["James Clear"],
                isbn_13="9780306406157",
                publisher="HarperOne",
            ),
        ]

        ranked = self._ranker.rank(
            candidates,
            ocr_title="Atomic Habits",
            ocr_author="James Clear",
            ocr_isbn="9780306406157",
            ocr_publisher="HarperOne",
        )

        result = ranked[0]
        assert result.isbn_score == 1.0  # Exact ISBN match
        assert result.title_score >= 0.95
        assert result.author_score >= 0.95
        assert result.publisher_score >= 0.95
        assert result.total_score > 0.95
        assert result.isbn_state == "MATCH"
        assert result.title_available is True
        assert result.author_available is True

    def test_similarity_method(self) -> None:
        """Test the static similarity helper."""
        from rapidfuzz import fuzz

        score = CandidateRanker._similarity("hello", "hello", fuzz)
        assert score == 1.0  # Exact match

        score = CandidateRanker._similarity("hello", "world", fuzz)
        assert score < 0.3  # Low similarity

        score = CandidateRanker._similarity("Hello World", "hello world", fuzz)
        assert score == 1.0  # Case-insensitive match

    def test_calculate_total_formula(self) -> None:
        """Test that the weighted total formula is correct."""
        # Using documented weights: TITLE=1.0, AUTHOR=1.0, ISBN=2.0, PUBLISHER=0.5
        total = CandidateRanker._calculate_total(1.0, 1.0, 1.0, 1.0)
        # (1.0*1.0 + 1.0*1.0 + 2.0*1.0 + 0.5*1.0) / (1.0+1.0+2.0+0.5)
        expected = (1.0 + 1.0 + 2.0 + 0.5) / 4.5
        assert abs(total - expected) < 0.001

    # --- New tests for ISBN conflict detection ---

    def test_isbn_conflict_detected(self) -> None:
        """ISBN conflict: OCR has ISBN, candidate has different ISBN."""
        candidates = [
            self._make_candidate(
                title="Book A",
                authors=["Author A"],
                isbn_13="9781111111111",
            ),
        ]
        ranked = self._ranker.rank(
            candidates,
            ocr_title="Book A",
            ocr_author="Author A",
            ocr_isbn="9789999999999",
        )
        assert ranked[0].isbn_state == "CONFLICT"
        assert ranked[0].isbn_score == 0.0

    def test_isbn_match_when_no_ocr_isbn_but_candidate_has_isbn(self) -> None:
        """When OCR has no ISBN but candidate does, state is UNAVAILABLE."""
        candidates = [
            self._make_candidate(
                title="Book A",
                authors=["Author A"],
                isbn_13="9781111111111",
            ),
        ]
        ranked = self._ranker.rank(
            candidates,
            ocr_title="Book A",
            ocr_author="Author A",
        )
        assert ranked[0].isbn_state == "UNAVAILABLE"

    def test_isbn_match_when_both_missing(self) -> None:
        """When neither OCR nor candidate has ISBN, state is UNAVAILABLE."""
        candidates = [self._make_candidate(title="Book A", authors=["Author A"])]
        ranked = self._ranker.rank(
            candidates,
            ocr_title="Book A",
            ocr_author="Author A",
        )
        assert ranked[0].isbn_state == "UNAVAILABLE"
        assert ranked[0].isbn_score == 0.0

    def test_title_and_author_availability_flags(self) -> None:
        """title_available and author_available should reflect presence."""
        candidates = [
            self._make_candidate(title="Book A", authors=["Author A"]),
        ]
        ranked = self._ranker.rank(
            candidates,
            ocr_title="Book A",
            ocr_author="Author A",
        )
        assert ranked[0].title_available is True
        assert ranked[0].author_available is True

    def test_title_available_but_no_author(self) -> None:
        """title_available=True, author_available=False when no OCR author."""
        candidates = [self._make_candidate(title="Book A", authors=["Author A"])]
        ranked = self._ranker.rank(candidates, ocr_title="Book A")
        assert ranked[0].title_available is True
        assert ranked[0].author_available is False

    # --- Regression tests for rapidfuzz runtime dependency ---

    def test_rapidfuzz_is_importable(self) -> None:
        """rapidfuzz must be installed and importable in the supported environment.

        This is a regression guard: if rapidfuzz is not declared as a runtime
        dependency, candidate ranking fails at runtime with ModuleNotFoundError.
        """
        from rapidfuzz import fuzz

        assert fuzz.token_sort_ratio("hello", "hello") == 100

    def test_rank_uses_rapidfuzz_real_scoring(self) -> None:
        """Ranking must exercise the real rapidfuzz-based scoring path.

        Ensures the candidate ranker does not silently mock or skip the
        rapidfuzz dependency during real scoring.
        """
        candidates = [
            self._make_candidate(
                title="Arabic Cooking", authors=["Fatima Mernissi"]
            ),
        ]
        ranked = self._ranker.rank(
            candidates,
            ocr_title="Arabic Cooking",
            ocr_author="Fatima Mernissi",
        )
        assert len(ranked) == 1
        # Real scoring must produce a high score, not a zero/default
        assert ranked[0].title_score > 0.9
        assert ranked[0].author_score > 0.9
        assert ranked[0].total_score > 0.0

    # --- Phase 17: Title-score floor prevents false-positive author matches ---

    def test_low_title_score_penalized_over_false_author_match(self) -> None:
        """Candidate with poor title match should not beat candidate with
        decent title match via a spurious author fuzzy-match.

        Regression test for 1nFyRFDKc: 'Theodori episcopi Mopsuesteni' by
        Theodore (false author match 0.6667) was beating candidates with
        better title matches but no author match.
        """
        # Wrong candidate: poor title (0.32) but spurious author match (0.67)
        wrong = self._make_candidate(
            title="Theodori episcopi Mopsuesteni",
            authors=["Theodore"],
            source="open_library",
        )
        # Better candidate: decent title (0.37) but no author match
        better_title = self._make_candidate(
            title="Origines de l'inivers et de l'homme",
            authors=[],
            source="open_library",
        )
        ranked = self._ranker.rank(
            [wrong, better_title],
            ocr_title="Lindividu et la communauté Raison dialectique",
            ocr_author="Théodore Litt",
        )
        # The false-positive author match should NOT make the wrong book win
        assert ranked[0].metadata.title != "Theodori episcopi Mopsuesteni"

    def test_good_title_match_beats_false_author_match(self) -> None:
        """A candidate with a strong title match should beat a candidate
        with poor title but a spurious author match.
        """
        good_title = self._make_candidate(
            title="Atomic Habits", authors=["James Clear"]
        )
        false_author = self._make_candidate(
            title="Completely Unrelated Book",
            authors=["James"],  # Shares token but different author
            source="open_library",
        )
        ranked = self._ranker.rank(
            [false_author, good_title],
            ocr_title="Atomic Habits",
            ocr_author="James Clear",
        )
        assert ranked[0].metadata.title == "Atomic Habits"

    def test_isbn_can_still_override_poor_title_match(self) -> None:
        """ISBN exact match should still beat a candidate with a bad title
        but decent author match — ISBN is the strongest evidence.
        """
        isbn_candidate = self._make_candidate(
            title="Different Book", isbn_13="9780000000000"
        )
        author_candidate = self._make_candidate(
            title="Unrelated Title", authors=["James"],
            source="open_library",
        )
        ranked = self._ranker.rank(
            [author_candidate, isbn_candidate],
            ocr_title="Some Book",
            ocr_author="James Clear",
            ocr_isbn="9780000000000",
        )
        assert ranked[0].metadata.title == "Different Book"
        assert ranked[0].isbn_score == 1.0

    def test_title_floor_does_not_penalize_good_title_match(self) -> None:
        """A candidate with a good title score (>= 0.50) should NOT be
        penalized by the title floor.
        """
        candidate = self._make_candidate(
            title="Atomic Habits",
            authors=["James Clear"],
            isbn_13="9780306406157",
            publisher="HarperOne",
        )
        ranked = self._ranker.rank(
            [candidate],
            ocr_title="Atomic Habits",
            ocr_author="James Clear",
            ocr_isbn="9780306406157",
            ocr_publisher="HarperOne",
        )
        assert ranked[0].total_score > 0.95
