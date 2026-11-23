"""Integration tests for app.py production wiring.

These tests verify that python app.py is the real production entry point
and performs the complete pipeline end-to-end with proper duplicate
prevention, new-file detection, and failure isolation.

External APIs are mocked to avoid network costs and keep tests fast.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config import Config


class _DriveFile:
    __slots__ = ("file_id", "name")

    def __init__(self, file_id: str, name: str) -> None:
        self.file_id = file_id
        self.name = name


def _make_config(tmp_path: Path) -> Config:
    """Create a Config pointing at temporary data dirs."""
    return Config(
        data_directory=str(tmp_path),
        google_sheets_credentials="test_creds.json",
        google_sheet_id="test_sheet_id",
        google_drive_folder_id="test_folder",
        ocr_language="en,fr",
        confidence_threshold=0.85,
        confidence_manual_review_threshold=0.50,
        confidence_rejected_threshold=0.50,
        google_books_api_key="test_gb_key",
        open_library_enabled=True,
        google_books_timeout=1,
        open_library_timeout=1,
        exa_api_key="test_exa_key",
        debug_retention_days=0,
    )


def _build_minimal_metadata(drive_file_id: str):
    """Build a minimal metadata-like object."""
    meta = MagicMock()
    meta.title = f"Book {drive_file_id[-4:]}"
    meta.authors = ["Author"]
    meta.publisher = "Test Publisher"
    meta.published_date = "2024"
    meta.language = "English"
    meta.isbn_13 = "9780000000000"
    meta.cover_url = "https://example.com/cover.jpg"
    meta.confidence = 0.95
    meta.source = "test"
    meta.status = "needs_review"
    meta.drive_file_id = drive_file_id
    meta.job_id = f"job_{drive_file_id}"
    meta.vision_title = f"Book {drive_file_id[-4:]}"
    meta.vision_author = "Author"
    meta.vision_publisher = "Test Publisher"
    meta.vision_confidence = 0.95
    meta.vision_used = 1
    return meta


class _PatchedDependencies(dict):
    """Mapping of patch target -> started mock, plus the patcher objects.

    The patchers themselves must be kept so ``stop()`` can restore the original
    attributes; storing only ``patcher.start()`` results leaves every dependency
    mocked for the rest of the test session and poisons unrelated unit tests.
    """

    def __init__(self) -> None:
        super().__init__()
        self.patchers: list = []


def _patch_app_dependencies(tmp_path: Path):
    """Patch app.py runtime dependencies by patching source modules."""
    patches = {
        "services.drive_service.DriveService": MagicMock(),
        "repositories.sqlite_repository.SQLiteRepository": MagicMock(),
        "repositories.job_repository.JobRepository": MagicMock(),
        "services.job_service.JobService": MagicMock(),
        "repositories.book_metadata_repository.BookMetadataRepository": MagicMock(),
        "repositories.export_repository.ExportRepository": MagicMock(),
        "repositories.cover_repository.CoverRepository": MagicMock(),
        "services.metadata_service.MetadataService": MagicMock(),
        "services.confidence_service.ConfidenceService": MagicMock(),
        "services.ocr_service.OCRService": MagicMock(),
        "services.image_preprocessor.ImagePreprocessor": MagicMock(),
        "services.text_normalizer.TextNormalizer": MagicMock(),
        "services.book_lookup_service.BookLookupService": MagicMock(),
        "services.candidate_ranker.CandidateRanker": MagicMock(),
        "services.metadata_validation_service.MetadataValidationService": MagicMock(),
        "services.cover_resolver_service.CoverResolverService": MagicMock(),
        "services.cover_download_service.CoverDownloadService": MagicMock(),
        "services.google_sheets_service.GoogleSheetsService": MagicMock(),
        "services.metadata_enrichment_service.MetadataEnrichmentService": MagicMock(),
        "services.clean_cover_service.CleanCoverService": MagicMock(),
        "services.export_service.ExportService": MagicMock(),
        "workers.export_worker.ExportWorker": MagicMock(),
        "workers.processing_worker.ProcessingWorker": MagicMock(),
    }

    started = _PatchedDependencies()
    for target, mock in patches.items():
        p = patch(target, mock)
        started[target] = p.start()
        started.patchers.append(p)

    # Configure behavior on the *instance* returned by the class mock.
    started[
        "services.drive_service.DriveService"
    ].return_value.authenticate.return_value = True
    started[
        "services.drive_service.DriveService"
    ].return_value.list_all_files.return_value = []

    started[
        "services.metadata_service.MetadataService"
    ].return_value.identify_book.return_value = (
        _build_minimal_metadata("drive_000"),
        [],
    )

    started[
        "services.clean_cover_service.CleanCoverService"
    ].return_value.find_clean_cover.return_value = (
        "https://example.com/cover.jpg",
        "google_books",
        os.path.join(str(tmp_path), "covers", "cover_0.jpg"),
    )

    started[
        "repositories.sqlite_repository.SQLiteRepository"
    ].return_value.__enter__ = MagicMock(return_value=MagicMock())
    started["repositories.sqlite_repository.SQLiteRepository"].return_value.__exit__ = (
        MagicMock(return_value=False)
    )

    started["repositories.job_repository.JobRepository"].return_value.__enter__ = (
        MagicMock(return_value=MagicMock())
    )
    started["repositories.job_repository.JobRepository"].return_value.__exit__ = (
        MagicMock(return_value=False)
    )

    started[
        "repositories.book_metadata_repository.BookMetadataRepository"
    ].return_value.__enter__ = MagicMock(return_value=MagicMock())
    started[
        "repositories.book_metadata_repository.BookMetadataRepository"
    ].return_value.__exit__ = MagicMock(return_value=False)

    started[
        "repositories.export_repository.ExportRepository"
    ].return_value.__enter__ = MagicMock(return_value=MagicMock())
    started["repositories.export_repository.ExportRepository"].return_value.__exit__ = (
        MagicMock(return_value=False)
    )

    started["repositories.cover_repository.CoverRepository"].return_value.__enter__ = (
        MagicMock(return_value=MagicMock())
    )
    started["repositories.cover_repository.CoverRepository"].return_value.__exit__ = (
        MagicMock(return_value=False)
    )

    started[
        "services.job_service.JobService"
    ].return_value.recover_stuck_jobs.return_value = 0
    started[
        "services.job_service.JobService"
    ].return_value.create_job_for_file.return_value = MagicMock(
        job_id="job_new", file_id="drive_new"
    )

    return started


def _unpatch_app_dependencies(started):
    for patcher in getattr(started, "patchers", []):
        patcher.stop()
    if hasattr(started, "patchers"):
        started.patchers = []


def test_initial_run_creates_jobs_db_records_and_sheet_rows(tmp_path: Path):
    """14 Drive files -> 14 jobs -> 14 DB records -> 14 Sheet rows."""
    config = _make_config(tmp_path)
    drive_files = [
        _DriveFile(file_id=f"drive_{i:03d}", name=f"book_{i}.jpg") for i in range(14)
    ]

    started = _patch_app_dependencies(tmp_path)
    try:
        file_repo = started[
            "repositories.sqlite_repository.SQLiteRepository"
        ].return_value.__enter__.return_value
        file_repo.get_all_file_ids.return_value = []

        drive_mock = started["services.drive_service.DriveService"].return_value
        drive_mock.list_all_files.side_effect = lambda: (
            list(drive_files)
            if drive_mock.list_all_files.call_count == 1
            else (_ for _ in ()).throw(KeyboardInterrupt("exit after first cycle"))
        )

        from app import run_sync_engine

        run_sync_engine(config, MagicMock())
    finally:
        _unpatch_app_dependencies(started)

    assert file_repo.insert_file.call_count == 14
    assert (
        started[
            "services.job_service.JobService"
        ].return_value.create_job_for_file.call_count
        == 14
    )


def test_second_run_no_new_jobs_or_duplicates(tmp_path: Path):
    """Same 14 files on second sync -> 0 new jobs, no duplicates."""
    config = _make_config(tmp_path)
    drive_files = [
        _DriveFile(file_id=f"drive_{i:03d}", name=f"book_{i}.jpg") for i in range(14)
    ]

    started = _patch_app_dependencies(tmp_path)
    try:
        started[
            "services.drive_service.DriveService"
        ].return_value.list_all_files.return_value = drive_files
        file_repo = started[
            "repositories.sqlite_repository.SQLiteRepository"
        ].return_value.__enter__.return_value
        file_repo.get_all_file_ids.return_value = [f.file_id for f in drive_files]

        from app import run_sync_engine

        sleep_calls = {"n": 0}

        def fake_sleep(seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise KeyboardInterrupt("exit after first cycle")

        with patch("app.time.sleep", side_effect=fake_sleep):
            run_sync_engine(config, MagicMock())
    finally:
        _unpatch_app_dependencies(started)

    assert file_repo.insert_file.call_count == 0
    assert (
        started[
            "services.job_service.JobService"
        ].return_value.create_job_for_file.call_count
        == 0
    )


def test_new_file_detected_and_processed(tmp_path: Path):
    """15 files after initial 14 -> exactly 1 new job."""
    config = _make_config(tmp_path)
    existing_ids = {f"drive_{i:03d}" for i in range(14)}
    drive_files = [
        _DriveFile(file_id=f"drive_{i:03d}", name=f"book_{i}.jpg") for i in range(14)
    ]
    drive_files.append(_DriveFile(file_id="drive_new", name="book_new.jpg"))

    started = _patch_app_dependencies(tmp_path)
    try:
        drive_mock = started["services.drive_service.DriveService"].return_value
        drive_mock.list_all_files.side_effect = [
            list(drive_files),
            KeyboardInterrupt("exit after first cycle"),
        ]
        file_repo = started[
            "repositories.sqlite_repository.SQLiteRepository"
        ].return_value.__enter__.return_value
        file_repo.get_all_file_ids.return_value = list(existing_ids)

        from app import run_sync_engine

        run_sync_engine(config, MagicMock())
    finally:
        _unpatch_app_dependencies(started)

    assert file_repo.insert_file.call_count == 1
    assert (
        started[
            "services.job_service.JobService"
        ].return_value.create_job_for_file.call_count
        == 1
    )
    assert (
        started["services.job_service.JobService"]
        .return_value.create_job_for_file.call_args[0][0]
        .file_id
        == "drive_new"
    )


def test_one_bad_book_does_not_stop_sync(tmp_path: Path):
    """Download failure for one book does not abort the sync cycle."""
    config = _make_config(tmp_path)
    good_file = _DriveFile(file_id="drive_good", name="good.jpg")
    bad_file = _DriveFile(file_id="drive_bad", name="bad.jpg")

    started = _patch_app_dependencies(tmp_path)
    try:
        started[
            "services.drive_service.DriveService"
        ].return_value.list_all_files.return_value = [good_file, bad_file]
        started[
            "services.drive_service.DriveService"
        ].return_value.download_file.side_effect = [
            None,
            RuntimeError("network failure"),
        ]

        file_repo = started[
            "repositories.sqlite_repository.SQLiteRepository"
        ].return_value.__enter__.return_value
        file_repo.get_all_file_ids.return_value = []

        from app import run_sync_engine

        sleep_calls = {"n": 0}

        def fake_sleep(seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise KeyboardInterrupt("exit after first cycle")

        with patch("app.time.sleep", side_effect=fake_sleep):
            run_sync_engine(config, MagicMock())
    finally:
        _unpatch_app_dependencies(started)

    assert file_repo.insert_file.call_count == 1
    assert (
        started[
            "services.job_service.JobService"
        ].return_value.create_job_for_file.call_count
        == 1
    )


def test_app_does_not_delete_existing_data_on_startup(tmp_path: Path):
    """python app.py startup must not delete metadata.db or covers."""
    config = _make_config(tmp_path)
    metadata_db_path = os.path.join(str(tmp_path), "metadata.db")
    covers_dir = os.path.join(str(tmp_path), "covers")
    os.makedirs(covers_dir, exist_ok=True)
    Path(metadata_db_path).write_text("existing")

    cover_file = Path(covers_dir) / "existing.jpg"
    cover_file.write_bytes(b"existing")

    drive_files = [
        _DriveFile(file_id="drive_new", name="new.jpg"),
    ]

    started = _patch_app_dependencies(tmp_path)
    try:
        started[
            "services.drive_service.DriveService"
        ].return_value.list_all_files.return_value = drive_files
        file_repo = started[
            "repositories.sqlite_repository.SQLiteRepository"
        ].return_value.__enter__.return_value
        file_repo.get_all_file_ids.return_value = []

        from app import run_sync_engine

        sleep_calls = {"n": 0}

        def fake_sleep(seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise KeyboardInterrupt("exit after first cycle")

        with patch("app.time.sleep", side_effect=fake_sleep):
            run_sync_engine(config, MagicMock())
    finally:
        _unpatch_app_dependencies(started)

    assert Path(metadata_db_path).exists()
    assert Path(metadata_db_path).read_text() == "existing"
    assert cover_file.exists()
    assert cover_file.read_bytes() == b"existing"
