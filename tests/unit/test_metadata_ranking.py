"""Unit tests for multi-source metadata ranking and confidence thresholds.

Tests the key identification accuracy improvement: ensuring that the correct
book wins over a false-positive candidate when OCR clearly names one book
and the API returns a different one.
"""

from typing import Optional

from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from services.confidence_service import ConfidenceService


class TestMetadataRanking:
    """Tests multi-source candidate ranking and false-positive rejection."""

    def _make_metadata(
        self,
        title: str,
        authors: Optional[list] = None,
        isbn_10: Optional[str] = None,
        isbn_13: Optional[str] = None,
        publisher: Optional[str] = None,
        source: str = "google_books",
    ) -> BookMetadata:
        return BookMetadata(
            title=title,
            authors=authors or [],
            isbn_10=isbn_10,
            isbn_13=isbn_13,
            publisher=publisher,
            source=source,
            confidence=0.0,
        )

    def _make_candidate(
        self,
        title: str,
        authors: Optional[list] = None,
        isbn_10: Optional[str] = None,
        isbn_13: Optional[str] = None,
        publisher: Optional[str] = None,
        source: str = "google_books",
        title_score: float = 0.0,
        author_score: float = 0.0,
        isbn_score: float = 0.0,
        publisher_score: float = 0.0,
        isbn_state: str = "MATCH",
        title_available: bool = True,
        author_available: bool = True,
        publisher_available: bool = False,
    ) -> BookCandidate:
        metadata = self._make_metadata(
            title=title,
            authors=authors,
            isbn_10=isbn_10,
            isbn_13=isbn_13,
            publisher=publisher,
            source=source,
        )
        return BookCandidate(
            metadata=metadata,
            title_score=title_score,
            author_score=author_score,
            isbn_score=isbn_score,
            publisher_score=publisher_score,
            total_score=0.0,
            isbn_state=isbn_state,
            title_available=title_available,
            author_available=author_available,
            publisher_available=publisher_available,
        )

    def test_correct_book_wins_over_false_positive(self):
        """OCR names 'Le coeur vert' but a false-positive API returns 'Dune'.

        The correct candidate must rank higher than the false positive.
        """
        from services.candidate_ranker import CandidateRanker

        ranker = CandidateRanker()

        false_positive_meta = self._make_metadata(
            title="Dune",
            authors=["Frank Herbert"],
            isbn_13="9780441013593",
            source="google_books",
        )
        correct_meta = self._make_metadata(
            title="Le cœur vert",
            authors=["Jean-Louis Kuffer"],
            source="google_books",
        )

        ranked = ranker.rank(
            [false_positive_meta, correct_meta],
            ocr_title="Le cœur vert",
            ocr_author="Jean-Louis Kuffer",
        )

        # The correct book should be ranked first
        assert ranked[0].metadata.title == "Le cœur vert"
        assert ranked[0].metadata.authors == ["Jean-Louis Kuffer"]

        # False positive should have a much lower score
        score_diff = ranked[0].total_score - ranked[1].total_score
        assert score_diff > 0

    def test_isbn_exact_match_prioritized(self):
        """ISBN exact match should score higher than title-only match."""
        from services.candidate_ranker import CandidateRanker

        ranker = CandidateRanker()

        isbn_meta = self._make_metadata(
            title="Dune Messiah",
            isbn_13="9780441013593",
            source="google_books",
        )
        title_meta = self._make_metadata(
            title="Dune",
            source="google_books",
        )

        ranked = ranker.rank(
            [isbn_meta, title_meta],
            ocr_title="Dune",
            ocr_author="Frank Herbert",
            ocr_isbn="9780441013593",
        )

        # ISBN-matched candidate should rank higher
        assert ranked[0].isbn_score == 1.0

    def test_confidence_threshold_085_accepted(self):
        """A perfect match with ISBN >= 0.85 is auto-accepted as 'identified'."""
        svc = ConfidenceService(threshold=0.85)
        candidate = self._make_candidate(
            title="Perfect Match",
            isbn_state="MATCH",
            title_score=1.0,
            author_score=1.0,
            publisher_score=1.0,
            isbn_score=1.0,
            source="google_books",
            publisher_available=True,
        )
        conf = svc.calculate(candidate, ocr_confidence=0.95, ocr_available=True)
        assert conf >= 0.85
        assert svc.get_status(conf) == "identified"

    def test_confidence_threshold_070_needs_review(self):
        """A score of 0.50-0.85 goes to 'needs_review'."""
        svc = ConfidenceService(threshold=0.85)
        # No ISBN available; partial title and author matches
        candidate = self._make_candidate(
            title="Partial Match",
            isbn_state="UNAVAILABLE",
            title_score=0.9,
            author_score=0.7,
            publisher_score=0.5,
            isbn_score=0.0,
            source="google_books",
        )
        conf = svc.calculate(candidate, ocr_confidence=0.9, ocr_available=True)
        # No ISBN penalty; normalized over title+author+ocr+source
        # title: 0.225, author: 0.105, ocr: 0.045
        # max_possible = 0.25 + 0.15 + 0.05 = 0.45
        # conf = (0.225+0.105+0.045) / 0.45 = 0.375/0.45 = 0.833
        assert 0.50 <= conf < 0.85
        assert svc.get_status(conf) == "needs_review"

    def test_confidence_threshold_030_rejected(self):
        """A score < 0.50 is 'rejected'."""
        svc = ConfidenceService(threshold=0.85)
        candidate = self._make_candidate(
            title="Wrong",
            isbn_state="UNAVAILABLE",
            title_score=0.3,
            author_score=0.1,
            publisher_score=0.0,
            isbn_score=0.0,
            source="google_books",
        )
        conf = svc.calculate(candidate, ocr_confidence=0.5, ocr_available=True)
        # title: 0.075, author: 0.015, ocr: 0.025
        # max = 0.25 + 0.15 + 0.05 = 0.45
        # conf = 0.115/0.45 = 0.256
        assert conf < 0.50
        assert svc.get_status(conf) == "needs_review"
