"""Simplified E2E verification — process all 14 downloaded books."""
import json
import os
import sys
import time
import traceback
from pathlib import Path

project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from core.config import Config
from core.logger import setup_logger
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.job_repository import JobRepository
from repositories.sqlite_repository import SQLiteRepository
from repositories.export_repository import ExportRepository
from repositories.cover_repository import CoverRepository
from services.metadata_service import MetadataService
from services.confidence_service import ConfidenceService
from services.candidate_ranker import CandidateRanker
from services.export_service import ExportService
from services.google_sheets_service import GoogleSheetsService
from services.book_lookup_service import BookLookupService
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer
from workers.processing_worker import ProcessingWorker
from workers.export_worker import ExportWorker
from services.job_service import JobService

logger = setup_logger("e2e_verify")

DATA_DIR = str(project_root / "data")
DOWNLOADS_DIR = os.path.join(DATA_DIR, "downloads")
VISION_CACHE_DIR = os.path.join(DATA_DIR, "vision_model_responses")
DB_PATH = os.path.join(DATA_DIR, "app.db")
JOBS_DB_PATH = os.path.join(DATA_DIR, "jobs.db")
METADATA_DB_PATH = os.path.join(DATA_DIR, "metadata.db")

def _load_drive_books() -> list:
    """Drive file ids for this verification run, one per line.

    The ids are collection data, not source code, so they live in a local file
    inside ``data/`` (gitignored) instead of being hard-coded here.
    """
    ids_path = os.path.join(DATA_DIR, "e2e_drive_ids.txt")
    if not os.path.exists(ids_path):
        return []
    with open(ids_path, "r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


DRIVE_BOOKS = _load_drive_books()


def main():
    if not DRIVE_BOOKS:
        raise SystemExit(
            f"Missing {os.path.join(DATA_DIR, 'e2e_drive_ids.txt')}: put one "
            "Drive file id per line there before running this verification "
            "script."
        )

    # Clean databases
    for db_path in [DB_PATH, JOBS_DB_PATH, METADATA_DB_PATH]:
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"Removed {db_path}")

    # Vision cache is pre-populated with vision_analyze results for 14 books.
    # Do NOT delete it — these are the vision model's FINAL ANALYSIS extractions.

    # Initialize config
    config = Config.from_env()
    config.validate()
    print(f"Config validated. OCR languages: {config.ocr_language}")

    # Set up repositories
    file_repo = SQLiteRepository(DB_PATH)
    file_repo._create_tables()
    job_repo = JobRepository(JOBS_DB_PATH)
    job_repo._create_tables()
    metadata_repo = BookMetadataRepository(METADATA_DB_PATH)
    metadata_repo._create_table()
    metadata_repo._migrate_schema()
    export_repo = ExportRepository(JOBS_DB_PATH, METADATA_DB_PATH)
    export_repo.init_db()

    job_service = JobService(job_repo)

    # Initialize services
    confidence_svc = ConfidenceService(
        threshold=0.85,
        manual_review_threshold=0.50,
        rejected_threshold=0.50,
    )
    ocr_svc = OCRService(
        languages=config.ocr_language.split(","),
        use_gpu=False,
        confidence_threshold=0.5,
    )
    text_normalizer = TextNormalizer()
    book_lookup = BookLookupService(
        google_books_api_key=getattr(config, "google_books_api_key", None),
        open_library_enabled=True,
        google_books_timeout=15,
        open_library_timeout=15,
    )
    metadata_service = MetadataService(
        ocr_service=ocr_svc,
        text_normalizer=text_normalizer,
        book_lookup=book_lookup,
        candidate_ranker=CandidateRanker(),
        confidence_service=confidence_svc,
        data_directory=DATA_DIR,
        metadata_repo=metadata_repo,
    )
    metadata_service.set_export_repo(export_repo)

    # Initialize export service
    sheets_service = GoogleSheetsService(
        credentials_file=config.google_sheets_credentials,
        sheets_id=config.google_sheet_id,
        tab_name=config.google_sheets_tab_name,
    )

    from services.metadata_validation_service import MetadataValidationService
    from services.cover_resolver_service import CoverResolverService
    from models.export_config import ExportConfig
    from services.cover_download_service import CoverDownloadService

    export_config = ExportConfig.from_env()
    validation_service = MetadataValidationService(confidence_threshold=0.85)
    cover_resolver = CoverResolverService(book_lookup_service=book_lookup)
    cover_repo = CoverRepository(os.path.join(DATA_DIR, 'covers.db'))
    cover_repo._create_tables()
    cover_downloader = CoverDownloadService(
        cover_repo=cover_repo,
        covers_dir=os.path.join(DATA_DIR, "covers"),
        timeout=export_config.covers_timeout,
        min_width=export_config.cover_min_width,
        min_height=export_config.cover_min_height,
    )
    export_service = ExportService(
        metadata_repo=metadata_repo,
        cover_repo=metadata_repo,
        export_repo=export_repo,
        validation_service=validation_service,
        cover_resolver=cover_resolver,
        cover_downloader=cover_downloader,
        sheets_service=sheets_service,
        config=export_config,
    )

    # Create processing worker
    worker = ProcessingWorker(job_service, metadata_service, metadata_repo=metadata_repo)

    # Get downloaded files
    downloaded_files = []
    for f in sorted(os.listdir(DOWNLOADS_DIR)):
        name, ext = os.path.splitext(f)
        if ext.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'):
            local_path = os.path.join(DOWNLOADS_DIR, f)
            downloaded_files.append((name, local_path, f))

    print(f"\nDownloaded image files: {len(downloaded_files)}")

    vision_stats = {"calls": 0, "cache_hits": 0, "live_calls": 0}

    # Process each file
    for drive_id, local_path, filename in downloaded_files:
        print(f"\n--- Processing: {drive_id} ({filename}) ---")

        # Create file repo entry
        from models.drive_file import DriveFile
        drive_file = DriveFile(
            file_id=drive_id,
            name=filename,
            mime_type="image/jpeg",
            modified_time="",
            size=os.path.getsize(local_path),
        )
        file_repo.insert_file(
            drive_file=drive_file,
            local_filename=filename,
            download_time=str(int(time.time())),
        )

        # Create processing job
        job = job_repo.create_job(
            file_id=drive_id,
            file_name=filename,
            local_path=local_path,
        )
        if job is None:
            print(f"  WARNING: Could not create job for {drive_id}, skipping")
            continue

        print(f"  Job created: {job.job_id}")

        # Process
        try:
            result = worker.process(job)
            print(f"  Worker process returned: {result}")
        except Exception as e:
            print(f"  ERROR processing: {e}")
            traceback.print_exc()

        # Check DB result
        db_record = metadata_repo.get_by_drive_file_id(drive_id)
        if db_record:
            print(f"  DB: found - title={db_record.title}, status={db_record.status}")
            print(f"  vision_used={getattr(db_record, 'vision_used', False)}")
            print(f"  vision_title={getattr(db_record, 'vision_title', None)}")
            print(f"  vision_author={getattr(db_record, 'vision_author', None)}")
            print(f"  vision_publisher={getattr(db_record, 'vision_publisher', None)}")
            print(f"  confidence={db_record.confidence}")
            if db_record.rejection_reason:
                print(f"  rejection_reason={db_record.rejection_reason}")
        else:
            print(f"  DB: MISSING!")

    # Export all to Google Sheets
    print("\n\n=== EXPORTING ALL TO GOOGLE SHEETS ===")
    export_entries = export_repo.get_pending_exports()
    print(f"Pending exports: {len(export_entries)}")

    exported_count = 0
    for entry in export_entries:
        try:
            result = export_service.process_export(
                drive_file_id=entry['drive_file_id'],
                job_id=entry['job_id'],
            )
            print(f"  Exported: drive_id={entry['drive_file_id']} status={result.status}")
            exported_count += 1
        except Exception as e:
            print(f"  Export error for {entry}: {e}")
            traceback.print_exc()

    print(f"Total exported: {exported_count}")
    time.sleep(5)

    # === VERIFICATION ===
    print("\n" + "=" * 80)
    print("FINAL VERIFICATION REPORT")
    print("=" * 80)

    all_db_records = {r.drive_file_id: r for r in metadata_repo.list()}
    # Verification only reads the workbook: never let a diagnostic look trigger
    # the Status-column migration.
    sheet_rows = sheets_service.read_all_rows(ensure_layout=False)

    cache_hits = sum(1 for d in DRIVE_BOOKS if os.path.exists(os.path.join(VISION_CACHE_DIR, f"{d}.json")))
    vision_processed = sum(1 for d in DRIVE_BOOKS
                          if getattr(all_db_records.get(d), 'vision_used', False))
    live_calls = vision_processed - cache_hits

    identified = sum(1 for r in all_db_records.values() if r.status == "identified")
    needs_review = sum(1 for r in all_db_records.values() if r.status == "needs_review")
    rejected = sum(1 for r in all_db_records.values() if r.status == "rejected")
    not_persisted = len(DRIVE_BOOKS) - len(all_db_records)

    print(f"\nDiscovered (Drive files):     {len(DRIVE_BOOKS)}")
    print(f"Downloaded (images):          {len(downloaded_files)}")
    print(f"Vision processed:             {vision_processed}")
    print(f"  Cache hits:                 {cache_hits}")
    print(f"  Live model calls:           {live_calls}")
    print(f"DB records persisted:         {len(all_db_records)}")
    print(f"  Identified:                 {identified}")
    print(f"  Needs review:               {needs_review}")
    print(f"  Rejected:                   {rejected}")
    print(f"  Not persisted:              {not_persisted}")
    print(f"Google Sheet rows:            {len(sheet_rows)}")
    print(f"Missing from DB:              {len(DRIVE_BOOKS) - len(all_db_records)}")
    print(f"Missing from Sheet:           {len(DRIVE_BOOKS) - len(sheet_rows)}")

    # Per-book table
    print("\n" + "-" * 110)
    print(f"{'Drive ID':<35} {'Vision':<8} {'Cache':<6} {'DB':<6} {'Status':<13} {'Title':<35} {'Authors'}")
    print("-" * 110)

    for drive_id in DRIVE_BOOKS:
        cache_hit = os.path.exists(os.path.join(VISION_CACHE_DIR, f"{drive_id}.json"))
        rec = all_db_records.get(drive_id)
        if rec:
            vision_used = getattr(rec, 'vision_used', False)
            vision_str = "yes" if vision_used else "NO"
            cache_str = "yes" if cache_hit else "no"
            db_str = "yes"
            status = rec.status
            title = (rec.title or "(null)")[:35]
            authors = ", ".join(rec.authors) if rec.authors else "(none)"
        else:
            vision_str = "NO"
            cache_str = "yes" if cache_hit else "no"
            db_str = "NO!"
            status = "NOT_PERSISTED"
            title = "N/A"
            authors = "N/A"

        print(f"{drive_id:<35} {vision_str:<8} {cache_str:<6} {db_str:<6} {status:<13} {title:<35} {authors}")

    # Invariant checks
    print("\n" + "=" * 80)
    print("INVARIANT CHECKS")
    print("=" * 80)

    checks = [
        ("Vision processed == discovered", vision_processed == len(DRIVE_BOOKS)),
        ("Vision skipped == 0", len(DRIVE_BOOKS) - vision_processed == 0),
        ("DB records == discovered", len(all_db_records) == len(DRIVE_BOOKS)),
        ("Rejected == 0", rejected == 0),
        ("Not persisted == 0", not_persisted == 0),
        ("Sheet rows == discovered", len(sheet_rows) == len(DRIVE_BOOKS)),
        ("identified + needs_review == discovered", (identified + needs_review) == len(DRIVE_BOOKS)),
    ]

    all_pass = True
    for check_name, passed in checks:
        status_str = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status_str}] {check_name}")

    if not all_pass:
        print("\nFailed checks:")
        for name, passed in checks:
            if not passed:
                print(f"  - {name}")

    # Save results
    output = {
        "summary": {
            "discovered": len(DRIVE_BOOKS),
            "downloaded": len(downloaded_files),
            "vision_processed": vision_processed,
            "vision_cache_hits": cache_hits,
            "vision_live_calls": live_calls,
            "db_records": len(all_db_records),
            "identified": identified,
            "needs_review": needs_review,
            "rejected": rejected,
            "sheet_rows": len(sheet_rows),
            "all_checks_passed": all_pass,
        },
        "checks": [{"name": n, "passed": p} for n, p in checks],
    }
    with open(os.path.join(DATA_DIR, "e2e_verification_results.json"), "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\nResults saved to: {DATA_DIR}/e2e_verification_results.json")

    if all_pass:
        print("\n*** ALL INVARIANTS VERIFIED ***")
    else:
        print("\n*** SOME INVARIANTS FAILED ***")

    # Cleanup
    file_repo.close()
    job_repo.close()
    metadata_repo.close()
    export_repo.close()

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
