"""Partial real-photo acceptance: the cached Drive photographs, run offline.

The images live in ``data/fixtures/real`` (override with
``REAL_PHOTO_FIXTURES``) and are byte-for-byte copies of photographs cached in
the local Drive download directory; nothing is downloaded, re-encoded, rotated
or EXIF-stripped here. They are photographs of a private book collection, so
they are not distributed with the repository and this module skips when they
are absent; the shared guards in ``tests/conftest.py`` explain how to point at
a local copy. Drive, Sheets, the network, the vision model and the OCR
engine are all stubbed, so any outcome that genuinely depends on OCR quality is
marked unverified in the assertions and in the module docstring above it.

What is real: the pixels, image normalization, orientation transforms, image
identity (SHA-256 / dHash / colour / aspect), the duplicate gate, the metadata
repository, the export repository and the worker's processing state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import PIL.Image
import pytest

from models.book_metadata import BookMetadata
from models.drive_file import DriveFile
from models.ocr_result import OCRResult
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.duplicate_repository import DuplicateRepository
from repositories.export_repository import ExportRepository
from repositories.job_repository import JobRepository
from services import content_identity
from services.book_text_extractor import BookTextExtractor
from services.candidate_ranker import CandidateRanker
from services.confidence_service import ConfidenceService
from services.image_preprocessor import ImagePreprocessor
from services.job_service import JobService
from services.metadata_service import MetadataService
from services.text_normalizer import TextNormalizer
from tests.conftest import REAL_FIXTURE_DIR, require_real_photo_fixtures
from workers.processing_worker import ProcessingWorker

require_real_photo_fixtures()

FIXTURE_DIR = REAL_FIXTURE_DIR
MANIFEST = json.loads((FIXTURE_DIR / "metadata.json").read_text(encoding="utf-8"))
BY_ROLE = {entry["role"]: entry for entry in MANIFEST["fixtures"]}

TEXT_MAL_ETE = "Le Mal Été\npar Jean Vuilleumier\nÉditeur: L'Âge d'Homme"
TEXT_COEUR_VERT = "Jean-Louis Kuffer\nLe Cœur Vert\nRécit\nL'Age d'Homme"
TEXT_YOUGOSLAVIE = "VIE ET MORT DE LA YOUGOSLAVIE\npar Paul Garde"
TEXT_GLARE_DEGRADED = "lN DÉSASTRES de la guerre"
TEXT_GHELBER = (
    "DÉBUT, DANS L'ORDRE ET L'HARMONIE DE LA CRÉATION\n"
    "par Angela Ghelber\n"
    "Éditeur: L'Âge d'Homme"
)


def _fixture(role: str) -> str:
    return str(FIXTURE_DIR / BY_ROLE[role]["file"])


def _drive_id(role: str) -> str:
    return BY_ROLE[role]["drive_file_id"]


def _sha256(path: str) -> str:
    return content_identity.sha256_file(path)


def _luma(path: str, size: int = 64) -> np.ndarray:
    with PIL.Image.open(path) as img:
        gray = img.convert("L").resize((size, size), PIL.Image.Resampling.LANCZOS)
    return np.asarray(gray, dtype=np.float32)


def _content_rotation(path: str, reference: str) -> int:
    """Which of 0/90/180/270 degrees best matches ``path`` to ``reference``.

    Used to prove what an image's stored pixels look like without trusting
    naming: 0 means "same orientation as the reference file".
    """
    ref, cur = _luma(reference), _luma(path)
    scores = {
        angle: float(np.abs(cur - np.rot90(ref, k=angle // 90)).mean())
        for angle in (0, 90, 180, 270)
    }
    return min(scores, key=scores.get)


class StubOcr:
    """Fake OCR engine.

    ``good_angle``/``reference`` let a test say "this photograph only looks
    right to the recogniser once the frame is rotated by N degrees", which is
    how the rotation fallback is exercised on a real upside-down photograph.
    """

    def __init__(
        self,
        text: str = TEXT_COEUR_VERT,
        confidence: float = 0.93,
        words: Optional[list] = None,
        error: Optional[str] = None,
        good_angle: Optional[int] = None,
        reference: Optional[str] = None,
        text_by_keyword: Optional[dict] = None,
    ) -> None:
        self.text = text
        self.confidence = confidence
        self.words = words if words is not None else text.split()
        self.error = error
        self.good_angle = good_angle
        self.reference = reference
        self.text_by_keyword = text_by_keyword or {}
        self.calls = 0

    def _payload(self, path: str):
        """Per-photo text, matched on the fixture stem in the temp file name."""
        name = os.path.basename(path).lower()
        for keyword, value in self.text_by_keyword.items():
            if keyword.lower() in name:
                text, confidence = value
                return text, confidence, text.split()
        return self.text, self.confidence, self.words

    def _frame_is_good(self, path: str) -> bool:
        if self.good_angle is None or self.reference is None:
            return True
        return _content_rotation(path, self.reference) == self.good_angle

    def extract_text(self, path: str) -> OCRResult:
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        text, confidence, words = self._payload(path)
        if self._frame_is_good(path):
            return OCRResult(
                text=text,
                confidence=confidence,
                words=words,
                language="fr",
                image_path=path,
            )
        return OCRResult(
            text="?? ??",
            confidence=0.20,
            words=["??", "??"],
            language="fr",
            image_path=path,
        )


class CatalogStub:
    """Offline catalogue: one book per key, counting lookups."""

    def __init__(self, books) -> None:
        self._books = books  # key -> factory
        self.calls = 0

    def _match(self, needle: str):
        for key, factory in self._books.items():
            if key.lower() in (needle or "").lower():
                return factory()
        return None

    def search_by_isbn(self, isbn: str):
        self.calls += 1
        factory = self._books.get(isbn)
        return [factory()] if factory else []

    def search_by_title_author(self, title: str, author=None):
        self.calls += 1
        book = self._match(title)
        return [book] if book else []

    def search_by_title(self, title: str):
        return self.search_by_title_author(title)

    def search_by_author(self, author: str):
        self.calls += 1
        return []


class _NoVision:
    def extract_vision_metadata(self, *args, **kwargs):
        return None


def _mal_ete() -> BookMetadata:
    return BookMetadata(
        title="Le Mal Été",
        authors=["Jean Vuilleumier"],
        publisher="L'Âge d'Homme",
        confidence=1.0,
        source="open_library",
    )


def _coeur_vert() -> BookMetadata:
    return BookMetadata(
        title="Le cœur vert",
        authors=["Jean-Louis Kuffer"],
        isbn_13="9782825104699",
        publisher="L'Âge d'Homme",
        confidence=1.0,
        source="open_library",
    )


def _ghelber() -> BookMetadata:
    return BookMetadata(
        title="Début, dans l'ordre et l'harmonie de la création",
        authors=["Angela Ghelber"],
        publisher="L'Âge d'Homme",
        confidence=1.0,
        source="open_library",
    )


def _desastres() -> BookMetadata:
    return BookMetadata(
        title="Les Désastres de la guerre",
        authors=["Jeanlouis Cornuz"],
        publisher="L'Âge d'Homme",
        confidence=1.0,
        source="open_library",
    )


def _all_books():
    return {
        "le mal été": _mal_ete,
        "le mal ete": _mal_ete,
        "le cœur vert": _coeur_vert,
        "9782825104699": _coeur_vert,
        "désastres": _desastres,
        "desastres": _desastres,
        "ghelber": _ghelber,
    }


class Harness:
    """Real metadata service, real preprocessor, real repositories."""

    def __init__(self, tmpdir: str, ocr, lookup) -> None:
        self.tmpdir = tmpdir
        self.ocr = ocr
        self.lookup = lookup
        self.jobs_db = os.path.join(tmpdir, "jobs.db")
        self.metadata_db = os.path.join(tmpdir, "metadata.db")

        self.metadata_repo = BookMetadataRepository(self.metadata_db)
        self.job_repo = JobRepository(self.jobs_db)
        self.duplicate_repo = DuplicateRepository(self.jobs_db)
        self.export_repo = ExportRepository(self.jobs_db, self.metadata_db)

        self.job_service = JobService(self.job_repo)
        self.service = MetadataService(
            image_preprocessor=ImagePreprocessor(temp_dir=tmpdir),
            ocr_service=ocr,
            text_normalizer=TextNormalizer(),
            book_lookup=lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(),
            text_extractor=BookTextExtractor(languages=["en", "fr"]),
            data_directory=tmpdir,
            metadata_repo=self.metadata_repo,
            export_repo=self.export_repo,
            vision_model=_NoVision(),
        )
        self.service.set_duplicate_repo(self.duplicate_repo)
        self.worker = ProcessingWorker(
            self.job_service, self.service, metadata_repo=self.metadata_repo
        )

    def run(self, drive_file_id: str, path: str, name: str = "cover.jpg"):
        drive_file = DriveFile(
            file_id=drive_file_id,
            name=name,
            mime_type="image/jpeg",
            modified_time="2024-01-01T00:00:00.000Z",
            size=os.path.getsize(path),
        )
        job = self.job_service.create_job_for_file(drive_file, path)
        assert job is not None
        self.worker._process_job(job)  # noqa: SLF001
        return self.job_repo.get_job_by_drive_file_id(drive_file_id)

    def row(self, drive_file_id: str):
        return self.metadata_repo.get_by_drive_file_id(drive_file_id)

    def duplicate_kind(self, drive_file_id: str):
        row = self.duplicate_repo.get_duplicate(drive_file_id)
        return row["match_kind"] if row else None

    def close(self) -> None:
        self.duplicate_repo.close()
        self.export_repo.close()
        self.metadata_repo.close()
        self.job_repo.close()


@pytest.fixture()
def make_harness(tmp_path):
    import gc

    created = []

    def factory(ocr=None, books=None) -> Harness:
        harness = Harness(
            str(tmp_path), ocr or StubOcr(), CatalogStub(books or _all_books())
        )
        created.append(harness)
        return harness

    yield factory
    for harness in reversed(created):
        harness.close()
    gc.collect()


def _integrity(db_path: str) -> str:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Fixture provenance
# ---------------------------------------------------------------------------


class TestFixtureProvenance:
    def test_every_fixture_is_a_byte_copy_of_an_unmodified_source(self) -> None:
        for entry in MANIFEST["fixtures"]:
            fixture = str(FIXTURE_DIR / entry["file"])
            source = str(Path(__file__).resolve().parents[2] / entry["source"])
            assert os.path.exists(source), f"original missing: {entry['source']}"
            assert _sha256(fixture) == entry["sha256"]
            assert _sha256(source) == entry["sha256"], entry["file"]
            with PIL.Image.open(fixture) as img:
                assert img.size == (entry["width"], entry["height"])
                assert img.getexif().get(0x0112) is None

    def test_no_fixture_is_derived_and_no_rotation_case_is_faked(self) -> None:
        assert all(entry["derived"] is False for entry in MANIFEST["fixtures"])
        assert sorted(MANIFEST["missing"]) == ["rot270.jpg", "rot90.jpg"]
        for name in MANIFEST["missing"]:
            assert not (FIXTURE_DIR / name).exists(), f"{name} must not be fabricated"

    def test_the_duplicate_pair_keeps_two_different_drive_ids(self) -> None:
        left, right = BY_ROLE["duplicate_a"], BY_ROLE["duplicate_b"]
        assert left["drive_file_id"] != right["drive_file_id"]
        assert left["sha256"] == right["sha256"]


# ---------------------------------------------------------------------------
# Upright photograph
# ---------------------------------------------------------------------------


class TestUprightPhoto:
    def test_normalization_keeps_the_photo_upright_and_unmodified(
        self, tmp_path
    ) -> None:
        source = _fixture("upright")
        before = _sha256(source)

        normalized = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(source)

        with PIL.Image.open(normalized) as img:
            assert img.mode == "L"
            assert img.size == (900, 1600)
        assert _content_rotation(normalized, source) == 0
        assert _sha256(source) == before, "normalization must not touch the source"

    def test_upright_photo_needs_no_rotation_passes(self, make_harness) -> None:
        harness = make_harness(StubOcr(TEXT_MAL_ETE))
        job = harness.run(_drive_id("upright"), _fixture("upright"), "upright.jpg")

        assert job.status.value == "completed"
        assert harness.ocr.calls == 1  # geometry gate + fallback both skipped
        row = harness.row(_drive_id("upright"))
        assert row is not None
        assert "vuilleumier" in (row.authors[0] if row.authors else "").lower()


# ---------------------------------------------------------------------------
# 180 degrees, no EXIF orientation tag
# ---------------------------------------------------------------------------


class TestRot180Photo:
    def test_normalization_cannot_fix_an_upside_down_photo(self, tmp_path) -> None:
        """Documented limit: without an EXIF tag the pixels are left inverted."""
        source = _fixture("rot180")
        before = _sha256(source)

        normalized = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(source)

        assert _content_rotation(normalized, source) == 0
        assert _sha256(source) == before

    def test_rotation_fallback_recovers_180_degrees(self, tmp_path) -> None:
        source = _fixture("rot180")
        preprocessor = ImagePreprocessor(temp_dir=str(tmp_path))
        normalized = preprocessor.preprocess(source)

        ocr = StubOcr(
            TEXT_YOUGOSLAVIE, confidence=0.90, good_angle=180, reference=source
        )
        service = MetadataService(image_preprocessor=preprocessor, ocr_service=ocr)

        initial = ocr.extract_text(normalized)
        assert len(initial.words) < 3, "the as-stored frame must be unreadable"

        corrected = service._try_orientation_correction(source, initial)  # noqa: SLF001

        assert corrected.orientation_corrected is True
        assert corrected.orientation_angle == 180
        assert len(corrected.words) > len(initial.words)
        assert ocr.calls <= 5, "at most the heuristic plus four rotations"

    def test_unrecoverable_upside_down_photo_is_isolated(self, make_harness) -> None:
        """OCR text is mocked, so recovery quality is unverified; safety is not."""
        harness = make_harness(StubOcr("?? ??", confidence=0.20, words=["??", "??"]))
        drive_file_id = _drive_id("rot180")
        job = harness.run(drive_file_id, _fixture("rot180"), "rot180.jpg")

        assert job.status.value in {"completed", "manual_review", "rejected"}
        assert job.retry_count == 0
        assert harness.ocr.calls >= 4, "the rotation search must be attempted"
        row = harness.row(drive_file_id)
        assert row is None or row.status != "identified"
        # The unreadable photo is queued once for human review, never exported
        # twice and never silently dropped.
        pending = [
            item
            for item in harness.export_repo.get_pending_exports(limit=100)
            if item["drive_file_id"] == drive_file_id
        ]
        assert len(pending) <= 1


# ---------------------------------------------------------------------------
# Blurry and glare photographs
# ---------------------------------------------------------------------------


class TestDegradedPhotos:
    def test_blurry_photo_ocr_failure_is_isolated(self, make_harness) -> None:
        drive_file_id = _drive_id("blurry")
        harness = make_harness(StubOcr(error="OCR backend unavailable"))

        job = harness.run(drive_file_id, _fixture("blurry"), "blurry.jpg")

        assert job.status.value == "retrying"
        assert harness.row(drive_file_id) is None
        assert harness.export_repo.get_export_by_drive_file_id(drive_file_id) is None
        assert _integrity(harness.metadata_db) == "ok"

    def test_failed_photo_does_not_poison_a_later_submission(
        self, make_harness
    ) -> None:
        """A failed OCR pass must not register a fingerprint that blocks the photo."""
        ocr = StubOcr(error="OCR backend unavailable")
        harness = make_harness(ocr)
        failed = harness.run(_drive_id("blurry"), _fixture("blurry"), "blurry.jpg")
        assert failed.status.value == "retrying"

        ocr.error = None
        ocr.text_by_keyword = {"blurry": (TEXT_GLARE_DEGRADED, 0.35)}
        retried = harness.run(
            _drive_id("blurry") + "_retry", _fixture("blurry"), "blurry.jpg"
        )

        assert retried.status.value != "retrying"
        assert retried.retry_count == 0
        assert harness.duplicate_kind(_drive_id("blurry") + "_retry") is None

    def test_glare_photo_failure_never_becomes_a_silent_drop(
        self, make_harness
    ) -> None:
        """Partial glare text is mocked; what is asserted is the safe failure."""
        drive_file_id = _drive_id("glare")
        harness = make_harness(StubOcr(TEXT_GLARE_DEGRADED, confidence=0.35))

        job = harness.run(drive_file_id, _fixture("glare"), "glare.jpg")

        assert job.status.value in {"completed", "manual_review", "rejected"}
        assert job.retry_count == 0
        row = harness.row(drive_file_id)
        assert row is None or row.status != "identified"
        pending = [
            item
            for item in harness.export_repo.get_pending_exports(limit=100)
            if item["drive_file_id"] == drive_file_id
        ]
        assert len(pending) <= 1, "one photo must never queue two export rows"
        assert _integrity(harness.jobs_db) == "ok"


# ---------------------------------------------------------------------------
# Tilted / angled photograph
# ---------------------------------------------------------------------------


class TestAnglePhoto:
    def test_angled_photo_is_still_identified(self, make_harness) -> None:
        drive_file_id = _drive_id("angle")
        harness = make_harness(StubOcr(TEXT_COEUR_VERT))

        job = harness.run(drive_file_id, _fixture("angle"), "angle.jpg")

        assert job.status.value == "completed"
        row = harness.row(drive_file_id)
        assert row is not None
        assert content_identity.normalize_text(row.title) == "le coeur vert"
        assert row.authors == ["Jean-Louis Kuffer"]
        assert row.status == "identified"
        assert row.confidence >= 0.85


# ---------------------------------------------------------------------------
# Exact duplicate across two different Drive ids
# ---------------------------------------------------------------------------


class TestExactDuplicateAcrossDriveIds:
    def test_second_drive_file_is_skipped_without_ocr_or_export(
        self, make_harness
    ) -> None:
        harness = make_harness(StubOcr(TEXT_COEUR_VERT))
        first_id, second_id = _drive_id("duplicate_a"), _drive_id("duplicate_b")

        first = harness.run(first_id, _fixture("duplicate_a"), "duplicate-a.jpg")
        calls_after_first = harness.ocr.calls
        second = harness.run(second_id, _fixture("duplicate_b"), "duplicate-b.jpg")

        assert first.status.value == "completed"
        assert second.status.value == "cancelled"
        assert f"exact_duplicate_of:{first_id}" in (second.error_message or "")
        assert calls_after_first == 1
        assert harness.ocr.calls == calls_after_first, "duplicate must not OCR"
        assert len(harness.metadata_repo.list_identity_rows()) == 1
        assert harness.duplicate_kind(second_id) == content_identity.DUPLICATE_EXACT
        assert harness.export_repo.get_export_by_drive_file_id(second_id) is None
        assert len(harness.export_repo.get_pending_exports(limit=100)) == 1


# ---------------------------------------------------------------------------
# Same book, two different photographs
# ---------------------------------------------------------------------------


class TestSameBookPair:
    def test_the_two_photographs_are_not_image_duplicates(self) -> None:
        upright = content_identity.ImageFingerprint.from_path(_fixture("upright"))
        second = content_identity.ImageFingerprint.from_path(_fixture("same_book_2"))

        assert upright.content_hash != second.content_hash
        verdict = content_identity.classify_fingerprint(
            second,
            upright.perceptual_hash,
            content_identity.encode_color(upright.color_signature),
            upright.aspect_ratio,
        )
        assert verdict == "not_duplicate", "the same-book route must be reachable"

    def test_second_photograph_is_flagged_not_merged_or_overwritten(
        self, make_harness
    ) -> None:
        """OCR text is mocked (no ISBN here), so the match itself is unverified."""
        harness = make_harness(StubOcr(TEXT_MAL_ETE))
        first_id, second_id = _drive_id("upright"), _drive_id("same_book_2")

        harness.run(first_id, _fixture("upright"), "upright.jpg")
        before = harness.row(first_id)
        assert before is not None

        second = harness.run(second_id, _fixture("same_book_2"), "same-book-2.jpg")

        assert second.status.value == "completed"
        assert (
            harness.duplicate_kind(second_id)
            == content_identity.DUPLICATE_POSSIBLE_SAME_BOOK
        )
        rows = harness.metadata_repo.list_identity_rows()
        assert len(rows) == 2, "both photographs must keep their own record"

        after = harness.row(first_id)
        assert after is not None
        assert after.title == before.title
        assert after.authors == before.authors
        assert after.isbn_13 == before.isbn_13
        assert after.status == before.status
        assert harness.row(second_id) is not None


# ---------------------------------------------------------------------------
# Whole-sequence safety
# ---------------------------------------------------------------------------


class TestSequenceIntegrity:
    def test_sequence_keeps_one_row_per_photograph_and_no_export_duplicates(
        self, make_harness
    ) -> None:
        ocr = StubOcr(
            text_by_keyword={
                "upright": (TEXT_MAL_ETE, 0.93),
                "same-book-2": (TEXT_MAL_ETE, 0.93),
                "duplicate": (TEXT_GHELBER, 0.93),
            }
        )
        harness = make_harness(ocr)
        order = ["upright", "duplicate_a", "duplicate_b", "same_book_2"]

        for role in order:
            harness.run(_drive_id(role), _fixture(role), f"{role}.jpg")

        row_ids = [row[0] for row in harness.metadata_repo.list_identity_rows()]
        assert sorted(row_ids) == sorted(
            {
                _drive_id("upright"),
                _drive_id("duplicate_a"),
                _drive_id("same_book_2"),
            }
        ), "one row per photograph, no overwrite"
        assert harness.metadata_repo.count_metadata() == 3

        kinds = sorted(
            (row["match_kind"], row["drive_file_id"])
            for row in harness.duplicate_repo.list_duplicates()
        )
        assert kinds == sorted(
            [
                (content_identity.DUPLICATE_EXACT, _drive_id("duplicate_b")),
                (
                    content_identity.DUPLICATE_POSSIBLE_SAME_BOOK,
                    _drive_id("same_book_2"),
                ),
            ]
        )

        pending = harness.export_repo.get_pending_exports(limit=100)
        pending_ids = [item["drive_file_id"] for item in pending]
        assert len(pending_ids) == len(set(pending_ids)), "no duplicate export rows"
        assert _drive_id("duplicate_b") not in pending_ids
        assert _integrity(harness.metadata_db) == "ok"
        assert _integrity(harness.jobs_db) == "ok"
