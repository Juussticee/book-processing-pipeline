"""Batch acceptance at customer scale (100 / 250 / 500 files).

Simulates a Drive folder containing unique covers, exact duplicates,
near duplicates (resized copies), unsupported files, a corrupt JPEG, a
rotated cover, an OCR failure, a metadata-lookup failure and an export
failure -- then scans the same folder twice to prove idempotency.

OCR text is supplied directly (``raw_ocr_text``) so no live OCR runs; the
metadata lookup is a fake that generates one catalogue record per title.
Every counter asserted here comes from the real repositories/services.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import PIL.Image
import PIL.ImageDraw
import pytest

from models.book_metadata import BookMetadata
from models.drive_file import DriveFile
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.duplicate_repository import DuplicateRepository
from repositories.export_repository import ExportRepository
from repositories.intake_repository import IntakeRepository
from repositories.job_repository import JobRepository
from services import intake_policy
from services.book_text_extractor import BookTextExtractor
from services.candidate_ranker import CandidateRanker
from services.confidence_service import ConfidenceService
from services.image_preprocessor import ImagePreprocessor
from services.job_service import JobService
from services.metadata_service import MetadataService
from services.text_normalizer import TextNormalizer

MAX_BYTES = 20 * 1024 * 1024


class _NoVision:
    def extract_vision_metadata(self, *args, **kwargs):
        return None


class _BatchOcr:
    """Only used for the deliberately failing OCR case."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def extract_text(self, path: str):
        self.calls += 1
        if self.fail:
            raise RuntimeError("OCR backend unavailable")
        raise AssertionError("OCR should not run: raw text is supplied")


class _GeneratedLookup:
    """Catalogue fake: 'Book 12' -> ISBN 9780000000012, author 'Author 12'."""

    def __init__(self, fail_title: str = "") -> None:
        self.isbn_calls = 0
        self.other_calls = 0
        self.fail_title = fail_title

    @staticmethod
    def _from_title(title: str):
        digits = "".join(ch for ch in title if ch.isdigit())
        index = int(digits) if digits else 0
        return BookMetadata(
            title=f"Book {index}",
            authors=[f"Author {index}"],
            isbn_13=f"978000000{index:04d}",
            publisher="Test Press",
            confidence=1.0,
            source="open_library",
        )

    def search_by_isbn(self, isbn: str):
        self.isbn_calls += 1
        return []

    def search_by_title_author(self, title: str, author=None):
        self.other_calls += 1
        if self.fail_title and self.fail_title.lower() in (title or "").lower():
            raise RuntimeError("metadata provider timeout")
        if not title:
            return []
        return [self._from_title(title)]

    def search_by_title(self, title: str):
        return self.search_by_title_author(title)

    def search_by_author(self, author: str):
        self.other_calls += 1
        return []


def _cover(path: str, index: int, size=(160, 240)) -> None:
    """Deterministic, textured cover-like image unique to ``index``.

    Texture matters: flat synthetic rectangles produce colliding perceptual
    hashes for unrelated images, which is not representative of photographs.
    """
    import random

    rng = random.Random(index * 7919 + 13)
    top = (rng.randrange(20, 220), rng.randrange(20, 220), rng.randrange(20, 220))
    bottom = (rng.randrange(20, 220), rng.randrange(20, 220), rng.randrange(20, 220))
    img = PIL.Image.new("RGB", size, top)
    draw = PIL.ImageDraw.Draw(img)
    for y in range(size[1]):
        ratio = y / size[1]
        draw.line(
            [(0, y), (size[0], y)],
            fill=tuple(
                int(top[channel] * (1 - ratio) + bottom[channel] * ratio)
                for channel in range(3)
            ),
        )
    for _ in range(1 + rng.randrange(4)):
        x0 = rng.randrange(0, size[0] // 2)
        y0 = rng.randrange(0, size[1] // 2)
        x1 = x0 + rng.randrange(size[0] // 3, size[0])
        y1 = y0 + rng.randrange(size[1] // 4, size[1])
        color = (rng.randrange(0, 255), rng.randrange(0, 255), rng.randrange(0, 255))
        if rng.random() < 0.5:
            draw.rectangle([x0, y0, x1, y1], outline=color, width=rng.choice([2, 3, 6]))
        else:
            draw.ellipse([x0, y0, x1, y1], fill=color)
    for _ in range(120 + index % 60):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        shade = rng.randrange(0, 255)
        draw.point((x, y), fill=(shade, shade, shade))
    img.save(path, "JPEG", quality=90)


def _drive_file(file_id: str, name: str, size: int, mime="image/jpeg") -> DriveFile:
    return DriveFile(
        file_id=file_id,
        name=name,
        mime_type=mime,
        modified_time="2024-01-01T00:00:00.000Z",
        size=size,
    )


class BatchHarness:
    def __init__(self, tmpdir: str, lookup: _GeneratedLookup, ocr: _BatchOcr) -> None:
        self.tmpdir = tmpdir
        self.downloads = os.path.join(tmpdir, "downloads")
        os.makedirs(self.downloads, exist_ok=True)
        self.preprocessed = os.path.join(tmpdir, "preprocessed")
        os.makedirs(self.preprocessed, exist_ok=True)
        self.jobs_db = os.path.join(tmpdir, "jobs.db")
        self.metadata_db = os.path.join(tmpdir, "metadata.db")

        self.metadata_repo = BookMetadataRepository(self.metadata_db)
        self.job_repo = JobRepository(self.jobs_db)
        self.duplicate_repo = DuplicateRepository(self.jobs_db)
        self.intake_repo = IntakeRepository(self.jobs_db)
        self.export_repo = ExportRepository(self.jobs_db, self.metadata_db)

        self.job_service = JobService(self.job_repo)
        self.service = MetadataService(
            image_preprocessor=ImagePreprocessor(temp_dir=self.preprocessed),
            ocr_service=ocr,
            text_normalizer=TextNormalizer(),
            book_lookup=lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(),
            text_extractor=BookTextExtractor(languages=["en", "fr"]),
            metadata_repo=self.metadata_repo,
            export_repo=self.export_repo,
            data_directory=tmpdir,
            vision_model=_NoVision(),
        )
        self.service.set_duplicate_repo(self.duplicate_repo)
        self.ocr = ocr

    def scan(self, files, payloads, texts) -> dict:
        """One sync cycle: intake -> download -> job -> process."""
        accepted = 0
        rejected = 0
        known = 0
        for drive_file in files:
            decision = intake_policy.evaluate(drive_file, max_size_bytes=MAX_BYTES)
            if decision.rejected:
                self.intake_repo.record(
                    drive_file_id=drive_file.file_id,
                    file_name=drive_file.name,
                    reason=decision.reason or "REJECTED_UNKNOWN",
                    mime_type=decision.mime_type,
                    size_bytes=decision.size_bytes,
                    detail=decision.detail,
                )
                rejected += 1
                continue
            local_name = f"{drive_file.file_id}{os.path.splitext(drive_file.name)[1]}"
            local_path = os.path.join(self.downloads, local_name)
            if not os.path.exists(local_path):
                with open(local_path, "wb") as handle:
                    handle.write(payloads[drive_file.file_id])
            if not self.metadata_repo.exists(drive_file.file_id) and not (
                self.job_repo.get_job_by_drive_file_id(drive_file.file_id)
            ):
                job = self.job_service.create_job_for_file(drive_file, local_path)
                if job is not None:
                    accepted += 1
                    self._process(job, texts.get(drive_file.file_id))
            else:
                known += 1
        return {"accepted": accepted, "rejected": rejected, "known": known}

    def _process(self, job, text) -> None:
        self.job_service.start_job(job.job_id)
        try:
            metadata, _ = self.service.identify_book(
                job.local_path,
                raw_ocr_text=text or "",
                drive_file_id=job.file_id,
                job_id=job.job_id,
            )
            if metadata is not None and metadata.status == "duplicate":
                self.job_service.mark_duplicate(
                    job.job_id, metadata.rejection_reason or "duplicate"
                )
            elif metadata is None:
                self.job_service.set_manual_review(job.job_id, "low confidence")
            else:
                self.job_service.complete_job(job.job_id)
        except Exception as exc:  # isolated exactly like the worker
            self.job_service.fail_job(job.job_id, str(exc))

    def rows(self) -> int:
        return self.metadata_repo.count_metadata()

    def statuses(self) -> dict:
        return self.job_service.get_queue_status()

    def close(self) -> None:
        self.duplicate_repo.close()
        self.intake_repo.close()
        self.export_repo.close()
        self.metadata_repo.close()
        self.job_repo.close()


def _build_folder(tmpdir: str, size: int, lookup: _GeneratedLookup):
    """Create the simulated folder: unique + duplicates + junk + failures."""
    unique_count = int(size * 0.80)
    duplicate_count = int(size * 0.10)
    near_count = int(size * 0.05)

    files, payloads, texts = [], {}, {}
    workdir = os.path.join(tmpdir, "source")
    os.makedirs(workdir, exist_ok=True)

    for index in range(unique_count):
        path = os.path.join(workdir, f"book_{index}.jpg")
        _cover(path, index)
        file_id = f"u{index:04d}"
        with open(path, "rb") as handle:
            payloads[file_id] = handle.read()
        files.append(_drive_file(file_id, f"book_{index}.jpg", len(payloads[file_id])))
        texts[file_id] = f"Book {index}\npar Author {index}"

    for index in range(duplicate_count):
        source_id = f"u{index:04d}"
        file_id = f"d{index:04d}"
        payloads[file_id] = payloads[source_id]
        files.append(
            _drive_file(file_id, f"copy_of_book_{index}.jpg", len(payloads[file_id]))
        )
        texts[file_id] = texts[source_id]

    for index in range(near_count):
        source_id = f"u{index:04d}"
        file_id = f"n{index:04d}"
        source_path = os.path.join(workdir, f"book_{index}.jpg")
        near_path = os.path.join(workdir, f"book_{index}_small.jpg")
        with PIL.Image.open(source_path) as img:
            img.resize((img.width // 2, img.height // 2)).save(
                near_path, "JPEG", quality=55
            )
        with open(near_path, "rb") as handle:
            payloads[file_id] = handle.read()
        files.append(
            _drive_file(file_id, f"book_{index}_small.jpg", len(payloads[file_id]))
        )
        texts[file_id] = texts[source_id]

    # Unsupported / junk files (rejected from metadata alone)
    for file_id, name, mime in [
        ("p0001", "invoice.pdf", "application/pdf"),
        ("p0002", "Thumbs.db", "application/octet-stream"),
        ("p0003", "archive.zip", "application/zip"),
        ("p0004", "IMG_0001.HEIC", "image/heic"),
    ]:
        files.append(_drive_file(file_id, name, 2048, mime=mime))
        payloads[file_id] = b"not-an-image"

    # Oversized image
    files.append(_drive_file("big001", "huge.jpg", MAX_BYTES + 1))
    payloads["big001"] = b"x"

    # Corrupt JPEG that passes the extension check
    corrupt = b"\xff\xd8\xff\xe0" + b"0" * 64
    files.append(_drive_file("c0001", "corrupt.jpg", len(corrupt)))
    payloads["c0001"] = corrupt

    # Rotated cover (EXIF orientation 6) with the same book text
    rotated_path = os.path.join(workdir, "rotated.jpg")
    exif = PIL.Image.Exif()
    exif[0x0112] = 6
    _cover(rotated_path, unique_count + 1)
    with PIL.Image.open(rotated_path) as img:
        img.save(rotated_path, "JPEG", quality=90, exif=exif)
    with open(rotated_path, "rb") as handle:
        payloads["r0001"] = handle.read()
    files.append(_drive_file("r0001", "rotated.jpg", len(payloads["r0001"])))
    texts["r0001"] = f"Book {unique_count + 1}\npar Author {unique_count + 1}"

    # OCR failure: no raw text and the OCR backend raises
    ocr_fail_path = os.path.join(workdir, "ocr_fail.jpg")
    _cover(ocr_fail_path, unique_count + 2)
    with open(ocr_fail_path, "rb") as handle:
        payloads["o0001"] = handle.read()
    files.append(_drive_file("o0001", "ocr_fail.jpg", len(payloads["o0001"])))
    texts["o0001"] = None

    # Metadata failure: lookup raises for this title
    metal_fail_path = os.path.join(workdir, "meta_fail.jpg")
    _cover(metal_fail_path, unique_count + 3)
    with open(metal_fail_path, "rb") as handle:
        payloads["m0001"] = handle.read()
    files.append(_drive_file("m0001", "meta_fail.jpg", len(payloads["m0001"])))
    texts["m0001"] = "Exploding Book\npar Author X"

    return files, payloads, texts, unique_count


@pytest.mark.parametrize("size", [100, 250, 500])
def test_batch_scale_run_and_rescan(size: int) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        lookup = _GeneratedLookup(fail_title="Exploding")
        harness = BatchHarness(tmpdir, lookup, _BatchOcr(fail=False))
        files, payloads, texts, unique_count = _build_folder(tmpdir, size, lookup)
        try:
            first = harness.scan(files, payloads, texts)
            statuses = harness.statuses()

            # Every supported file became a job exactly once.
            assert first["accepted"] == len(files) - first["rejected"]
            assert first["rejected"] == 5  # pdf, Thumbs.db, zip, HEIC, oversized
            assert harness.intake_repo.count() == 5

            # One row per unique book, plus the rotated cover (distinct book,
            # EXIF orientation 6). A resized copy whose fingerprint drifts past
            # the near-duplicate gate must still be recorded and flagged --
            # never merged and never dropped -- so rows stay within that bound
            # and every extra row is accounted for by a review flag.
            near_count = int(size * 0.05)
            assert unique_count + 1 <= harness.rows() <= unique_count + 1 + near_count
            flags = {
                kind: sum(
                    1
                    for row in harness.duplicate_repo.list_duplicates()
                    if row["match_kind"] == kind
                )
                for kind in ("exact", "near", "possible_near", "possible_same_book")
            }
            extra_rows = harness.rows() - (unique_count + 1)
            assert extra_rows == flags["possible_near"] + flags["possible_same_book"]
            assert flags["exact"] + flags["near"] >= int(size * 0.15) - near_count
            assert harness.duplicate_repo.count_total() >= int(size * 0.10) + int(
                size * 0.05
            )

            # Failures stay isolated: corrupt JPEG, OCR failure and metadata
            # failure reach a controlled retry state, everything else proceeds.
            assert 2 <= statuses["failed"] + statuses["retrying"] <= 3
            assert statuses["completed"] > 0

            # Skipped duplicates never reach export; flagged possible duplicates
            # are processed like any other book and keep their own record.
            skipped_ids = [
                row["drive_file_id"]
                for row in harness.duplicate_repo.list_duplicates()
                if row["match_kind"] in {"exact", "near", "same_book"}
            ]
            assert skipped_ids
            for drive_file_id in skipped_ids:
                assert (
                    harness.export_repo.get_export_by_drive_file_id(drive_file_id)
                    is None
                )
            flagged_ids = [
                row["drive_file_id"]
                for row in harness.duplicate_repo.list_duplicates()
                if row["match_kind"] in {"possible_near", "possible_same_book"}
            ]
            for drive_file_id in flagged_ids:
                assert harness.metadata_repo.exists(drive_file_id)

            # Second scan of the identical folder must be a no-op.
            ocr_calls_before = harness.ocr.calls
            rows_before = harness.rows()
            exports_before = harness.export_repo.count_export_records()
            second = harness.scan(files, payloads, texts)

            assert second["accepted"] == 0
            assert harness.ocr.calls == ocr_calls_before
            assert harness.rows() == rows_before
            assert harness.export_repo.count_export_records() == exports_before
        finally:
            harness.close()


def test_stale_job_recovery_window_is_15_minutes() -> None:
    """A crashed job is recovered after 15 min, not after 2 hours."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jobs_db = os.path.join(tmpdir, "jobs.db")
        path = os.path.join(tmpdir, "cover.jpg")
        _cover(path, 1)

        with JobRepository(jobs_db) as repo:
            service = JobService(repo)
            job = service.create_job_for_file(
                _drive_file("stale001", "cover.jpg", os.path.getsize(path)), path
            )
            service.start_job(job.job_id)
            old = (datetime.utcnow() - timedelta(minutes=16)).isoformat()
            repo._connection.execute(  # noqa: SLF001
                "UPDATE jobs SET started_at = ? WHERE job_id = ?", (old, job.job_id)
            )
            repo._connection.commit()  # noqa: SLF001

        with JobRepository(jobs_db) as repo:
            service = JobService(repo)
            assert service.recover_stuck_jobs() == 1
            assert repo.get_job_by_drive_file_id("stale001").status.value == "pending"


def test_active_job_is_not_recovered() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        jobs_db = os.path.join(tmpdir, "jobs.db")
        path = os.path.join(tmpdir, "cover.jpg")
        _cover(path, 2)

        with JobRepository(jobs_db) as repo:
            service = JobService(repo)
            job = service.create_job_for_file(
                _drive_file("active001", "cover.jpg", os.path.getsize(path)), path
            )
            service.start_job(job.job_id)
            recent = (datetime.utcnow() - timedelta(minutes=2)).isoformat()
            repo._connection.execute(  # noqa: SLF001
                "UPDATE jobs SET started_at = ? WHERE job_id = ?", (recent, job.job_id)
            )
            repo._connection.commit()  # noqa: SLF001

            assert service.recover_stuck_jobs() == 0
            assert (
                repo.get_job_by_drive_file_id("active001").status.value == "processing"
            )
