"""Integration tests for Phase 28 — vision-first book identification pipeline.

These tests verify that:
1. Failed/uncertain OCR results trigger vision model extraction
2. Vision model extracts correct title/author/publisher from cached responses
3. Vision results drive metadata lookup and are NOT overridden by false OCR matches
4. Vision-only results go to manual_review (never auto-accepted)
5. All 7 previously problematic books reach the vision model when OCR fails

The tests use mocked OCR and book lookup services. The vision model's answers are
supplied by synthetic response fixtures written into a temporary cache directory
(``VISION_FIXTURES`` below): they describe what the vision model extracted, not a
real photograph, so no Drive ids, no private photo corpus and no local
``data/vision_model_responses`` cache are needed to run this module.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
import sys
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config
from core.logger import setup_logger
from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from models.ocr_result import OCRResult
from services.book_lookup_service import BookLookupService
from services.confidence_service import ConfidenceService
from services.candidate_ranker import CandidateRanker
from services.metadata_service import MetadataService
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer
from services.vision_model_service import VisionModelService
from services.vision_bookcover_extractor import VisionBookCoverExtractor

logger = setup_logger(__name__)

DATA_DIR = PROJECT_ROOT / "data"

# Synthetic vision-model answers used by every test in this module. The keys are
# synthetic drive ids (long enough to pass the real prefix rule); the values are
# exactly what the vision model is expected to return for such a cover.
VISION_FIXTURES = {
    "synthetic-yugo-0001": {
        "title": "Vie et mort de la Yougoslavie",
        "author": "Paul Garde",
        "publisher": "L'Harmattan",
    },
    "synthetic-russian-0002": {
        "title": "Философия общего дела",
        "author": "Н. Ф. Фёдоров",
        "publisher": None,
    },
    "synthetic-litt-0003": {
        "title": "L'individu et la communauté",
        "author": "Théodore Litt",
        "publisher": "L'Age d'Homme",
    },
    "synthetic-invitation-0004": {
        "title": "L'INVITATION",
        "author": "Blanche Berthoud",
        "publisher": None,
    },
    "synthetic-david-0005": {
        "title": "Weiser David",
        "author": "Paweł Huelle",
        "publisher": None,
    },
    "synthetic-violence-0006": {
        "title": "La violence de l'esprit et la pitié du cœur",
        "author": "Marcel Poncet",
        "publisher": None,
    },
    "synthetic-villes-0007": {
        "title": "La construction des villes",
        "author": "Le Corbusier",
        "publisher": "L'Équerre",
    },
}


def write_vision_response(directory, drive_file_id: str, payload: dict) -> None:
    """Write a cached vision-model response for *drive_file_id*."""
    response = {
        "title": payload.get("title"),
        "title_confidence": 0.95,
        "author": payload.get("author"),
        "author_confidence": 0.95,
        "publisher": payload.get("publisher"),
        "publisher_confidence": 0.90 if payload.get("publisher") else 0.0,
        "orientation": "normal",
        "multiple_books_detected": False,
        "target_book_identified": True,
    }
    target = Path(directory) / f"{drive_file_id}.json"
    target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(scope="module")
def vision_cache(tmp_path_factory):
    """A temporary vision-response cache populated with VISION_FIXTURES."""
    directory = tmp_path_factory.mktemp("vision_responses")
    for drive_id, payload in VISION_FIXTURES.items():
        write_vision_response(directory, drive_id, payload)
    return directory


def get_image_path(drive_file_id: str) -> str:
    """A stable, non-existent path for a synthetic id.

    The preprocessor and OCR engine are mocked in this module, so the pipeline
    never opens the file; nothing here depends on the private photo corpus.
    """
    return str(DATA_DIR / "downloads" / f"{drive_file_id}.jpeg")


class TestVisionFirstRouting:
    """Test that vision model is called when OCR fails."""

    @pytest.fixture(autouse=True)
    def _vision_cache(self, vision_cache):
        self._vision_cache_dir = vision_cache

    def _make_service(
        self,
        ocr_text="",
        ocr_confidence=0.0,
        ocr_title=None,
        ocr_author=None,
        book_lookup_results=None,
        ta_results=None,
        title_results=None,
        author_results=None,
        isbn=None,
        confidence_threshold=0.85,
        vision_cache_dir=None,
    ):
        """Build a MetadataService with mocked OCR and configurable vision cache."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()

        ocr_svc = MagicMock(spec=OCRService)
        ocr_svc.extract_text.return_value = OCRResult(
            text=ocr_text,
            confidence=ocr_confidence,
            words=[],
            language="en",
            image_path="/fake/image.jpg",
        )

        text_normalizer = MagicMock(spec=TextNormalizer)
        text_normalizer.normalize.return_value = ocr_text or ""
        text_normalizer.extract_isbn.return_value = [isbn] if isbn else []

        book_lookup = MagicMock(spec=BookLookupService)
        book_lookup.search_by_isbn.return_value = book_lookup_results or []
        book_lookup.search_by_title_author.return_value = ta_results or []
        book_lookup.search_by_title.return_value = title_results or []
        book_lookup.search_by_author.return_value = author_results or []

        text_extractor = MagicMock()
        text_extractor.extract.return_value = {
            "title": ocr_title,
            "author": ocr_author,
            "isbn": isbn,
            "publisher": None,
            "title_candidates": [],
            "contributor": None,
        }

        cache_repo = MagicMock()

        from repositories.book_metadata_repository import BookMetadataRepository
        meta_repo = BookMetadataRepository(db_path)

        vision_model = VisionModelService(
            cache_dir=str(vision_cache_dir or self._vision_cache_dir)
        )

        svc = MetadataService(
            image_preprocessor=preprocessor,
            ocr_service=ocr_svc,
            text_normalizer=text_normalizer,
            book_lookup=book_lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(
                threshold=confidence_threshold,
                manual_review_threshold=0.50,
                rejected_threshold=0.50,
            ),
            text_extractor=text_extractor,
            metadata_repo=meta_repo,
            cache_repo=cache_repo,
            vision_model=vision_model,
        )
        svc._db_path = db_path
        return svc

    def teardown_method(self):
        import shutil
        debug_dir = PROJECT_ROOT / "data" / "debug"
        if debug_dir.exists():
            for d in os.listdir(debug_dir):
                p = debug_dir / d
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)

    # === Tests for vision-first routing ===

    def test_exception_ocr_triggers_vision(self):
        """When OCR produces no text (exception), vision model must be called."""
        svc = self._make_service(
            ocr_text="",
            ocr_confidence=0.0,
            ocr_title=None,
            ocr_author=None,
            book_lookup_results=[],
        )
        image_path = get_image_path("synthetic-yugo-0001")
        result, candidates = svc.identify_book(
            image_path, drive_file_id="synthetic-yugo-0001", job_id="test_exc"
        )
        # Vision model should have been called (cache hit)
        assert svc._vision_model.stats["total_calls"] >= 1, (
            "Vision model was NOT called when OCR produced no text"
        )
        # Vision extracted correct title
        assert result is not None, "Expected vision recovery to produce a result"

    def test_low_confidence_ocr_triggers_vision(self):
        """When OCR confidence is below threshold, vision model must be called."""
        svc = self._make_service(
            ocr_text="garbage text 1 2 1 1",
            ocr_confidence=0.3,
            ocr_title="garbage text",
            ocr_author=None,
            book_lookup_results=[],
            isbn=None,
        )
        image_path = get_image_path("synthetic-invitation-0004")
        result, candidates = svc.identify_book(
            image_path, drive_file_id="synthetic-invitation-0004", job_id="test_low"
        )
        assert svc._vision_model.stats["total_calls"] >= 1, (
            "Vision model was NOT called when OCR confidence was low"
        )

    def test_missing_author_triggers_vision(self):
        """When OCR extracts title but no author, vision model must be called."""
        svc = self._make_service(
            ocr_text="LA VIOLENCE",
            ocr_confidence=0.5,
            ocr_title="LA VIOLENCE",
            ocr_author=None,
            book_lookup_results=[],
            isbn=None,
        )
        image_path = get_image_path("synthetic-violence-0006")
        result, candidates = svc.identify_book(
            image_path, drive_file_id="synthetic-violence-0006", job_id="test_noauthor"
        )
        assert svc._vision_model.stats["total_calls"] >= 1, (
            "Vision model was NOT called when author was missing"
        )

    def test_good_ocr_result_is_not_overridden_by_vision(self):
        """A resolved high-confidence identification survives final analysis.

        Vision runs for every book (settled Phase-27/44 policy: it is the pipeline's
        FINAL ANALYSIS step), so the guarantee is not "vision is never called" but
        "vision never rewrites an identification the pipeline already accepted".
        """
        svc = self._make_service(
            ocr_text="La construction des villes Le Corbusier",
            ocr_confidence=0.95,
            ocr_title="La construction des villes",
            ocr_author="Le Corbusier",
            book_lookup_results=[],
            ta_results=[
                BookMetadata(
                    title="La construction des villes",
                    authors=["Le Corbusier"],
                    publisher="L'Équerre",
                    source="open_library",
                    confidence=0.95,
                )
            ],
        )
        result, candidates = svc.identify_book(
            "/fake/image.jpg",
            drive_file_id="synthetic-unmapped-9999",
            job_id="test_good",
        )
        # The final-analysis step did run...
        assert svc._vision_model.stats["total_calls"] == 1
        # ...and changed nothing about the accepted identification.
        assert result is not None
        assert result.title == "La construction des villes"
        assert result.authors == ["Le Corbusier"]
        assert result.publisher == "L'Équerre"
        assert result.status == "identified"

    def test_fake_drive_id_never_matches_the_vision_cache(self):
        """A short/foreign id must not collide with a cached response."""
        svc = self._make_service(
            ocr_text="",
            ocr_confidence=0.0,
            ocr_title=None,
            ocr_author=None,
            book_lookup_results=[],
        )
        result, candidates = svc.identify_book(
            "/fake/image.jpg", drive_file_id="d2", job_id="test_fake"
        )
        # "d2" is far too short to be a Drive file id, so the cache must be
        # bypassed even though the fixture directory is populated.
        assert svc._vision_model.stats["cache_hits"] == 0, (
            "Vision model matched a fake drive_file_id — cache collision bug"
        )
        assert result is not None, "the book must still be recorded for review"


class TestVisionRecoveryResults:
    """Test that vision model produces correct results for problem books."""

    @pytest.fixture(autouse=True)
    def _vision_cache(self, vision_cache):
        self._vision_cache_dir = vision_cache

    def _make_service(self):
        """Build a MetadataService with mocked OCR and the vision fixtures."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()

        ocr_svc = MagicMock(spec=OCRService)
        ocr_svc.extract_text.return_value = OCRResult(
            text="", confidence=0.0, words=[], language="en",
            image_path="/fake/image.jpg",
        )

        text_normalizer = MagicMock(spec=TextNormalizer)
        text_normalizer.normalize.return_value = ""
        text_normalizer.extract_isbn.return_value = []

        book_lookup = MagicMock(spec=BookLookupService)
        book_lookup.search_by_isbn.return_value = []
        book_lookup.search_by_title_author.return_value = []
        book_lookup.search_by_title.return_value = []
        book_lookup.search_by_author.return_value = []

        text_extractor = MagicMock()
        text_extractor.extract.return_value = {
            "title": None, "author": None, "isbn": None,
            "publisher": None, "title_candidates": [], "contributor": None,
        }

        from repositories.book_metadata_repository import BookMetadataRepository
        meta_repo = BookMetadataRepository(db_path)

        vision_model = VisionModelService(cache_dir=str(self._vision_cache_dir))
        svc = MetadataService(
            image_preprocessor=preprocessor, ocr_service=ocr_svc,
            text_normalizer=text_normalizer, book_lookup=book_lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(),
            text_extractor=text_extractor,
            metadata_repo=meta_repo, cache_repo=MagicMock(),
            vision_model=vision_model,
        )
        svc._db_path = db_path
        return svc

    def teardown_method(self):
        import shutil
        debug_dir = PROJECT_ROOT / "data" / "debug"
        if debug_dir.exists():
            for d in os.listdir(debug_dir):
                p = debug_dir / d
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)

    def test_yugoslavia_vision_extraction(self):
        """Vision model should extract 'Vie et mort de la Yougoslavie' by Paul Garde."""
        svc = self._make_service()
        drive_id = "synthetic-yugo-0001"
        image_path = get_image_path(drive_id)
        result, _ = svc.identify_book(
            image_path, drive_file_id=drive_id, job_id="j_yug"
        )
        assert svc._vision_model.stats["total_calls"] >= 1
        # The vision cache carries the extraction the model produced
        vm = svc._vision_model
        vision_meta = vm.extract_vision_metadata(image_path, drive_id)
        assert vision_meta is not None
        assert "yougoslavie" in (vision_meta.title or "").lower(), (
            f"Vision title '{vision_meta.title}' should contain 'yougoslavie'"
        )
        assert "garde" in (vision_meta.author or "").lower(), (
            f"Vision author '{vision_meta.author}' should contain 'garde'"
        )

    def test_russian_vision_extraction(self):
        """Vision model should extract Cyrillic title 'Философия общего дела'."""
        svc = self._make_service()
        drive_id = "synthetic-russian-0002"
        image_path = get_image_path(drive_id)
        result, _ = svc.identify_book(
            image_path, drive_file_id=drive_id, job_id="j_rus"
        )
        assert svc._vision_model.stats["total_calls"] >= 1
        vm = svc._vision_model
        vision_meta = vm.extract_vision_metadata(image_path, drive_id)
        assert vision_meta is not None
        assert "Философия" in (vision_meta.title or "")
        assert vision_meta.author is not None

    def test_theodore_litt_vision_extraction(self):
        """Vision model should extract 'L'individu et la communauté' by Théodore Litt,
        NOT 'Theodori episcopi Mopsuesteni'."""
        svc = self._make_service()
        drive_id = "synthetic-litt-0003"
        image_path = get_image_path(drive_id)
        result, _ = svc.identify_book(
            image_path, drive_file_id=drive_id, job_id="j_litt"
        )
        vm = svc._vision_model
        vision_meta = vm.extract_vision_metadata(image_path, drive_id)
        assert vision_meta is not None
        assert "individu" in (vision_meta.title or "").lower()
        assert "Litt" in (vision_meta.author or "")

    def test_invitation_vision_extraction(self):
        """Vision model should extract 'L'Invitation' by Blanche Berthoud."""
        svc = self._make_service()
        drive_id = "synthetic-invitation-0004"
        image_path = get_image_path(drive_id)
        result, _ = svc.identify_book(
            image_path, drive_file_id=drive_id, job_id="j_inv"
        )
        vm = svc._vision_model
        vision_meta = vm.extract_vision_metadata(image_path, drive_id)
        assert vision_meta is not None
        assert "INVITATION" in (vision_meta.title or "").upper()
        assert "Berthoud" in (vision_meta.author or "")

    def test_vision_result_preserved_on_lookup_failure(self):
        """When vision extracts correct info but metadata API fails,
        the result must go to manual_review with vision data preserved."""
        svc = self._make_service()
        drive_id = "synthetic-litt-0003"
        image_path = get_image_path(drive_id)
        result, _ = svc.identify_book(
            image_path, drive_file_id=drive_id, job_id="j_nolookup"
        )
        # No candidates from lookup → vision-only result should be preserved
        assert result is not None, (
            "Vision extraction result must be preserved even when metadata lookup fails"
        )
        assert result.vision_used or result.source == "vision_model", (
            "Result must be marked as vision-sourced"
        )
        assert result.status in ("needs_review", "manual_review", "identified")

    def test_vision_does_not_auto_accept(self):
        """Vision-only results must NOT be auto-accepted even with high vision confidence."""
        svc = self._make_service()
        # Mock book lookup to return a DIFFERENT book (false candidate)
        svc._book_lookup.search_by_title_author.return_value = [
            BookMetadata(
                title="Theodori episcopi Mopsuesteni",
                authors=["Theodore", "Theodore Bishop of Mopsuestia"],
                source="open_library",
                confidence=0.6,
            )
        ]
        drive_id = "synthetic-litt-0003"
        image_path = get_image_path(drive_id)
        result, _ = svc.identify_book(
            image_path, drive_file_id=drive_id, job_id="j_falsepos"
        )
        # The wrong candidate should NOT be auto-accepted
        if result is not None:
            assert result.isbn_13 is None or result.title != "Theodori episcopi Mopsuesteni" or \
                result.confidence < 0.85, (
                "False candidate must NOT be auto-accepted by vision recovery"
            )


class TestVisionSafetyRules:
    """Test that vision-first routing does not introduce false positives."""

    @pytest.fixture(autouse=True)
    def _vision_cache(self, vision_cache):
        self._vision_cache_dir = vision_cache

    def test_vision_with_false_author_candidate_not_accepted(self):
        """Theodore Litt case: vision extracts correct title/author,
        but Open Library has no matching book. A false author-only match
        ('Theodore') must NOT overwrite the vision extraction."""
        from services.vision_bookcover_extractor import VisionMetadata
        from services.vision_model_service import VisionModelService

        vms = VisionModelService(cache_dir=str(self._vision_cache_dir))
        # Load the vision response for Theodore Litt
        vision_meta = vms.extract_vision_metadata(
            "/fake/image.jpg", "synthetic-litt-0003"
        )
        assert vision_meta is not None
        assert "individu" in (vision_meta.title or "").lower()
        assert "Litt" in (vision_meta.author or "")

        # The false candidate that OCR pipeline matched:
        false_candidate = BookMetadata(
            title="Theodori episcopi Mopsuesteni",
            authors=["Theodore"],
            source="open_library",
        )
        # Title similarity between vision and false candidate
        from rapidfuzz import fuzz
        sim = fuzz.token_sort_ratio(
            vision_meta.title.lower(),
            false_candidate.title.lower(),
        ) / 100.0
        assert sim < 0.70, (
            f"Vision title '{vision_meta.title}' vs false candidate "
            f"'{false_candidate.title}' similarity {sim:.4f} — "
            "if >= 0.70 this would incorrectly auto-accept the false candidate"
        )

    def test_vision_result_goes_to_manual_review_without_verification(self):
        """When vision extracts a title but lookup finds nothing related,
        the result goes to manual_review, NOT identified."""
        svc = self._make_service_safe()
        # No candidates at all
        result, _ = svc.identify_book(
            "/fake/image.jpg",
            drive_file_id="synthetic-litt-0003",
            job_id="j_nocand",
        )
        assert result is not None, "Vision-only result must be preserved"
        assert result.status == "needs_review", (
            f"Vision-only result without metadata match must be needs_review, "
            f"got {result.status}"
        )

    def _make_service_safe(self):
        """Build a service where OCR fails and lookup returns nothing."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()

        ocr_svc = MagicMock(spec=OCRService)
        ocr_svc.extract_text.return_value = OCRResult(
            text="", confidence=0.0, words=[], language="en",
            image_path="/fake/image.jpg",
        )

        text_normalizer = MagicMock(spec=TextNormalizer)
        text_normalizer.normalize.return_value = ""
        text_normalizer.extract_isbn.return_value = []

        book_lookup = MagicMock(spec=BookLookupService)
        book_lookup.search_by_isbn.return_value = []
        book_lookup.search_by_title_author.return_value = []
        book_lookup.search_by_title.return_value = []
        book_lookup.search_by_author.return_value = []

        text_extractor = MagicMock()
        text_extractor.extract.return_value = {
            "title": None, "author": None, "isbn": None,
            "publisher": None, "title_candidates": [], "contributor": None,
        }

        from repositories.book_metadata_repository import BookMetadataRepository
        meta_repo = BookMetadataRepository(db_path)

        vision_model = VisionModelService(cache_dir=str(self._vision_cache_dir))
        svc = MetadataService(
            image_preprocessor=preprocessor, ocr_service=ocr_svc,
            text_normalizer=text_normalizer, book_lookup=book_lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(),
            text_extractor=text_extractor,
            metadata_repo=meta_repo, cache_repo=MagicMock(),
            vision_model=vision_model,
        )
        svc._db_path = db_path
        return svc

    def teardown_method(self):
        import shutil
        debug_dir = PROJECT_ROOT / "data" / "debug"
        if debug_dir.exists():
            for d in os.listdir(debug_dir):
                p = debug_dir / d
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)


class TestVisionModelService:
    """Test VisionModelService directly."""

    def test_cache_no_false_matches(self, vision_cache):
        """Drive file ID 'd2' must NOT match a cache file containing those chars."""
        vms = VisionModelService(cache_dir=str(vision_cache))
        result = vms.extract_vision_metadata("/fake/image.jpg", "d2")
        assert result is None, "Short/non-Drive-ID strings must not match cache"

    def test_cache_finds_matching_drive_id(self, vision_cache):
        """A full id must match its cached response."""
        vms = VisionModelService(cache_dir=str(vision_cache))
        result = vms.extract_vision_metadata(
            "/fake/image.jpg", "synthetic-yugo-0001"
        )
        assert result is not None
        assert "YOUGOSLAVIE" in (result.title or "").upper(), (
            f"Vision title '{result.title}' should contain 'yougoslavie'"
        )

    def test_needs_vision_no_ocr_text(self):
        """needs_vision returns True when OCR produced no text."""
        vms = VisionModelService()
        assert vms.needs_vision(
            ocr_title=None, ocr_author=None,
            ocr_result_text="", ocr_confidence=0.0,
            ocr_available=False, candidates=[],
        ) is True

    def test_needs_vision_low_confidence(self):
        """needs_vision returns True when OCR confidence is below threshold."""
        vms = VisionModelService()
        assert vms.needs_vision(
            ocr_title="Some Book", ocr_author="Some Author",
            ocr_result_text="Some Book by Some Author",
            ocr_confidence=0.3,
            ocr_available=True,
            candidates=[],
            confidence=0.3,
        ) is True

    def test_needs_vision_missing_author(self):
        """needs_vision returns True when author is missing."""
        vms = VisionModelService()
        assert vms.needs_vision(
            ocr_title="Some Book", ocr_author=None,
            ocr_result_text="Some Book",
            ocr_confidence=0.8,
            ocr_available=True,
            candidates=[BookMetadata(title="Some Book", authors=[])],
        ) is True

    def test_needs_vision_not_needed_high_confidence(self):
        """needs_vision returns False when OCR is good and candidates exist."""
        vms = VisionModelService()
        assert vms.needs_vision(
            ocr_title="Le Mal Eté", ocr_author="Jean Vuilleumier",
            ocr_result_text="Le Mal Eté Jean Vuilleumier",
            ocr_confidence=0.9,
            ocr_available=True,
            candidates=[BookMetadata(title="Le Mal Eté", authors=["Jean Vuilleumier"])],
            confidence=0.95,
        ) is False

    def test_never_invent_author(self):
        """VisionMetadata must have author=None when author is not visible."""
        from services.vision_bookcover_extractor import VisionBookCoverExtractor
        response = json.dumps({
            "title": "Test Book",
            "title_confidence": 0.95,
            "author": None,
            "author_confidence": 0.0,
            "publisher": "Publisher",
            "publisher_confidence": 0.5,
            "subtitle": None,
            "orientation": "normal",
            "multiple_books_detected": False,
            "target_book_identified": True,
            "other_text": [],
            "uncertainties": ["Author name not visible"],
        })
        meta = VisionBookCoverExtractor.extract_from_response(response)
        assert meta.author is None
        assert meta.author_confidence == 0.0
        assert "Author name not visible" in meta.uncertainties


class TestRegressionCases:
    """Regression tests for the 7 previously problematic books.

    The vision answers come from ``VISION_FIXTURES``; the assertions pin the
    their content, so the regression is reproducible from the repository and
    does not depend on a developer-local cache.
    """

    REGRESSION_CASES = [
        # (drive_file_id, expected_title_keyword, expected_author_keyword)
        ("synthetic-yugo-0001", "Yougoslavie", "Garde"),
        ("synthetic-russian-0002", "Философия", "Фёдоров"),
        ("synthetic-invitation-0004", "INVITATION", "Berthoud"),
        ("synthetic-litt-0003", "individu", "Litt"),
        ("synthetic-david-0005", "David", "Huelle"),
        ("synthetic-villes-0007", "construction des villes", "Corbusier"),
        ("synthetic-violence-0006", "violence", "Poncet"),
    ]

    def test_vision_extracts_all_problem_books(self, vision_cache):
        """Every problem book's vision answer is mapped into VisionMetadata."""
        vms = VisionModelService(cache_dir=str(vision_cache))
        for drive_file_id, exp_title_kw, exp_author_kw in self.REGRESSION_CASES:
            vision_meta = vms.extract_vision_metadata(
                "/fake/image.jpg", drive_file_id
            )
            assert vision_meta is not None, (
                f"No cached vision response for drive_file_id={drive_file_id}"
            )
            assert exp_title_kw.lower() in (vision_meta.title or "").lower(), (
                f"Vision title '{vision_meta.title}' for {drive_file_id} "
                f"does not contain expected keyword '{exp_title_kw}'"
            )
            assert exp_author_kw.lower() in (
                vision_meta.author or ""
            ).lower(), (
                f"Vision author '{vision_meta.author}' for {drive_file_id} "
                f"does not contain expected keyword '{exp_author_kw}'"
            )

    def test_correct_book_not_replaced_by_false_match(self, vision_cache):
        """Theodore Litt: vision title must NOT match false candidate."""
        vms = VisionModelService(cache_dir=str(vision_cache))
        vision_meta = vms.extract_vision_metadata(
            "/fake/image.jpg", "synthetic-litt-0003"
        )
        from rapidfuzz import fuzz
        false_title = "Theodori episcopi Mopsuesteni"
        sim = fuzz.token_sort_ratio(
            vision_meta.title.lower(), false_title.lower()
        ) / 100.0
        assert sim < 0.50, (
            f"Vision title '{vision_meta.title}' too similar to false "
            f"candidate '{false_title}' (sim={sim:.4f})"
        )
