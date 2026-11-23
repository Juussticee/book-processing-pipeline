"""Tests for strict confidence thresholds.

Tests the adaptive confidence model that renormalizes over
available evidence and distinguishes ISBN MATCH/CONFLICT/UNKNOWN.
"""

from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from services.confidence_service import ConfidenceService

# IsbnState constants (strings, matching the BookCandidate model)
MATCH = "MATCH"
CONFLICT = "CONFLICT"
UNAVAILABLE = "UNAVAILABLE"


def _make_candidate(
    isbn_state=UNAVAILABLE,
    title_score=0.0,
    title_available=True,
    author_score=0.0,
    author_available=True,
    publisher_score=0.0,
    publisher_available=False,
    source="google_books",
) -> BookCandidate:
    metadata = BookMetadata(
        title="Test Book",
        authors=["Test Author"],
        source=source,
        confidence=0.0,
    )
    candidate = BookCandidate(
        metadata=metadata,
        title_score=title_score,
        author_score=author_score,
        isbn_score=1.0 if isbn_state == MATCH else 0.0,
        publisher_score=publisher_score,
        total_score=0.0,
        isbn_state=isbn_state,
        title_available=title_available,
        author_available=author_available,
        publisher_available=publisher_available,
    )
    return candidate


class TestConfidenceThreshold:
    """Test strict confidence threshold logic."""

    def setup_method(self):
        self._svc = ConfidenceService(threshold=0.85)

    def test_095_accepted(self):
        """High confidence with ISBN match → identified."""
        candidate = _make_candidate(
            isbn_state=MATCH,
            title_score=0.95,
            author_score=0.95,
            publisher_score=0.90,
            publisher_available=True,
        )
        conf = self._svc.calculate(candidate, ocr_confidence=0.95, ocr_available=True)
        assert conf >= 0.85
        assert self._svc.get_status(conf) == "identified"

    def test_070_needs_review(self):
        """Moderate confidence → needs_review (between 0.50 and 0.85)."""
        candidate = _make_candidate(
            isbn_state=UNAVAILABLE,
            title_score=0.55,
            author_score=0.6,
            publisher_score=0.0,
        )
        conf = self._svc.calculate(candidate, ocr_confidence=0.9, ocr_available=True)
        # With normalization over available evidence (no ISBN):
        # title: 0.55*0.25=0.1375, author: 0.6*0.15=0.09
        # ocr: 0.9*0.05=0.045, source: 0.0*0.05=0
        # max_possible = 0.25 + 0.15 + 0.05 + 0 = 0.45
        # conf = (0.1375+0.09+0.045) / 0.45 = 0.2725/0.45 = 0.605
        assert 0.50 <= conf < 0.85
        assert self._svc.get_status(conf) == "needs_review"

    def test_030_needs_review(self):
        """Low confidence → needs_review (never rejected)."""
        candidate = _make_candidate(
            isbn_state=UNAVAILABLE,
            title_score=0.1,
            author_score=0.1,
            publisher_score=0.0,
        )
        conf = self._svc.calculate(candidate, ocr_confidence=0.3, ocr_available=True)
        # Low title, low author, low OCR → well below 0.50
        assert conf < 0.50
        assert self._svc.get_status(conf) == "needs_review"

    def test_default_threshold_is_085(self):
        """Default threshold should be 0.85 for strict accuracy."""
        svc = ConfidenceService()
        assert svc.threshold == 0.85

    def test_get_best_candidate_picks_highest(self):
        """get_best_candidate should return the candidate with highest confidence."""
        svc = ConfidenceService()
        low = _make_candidate(
            isbn_state=UNAVAILABLE,
            title_score=0.3,
            author_score=0.3,
        )
        low.metadata.title = "Low Match"
        high = _make_candidate(
            isbn_state=MATCH,
            title_score=1.0,
            author_score=1.0,
            publisher_score=1.0,
            publisher_available=True,
        )
        best = svc.get_best_candidate(
            [low, high], ocr_confidence=0.95, ocr_available=True
        )
        assert best is not None
        assert best.metadata.title == "Test Book"  # high candidate's title

    def test_get_best_candidate_empty_returns_none(self):
        """Empty candidate list returns None."""
        best = self._svc.get_best_candidate([], ocr_confidence=0.5, ocr_available=True)
        assert best is None

    def test_threshold_boundary_085(self):
        """Exactly 0.85 should be identified (boundary inclusive)."""
        assert self._svc.get_status(0.85) == "identified"

    def test_threshold_boundary_050(self):
        """Exactly 0.50 should be needs_review (boundary inclusive)."""
        assert self._svc.get_status(0.50) == "needs_review"

    def test_threshold_boundary_049(self):
        """0.49 should be needs_review (just below accept threshold)."""
        assert self._svc.get_status(0.49) == "needs_review"

    def test_isbn_dominance_prevents_false_positives(self):
        """Weak title+author match without ISBN should be needs_review (never rejected).

        With very low title similarity (0.25) and very low author similarity
        (0.02), even after normalization over available evidence, the
        confidence should stay below 0.50 — preventing false positives
        where a book with no ISBN match but weak title similarity sneaks
        through.
        """
        candidate = _make_candidate(
            isbn_state=UNAVAILABLE,
            title_score=0.25,
            author_score=0.02,
            publisher_score=0.0,
        )
        conf = self._svc.calculate(candidate, ocr_confidence=0.9, ocr_available=True)
        # Normalized: title=0.0625, author=0.003, ocr=0.045
        # max = 0.25+0.15+0.05 = 0.45
        # conf = (0.0625+0.003+0.045)/0.45 = 0.1105/0.45 = 0.245
        assert conf < 0.50
        assert self._svc.get_status(conf) == "needs_review"
