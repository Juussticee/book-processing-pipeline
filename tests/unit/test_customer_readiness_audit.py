"""Offline evidence for the customer-readiness audit.

These tests add no production behaviour; they exercise the *existing*
implementation at customer-relevant scale using mocks and synthetic images:

- Drive pagination over 250 simulated files
- Incremental synchronisation (100 files, then 120) without duplicate jobs
- Current duplicate behaviour for identical bytes under different Drive IDs
- Real EXIF orientation handling (1/3/6/8) on real JPEGs, original preserved
- Visual rotation fallback (90/180/270) driven by OCR quality
- 100/250/500-job batches with one failing image and restart recovery

Synthetic images here are geometric markers for rotation checks only; they are
not presented as book photographs.
"""

from __future__ import annotations

import os
import tempfile
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import PIL.Image
import PIL.ImageStat
import pytest

from app import run_sync_engine
from core.config import Config
from models.drive_file import DriveFile
from models.ocr_result import OCRResult
from repositories.job_repository import JobRepository
from services.drive_service import DriveService
from services.job_service import JobService
from workers.processing_worker import ProcessingWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(tmpdir: str) -> Config:
    return Config(
        app_name="Book Processor",
        version="0.1.0",
        environment="Development",
        log_level="INFO",
        data_directory=tmpdir,
        log_directory=os.path.join(tmpdir, "logs"),
        temp_directory=os.path.join(tmpdir, "temp"),
        google_drive_folder_id="audit_folder",
        google_sheet_id="",
        google_service_account_file="",
        ocr_language="en",
        google_books_api_key="",
    )


def _drive_file(
    file_id: str, name: Optional[str] = None, mime: str = "image/jpeg", size=1024
) -> DriveFile:
    return DriveFile(
        file_id=file_id,
        name=name or f"{file_id}.jpg",
        mime_type=mime,
        modified_time="2024-01-01T00:00:00.000Z",
        size=size,
        md5_checksum=None,
        web_view_link=None,
    )


def _jpeg_bytes(tmpdir: str, color: int = 128) -> bytes:
    """A tiny real JPEG, written once and reused as download payload."""
    path = os.path.join(tmpdir, "_payload.jpg")
    if not os.path.exists(path):
        PIL.Image.new("RGB", (32, 48), (color, color, color)).save(path, "JPEG")
    with open(path, "rb") as fh:
        return fh.read()


class _Execute:
    def __init__(self, payload: Dict) -> None:
        self._payload = payload

    def execute(self) -> Dict:
        return self._payload


class _FilesApi:
    """Mimics drive.files().list(pageToken=...) with fixed pages."""

    def __init__(self, pages: List[List[Dict]]) -> None:
        self._pages = pages
        self.calls: List[Optional[str]] = []

    def list(self, **kwargs) -> _Execute:
        token = kwargs.get("pageToken")
        self.calls.append(token)
        index = int(token.split("_")[-1]) if token else 0
        payload: Dict = {"files": self._pages[index]}
        if index + 1 < len(self._pages):
            payload["nextPageToken"] = f"page_{index + 1}"
        return _Execute(payload)


class _DriveApi:
    def __init__(self, files_api: _FilesApi) -> None:
        self._files_api = files_api

    def files(self) -> _FilesApi:
        return self._files_api


def _run_one_sync_cycle(
    config: Config, drive_files: List[DriveFile], download: MagicMock
) -> None:
    """Run exactly one sync cycle of app.run_sync_engine with workers stubbed."""
    os.makedirs(os.path.join(config.data_directory, "downloads"), exist_ok=True)

    service = DriveService(config)
    service.authenticate = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._service = MagicMock()  # type: ignore[assignment]
    service.list_all_files = MagicMock(return_value=drive_files)  # type: ignore[method-assign]
    service.download_file = download  # type: ignore[method-assign]

    with patch("services.drive_service.DriveService", return_value=service):
        with patch("workers.processing_worker.ProcessingWorker") as worker_cls:
            with patch("workers.export_worker.ExportWorker") as export_cls:
                worker_cls.return_value.run = MagicMock()
                export_cls.return_value.run = MagicMock()
                with patch("app.time.sleep") as sleep:
                    sleep.side_effect = SystemExit("cycle done")
                    with pytest.raises(SystemExit, match="cycle done"):
                        run_sync_engine(config, MagicMock())


def _job_ids(tmpdir: str) -> List[str]:
    with JobRepository(os.path.join(tmpdir, "jobs.db")) as repo:
        return [job.file_id for job in repo.get_all_jobs()]


# ---------------------------------------------------------------------------
# Drive discovery
# ---------------------------------------------------------------------------


class TestDriveDiscovery:
    def test_250_files_over_three_pages_discovered_exactly_once(self) -> None:
        """Pagination must return each of 250 simulated files exactly once."""
        ids = [f"file_{i:03d}" for i in range(250)]
        pages = [
            [
                {
                    "id": i,
                    "name": f"{i}.jpg",
                    "mimeType": "image/jpeg",
                    "modifiedTime": "2024-01-01T00:00:00.000Z",
                    "size": "1024",
                }
                for i in ids[:100]
            ],
            [
                {
                    "id": i,
                    "name": f"{i}.jpg",
                    "mimeType": "image/jpeg",
                    "modifiedTime": "2024-01-01T00:00:00.000Z",
                    "size": "1024",
                }
                for i in ids[100:200]
            ],
            [
                {
                    "id": i,
                    "name": f"{i}.jpg",
                    "mimeType": "image/jpeg",
                    "modifiedTime": "2024-01-01T00:00:00.000Z",
                    "size": "1024",
                }
                for i in ids[200:]
            ],
        ]
        files_api = _FilesApi(pages)

        with tempfile.TemporaryDirectory() as tmpdir:
            service = DriveService(_config(tmpdir))
            service._service = _DriveApi(files_api)  # type: ignore[assignment]
            discovered = service.list_all_files()

        assert len(discovered) == 250
        assert len({f.file_id for f in discovered}) == 250
        assert files_api.calls == [None, "page_1", "page_2"]


class TestIncrementalSync:
    def test_second_scan_creates_jobs_only_for_new_files(self) -> None:
        """100 files, then 120: second cycle adds exactly 20 jobs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(tmpdir)
            payload = _jpeg_bytes(tmpdir)

            def download(_drive_file, path: str) -> bool:
                with open(path, "wb") as fh:
                    fh.write(payload)
                return True

            first = [_drive_file(f"a{i:03d}") for i in range(100)]
            _run_one_sync_cycle(config, first, MagicMock(side_effect=download))
            assert len(_job_ids(tmpdir)) == 100

            second = first + [_drive_file(f"b{i:03d}") for i in range(20)]
            download2 = MagicMock(side_effect=download)
            _run_one_sync_cycle(config, second, download2)

            assert download2.call_count == 20
            ids = _job_ids(tmpdir)
            assert len(ids) == 120
            assert len(set(ids)) == 120

    def test_restart_rescan_creates_no_new_jobs(self) -> None:
        """Rescanning after a restart must not create duplicate jobs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(tmpdir)
            payload = _jpeg_bytes(tmpdir)

            def download(_drive_file, path: str) -> bool:
                with open(path, "wb") as fh:
                    fh.write(payload)
                return True

            files = [_drive_file(f"a{i:03d}") for i in range(30)]
            _run_one_sync_cycle(config, files, MagicMock(side_effect=download))
            download2 = MagicMock(side_effect=download)
            _run_one_sync_cycle(config, files, download2)

            assert download2.call_count == 0
            assert len(_job_ids(tmpdir)) == 30


class TestExactDuplicates:
    def test_identical_bytes_different_ids_get_jobs_but_dedupe_at_processing(
        self,
    ) -> None:
        """Content dedupe happens at processing time, not at Drive listing.

        Two Drive files with different IDs and identical bytes each receive a
        job (the sync loop keys on Drive ID), but only the first is OCR'd and
        persisted; the second is cancelled as an exact duplicate. See
        ``tests/unit/test_duplicate_protection.py`` for that assertion.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(tmpdir)
            payload = _jpeg_bytes(tmpdir)

            def download(_drive_file, path: str) -> bool:
                with open(path, "wb") as fh:
                    fh.write(payload)
                return True

            first = _drive_file("orig_001", "front.jpg")
            second = _drive_file("copy_002", "front-copy.jpg")
            download_mock = MagicMock(side_effect=download)
            _run_one_sync_cycle(config, [first, second], download_mock)

            assert sorted(_job_ids(tmpdir)) == ["copy_002", "orig_001"]

    def test_same_drive_id_never_creates_a_second_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(tmpdir)
            payload = _jpeg_bytes(tmpdir)

            def download(_drive_file, path: str) -> bool:
                with open(path, "wb") as fh:
                    fh.write(payload)
                return True

            same = _drive_file("same_001")
            _run_one_sync_cycle(config, [same], MagicMock(side_effect=download))
            _run_one_sync_cycle(config, [same], MagicMock(side_effect=download))
            assert _job_ids(tmpdir) == ["same_001"]


class TestIntakeGateAtSync:
    def test_unsupported_files_are_never_downloaded(self) -> None:
        """PDF/Thumbs.db/HEIC/oversized files are rejected from metadata alone."""
        from repositories.intake_repository import IntakeRepository
        from services import intake_policy

        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(tmpdir)
            payload = _jpeg_bytes(tmpdir)

            def download(_drive_file, path: str) -> bool:
                with open(path, "wb") as fh:
                    fh.write(payload)
                return True

            files = [
                _drive_file("pdf1", "scan.pdf", mime="application/pdf"),
                _drive_file("db1", "Thumbs.db", mime="application/octet-stream"),
                _drive_file("heic1", "IMG_1.HEIC", mime="image/heic"),
                _drive_file(
                    "big1",
                    "huge.jpg",
                    size=intake_policy.max_size_bytes_from_mb(20) + 1,
                ),
                _drive_file("ok1", "cover.jpg"),
            ]
            download_mock = MagicMock(side_effect=download)
            _run_one_sync_cycle(config, files, download_mock)

            assert download_mock.call_count == 1
            assert _job_ids(tmpdir) == ["ok1"]

            with IntakeRepository(os.path.join(tmpdir, "jobs.db")) as repo:
                reasons = {
                    row["drive_file_id"]: row["reason"] for row in repo.list_all()
                }
            assert reasons["pdf1"] == intake_policy.REJECTED_UNSUPPORTED_TYPE
            assert reasons["db1"] == intake_policy.REJECTED_BLOCKED_NAME
            assert reasons["heic1"] == intake_policy.REJECTED_HEIC_UNSUPPORTED
            assert reasons["big1"] == intake_policy.REJECTED_FILE_TOO_LARGE

    def test_rejections_are_recorded_once(self) -> None:
        from repositories.intake_repository import IntakeRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            config = _config(tmpdir)
            files = [_drive_file("pdf1", "scan.pdf", mime="application/pdf")]
            download = MagicMock()
            _run_one_sync_cycle(config, files, download)
            _run_one_sync_cycle(config, files, download)

            assert download.call_count == 0
            with IntakeRepository(os.path.join(tmpdir, "jobs.db")) as repo:
                assert len(repo.list_all()) == 1
                assert repo.count() == 1


# ---------------------------------------------------------------------------
# Image orientation
# ---------------------------------------------------------------------------


def _image_with_exif(path: str, orientation: int, size=(40, 60)) -> None:
    img = PIL.Image.new("RGB", size, (200, 30, 30))
    exif = PIL.Image.Exif()
    exif[0x0112] = orientation
    img.save(path, "JPEG", exif=exif)


class TestExifOrientation:
    @pytest.mark.parametrize(
        "orientation,expected_size",
        [(1, (40, 60)), (3, (40, 60)), (6, (60, 40)), (8, (60, 40))],
    )
    def test_exif_orientation_is_applied(self, orientation: int, expected_size) -> None:
        from services.image_preprocessor import ImagePreprocessor

        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, f"exif_{orientation}.jpg")
            _image_with_exif(source, orientation)
            with open(source, "rb") as fh:
                before = fh.read()

            pre = ImagePreprocessor(temp_dir=os.path.join(tmpdir, "temp"))
            os.makedirs(pre._temp_dir, exist_ok=True)
            out = pre.preprocess(source)

            with PIL.Image.open(out) as processed:
                assert processed.size == expected_size

            with open(source, "rb") as fh:
                assert fh.read() == before, "original image was modified"

    def test_preprocess_twice_does_not_cumulatively_rotate(self) -> None:
        from services.image_preprocessor import ImagePreprocessor

        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "exif_6.jpg")
            _image_with_exif(source, 6)
            pre = ImagePreprocessor(temp_dir=os.path.join(tmpdir, "temp"))
            os.makedirs(pre._temp_dir, exist_ok=True)

            first = pre.preprocess(source)
            second = pre.preprocess(source)
            with PIL.Image.open(first) as a, PIL.Image.open(second) as b:
                assert a.size == b.size == (60, 40)

    def test_normal_portrait_is_not_rotated(self) -> None:
        from services.image_preprocessor import ImagePreprocessor

        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "plain.jpg")
            PIL.Image.new("RGB", (40, 60), (10, 10, 10)).save(source, "JPEG")
            pre = ImagePreprocessor(temp_dir=os.path.join(tmpdir, "temp"))
            os.makedirs(pre._temp_dir, exist_ok=True)
            with PIL.Image.open(pre.preprocess(source)) as out:
                assert out.size == (40, 60)


class _OrientationAwareOCR:
    """Fake OCR that only 'reads' the image when a dark bar is on top.

    Simulates the real signal used by the pipeline: an upright cover yields
    many confident words, a wrongly rotated one yields almost none.
    """

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _upright(path: str) -> bool:
        with PIL.Image.open(path).convert("L") as img:
            w, h = img.size
            band = max(1, h // 4)
            top = PIL.ImageStat.Stat(img.crop((0, 0, w, band))).mean[0]
            bottom = PIL.ImageStat.Stat(img.crop((0, h - band, w, h))).mean[0]
            return top < bottom

    def extract_text(self, path: str) -> OCRResult:
        self.calls += 1
        if self._upright(path):
            return OCRResult(
                text="LE COEUR VERT JEAN-LOUIS KUFFER",
                confidence=0.92,
                words=["LE", "COEUR", "VERT", "JEAN", "LOUIS", "KUFFER"],
                language="fr",
                image_path=path,
            )
        return OCRResult(
            text="|||", confidence=0.10, words=["|"], language="fr", image_path=path
        )


class TestVisualRotationFallback:
    def _service(self, ocr, tmpdir: str):
        from services.metadata_service import MetadataService

        return MetadataService(
            image_preprocessor=__import__(
                "services.image_preprocessor", fromlist=["ImagePreprocessor"]
            ).ImagePreprocessor(temp_dir=os.path.join(tmpdir, "temp")),
            ocr_service=ocr,
            text_normalizer=MagicMock(),
            book_lookup=MagicMock(),
            candidate_ranker=MagicMock(),
            confidence_service=MagicMock(),
            data_directory=tmpdir,
        )

    @pytest.mark.parametrize("true_rotation", [180, 90, 270])
    def test_upside_down_or_sideways_image_is_reoriented(
        self, true_rotation: int
    ) -> None:
        """A visually rotated photo must be re-OCR'd at the right angle.

        The image has a dark bar at the top when upright; here it is saved
        already rotated, with no EXIF orientation tag.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "temp"), exist_ok=True)
            upright = PIL.Image.new("L", (120, 180), 220)
            for x in range(120):
                for y in range(0, 40):
                    upright.putpixel((x, y), 20)
            rotated = upright.rotate(-true_rotation, expand=True)
            path = os.path.join(tmpdir, f"rotated_{true_rotation}.jpg")
            rotated.save(path, "JPEG")

            ocr = _OrientationAwareOCR()
            service = self._service(ocr, tmpdir)
            first = OCRResult(
                text="|||",
                confidence=0.10,
                words=["|"],
                language="fr",
                image_path=path,
            )
            result = service._try_orientation_correction(path, first)

            assert len(result.words) == 6
            assert result.confidence > 0.9
            assert getattr(result, "orientation_corrected", False) is True
            assert ocr.calls >= 2

    def test_upright_image_is_not_reocr_d_needlessly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "temp"), exist_ok=True)
            upright = PIL.Image.new("L", (120, 180), 220)
            for x in range(120):
                for y in range(0, 40):
                    upright.putpixel((x, y), 20)
            path = os.path.join(tmpdir, "upright.jpg")
            upright.save(path, "JPEG")

            ocr = _OrientationAwareOCR()
            service = self._service(ocr, tmpdir)
            good = OCRResult(
                text="LE COEUR VERT",
                confidence=0.9,
                words=["LE", "COEUR", "VERT"],
                language="fr",
                image_path=path,
            )
            result = service._try_orientation_correction(path, good)
            assert ocr.calls == 0
            assert result is good


# ---------------------------------------------------------------------------
# Batch scale, failure isolation, restart
# ---------------------------------------------------------------------------


class _FlakyWorker(ProcessingWorker):
    """Worker whose process() fails for files named bad_*.jpg."""

    def __init__(self, job_service) -> None:  # noqa: D107
        super().__init__(job_service)
        self.processed: List[str] = []

    def process(self, job) -> bool:  # noqa: D102
        self.processed.append(job.file_id)
        if job.file_name.startswith("bad_"):
            raise RuntimeError("simulated OCR failure")
        return True


class TestBatchScale:
    @pytest.mark.parametrize("size", [100, 250, 500])
    def test_batch_creates_all_jobs_fifo_and_isolates_one_failure(
        self, size: int
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "one.jpg")
            PIL.Image.new("RGB", (8, 8)).save(local, "JPEG")

            jobs_path = os.path.join(tmpdir, "jobs.db")
            with JobRepository(jobs_path) as repo:
                service = JobService(repo)
                for i in range(size):
                    name = f"bad_{i}.jpg" if i == size // 2 else f"ok_{i}.jpg"
                    job = service.create_job_for_file(
                        _drive_file(f"f{i:04d}", name), local
                    )
                    assert job is not None

                assert len(repo.get_all_jobs()) == size

                worker = _FlakyWorker(service)
                for job in repo.get_all_jobs():
                    worker._process_job(job)  # noqa: SLF001

                counts = repo.get_count_by_status()
                assert counts.get("completed") == size - 1
                assert counts.get("retrying") == 1
                assert counts.get("failed", 0) == 0

                # The failing job retries until the limit, then stops.
                bad_id = f"f{size // 2:04d}"
                bad_job = repo.get_job_by_drive_file_id(bad_id)
                for _ in range(service.MAX_RETRIES):
                    service.fail_job(bad_job.job_id, "simulated OCR failure")
                assert repo.get_job_by_drive_file_id(bad_id).status.value == "failed"

    def test_stuck_job_is_recovered_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "one.jpg")
            PIL.Image.new("RGB", (8, 8)).save(local, "JPEG")
            jobs_path = os.path.join(tmpdir, "jobs.db")

            with JobRepository(jobs_path) as repo:
                service = JobService(repo)
                job = service.create_job_for_file(_drive_file("crash_001"), local)
                service.start_job(job.job_id)  # process died here
                # Recovery only touches jobs older than the processing timeout
                # (default 7200s), so simulate a crash three hours ago.
                backdated = (
                    __import__("datetime")
                    .datetime.utcnow()
                    .__sub__(__import__("datetime").timedelta(hours=3))
                    .isoformat()
                )
                repo._connection.execute(  # noqa: SLF001
                    "UPDATE jobs SET started_at = ? WHERE job_id = ?",
                    (backdated, job.job_id),
                )
                repo._connection.commit()  # noqa: SLF001

            with JobRepository(jobs_path) as repo:
                service = JobService(repo)
                recovered = service.recover_stuck_jobs()
                assert recovered == 1
                after = repo.get_job_by_drive_file_id("crash_001")
                assert after.status.value == "pending"

    def test_recently_started_job_is_not_prematurely_recovered(self) -> None:
        """A job started seconds ago must not be reset by a second worker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "one.jpg")
            PIL.Image.new("RGB", (8, 8)).save(local, "JPEG")
            with JobRepository(os.path.join(tmpdir, "jobs.db")) as repo:
                service = JobService(repo)
                job = service.create_job_for_file(_drive_file("busy_001"), local)
                service.start_job(job.job_id)
                assert service.recover_stuck_jobs() == 0
                assert repo.get_job_by_drive_file_id("busy_001").status.value == (
                    "processing"
                )

    def test_completed_job_is_not_reprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "one.jpg")
            PIL.Image.new("RGB", (8, 8)).save(local, "JPEG")
            jobs_path = os.path.join(tmpdir, "jobs.db")

            with JobRepository(jobs_path) as repo:
                service = JobService(repo)
                job = service.create_job_for_file(_drive_file("done_001"), local)
                service.complete_job(job.job_id)

                again = service.create_job_for_file(_drive_file("done_001"), local)
                assert again is not None  # a *new* job row, not a reprocess
                statuses = [j.status.value for j in repo.get_all_jobs()]
                assert statuses.count("completed") == 1


# ---------------------------------------------------------------------------
# Known-good regression: "Le cœur vert" / Jean-Louis Kuffer
# ---------------------------------------------------------------------------


class TestKnownGoodBook:
    """The verified production identification must keep working offline.

    Real text extractor, real candidate ranker and real confidence service;
    only OCR and the network lookup are mocked.
    """

    OCR_TEXT = (
        "Le cœur vert\n"
        "par Jean-Louis Kuffer\n"
        "ISBN 9780441013593\n"
        "Éditeur: Gallimard"
    )

    def _candidate_metadata(self):
        from models.book_metadata import BookMetadata

        return BookMetadata(
            title="Le cœur vert",
            authors=["Jean-Louis Kuffer"],
            isbn_13="9780441013593",
            publisher="Gallimard",
            confidence=1.0,
            source="open_library",
        )

    def test_identified_with_high_confidence_and_completed(self) -> None:
        from services.book_text_extractor import BookTextExtractor
        from services.candidate_ranker import CandidateRanker
        from services.confidence_service import ConfidenceService
        from services.metadata_service import MetadataService
        from services.text_normalizer import TextNormalizer

        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "cover.jpg")
            PIL.Image.new("RGB", (60, 90), (180, 180, 180)).save(local, "JPEG")

            ocr = MagicMock()
            ocr.extract_text.return_value = OCRResult(
                text=self.OCR_TEXT,
                confidence=0.93,
                words=["Le", "cœur", "vert", "Jean-Louis", "Kuffer", "Gallimard"],
                language="fr",
                image_path=local,
            )
            preprocessor = MagicMock()
            preprocessor.preprocess.return_value = local
            preprocessor.cleanup = MagicMock()

            book = self._candidate_metadata()
            lookup = MagicMock()
            lookup.search_by_isbn.return_value = [book]
            lookup.search_by_title_author.return_value = [book]
            lookup.search_by_title.return_value = [book]
            lookup.search_by_author.return_value = []

            service = MetadataService(
                image_preprocessor=preprocessor,
                ocr_service=ocr,
                text_normalizer=TextNormalizer(),
                book_lookup=lookup,
                candidate_ranker=CandidateRanker(),
                confidence_service=ConfidenceService(),
                text_extractor=BookTextExtractor(languages=["en", "fr"]),
                data_directory=tmpdir,
            )

            metadata, _candidates = service.identify_book(
                local, drive_file_id="known_good_001", job_id="job_kg_001"
            )

            assert metadata is not None, "known-good book must be identified"
            assert metadata.title == "Le cœur vert"
            assert metadata.authors == ["Jean-Louis Kuffer"]
            assert metadata.status == "identified"
            assert metadata.confidence >= 0.85

            # And the worker must complete the job for such metadata.
            with JobRepository(os.path.join(tmpdir, "jobs.db")) as repo:
                job_service = JobService(repo)
                job = job_service.create_job_for_file(
                    _drive_file("known_good_001", "cover.jpg"), local
                )
                worker = ProcessingWorker(job_service, service)
                worker._process_job(job)  # noqa: SLF001
                assert (
                    repo.get_job_by_drive_file_id("known_good_001").status.value
                    == "completed"
                )
