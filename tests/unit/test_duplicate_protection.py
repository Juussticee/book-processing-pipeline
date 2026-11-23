"""Duplicate protection at processing time.

Uses the real MetadataService, repositories and worker with a mocked OCR
and a mocked metadata lookup, so the dedupe decisions and their side
effects (OCR calls, book rows, export rows) are exercised end to end.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import PIL.Image
import PIL.ImageDraw
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
from services.job_service import JobService
from services.metadata_service import MetadataService
from services.text_normalizer import TextNormalizer
from workers.processing_worker import ProcessingWorker

OCR_TEXT = (
    "Le cœur vert\n"
    "par Jean-Louis Kuffer\n"
    "ISBN 9780441013593\n"
    "Éditeur: Gallimard"
)


def _cover(path: str, color, bars: int = 3) -> None:
    img = PIL.Image.new("RGB", (300, 450), color)
    draw = PIL.ImageDraw.Draw(img)
    for index in range(bars):
        draw.rectangle(
            [30, 60 + index * 70, 270, 100 + index * 70], fill=(245, 240, 230)
        )
    img.save(path, "JPEG", quality=92)


class _NoVision:
    """Vision model stub: never finds anything, so no vision path is taken."""

    def extract_vision_metadata(self, *args, **kwargs):
        return None


class FakeOcr:
    def __init__(self, text: str = OCR_TEXT) -> None:
        self.text = text
        self.calls = 0
        self.fail = False

    def extract_text(self, path: str) -> OCRResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("OCR backend unavailable")
        return OCRResult(
            text=self.text,
            confidence=0.93,
            words=self.text.split(),
            language="fr",
            image_path=path,
        )


class FakeLookup:
    """Returns one catalogue record per book key, counting real lookups."""

    def __init__(self, books) -> None:
        self._books = books  # key -> metadata factory
        self.isbn_calls = 0
        self.other_calls = 0

    def _book_for(self, needle: str):
        for key, factory in self._books.items():
            if key.lower() in (needle or "").lower():
                return factory()
        return None

    def search_by_isbn(self, isbn: str):
        self.isbn_calls += 1
        book = self._books.get(isbn)
        return [book()] if book else []

    def search_by_title_author(self, title: str, author=None):
        self.other_calls += 1
        book = self._book_for(title)
        return [book] if book else []

    def search_by_title(self, title: str):
        return self.search_by_title_author(title)

    def search_by_author(self, author: str):
        self.other_calls += 1
        return []


def _book(
    isbn: str | None = "9780441013593",
    title="Le cœur vert",
    author="Jean-Louis Kuffer",
):
    return lambda: BookMetadata(
        title=title,
        authors=[author],
        isbn_13=isbn,
        publisher="Gallimard",
        confidence=1.0,
        source="open_library",
    )


class Harness:
    def __init__(self, tmpdir: str, ocr: FakeOcr, lookup: FakeLookup) -> None:
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
            image_preprocessor=_Preprocessor(tmpdir),
            ocr_service=ocr,
            text_normalizer=TextNormalizer(),
            book_lookup=lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(),
            text_extractor=BookTextExtractor(languages=["en", "fr"]),
            data_directory=tmpdir,
            metadata_repo=self.metadata_repo,
            export_repo=self.export_repo,
            # Avoid loading a local vision model in unit tests.
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

    def rows(self):
        return self.metadata_repo.list_identity_rows()

    def duplicate_kind(self, drive_file_id: str):
        row = self.duplicate_repo.get_duplicate(drive_file_id)
        return row["match_kind"] if row else None

    def close(self) -> None:
        self.duplicate_repo.close()
        self.export_repo.close()
        self.metadata_repo.close()
        self.job_repo.close()


class _Preprocessor:
    """Real ImagePreprocessor, isolated temp dir per test."""

    def __init__(self, tmpdir: str) -> None:
        from services.image_preprocessor import ImagePreprocessor

        self._inner = ImagePreprocessor(temp_dir=tmpdir)

    def preprocess(self, path: str) -> str:
        return self._inner.preprocess(path)

    def cleanup(self, path: str) -> None:
        self._inner.cleanup(path)

    def detect_orientation(self, img):
        return self._inner.detect_orientation(img)

    def correct_orientation(self, img, orient):
        return self._inner.correct_orientation(img, orient)


@pytest.fixture()
def tmpdir():
    with tempfile.TemporaryDirectory() as path:
        yield path


def _harness(tmpdir: str, ocr=None, books=None) -> Harness:
    ocr = ocr or FakeOcr()
    lookup = FakeLookup(books or {"9780441013593": _book(), "le cœur vert": _book()})
    return Harness(tmpdir, ocr, lookup)


@pytest.fixture()
def make_harness(tmpdir):
    """Builds harnesses and guarantees SQLite handles are released."""
    import gc

    created = []

    def factory(ocr=None, books=None) -> Harness:
        harness = _harness(tmpdir, ocr, books)
        created.append(harness)
        return harness

    yield factory
    for harness in reversed(created):
        harness.close()
    gc.collect()


class TestExactDuplicates:
    def test_identical_bytes_with_different_drive_id_skip_ocr(
        self, make_harness, tmpdir
    ) -> None:
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (120, 30, 40))
        second = os.path.join(tmpdir, "b.jpg")
        with open(first, "rb") as src, open(second, "wb") as dst:
            dst.write(src.read())

        job_a = harness.run("A", first)
        ocr_after_first = harness.ocr.calls
        job_b = harness.run("B", second, name="copy_of_cover.jpg")

        assert job_a.status.value == "completed"
        assert job_b.status.value == "cancelled"
        assert "exact_duplicate_of:A" in (job_b.error_message or "")
        assert harness.ocr.calls == ocr_after_first == 1, "duplicate must not OCR"
        assert len(harness.rows()) == 1
        assert harness.export_repo.count_export_records().get("completed", 0) == 0
        assert harness.duplicate_kind("B") == content_identity.DUPLICATE_EXACT

    def test_duplicate_is_not_a_failure_and_does_not_retry(
        self, make_harness, tmpdir
    ) -> None:
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (90, 90, 90))
        second = os.path.join(tmpdir, "b.jpg")
        with open(first, "rb") as src, open(second, "wb") as dst:
            dst.write(src.read())

        harness.run("A", first)
        job_b = harness.run("B", second)

        assert job_b.status.value == "cancelled"
        assert job_b.retry_count == 0

    def test_different_filename_does_not_defeat_dedupe(
        self, make_harness, tmpdir
    ) -> None:
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (30, 120, 60))
        second = os.path.join(tmpdir, "totally-different-name.jpg")
        with open(first, "rb") as src, open(second, "wb") as dst:
            dst.write(src.read())

        harness.run("A", first)
        harness.run("B", second, name="totally-different-name.jpg")

        assert len(harness.rows()) == 1

    def test_same_drive_id_retry_is_not_a_self_duplicate(
        self, make_harness, tmpdir
    ) -> None:
        """A retried job for the same file must still be processed."""
        harness = make_harness()
        path = os.path.join(tmpdir, "a.jpg")
        _cover(path, (10, 10, 200))

        harness.run("A", path)
        (
            harness.export_repo.delete_export("A")
            if hasattr(harness.export_repo, "delete_export")
            else None
        )
        job_again = harness.run("A", path)

        assert job_again.status.value == "completed"
        assert harness.ocr.calls == 2
        assert len(harness.rows()) == 1


class TestFingerprintRegistration:
    def test_failed_ocr_does_not_mask_a_later_copy(self, make_harness, tmpdir) -> None:
        """A photo whose OCR failed must not block the same photo later."""
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (11, 99, 44))
        second = os.path.join(tmpdir, "a-copy.jpg")
        with open(first, "rb") as src, open(second, "wb") as dst:
            dst.write(src.read())

        harness.ocr.fail = True
        job_a = harness.run("A", first)
        assert job_a.status.value == "retrying"
        assert len(harness.rows()) == 0

        harness.ocr.fail = False
        job_b = harness.run("B", second, name="a-copy.jpg")

        assert job_b.status.value == "completed"
        assert len(harness.rows()) == 1

    def test_only_persisted_images_are_registered(self, make_harness, tmpdir) -> None:
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (7, 7, 200))
        second = os.path.join(tmpdir, "b.jpg")
        with open(first, "rb") as src, open(second, "wb") as dst:
            dst.write(src.read())

        harness.run("A", first)
        harness.run("B", second)

        assert len(harness.duplicate_repo.list_fingerprints()) == 1


class TestNearDuplicates:
    def test_resized_copy_is_skipped_without_ocr(self, make_harness, tmpdir) -> None:
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (150, 60, 30))
        second = os.path.join(tmpdir, "resized.jpg")
        with PIL.Image.open(first) as img:
            img.resize((150, 225)).save(second, "JPEG", quality=75)

        harness.run("A", first)
        ocr_after_first = harness.ocr.calls
        job_b = harness.run("B", second)

        assert job_b.status.value == "cancelled"
        assert harness.ocr.calls == ocr_after_first == 1
        assert harness.duplicate_kind("B") == content_identity.DUPLICATE_NEAR
        assert len(harness.rows()) == 1

    def test_possible_duplicate_is_processed_and_flagged(
        self, make_harness, tmpdir
    ) -> None:
        ocr = FakeOcr()
        harness = make_harness(
            ocr=ocr,
            books={
                "9780441013593": _book(),
                "le cœur vert": _book(),
                "9780441172719": _book(
                    isbn="9780441172719", title="Dune", author="Frank Herbert"
                ),
                "dune": _book(
                    isbn="9780441172719", title="Dune", author="Frank Herbert"
                ),
            },
        )
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (150, 60, 30))
        second = os.path.join(tmpdir, "other.jpg")
        _cover(second, (150, 60, 30), bars=4)

        harness.run("A", first)
        ocr.text = "Dune\npar Frank Herbert\nISBN 9780441172719"
        with patch(
            "services.metadata_service.classify_fingerprint",
            return_value=content_identity.DUPLICATE_POSSIBLE_NEAR,
        ):
            job_b = harness.run("B", second)

        assert job_b.status.value in {"completed", "manual_review"}
        assert harness.duplicate_kind("B") == (
            content_identity.DUPLICATE_POSSIBLE_NEAR
        ), "uncertain matches must be recorded, not silently dropped"
        assert harness.ocr.calls >= 2


class TestSameBook:
    def test_same_book_via_isbn_creates_one_record(self, make_harness, tmpdir) -> None:
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))
        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (30, 200, 40), bars=4)

        harness.run("A", first)
        job_b = harness.run("B", second)

        assert job_b.status.value == "cancelled"
        assert "same_book_as:A" in (job_b.error_message or "")
        assert len(harness.rows()) == 1
        assert harness.duplicate_kind("B") == content_identity.DUPLICATE_SAME_BOOK

    def test_different_isbn_keeps_editions_separate(self, make_harness, tmpdir) -> None:
        ocr = FakeOcr()
        harness = make_harness(
            ocr=ocr,
            books={
                "9780441013593": _book(),
                "9782070360024": _book(isbn="9782070360024"),
                "le cœur vert": _book(),
            },
        )
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))
        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (30, 30, 200), bars=4)

        harness.run("A", first)
        ocr.text = OCR_TEXT.replace("9780441013593", "9782070360024")
        harness.run("B", second)

        assert len(harness.rows()) == 2
        assert harness.duplicate_kind("B") is None

    def test_same_title_different_author_keeps_records_separate(
        self, make_harness, tmpdir
    ) -> None:
        ocr = FakeOcr()
        harness = make_harness(
            ocr=ocr,
            books={
                "9780441013593": _book(),
                "le cœur vert": _book(),
            },
        )
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))

        harness.run("A", first)

        # Second photo is the same title but the catalogue record is another
        # author, and OCR carries no ISBN.
        ocr.text = "Le cœur vert\npar Autre Auteur"
        harness.lookup._books["le cœur vert"] = _book(  # noqa: SLF001
            isbn="9780000000002", author="Autre Auteur"
        )
        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (30, 200, 200), bars=5)
        harness.run("B", second)

        assert len(harness.rows()) == 2

    def test_same_title_and_author_with_different_isbn_is_flagged_only(
        self, make_harness, tmpdir
    ) -> None:
        """Two valid but different ISBNs are different editions: both kept."""
        ocr = FakeOcr()
        harness = make_harness(
            ocr=ocr,
            books={
                "9780441013593": _book(),
                "9782070360024": _book(isbn="9782070360024"),
                "le cœur vert": _book(),
            },
        )
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))
        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (30, 30, 200), bars=4)

        harness.run("A", first)
        ocr.text = OCR_TEXT.replace("9780441013593", "9782070360024")
        harness.run("B", second)

        assert len(harness.rows()) == 2
        assert harness.duplicate_kind("B") is None

    def test_same_title_and_author_without_isbn_is_flagged_never_merged(
        self, make_harness, tmpdir
    ) -> None:
        """Title + full author without an ISBN is a review flag, not a skip."""
        ocr = FakeOcr()
        harness = make_harness(
            ocr=ocr,
            books={
                "9780441013593": _book(),
                "le cœur vert": _book(isbn=None),
            },
        )
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))
        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (30, 200, 90), bars=4)

        harness.run("A", first)
        ocr.text = "Le cœur vert\npar Jean-Louis Kuffer\nÉditeur: Gallimard"
        job_b = harness.run("B", second)

        assert job_b.status.value == "completed"
        assert len(harness.rows()) == 2
        assert (
            harness.duplicate_kind("B") == content_identity.DUPLICATE_POSSIBLE_SAME_BOOK
        )

    def test_flagged_match_preserves_the_original_record(self, make_harness, tmpdir):
        ocr = FakeOcr()
        harness = make_harness(
            ocr=ocr,
            books={"9780441013593": _book(), "le cœur vert": _book(isbn=None)},
        )
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))
        harness.run("A", first)
        before = harness.metadata_repo.get_by_drive_file_id("A")

        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (30, 200, 90), bars=4)
        ocr.text = "Le cœur vert\npar Jean-Louis Kuffer"
        harness.run("B", second)

        after = harness.metadata_repo.get_by_drive_file_id("A")
        assert after is not None and before is not None
        assert after.drive_file_id == before.drive_file_id
        assert after.title == before.title
        assert after.isbn_13 == before.isbn_13
        assert after.status == before.status

    def test_minor_title_typo_is_flagged_and_both_records_kept(
        self, make_harness, tmpdir
    ) -> None:
        ocr = FakeOcr()
        harness = make_harness(
            ocr=ocr,
            books={"9780441013593": _book(), "le coeur ver": _book(isbn=None)},
        )
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))
        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (40, 200, 90), bars=4)

        harness.run("A", first)
        ocr.text = "Le coeur ver\npar Jean-Louis Kuffer"
        job_b = harness.run("B", second)

        assert job_b.status.value == "completed"
        assert len(harness.rows()) == 2
        assert (
            harness.duplicate_kind("B") == content_identity.DUPLICATE_POSSIBLE_SAME_BOOK
        )

    def test_matching_isbn_still_skips_the_duplicate_records(
        self, make_harness, tmpdir
    ):
        harness = make_harness()
        first = os.path.join(tmpdir, "a.jpg")
        _cover(first, (200, 30, 40))
        second = os.path.join(tmpdir, "b.jpg")
        _cover(second, (30, 200, 40), bars=4)

        harness.run("A", first)
        job_b = harness.run("B", second)

        assert job_b.status.value == "cancelled"
        assert len(harness.rows()) == 1
        assert harness.duplicate_kind("B") == content_identity.DUPLICATE_SAME_BOOK
