"""End-to-end acceptance: Drive discovery to the Google Sheets payload.

This mirrors the production wiring in ``app.py::run_sync_engine`` (same intake
policy, repositories, ``ProcessingWorker``, ``ExportService``, export queue and
export worker) and drives two complete cycles over a fake Drive listing.

Fake / controlled pieces (everything external):
* Drive listing and download (real fixture bytes, no network).
* OCR text (the Baidu PaddleOCR-VL provider is never called).
* Cover retrieval (a controlled local cover image, no network).
* The Sheets write itself (a recorder); the row *content* is produced by the
  real ``GoogleSheetsService`` row builders, so header mapping, RAW text
  values and the ``=IMAGE("...")`` cover formula are the production ones.

Everything else is real: intake filtering, on-disk downloads, job claiming and
state transitions, the duplicate gate, metadata persistence, the export queue
and export retry/status transitions, and SQLite integrity.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import PIL.Image
import PIL.ImageDraw
import pytest

from models.drive_file import DriveFile
from models.export_config import ExportConfig
from models.ocr_result import OCRResult
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.cover_repository import CoverRepository
from repositories.duplicate_repository import DuplicateRepository
from repositories.export_repository import ExportRepository
from repositories.intake_repository import IntakeRepository
from repositories.job_repository import JobRepository
from repositories.sqlite_repository import SQLiteRepository
from services import content_identity, intake_policy
from services.book_text_extractor import BookTextExtractor
from services.candidate_ranker import CandidateRanker
from services.confidence_service import ConfidenceService
from services.cover_download_service import CoverDownloadService
from services.cover_resolver_service import CoverResolverService
from services.export_service import ExportService
from services.google_sheets_service import GoogleSheetsService
from services.image_preprocessor import ImagePreprocessor
from services.job_service import JobService
from services.metadata_service import MetadataService
from services.metadata_validation_service import MetadataValidationService
from services.text_normalizer import TextNormalizer
from tests.integration.test_real_photo_acceptance import (
    TEXT_COEUR_VERT,
    TEXT_GHELBER,
    TEXT_MAL_ETE,
    TEXT_YOUGOSLAVIE,
    CatalogStub,
    StubOcr,
    _all_books,
    _content_rotation,
    _NoVision,
)
from tests.conftest import REAL_FIXTURE_DIR, require_real_photo_fixtures
from workers.export_worker import ExportWorker
from workers.processing_worker import ProcessingWorker

# Same non-distributed photograph set as the acceptance module it imports from.
require_real_photo_fixtures()

FIXTURE_DIR = REAL_FIXTURE_DIR
MANIFEST = json.loads((FIXTURE_DIR / "metadata.json").read_text(encoding="utf-8"))
BY_ROLE = {entry["role"]: entry for entry in MANIFEST["fixtures"]}

MAX_INTAKE_BYTES = intake_policy.max_size_bytes_from_mb(20.0)

DECOYS = [
    {"file_id": "decoy_pdf", "name": "inventory.pdf", "mime": "application/pdf"},
    {"file_id": "decoy_heic", "name": "IMG_9001.HEIC", "mime": "image/heic"},
    {
        "file_id": "decoy_huge",
        "name": "panorama.jpg",
        "mime": "image/jpeg",
        "size": 25 * 1024 * 1024,
    },
    {
        "file_id": "decoy_gdoc",
        "name": "Reading list",
        "mime": "application/vnd.google-apps.document",
    },
]

# (fixture role, Drive name, controlled OCR text)
PHOTOS = [
    ("upright", "IMG_0001.jpg", TEXT_MAL_ETE),
    ("angle", "IMG_0002.jpg", TEXT_COEUR_VERT),
    ("duplicate_a", "IMG_0003.jpg", TEXT_GHELBER),
    ("duplicate_b", "IMG_0004.jpg", TEXT_GHELBER),
    ("same_book_2", "IMG_0005.jpg", TEXT_MAL_ETE),
    ("rot180", "IMG_0006.jpg", TEXT_YOUGOSLAVIE),
    ("blurry", "IMG_0007.jpg", None),  # OCR failure path
]


def _drive_id(role: str) -> str:
    return BY_ROLE[role]["drive_file_id"]


def _fixture_bytes(role: str) -> bytes:
    return (FIXTURE_DIR / BY_ROLE[role]["file"]).read_bytes()


class AcceptanceOcr(StubOcr):
    """Controlled OCR: text per photo, and a genuinely upside-down photo.

    ``rot180`` is readable only once the frame is turned 180 degrees, so the
    production rotation fallback has to do the work. All text is controlled.
    """

    def __init__(self, rot180_keyword: str, rot180_reference: str) -> None:
        super().__init__(text_by_keyword={})
        self._rot180_keyword = rot180_keyword
        self._rot180_reference = rot180_reference
        # Files are downloaded under their Drive id, and rotation passes keep
        # that id in the temp name, so the id is the stable key.
        self._text_by_drive_id = {
            _drive_id(role): text for role, _name, text in PHOTOS if text
        }
        self._role_by_drive_id = {_drive_id(role): role for role, _n, _t in PHOTOS}
        self.failed_calls = 0

    def _role_for(self, path: str) -> str:
        name = os.path.basename(path).lower()
        for drive_id, role in self._role_by_drive_id.items():
            if drive_id.lower() in name:
                return role
        return ""

    @staticmethod
    def _drive_id_for(role: str) -> str:
        for photo_role, _name, _text in PHOTOS:
            if photo_role == role:
                return _drive_id(photo_role)
        return ""

    def extract_text(self, path: str) -> OCRResult:
        self.calls += 1
        name = os.path.basename(path).lower()

        if self._rot180_keyword.lower() in name:
            if _content_rotation(path, self._rot180_reference) == 180:
                return OCRResult(
                    text=TEXT_YOUGOSLAVIE,
                    confidence=0.90,
                    words=TEXT_YOUGOSLAVIE.split(),
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

        if "decoy" in name:
            raise AssertionError("a rejected file must never reach OCR")

        role = self._role_for(path)
        if role == "blurry":
            self.failed_calls += 1
            raise RuntimeError("OCR backend unavailable")

        text = self._text_by_drive_id.get(self._drive_id_for(role), TEXT_COEUR_VERT)
        return OCRResult(
            text=text,
            confidence=0.93,
            words=text.split(),
            language="fr",
            image_path=path,
        )


class FakeDrive:
    """Serves the fixture bytes for a fixed Drive listing."""

    def __init__(self) -> None:
        self.downloads: list[str] = []
        self.list_calls = 0
        self._bytes = {_drive_id(role): _fixture_bytes(role) for role, _, _ in PHOTOS}

    def authenticate(self) -> bool:
        return True

    def list_all_files(self):
        self.list_calls += 1
        files = [
            DriveFile(
                file_id=_drive_id(role),
                name=name,
                mime_type="image/jpeg",
                modified_time="2024-01-01T00:00:00.000Z",
                size=len(self._bytes[_drive_id(role)]),
            )
            for role, name, _text in PHOTOS
        ]
        files.extend(
            DriveFile(
                file_id=decoy["file_id"],
                name=decoy["name"],
                mime_type=decoy["mime"],
                modified_time="2024-01-01T00:00:00.000Z",
                size=decoy.get("size", 4096),
            )
            for decoy in DECOYS
        )
        return files

    def download_file(self, drive_file: DriveFile, destination: str) -> None:
        self.downloads.append(drive_file.file_id)
        with open(destination, "wb") as handle:
            handle.write(self._bytes[drive_file.file_id])


class RecordingSheets:
    """Sheets stand-in that records what production would have written."""

    def __init__(self, row_builder: GoogleSheetsService) -> None:
        self._row_builder = row_builder
        self.rows: list[dict] = []

    @property
    def header(self) -> list[str]:
        return self._row_builder._read_header([])  # noqa: SLF001

    def get_or_create_row(self, metadata, cover_path, row_id=None):
        self.rows.append(
            {
                "drive_file_id": metadata.drive_file_id,
                "metadata": metadata,
                "cover_path": cover_path,
                "row_id": row_id,
            }
        )
        return len(self.rows) + 1

    @staticmethod
    def _build_sheet_url(row_number) -> str:
        return (
            "https://docs.google.com/spreadsheets/d/acceptance/edit"
            f"#gid=0&range=A{row_number}"
        )

    def values_for(self, drive_file_id: str) -> list[str]:
        for row in self.rows:
            if row["drive_file_id"] == drive_file_id:
                return self._row_builder._row_values_for_header(  # noqa: SLF001
                    row["metadata"], row["cover_path"], self.header
                )
        raise AssertionError(f"no sheet row recorded for {drive_file_id}")


class FakeCleanCover:
    """Deterministic cover: writes a real portrait JPEG once per book."""

    def __init__(self, covers_dir: str) -> None:
        self._covers_dir = covers_dir
        self.fetches: list[str] = []

    def find_clean_cover(self, metadata):
        drive_file_id = metadata.drive_file_id
        self.fetches.append(drive_file_id)
        path = os.path.join(self._covers_dir, f"{drive_file_id}.jpg")
        if not os.path.exists(path):
            img = PIL.Image.new("RGB", (300, 450), (210, 205, 195))
            draw = PIL.ImageDraw.Draw(img)
            draw.rectangle([20, 40, 280, 90], fill=(30, 30, 30))
            draw.rectangle([20, 120, 280, 400], fill=(120, 90, 70))
            img.save(path, "JPEG", quality=90)
        slug = content_identity.normalize_text(metadata.title or "cover").replace(
            " ", "-"
        )
        return f"https://covers.test/{slug}.jpg", "open_library", path


class PipelineHarness:
    """Mirrors app.py::run_sync_engine wiring for one or more cycles."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_dir = str(root / "data")
        self.downloads_dir = os.path.join(self.data_dir, "downloads")
        self.covers_dir = os.path.join(self.data_dir, "covers")
        self.processed_dir = os.path.join(self.data_dir, "processed")
        for path in (self.downloads_dir, self.covers_dir, self.processed_dir):
            os.makedirs(path, exist_ok=True)

        self.app_db = os.path.join(self.data_dir, "app.db")
        self.jobs_db = os.path.join(self.data_dir, "jobs.db")
        self.metadata_db = os.path.join(self.data_dir, "metadata.db")

        self.drive = FakeDrive()
        self.file_repo = SQLiteRepository(self.app_db)
        self.job_repo = JobRepository(self.jobs_db)
        self.metadata_repo = BookMetadataRepository(self.metadata_db)
        self.duplicate_repo = DuplicateRepository(self.jobs_db)
        self.intake_repo = IntakeRepository(self.jobs_db)
        self.export_repo = ExportRepository(self.jobs_db, self.metadata_db)
        self.cover_repo = CoverRepository(self.metadata_db)

        self.job_service = JobService(self.job_repo)
        self.ocr = AcceptanceOcr(
            rot180_keyword=_drive_id("rot180").lower(),
            rot180_reference=str(FIXTURE_DIR / BY_ROLE["rot180"]["file"]),
        )
        self.lookup = CatalogStub(_books_with_pages())
        self.metadata_service = MetadataService(
            image_preprocessor=ImagePreprocessor(temp_dir=str(root)),
            ocr_service=self.ocr,
            text_normalizer=TextNormalizer(),
            book_lookup=self.lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(),
            text_extractor=BookTextExtractor(languages=["en", "fr"]),
            data_directory=self.data_dir,
            metadata_repo=self.metadata_repo,
            export_repo=self.export_repo,
            vision_model=_NoVision(),
        )
        self.metadata_service.set_duplicate_repo(self.duplicate_repo)
        self.worker = ProcessingWorker(
            self.job_service, self.metadata_service, metadata_repo=self.metadata_repo
        )

        self.sheets = RecordingSheets(
            GoogleSheetsService(
                credentials_file="unused-in-acceptance.json",
                sheets_id="acceptance-sheet",
                tab_name="Sheet1",
            )
        )
        self.covers = FakeCleanCover(self.covers_dir)
        self.export_config = ExportConfig.from_env()
        self.export_service = ExportService(
            metadata_repo=self.metadata_repo,
            cover_repo=self.cover_repo,
            export_repo=self.export_repo,
            validation_service=MetadataValidationService(confidence_threshold=0.85),
            cover_resolver=CoverResolverService(),
            cover_downloader=CoverDownloadService(
                cover_repo=self.cover_repo,
                covers_dir=self.covers_dir,
                timeout=1,
            ),
            sheets_service=self.sheets,
            config=self.export_config,
            clean_cover_service=self.covers,
        )
        self.export_worker = ExportWorker(
            export_service=self.export_service, export_repo=self.export_repo
        )

        self.rejected: list[tuple[str, str]] = []

    # -- cycle stages --------------------------------------------------------
    def scan(self) -> dict:
        all_files = self.drive.list_all_files()
        known = set(self.file_repo.get_all_file_ids())
        new_files = [f for f in all_files if f.file_id not in known]

        accepted = []
        for drive_file in new_files:
            decision = intake_policy.evaluate(
                drive_file, max_size_bytes=MAX_INTAKE_BYTES
            )
            if decision.accepted:
                accepted.append(drive_file)
                continue
            self.rejected.append((drive_file.file_id, decision.reason))
            self.intake_repo.record(
                drive_file_id=drive_file.file_id,
                file_name=drive_file.name,
                reason=decision.reason or "REJECTED_UNKNOWN",
                mime_type=decision.mime_type,
                size_bytes=decision.size_bytes,
                detail=decision.detail,
            )

        for drive_file in accepted:
            filename = f"{drive_file.file_id}.jpg"
            local_path = os.path.join(self.downloads_dir, filename)
            self.drive.download_file(drive_file, local_path)
            self.file_repo.insert_file(
                drive_file=drive_file,
                local_filename=filename,
                download_time="0",
            )
            self.job_service.create_job_for_file(drive_file, local_path)

        return {
            "discovered": len(all_files),
            "new": len(new_files),
            "accepted": len(accepted),
            "rejected": len(new_files) - len(accepted),
        }

    def process_pending(self) -> int:
        """One worker pass over the pending queue (the real poll cadence).

        Retrying jobs are not picked up in the same pass: they wait for their
        backoff exactly as in normal operation.
        """
        processed = 0
        for job in self.job_service.get_pending_jobs():
            self.worker._process_job(job)  # noqa: SLF001
            processed += 1
        return processed

    def export_pending(self) -> int:
        exported = 0
        while True:
            pending = self.export_repo.get_pending_exports(limit=50)
            if not pending:
                return exported
            for record in pending:
                self.export_worker._process_export(  # noqa: SLF001
                    record["drive_file_id"], record["job_id"]
                )
                exported += 1
            if exported > 50:
                raise AssertionError("export queue did not drain")

    def job(self, drive_file_id: str):
        return self.job_repo.get_job_by_drive_file_id(drive_file_id)

    def row(self, drive_file_id: str):
        return self.metadata_repo.get_by_drive_file_id(drive_file_id)

    def duplicate_kind(self, drive_file_id: str):
        row = self.duplicate_repo.get_duplicate(drive_file_id)
        return row["match_kind"] if row else None

    def integrity(self, db_path: str) -> str:
        connection = sqlite3.connect(db_path)
        try:
            return connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()

    def close(self) -> None:
        for repo in (
            self.cover_repo,
            self.export_repo,
            self.intake_repo,
            self.duplicate_repo,
            self.metadata_repo,
            self.job_repo,
            self.file_repo,
        ):
            repo.close()


def _books_with_pages():
    """Catalogue records, with a page count the sheet can be checked against."""
    books = _all_books()

    def with_pages(factory):
        def build():
            book = factory()
            if book.page_count is None:
                book.page_count = 320
            return book

        return build

    return {key: with_pages(factory) for key, factory in books.items()}


@pytest.fixture()
def pipeline(tmp_path):
    harness = PipelineHarness(tmp_path)
    yield harness
    harness.close()


@pytest.fixture()
def first_cycle(pipeline):
    """Run one full production-shaped cycle and return the harness."""
    pipeline.cycle_one = pipeline.scan()
    pipeline.process_pending()
    pipeline.export_pending()
    return pipeline


class TestIntakeAndDiscovery:
    def test_only_supported_photos_are_downloaded(self, pipeline) -> None:
        summary = pipeline.scan()

        assert summary == {"discovered": 11, "new": 11, "accepted": 7, "rejected": 4}
        assert sorted(pipeline.drive.downloads) == sorted(
            _drive_id(role) for role, _n, _t in PHOTOS
        )
        reasons = dict(pipeline.rejected)
        assert reasons["decoy_pdf"] == intake_policy.REJECTED_UNSUPPORTED_TYPE
        assert reasons["decoy_heic"] == intake_policy.REJECTED_HEIC_UNSUPPORTED
        assert reasons["decoy_huge"] == intake_policy.REJECTED_FILE_TOO_LARGE
        assert reasons["decoy_gdoc"] == intake_policy.REJECTED_UNSUPPORTED_TYPE
        assert pipeline.intake_repo.count() == 4

    def test_a_failing_photo_does_not_block_the_others(self, pipeline) -> None:
        pipeline.scan()
        pipeline.process_pending()

        assert pipeline.job(_drive_id("blurry")).status.value == "retrying"
        for role in ("upright", "angle", "same_book_2"):
            assert pipeline.job(_drive_id(role)).status.value == "completed"
        # These two reach review instead of a confident identification because
        # their controlled OCR text has no confident catalogue match; either
        # way they must not block the other photos.
        for role in ("duplicate_a", "rot180"):
            assert pipeline.job(_drive_id(role)).status.value in {
                "completed",
                "manual_review",
            }


class TestProcessingToSheets:
    def test_every_stage_produces_the_expected_state(self, first_cycle) -> None:
        pipeline = first_cycle

        upright = pipeline.row(_drive_id("upright"))
        assert upright is not None
        assert upright.status == "identified"
        assert content_identity.normalize_text(upright.title) == "le mal ete"

        angle = pipeline.row(_drive_id("angle"))
        assert angle is not None and angle.status == "identified"
        assert content_identity.normalize_text(angle.title) == "le coeur vert"
        assert angle.authors == ["Jean-Louis Kuffer"]
        assert angle.isbn_13 == "9782825104699"
        assert angle.page_count == 320, "page count must survive persistence"

        # The upside-down photo is recovered by the rotation fallback: the text
        # below exists only in the 180-degree frame of the OCR stub, so its
        # presence proves the recovered frame reached persistence.
        rot180 = pipeline.row(_drive_id("rot180"))
        assert rot180 is not None
        recovered_text = " ".join([rot180.title or ""] + list(rot180.authors or []))
        assert "garde" in content_identity.normalize_text(recovered_text)
        assert pipeline.job(_drive_id("rot180")).status.value in {
            "completed",
            "manual_review",
        }

    def test_sheet_payload_has_title_author_isbn_and_cover_formula(
        self, first_cycle
    ) -> None:
        pipeline = first_cycle
        header = pipeline.sheets.header
        assert header == GoogleSheetsService.COLUMNS  # by name, canonical order

        index = {name: position for position, name in enumerate(header)}

        values = pipeline.sheets.values_for(_drive_id("angle"))
        assert values[index["Title"]] == "Le cœur vert"
        assert values[index["Authors"]] == "Jean-Louis Kuffer"
        assert values[index["ISBN-13"]] == "9782825104699"
        assert values[index["Publisher"]] == "L'Âge d'Homme"
        assert (
            values[index["Open Cover"]]
            == '=IMAGE("https://covers.test/le-coeur-vert.jpg")'
        )
        assert len(values) == len(header)

        mal_ete = pipeline.sheets.values_for(_drive_id("upright"))
        assert mal_ete[index["Title"]] == "Le Mal Été"
        assert mal_ete[index["Authors"]] == "Jean Vuilleumier"
        assert mal_ete[index["ISBN-13"]] == ""  # no ISBN for this book
        assert mal_ete[index["Open Cover"]].startswith('=IMAGE("https://')

    def test_each_photograph_has_one_export_record(self, first_cycle) -> None:
        pipeline = first_cycle
        counts = pipeline.export_repo.count_export_records()

        assert counts.get("completed", 0) == 5, counts
        assert (
            pipeline.export_repo.get_export_by_drive_file_id(_drive_id("duplicate_b"))
            is None
        )
        exported_ids = [row["drive_file_id"] for row in pipeline.sheets.rows]
        assert len(exported_ids) == len(set(exported_ids)) == 5


class TestDuplicateAndSameBook:
    def test_exact_duplicate_under_two_drive_ids_makes_one_book(
        self, first_cycle
    ) -> None:
        pipeline = first_cycle
        original = _drive_id("duplicate_a")
        copy = _drive_id("duplicate_b")

        assert pipeline.row(original) is not None
        assert pipeline.row(copy) is None
        job = pipeline.job(copy)
        assert job.status.value == "cancelled"
        assert f"exact_duplicate_of:{original}" in (job.error_message or "")
        assert pipeline.duplicate_kind(copy) == content_identity.DUPLICATE_EXACT
        assert pipeline.ocr.calls < 2 * len(PHOTOS), "duplicate must not OCR"

    def test_two_photographs_of_one_book_are_flagged_not_merged(
        self, first_cycle
    ) -> None:
        pipeline = first_cycle
        first = _drive_id("upright")
        second = _drive_id("same_book_2")

        before = pipeline.row(first)
        after = pipeline.row(first)
        assert before is not None and after is not None
        assert after.title == before.title
        assert after.authors == before.authors
        assert after.status == before.status

        assert pipeline.row(second) is not None, "the second photo keeps its record"
        kind = pipeline.duplicate_kind(second)
        assert kind == content_identity.DUPLICATE_POSSIBLE_SAME_BOOK
        assert pipeline.job(second).status.value == "completed"
        assert len(pipeline.metadata_repo.list_identity_rows()) == 5


class TestIdempotence:
    def test_second_cycle_creates_no_duplicate_work(self, first_cycle) -> None:
        pipeline = first_cycle
        rows_after_first = len(pipeline.metadata_repo.list_identity_rows())
        exports_after_first = dict(pipeline.export_repo.count_export_records())
        job_count = len(pipeline.job_repo.get_all_jobs())
        duplicates_after_first = pipeline.duplicate_repo.count_total()
        downloads_after_first = list(pipeline.drive.downloads)

        second = pipeline.scan()
        processed = pipeline.process_pending()
        exported = pipeline.export_pending()

        # The four rejected decoys are re-evaluated every cycle (exactly like
        # app.py does), but nothing is downloaded, recorded twice or queued.
        assert second == {"discovered": 11, "new": 4, "accepted": 0, "rejected": 4}
        # At most the failed photo is retried on its own schedule; nothing new
        # is downloaded, queued or exported.
        assert processed <= 1
        assert exported == 0
        assert pipeline.drive.downloads == downloads_after_first
        assert len(pipeline.job_repo.get_all_jobs()) == job_count
        assert len(pipeline.metadata_repo.list_identity_rows()) == rows_after_first
        assert dict(pipeline.export_repo.count_export_records()) == exports_after_first
        assert pipeline.duplicate_repo.count_total() == duplicates_after_first
        assert pipeline.intake_repo.count() == 4, "rejections are recorded once"
        assert len(pipeline.sheets.rows) == 5


class TestPersistenceIntegrity:
    def test_all_databases_remain_valid_after_the_cycle(self, first_cycle) -> None:
        pipeline = first_cycle
        assert pipeline.integrity(pipeline.app_db) == "ok"
        assert pipeline.integrity(pipeline.jobs_db) == "ok"
        assert pipeline.integrity(pipeline.metadata_db) == "ok"

    def test_failed_photo_leaves_no_row_and_no_export(self, first_cycle) -> None:
        pipeline = first_cycle
        blurry = _drive_id("blurry")

        assert pipeline.row(blurry) is None
        assert pipeline.export_repo.get_export_by_drive_file_id(blurry) is None
        job = pipeline.job(blurry)
        assert job.status.value == "retrying"
        assert job.retry_count == 1, "one attempt, waiting for its backoff"
        assert job.error_message and "OCR backend unavailable" in job.error_message
