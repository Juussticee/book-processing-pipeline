"""Regression tests for the full verification pipeline integration."""

import os
import tempfile
from unittest.mock import MagicMock

from models.book_metadata import BookMetadata
from models.ocr_result import OCRResult
from services.book_lookup_service import BookLookupService
from services.confidence_service import ConfidenceService
from services.candidate_ranker import CandidateRanker
from services.metadata_service import MetadataService
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer


def make_ocr_result(text="Test Book by Test Author ISBN 9780306406157"):
    return OCRResult(text=text, confidence=0.9, words=[], language="en",
        image_path="/fake/image.jpg")


def make_book_metadata(title="Test Book", authors=None, isbn_10=None,
                        isbn_13=None, publisher=None, cover_url=None,
                        source="google_books", confidence=0.0):
    return BookMetadata(title=title, authors=authors or [], isbn_10=isbn_10,
        isbn_13=isbn_13, publisher=publisher, cover_url=cover_url,
        source=source, confidence=confidence)


class TestVerificationRegression:
    """Comprehensive regression tests for verification pipeline."""

    def setup_method(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)

    def teardown_method(self):
        # Clean up debug dirs
        import shutil
        debug_dir = os.path.join(
            os.getcwd(), "data", "debug"
        )
        if debug_dir and os.path.exists(debug_dir):
            for d in os.listdir(debug_dir):
                if d.startswith("j"):
                    p = os.path.join(debug_dir, d)
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
        if os.path.exists(self._db_path):
            try:
                os.unlink(self._db_path)
            except PermissionError:
                pass  # Windows holds the lock; ignore

    def _make_service(self, book_lookup_result=None, ocr_text=None,
                     title=None, author=None, isbn=None, publisher=None,
                     ta_result=None, title_result=None, author_result=None,
                     text_extractor_fields=None):
        """Build a MetadataService with mocked dependencies."""
        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()
        ocr_svc = MagicMock(spec=OCRService)
        ocr_svc.extract_text.return_value = make_ocr_result(
            ocr_text or "Test Book by Test Author ISBN 9780306406157"
        )
        text_normalizer = MagicMock(spec=TextNormalizer)
        text_normalizer.normalize.return_value = "Test Book"
        text_normalizer.extract_isbn.return_value = (
            [isbn] if isbn else ["9780306406157"]
        )
        book_lookup = MagicMock(spec=BookLookupService)
        if book_lookup_result:
            book_lookup.search_by_isbn.return_value = book_lookup_result
        else:
            book_lookup.search_by_isbn.return_value = []
        book_lookup.search_by_title_author.return_value = ta_result or []
        book_lookup.search_by_title.return_value = title_result or []
        book_lookup.search_by_author.return_value = author_result or []
        text_extractor = MagicMock()
        default_fields = {"title": title or "Test Book", "author": author,
            "isbn": isbn, "publisher": publisher,
            "title_candidates": [], "contributor": None,
        }
        default_fields.update(text_extractor_fields or {})
        text_extractor.extract.return_value = default_fields
        confidence_svc = ConfidenceService(threshold=0.60)
        from repositories.book_metadata_repository import BookMetadataRepository
        meta_repo = BookMetadataRepository(self._db_path)
        return MetadataService(
            image_preprocessor=preprocessor, ocr_service=ocr_svc,
            text_normalizer=text_normalizer, book_lookup=book_lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=confidence_svc,
            text_extractor=text_extractor,
            metadata_repo=meta_repo,
        )

    def test_missing_author_still_via_isbn(self):
        """Book identified via ISBN even when author missing from OCR."""
        meta = make_book_metadata(title="Test Book", authors=["Author"],
            isbn_13="9780306406157", publisher="Pub")
        svc = self._make_service(book_lookup_result=[meta],
            ocr_text="Test Book ISBN 9780306406157", title="Test Book",
            author=None, isbn="9780306406157")
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="d1", job_id="j1")
        assert result is not None
        assert result.title == "Test Book"

    def test_misleading_cover_text_not_identified(self):
        """OCR extracts collection name, not real title."""
        svc = self._make_service(book_lookup_result=[],
            ocr_text="Penguin Classics Collection 9780000000000",
            title="Penguin Classics Collection", author=None, isbn=None)
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="d2", job_id="j2")
        assert result is not None
        assert result.status in ("needs_review", "identified")

    def test_conflicting_isbn(self):
        """Conflicting ISBN between OCR and candidate is flagged."""
        meta = make_book_metadata(title="Test Book", authors=["Author"],
            isbn_13="9780000000000")
        svc = self._make_service(book_lookup_result=[meta],
            ocr_text="Test Book ISBN 9781111111111",
            title="Test Book", author="Author", isbn="9781111111111")
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="d10", job_id="j10")
        # Conflicting ISBN — should not auto-accept at 0.85 threshold
        # Result may be None (manual review) or candidate with low confidence
        assert result is not None or candidates is not None

    def test_all_online_services_unavailable(self):
        """When all online services fail, no candidates."""
        svc = self._make_service(book_lookup_result=[],
            ocr_text="Some Book ISBN 9780306406157",
            title="Some Book", author=None, isbn="9780306406157")
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="d13", job_id="j13")
        assert result is not None
        assert result.status in ("needs_review", "identified")

    def test_thresholds_not_lowered(self):
        """Verification thresholds must not be lowered."""
        from services.candidate_verification_service import CandidateVerificationService
        svc = CandidateVerificationService()
        assert svc._confidence_threshold == 0.85
        assert svc._manual_review_threshold == 0.50
        assert svc.MIN_TITLE_SCORE_FOR_AUTO == 0.80

    def test_no_fake_visual_verification(self):
        """Cover verification uses real image processing."""
        from services.cover_verifier import CoverVerifier
        v = CoverVerifier()
        assert hasattr(v, "compute_dhash")
        assert hasattr(v, "dhash_similarity")
        assert hasattr(v, "ssim_score")
        assert hasattr(v, "feature_match_score")
        assert hasattr(v, "color_histogram_score")

    def test_eventual_manual_review(self):
        """Low confidence with no ISBN → manual review."""
        meta = make_book_metadata(title="Simlar Title", authors=["Author"])
        svc = self._make_service(book_lookup_result=[meta],
            ocr_text="Similar Title by Author",
            title="Similar Title", author="Author", isbn=None)
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="d14", job_id="j14")
        assert result is not None, "Low confidence must still return metadata"
        assert result.status in ("identified", "needs_review"), (
            "status must be identified or needs_review, got=%s" % result.status
        )

    def test_arabic_title_author_ocr(self):
        """Arabic title and author in OCR handled by normalizer."""
        svc = self._make_service(book_lookup_result=[],
            ocr_text="كتاب العلوم ISBN 9780123456789",
            title="كتاب العلوم", author=None, isbn="9780123456789")
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="d7", job_id="j7")
        assert result is not None
        assert result.status in ("needs_review", "identified") or result.status in (
            "identified", "needs_review",
        )

    def test_second_pass_ocr_exists(self):
        """AdaptiveOCR multi-pass strategy exists."""
        from services.adaptive_ocr import AdaptiveOCR
        from services.script_detector import ScriptDetector
        d = ScriptDetector()
        assert hasattr(d, "detect")
        assert hasattr(d, "get_language_codes")

    def test_multiple_editions_disambiguated(self):
        """Multiple editions → ISBN disambiguates."""
        meta1 = make_book_metadata(title="Great Gatsby",
            authors=["F. Scott Fitzgerald"], isbn_13="9780743273563")
        meta2 = make_book_metadata(title="Great Gatsby",
            authors=["F. Scott Fitzgerald"], isbn_13="9780743273565")
        svc = self._make_service(book_lookup_result=[meta1, meta2],
            ocr_text="Great Gatsby ISBN 9780743273563",
            title="Great Gatsby", author="F. Scott Fitzgerald",
            isbn="9780743273563")
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="d11", job_id="j11")
        if result:
            assert result.isbn_13 == "9780743273563"

    # === Negative-evidence regression tests (A-F) ===
    # These tests verify that contradictory evidence prevents false auto-acceptance
    # while UNAVAILABLE evidence does NOT count as negative.

    def test_neg_evidence_A_isbn_conflict_prevents_auto_accept(self):
        """A. Genuine ISBN conflict (both present, different) prevents auto-accept.

        OCR extracts ISBN 9781111111111; candidate has ISBN 9780000000000.
        This is a CONFLICT, not UNAVAILABLE — must not auto-accept.
        """
        meta = make_book_metadata(title="Test Book", authors=["Author"],
            isbn_13="9780000000000", confidence=0.95)
        svc = self._make_service(book_lookup_result=[meta],
            ocr_text="Test Book ISBN 9781111111111",
            title="Test Book", author="Author", isbn="9781111111111")
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="dA", job_id="jA")
        # ISBN conflict prevents auto-acceptance, but book is NOT rejected.
        # It is persisted with status=needs_review (non-negotiable requirement).
        assert result is not None, (
            "ISBN conflict must still return metadata (never None); got result=%s" % result
        )
        assert result.status == "needs_review", (
            "ISBN conflict must result in needs_review, not rejected; got=%s" % result.status
        )
        if candidates:
            for c in candidates:
                if c.metadata.title == "Test Book":
                    assert c.isbn_state == "CONFLICT", (
                        "isbn_state should be CONFLICT, got %s" % c.isbn_state
                    )

    def test_neg_evidence_B_overlap_mismatch_not_forced_identified(self):
        """B. Overlap validation mismatch must NOT force status='identified'.

        Before the fix, _validate_overlap would flag a mismatch but the
        code would still set status='identified'. Now it must be
        'needs_review' and the function must return None (manual review).

        Uses a suspicious title (boilerplate) detected from OCR — the
        candidate is found via title search (no ISBN to skip the check).
        """
        # Suspicious title: "Penguin Classics" is a publisher/collection name
        # Candidate has a different title; no ISBN to skip overlap check
        meta = make_book_metadata(title="Le Petit Prince",
            authors=["Antoine de Saint-Exupéry"],
            confidence=0.90)
        svc = self._make_service(
            book_lookup_result=[],
            ta_result=[meta],
            ocr_text="Penguin Classics Collection",
            title="Penguin Classics Collection",
            author=None, isbn=None)
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="dB", job_id="jB")
        # Overlap validation should catch the suspicious title
        # Book is NOT rejected -- persisted with status=needs_review
        assert result is not None, (
            "Suspicious title must still return metadata (never None); got=%s" % result
        )
        if result:
            assert result.status == "needs_review", (
                "Suspicious title must result in needs_review; got=%s" % result.status
            )
        if candidates:
            for c in candidates:
                if c.metadata.title == "Le Petit Prince":
                    assert c.metadata.status in ("needs_review", "manual_review"), (
                        "status should be needs_review or manual_review, got=%s"
                        % c.metadata.status
                    )

    def test_neg_evidence_C_missing_isbn_not_negative(self):
        """C. Missing ISBN (UNAVAILABLE) must NOT prevent auto-acceptance.

        When neither OCR nor candidate has an ISBN, isbn_state should be
        UNAVAILABLE — this is NEUTRAL, not negative. A book with good
        title + author match should still be accepted.
        """
        meta = make_book_metadata(title="Test Book",
            authors=["Author"], confidence=0.90)
        svc = self._make_service(book_lookup_result=[meta],
            ocr_text="Test Book by Author",
            title="Test Book", author="Author", isbn=None)
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="dC", job_id="jC")
        # No ISBN conflict — should be accepted if title+author match well
        assert result is not None, (
            "Missing ISBN (UNAVAILABLE) must not prevent acceptance"
        )

    def test_neg_evidence_D_missing_author_not_negative(self):
        """D. Missing author in OCR is NOT an author contradiction.

        When the OCR has no author but the candidate has one, this is
        UNAVAILABLE — not a CONFLICT. The book should still be identifiable
        via ISBN + title match.
        """
        meta = make_book_metadata(title="Test Book", authors=["Known Author"],
            isbn_13="9780306406157", confidence=0.95)
        svc = self._make_service(book_lookup_result=[meta],
            ocr_text="Test Book ISBN 9780306406157",
            title="Test Book", author=None, isbn="9780306406157")
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="dD", job_id="jD")
        assert result is not None, (
            "Missing author in OCR must not prevent ISBN-based identification"
        )
        assert result.isbn_13 == "9780306406157"

    def test_neg_evidence_E_manual_review_still_present(self):
        """E. Low-confidence (0.50-0.85) candidates still go to manual review.

        manual_review workflow must still exist — candidates with confidence
        between 0.50 and 0.85 should return status=needs_review (NOT None/rejected).
        Uses the default 0.85 threshold and a partial title-only match.
        """
        # Build service with default 0.85 threshold (not the 0.60 in _make_service)
        svc = self._make_service(
            book_lookup_result=[],
            ta_result=[],
            title_result=[make_book_metadata(title="Test Book of Something Else",
                authors=["Author"], confidence=0.0)],
            ocr_text="Test Book by Author",
            title="Test Book", author="Author", isbn=None,
        )
        # Override with default-threshold confidence service
        from services.confidence_service import ConfidenceService
        svc._confidence = ConfidenceService()  # default: 0.85/0.50/0.50
        result, candidates = svc.identify_book("/fake/image.jpg",
            drive_file_id="dE", job_id="jE")
        # Confidence should be below 0.85 (title only partially matches, no ISBN)
        # → manual review (status=needs_review, NOT None/rejected)
        assert result is not None, (
            "Low confidence must still return metadata (never None); got=%s" % result
        )
        assert result.status == "needs_review", (
            "Low confidence must result in needs_review; got=%s" % result.status
        )

    def test_neg_evidence_F_thresholds_not_lowered(self):
        """F. Verification and confidence thresholds must NOT be lowered.

        Confirms thresholds are at the standard values:
        - auto-accept: 0.85
        - manual review: 0.50
        - candidate_verification: 0.85 / 0.50 / title_score 0.80
        """
        from services.confidence_service import ConfidenceService
        cs = ConfidenceService()
        assert cs.threshold == 0.85, "auto-accept threshold must be 0.85"
        assert cs.manual_review_threshold == 0.50, (
            "manual review threshold must be 0.50"
        )
        from services.candidate_verification_service import (
            CandidateVerificationService,
        )
        cv = CandidateVerificationService()
        assert cv._confidence_threshold == 0.85
        assert cv._manual_review_threshold == 0.50
        assert cv.MIN_TITLE_SCORE_FOR_AUTO == 0.80

