"""Unit tests for ConfidenceService.

Tests the adaptive confidence calculation model that distinguishes
between MATCH, CONFLICT, and UNAVAILABLE ISBN states.
"""

from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from services.confidence_service import ConfidenceService


class TestConfidenceService:
    """Test the confidence calculation service."""

    def setup_method(self) -> None:
        self._service = ConfidenceService(threshold=0.85)

    def _make_candidate(
        self,
        isbn_score: float = 0.0,
        isbn_state: str = "UNAVAILABLE",
        title_score: float = 0.0,
        author_score: float = 0.0,
        publisher_score: float = 0.0,
        title_available: bool = False,
        author_available: bool = False,
        publisher_available: bool = False,
        source: str = "google_books",
    ) -> BookCandidate:
        metadata = BookMetadata(
            title="Test",
            subtitle=None,
            authors=[],
            isbn_10=None,
            isbn_13=None,
            publisher=None,
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
        return BookCandidate(
            metadata=metadata,
            title_score=title_score,
            author_score=author_score,
            isbn_score=isbn_score,
            publisher_score=publisher_score,
            total_score=0.0,
            source=source,
            isbn_state=isbn_state,
            title_available=title_available,
            author_available=author_available,
            publisher_available=publisher_available,
        )

    def test_perfect_confidence(self) -> None:
        """All scores at 1.0 with ISBN MATCH and title+author available."""
        candidate = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=1.0,
            publisher_available=True,
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.95, ocr_available=True
        )
        assert confidence > 0.90

    def test_isbn_weight(self) -> None:
        """ISBN should dominate the confidence score (50% weight)."""
        candidate_with_isbn = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_available=True,
            author_available=True,
        )
        candidate_without_isbn = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_available=True,
            author_available=True,
        )
        conf_with = self._service.calculate(
            candidate_with_isbn, ocr_confidence=1.0, ocr_available=True
        )
        conf_without = self._service.calculate(
            candidate_without_isbn, ocr_confidence=1.0, ocr_available=True
        )
        assert conf_with > conf_without

    def test_no_ocr_penalty(self) -> None:
        """When OCR text is not available, confidence is penalized."""
        candidate = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=1.0,
            publisher_available=True,
        )
        conf_with_ocr = self._service.calculate(
            candidate, ocr_confidence=1.0, ocr_available=True
        )
        conf_without_ocr = self._service.calculate(
            candidate, ocr_confidence=0.0, ocr_available=False
        )
        assert conf_without_ocr < conf_with_ocr

    def test_source_bonus(self) -> None:
        """Google Books source should get a small bonus over Open Library."""
        candidate_gb = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_available=True,
            author_available=True,
            source="google_books",
        )
        candidate_ol = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_available=True,
            author_available=True,
            source="open_library",
        )
        conf_gb = self._service.calculate(
            candidate_gb, ocr_confidence=1.0, ocr_available=True
        )
        conf_ol = self._service.calculate(
            candidate_ol, ocr_confidence=1.0, ocr_available=True
        )
        assert conf_gb > conf_ol

    def test_low_confidence_below_threshold(self) -> None:
        """Confidence below threshold should return False from is_auto_accepted."""
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=0.3,
            title_available=True,
            author_score=0.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="google_books",
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.5, ocr_available=True
        )
        # Without title AND author, capped at 0.60
        assert confidence <= 0.60
        assert not self._service.is_auto_accepted(confidence)
        assert self._service.get_status(confidence) == "needs_review"

    def test_high_confidence_above_threshold(self) -> None:
        """Confidence above threshold passes."""
        candidate = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=0.95,
            title_available=True,
            author_score=0.95,
            author_available=True,
            publisher_score=0.90,
            publisher_available=True,
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.95, ocr_available=True
        )
        assert confidence >= 0.85
        assert self._service.is_auto_accepted(confidence)
        assert self._service.get_status(confidence) == "identified"

    def test_confidence_clamped(self) -> None:
        """Confidence should never exceed 1.0."""
        candidate = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=1.0,
            publisher_available=True,
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=1.0, ocr_available=True
        )
        assert confidence <= 1.0

    def test_confidence_never_negative(self) -> None:
        """Confidence should never be below 0.0."""
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=0.0,
            title_available=True,
            author_score=0.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=True,
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.0, ocr_available=False
        )
        assert confidence >= 0.0

    def test_get_best_candidate(self) -> None:
        """get_best_candidate should return the highest-scored candidate."""
        candidates = [
            self._make_candidate(
                isbn_score=0.0,
                isbn_state="UNAVAILABLE",
                title_score=0.5,
                title_available=True,
                author_score=0.5,
                author_available=True,
                publisher_score=0.0,
                publisher_available=False,
                source="google_books",
            ),
            self._make_candidate(
                isbn_score=1.0,
                isbn_state="MATCH",
                title_score=1.0,
                title_available=True,
                author_score=1.0,
                author_available=True,
                publisher_score=1.0,
                publisher_available=True,
                source="google_books",
            ),
        ]
        best = self._service.get_best_candidate(
            candidates, ocr_confidence=0.95, ocr_available=True
        )
        assert best is not None
        assert best.metadata.source == "google_books"
        assert best.metadata.confidence >= 0.90

    def test_get_best_candidate_empty(self) -> None:
        """get_best_candidate returns None for empty list."""
        best = self._service.get_best_candidate(
            [], ocr_confidence=0.5, ocr_available=True
        )
        assert best is None

    def test_default_threshold(self) -> None:
        """Default threshold should be 0.85."""
        service = ConfidenceService()
        assert service.threshold == 0.85

    def test_custom_threshold(self) -> None:
        """Custom threshold should be respected."""
        service = ConfidenceService(threshold=0.5)
        assert service.threshold == 0.5
        assert service.is_auto_accepted(0.6)
        assert not service.is_auto_accepted(0.4)

    def test_low_ocr_confidence_penalty(self) -> None:
        """Low OCR confidence should reduce the score vs high OCR confidence."""
        candidate = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=0.5,
            title_available=True,
            author_score=0.5,
            author_available=True,
            publisher_score=0.5,
            publisher_available=True,
        )
        conf_high_ocr = self._service.calculate(
            candidate, ocr_confidence=0.9, ocr_available=True
        )
        conf_low_ocr = self._service.calculate(
            candidate, ocr_confidence=0.3, ocr_available=True
        )
        assert conf_low_ocr < conf_high_ocr

    # --- New tests for adaptive scoring ---

    def test_exact_title_author_no_isbn_accepted(self) -> None:
        """Exact title + author match with no ISBN should be accepted.

        This is the core fix: missing ISBN (UNAVAILABLE) should NOT
        penalize candidates. After normalization over available evidence,
        a perfect title+author match should exceed 0.85.
        """
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="open_library",
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.92, ocr_available=True
        )
        assert confidence >= 0.85
        assert self._service.is_auto_accepted(confidence)
        assert self._service.get_status(confidence) == "identified"

    def test_isbn_conflict_penalized(self) -> None:
        """ISBN conflict (both present but different) should be penalized."""
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="CONFLICT",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="google_books",
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.92, ocr_available=True
        )
        # Should be well below threshold due to conflict penalty
        assert confidence < 0.60
        assert not self._service.is_auto_accepted(confidence)
        assert self._service.get_status(confidence) == "needs_review"

    def test_title_only_not_accepted(self) -> None:
        """Exact title match alone (no author) should NOT be auto-accepted."""
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=1.0,
            title_available=True,
            author_score=0.0,
            author_available=False,
            publisher_score=0.0,
            publisher_available=False,
            source="open_library",
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.92, ocr_available=True
        )
        assert confidence <= 0.60
        assert not self._service.is_auto_accepted(confidence)

    def test_author_only_not_accepted(self) -> None:
        """Exact author match alone (no title) should NOT be auto-accepted."""
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=0.0,
            title_available=False,
            author_score=1.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="open_library",
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.92, ocr_available=True
        )
        assert confidence <= 0.60
        assert not self._service.is_auto_accepted(confidence)

    def test_wrong_author_not_accepted(self) -> None:
        """Exact title but wrong author should not be auto-accepted."""
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=1.0,
            title_available=True,
            author_score=0.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="open_library",
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.92, ocr_available=True
        )
        # Title+author both available, but author_score=0 should reduce it
        assert not self._service.is_auto_accepted(confidence)

    def test_wrong_title_not_accepted(self) -> None:
        """Wrong title but correct author should not be auto-accepted."""
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=0.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="open_library",
        )
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.92, ocr_available=True
        )
        assert not self._service.is_auto_accepted(confidence)

    def test_ambiguous_candidate_detection(self) -> None:
        """get_best_candidate_with_margin should flag ambiguous candidates."""
        c1 = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            source="google_books",
        )
        c2 = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=0.95,
            author_available=True,
            source="google_books",
        )
        best, conf, ambiguous = self._service.get_best_candidate_with_margin(
            [c1, c2], ocr_confidence=0.95, ocr_available=True
        )
        assert best is not None
        assert ambiguous is True

    def test_unambiguous_candidate_detection(self) -> None:
        """get_best_candidate_with_margin should not flag clear winners."""
        c1 = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            source="google_books",
        )
        c2 = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=0.3,
            title_available=True,
            author_score=0.3,
            author_available=True,
            source="google_books",
        )
        best, conf, ambiguous = self._service.get_best_candidate_with_margin(
            [c1, c2], ocr_confidence=0.95, ocr_available=True
        )
        assert best is not None
        assert ambiguous is False

    # --- Milestone 8.1: Configurable thresholds ---

    def test_default_thresholds(self) -> None:
        """Default manual_review and rejected thresholds should be 0.50."""
        service = ConfidenceService()
        assert service.threshold == 0.85
        assert service.manual_review_threshold == 0.50
        assert service.rejected_threshold == 0.50

    def test_custom_manual_review_threshold(self) -> None:
        """Custom manual_review_threshold should be respected."""
        service = ConfidenceService(manual_review_threshold=0.60)
        assert service.manual_review_threshold == 0.60
        assert service.is_auto_accepted(0.9)
        assert service.is_manual_review(0.7)
        assert not service.is_manual_review(0.5)

    def test_custom_rejected_threshold(self) -> None:
        """Custom rejected_threshold is respected (backward compat)."""
        service = ConfidenceService(rejected_threshold=0.40)
        assert service.rejected_threshold == 0.40
        # get_status uses manual_review_threshold as the boundary
        assert service.get_status(0.35) == "needs_review"
        assert service.get_status(0.45) == "needs_review"

    def test_all_thresholds_custom(self) -> None:
        """All three thresholds can be set together."""
        service = ConfidenceService(
            threshold=0.90,
            manual_review_threshold=0.60,
            rejected_threshold=0.40,
        )
        assert service.threshold == 0.90
        assert service.manual_review_threshold == 0.60
        assert service.rejected_threshold == 0.40
        assert service.is_auto_accepted(0.95)
        assert service.is_manual_review(0.7)
        assert service.get_status(0.3) == "needs_review"

    def test_invalid_threshold_above_1(self) -> None:
        """Threshold above 1.0 should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="threshold must be between"):
            ConfidenceService(threshold=1.5)

    def test_invalid_threshold_below_0(self) -> None:
        """Threshold below 0.0 should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="threshold must be between"):
            ConfidenceService(threshold=-0.1)

    def test_invalid_manual_review_threshold(self) -> None:
        """Out-of-range manual_review_threshold should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="manual_review_threshold"):
            ConfidenceService(manual_review_threshold=1.5)

    def test_invalid_rejected_threshold(self) -> None:
        """Out-of-range rejected_threshold should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="rejected_threshold"):
            ConfidenceService(rejected_threshold=-0.5)

    def test_boundary_at_threshold(self) -> None:
        """Confidence exactly at threshold is auto-accepted."""
        service = ConfidenceService(threshold=0.90)
        assert service.is_auto_accepted(0.90)
        assert service.is_auto_accepted(0.8999) is False

    def test_boundary_at_manual_review_threshold(self) -> None:
        """Confidence exactly at manual_review_threshold is in review range."""
        service = ConfidenceService(
            threshold=0.90, manual_review_threshold=0.50, rejected_threshold=0.50
        )
        assert service.is_manual_review(0.50)
        assert not service.is_manual_review(0.4999)

    def test_boundary_at_rejected_threshold(self) -> None:
        """Confidence below threshold is needs_review (never rejected)."""
        service = ConfidenceService(
            threshold=0.90, manual_review_threshold=0.60, rejected_threshold=0.40
        )
        # get_status: all confidence below threshold returns needs_review
        # (no 'rejected' status — non-negotiable requirement)
        assert service.get_status(0.40) == "needs_review"
        assert service.get_status(0.39) == "needs_review"
        assert service.get_status(0.59) == "needs_review"

    def test_status_classification_all_ranges(self) -> None:
        """Verify status classification across all confidence ranges."""
        service = ConfidenceService(
            threshold=0.85, manual_review_threshold=0.50, rejected_threshold=0.50
        )
        assert service.get_status(0.90) == "identified"
        assert service.get_status(0.70) == "needs_review"
        assert service.get_status(0.30) == "needs_review"

    # --- Extraction quality penalty tests ---

    def test_suspicious_title_caps_confidence(self) -> None:
        """If OCR-extracted title is a contributor phrase, confidence is capped."""
        from services.book_text_extractor import BookTextExtractor
        candidate = self._make_candidate(
            isbn_score=0.0,
            isbn_state="UNAVAILABLE",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="google_books",
        )
        # Set a suspicious title on the metadata
        candidate.metadata.extracted_title = "avec la collaboration de"
        candidate.metadata.ocr_text = "avec la collaboration de"
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.95, ocr_available=True
        )
        # Should be capped below auto-accept threshold
        assert confidence <= 0.40

    def test_multiple_title_candidates_penalty(self) -> None:
        """Multiple title candidates should cap confidence below 0.75."""
        candidate = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="google_books",
        )
        candidate.metadata.extracted_title = "LE CHRÉTIEN"
        candidate.metadata.extracted_title_candidates = ["LE CHRÉTIEN", "MESSA LAGE HOMME"]
        candidate.metadata.ocr_text = "LE CHRÉTIEN"
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.95, ocr_available=True
        )
        # Should be capped at 0.75 due to multiple candidates
        assert confidence <= 0.75

    def test_ocr_fragmentation_penalty(self) -> None:
        """OCR with many single-character lines should reduce confidence."""
        candidate = self._make_candidate(
            isbn_score=1.0,
            isbn_state="MATCH",
            title_score=1.0,
            title_available=True,
            author_score=1.0,
            author_available=True,
            publisher_score=0.0,
            publisher_available=False,
            source="google_books",
        )
        candidate.metadata.extracted_title = "LE CHRÉTIEN"
        candidate.metadata.ocr_text = "ERIC\nK\nA\nY\nA\nN\nLE CHRÉTIEN"
        candidate.metadata.title_candidates = ["LE CHRÉTIEN"]
        confidence = self._service.calculate(
            candidate, ocr_confidence=0.95, ocr_available=True
        )
        # Without fragmentation: would be high. With fragmentation: reduced.
        assert confidence < 0.95

