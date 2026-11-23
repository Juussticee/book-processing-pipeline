"""Regression tests for CandidateVerificationService evidence-based verification."""

import pytest
from unittest.mock import MagicMock, Mock

from models.book_metadata import BookMetadata
from models.book_candidate import BookCandidate
from services.candidate_verification_service import (
    CandidateVerificationService,
    VerificationEvidence,
    VerificationResult,
)
from services.cover_verifier import CoverVerifier
from services.text_normalizer import TextNormalizer


def make_metadata(
    title="Test Book",
    authors=None,
    isbn_10=None,
    isbn_13=None,
    publisher=None,
    cover_url=None,
    source="google_books",
    confidence=0.0,
):
    return BookMetadata(
        title=title,
        authors=authors or [],
        isbn_10=isbn_10,
        isbn_13=isbn_13,
        publisher=publisher,
        cover_url=cover_url,
        source=source,
        confidence=confidence,
    )


class TestVerificationEvidence:
    """Tests for the VerificationEvidence dataclass."""

    def test_default_values(self):
        ev = VerificationEvidence()
        assert ev.title_score == 0.0
        assert ev.author_score == 0.0
        assert ev.isbn_score == 0.0
        assert ev.publisher_score == 0.0
        assert ev.isbn_state == "UNAVAILABLE"
        assert ev.cover_similarity == 0.0
        assert ev.title_available is False


class TestCandidateVerificationService:
    """Tests for the CandidateVerificationService."""

    def setup_method(self):
        self.cover_verifier = Mock(spec=CoverVerifier)
        self.cover_verifier.verify_covers.return_value = None
        self.text_normalizer = Mock(spec=TextNormalizer)
        self.text_normalizer.normalize.return_value = ""
        self.service = CandidateVerificationService(
            cover_verifier=self.cover_verifier,
            text_normalizer=self.text_normalizer,
        )

    def test_high_confidence_with_title_and_author(self):
        meta = make_metadata(
            title="Le Mal",
            authors=["Herve Joyaux"],
            isbn_13="9782070327799",
        )
        candidate = BookCandidate(
            metadata=meta, title_score=0.0, author_score=0.0,
            isbn_score=0.0, publisher_score=0.0, total_score=0.0,
            source="google_books", isbn_state="UNAVAILABLE",
            title_available=True, author_available=True,
        )
        result = self.service.verify_candidates(
            candidates=[candidate],
            extracted_title="Le Mal",
            extracted_author="Herve Joyaux",
            extracted_isbn="9782070327799",
        )
        assert result.best_candidate is not None
        assert result.best_evidence.isbn_state == "MATCH"
        assert result.best_evidence.title_score >= 0.9

    def test_conflict_isbn_penalizes(self):
        meta = make_metadata(
            title="Test Book", authors=["Test Author"],
            isbn_13="9780000000000",
        )
        candidate = BookCandidate(
            metadata=meta, title_score=0.0, author_score=0.0,
            isbn_score=0.0, publisher_score=0.0, total_score=0.0,
            source="google_books", isbn_state="UNAVAILABLE",
            title_available=True, author_available=True,
        )
        result = self.service.verify_candidates(
            candidates=[candidate],
            extracted_title="Test Book",
            extracted_author="Test Author",
            extracted_isbn="9781111111111",
        )
        assert result.best_evidence.isbn_state == "CONFLICT"

    def test_missing_author_no_penalty(self):
        meta = make_metadata(
            title="Test Book", authors=["Real Author"],
            isbn_13="9780306406157",
        )
        candidate = BookCandidate(
            metadata=meta, title_score=0.0, author_score=0.0,
            isbn_score=0.0, publisher_score=0.0, total_score=0.0,
            source="google_books", isbn_state="UNAVAILABLE",
            title_available=True, author_available=False,
        )
        result = self.service.verify_candidates(
            candidates=[candidate],
            extracted_title="Test Book",
            extracted_author=None,
            extracted_isbn="9780306406157",
        )
        assert result.best_evidence.isbn_state == "MATCH"
        assert result.best_evidence.author_available is False

    def test_misleading_cover_text_title_no_match(self):
        meta = make_metadata(
            title="Completely Different Book",
            authors=["Some Author"],
        )
        candidate = BookCandidate(
            metadata=meta, title_score=0.0, author_score=0.0,
            isbn_score=0.0, publisher_score=0.0, total_score=0.0,
            source="google_books", isbn_state="UNAVAILABLE",
            title_available=True, author_available=True,
        )
        result = self.service.verify_candidates(
            candidates=[candidate],
            extracted_title="Misleading Collection Name",
            extracted_author="Some Author",
        )
        assert result.best_evidence.title_score < 0.5

    def test_publisher_before_title(self):
        meta = make_metadata(
            title="The Real Title",
            authors=["Real Author"],
            publisher="Penguin Random House",
            isbn_13="9780141439600",
        )
        candidate = BookCandidate(
            metadata=meta, title_score=0.0, author_score=0.0,
            isbn_score=0.0, publisher_score=0.0, total_score=0.0,
            source="google_books", isbn_state="UNAVAILABLE",
            title_available=True, author_available=True,
            publisher_available=True,
        )
        result = self.service.verify_candidates(
            candidates=[candidate],
            extracted_title="Penguin Random House",
            extracted_author="Real Author",
            extracted_isbn="9780141439600",
            extracted_publisher="Penguin Random House",
        )
        assert result.best_evidence.publisher_score >= 0.7
        assert result.best_evidence.isbn_state == "MATCH"

    def test_multiple_edition_candidates(self):
        meta_hb = make_metadata(
            title="The Great Gatsby",
            authors=["F. Scott Fitzgerald"],
            isbn_13="9780743273563",
        )
        meta_pb = make_metadata(
            title="The Great Gatsby",
            authors=["F. Scott Fitzgerald"],
            isbn_13="9780743273565",
        )
        candidates = [
            BookCandidate(metadata=meta_hb, title_score=0.0, author_score=0.0,
                isbn_score=0.0, publisher_score=0.0, total_score=0.0,
                source="google_books", isbn_state="UNAVAILABLE",
                title_available=True, author_available=True,
            ),
            BookCandidate(metadata=meta_pb, title_score=0.0, author_score=0.0,
                isbn_score=0.0, publisher_score=0.0, total_score=0.0,
                source="google_books", isbn_state="UNAVAILABLE",
                title_available=True, author_available=True,
            ),
        ]
        result = self.service.verify_candidates(
            candidates=candidates,
            extracted_title="The Great Gatsby",
            extracted_author="F. Scott Fitzgerald",
            extracted_isbn="9780743273563",
        )
        assert result.best_candidate.metadata.isbn_13 == "9780743273563"

    def test_duplicate_titles_disambiguated_by_author(self):
        meta1 = make_metadata(title="The Great Gatsby", authors=["F. Scott Fitzgerald"])
        meta2 = make_metadata(title="The Great Gatsby", authors=["Someone Else"])
        candidates = [
            BookCandidate(metadata=meta1, title_score=0.0, author_score=0.0,
                isbn_score=0.0, publisher_score=0.0, total_score=0.0,
                isbn_state="UNAVAILABLE",
                title_available=True, author_available=True,
            ),
            BookCandidate(metadata=meta2, title_score=0.0, author_score=0.0,
                isbn_score=0.0, publisher_score=0.0, total_score=0.0,
                isbn_state="UNAVAILABLE",
                title_available=True, author_available=True,
            ),
        ]
        result = self.service.verify_candidates(
            candidates=candidates,
            extracted_title="The Great Gatsby",
            extracted_author="F. Scott Fitzgerald",
        )
        assert "F. Scott Fitzgerald" in result.best_candidate.metadata.authors

    def test_no_candidates_returns_none_best(self):
        result = self.service.verify_candidates(
            candidates=[],
            extracted_title="Some Book",
        )
        assert result.best_candidate is None

    def test_all_services_unavailable(self):
        result = self.service.verify_candidates(
            candidates=[],
            extracted_title=None,
            extracted_author=None,
        )
        assert result.status == "needs_review"

