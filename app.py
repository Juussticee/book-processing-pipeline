"""Book Processor - Application entry point.

Startup sequence (Milestone 5):
1. Initialize (Config, Logger)
2. Recover interrupted jobs (reset PROCESSING -> PENDING)
3. Start ProcessingWorker (polls job queue)
4. Start ExportWorker (polls export queue — Milestone 5)
5. Drive Sync Loop (sync new files, create jobs for new downloads)
6. Graceful shutdown on KeyboardInterrupt
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.config import Config  # noqa: E402
from core.constants import APP_NAME, VERSION  # noqa: E402
from core.logger import setup_logger  # noqa: E402


def safe_local_filename(drive_file_id: str, original_name: str) -> str:
    """Generate a safe local filename from a Drive file ID.

    Uses the Drive file ID as the base name and preserves the original
    file extension (sanitized). Prevents path traversal and disallowed
    characters.

    Args:
        drive_file_id: The Google Drive file ID (guaranteed unique).
        original_name: The original filename for extension extraction.

    Returns:
        A safe filename like ``1AbCdEfGh.jpg``.
    """
    import re

    # Extract and sanitize the extension from the original filename
    ext = ""
    if original_name and "." in original_name:
        raw_ext = original_name.rsplit(".", 1)[-1].lower()
        # Allow only alphanumeric extensions (max 4 chars)
        if re.match(r"^[a-z0-9]{1,4}$", raw_ext):
            ext = raw_ext

    # Sanitize the Drive file ID — it should be alphanumeric + hyphens
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", drive_file_id)
    if not safe_id:
        safe_id = "unknown"

    if ext:
        return f"{safe_id}.{ext}"
    return safe_id


def print_startup_report(status_report: dict) -> None:
    """Print a configuration summary report."""
    print("\nConfiguration\n")

    present = status_report.get("present", [])
    missing = status_report.get("missing", [])
    future = status_report.get("future", [])

    if present:
        print("  Present")
        for item in present:
            print(f"    {item}")

    if missing:
        print("\n  Missing")
        for item in missing:
            print(f"    {item}")

    if future:
        print("\n  Future Features")
        for item in future:
            print(f"    {item}")

    print()


def log_sync_summary(
    logger,
    *,
    discovered: int,
    new_files: int,
    already_known: int,
    rejected_cycle: int,
    rejected_total: int,
    duplicate_counts: dict,
    queue: dict,
    ocr_calls: int,
    metadata_lookups: int,
    cover_downloads: int,
    exports: dict,
) -> None:
    """Log the per-cycle processing summary requested by the operator.

    Every number comes from the database or a service counter; nothing is
    estimated, and the categories are mutually exclusive so they can be summed.
    The two ``check`` figures must read OK; a MISMATCH means the cycle lost or
    double-counted files, which is reported rather than hidden.

    Files move through two disjoint stages, each counted once:

    1. Discovery: already known (Drive ID) + rejected by intake + accepted.
    2. Screening of accepted files: exact / near / same-book skips + processed.

    ``possible`` duplicates are a *subset* of processed files: they were
    recorded and flagged, never merged, so they are labelled as such instead of
    being added to the skip totals.
    """
    exact = duplicate_counts.get("exact", 0)
    near = duplicate_counts.get("near", 0)
    possible = duplicate_counts.get("possible_near", 0)
    same_book = duplicate_counts.get("same_book", 0)
    possible_book = duplicate_counts.get("possible_same_book", 0)

    accepted = max(new_files - rejected_cycle, 0)
    skipped_by_fingerprint = exact + near + same_book
    processed = max(accepted - skipped_by_fingerprint, 0)
    skipped_repeated = already_known + rejected_cycle + skipped_by_fingerprint

    discovery_total = already_known + rejected_cycle + accepted
    discovery_check = (
        "OK"
        if discovery_total == discovered
        else f"MISMATCH {discovery_total}/{discovered}"
    )
    screening_total = skipped_by_fingerprint + processed
    screening_check = (
        "OK"
        if screening_total == accepted
        else f"MISMATCH {screening_total}/{accepted}"
    )

    export_line = (
        ", ".join(f"{status}={count}" for status, count in sorted(exports.items()))
        or "none"
    )

    logger.info(
        "\n"
        "========================================\n"
        "BOOK PROCESSING SUMMARY\n"
        "========================================\n"
        "FILES DISCOVERED THIS CYCLE\n"
        "  Listed on Drive:          %d\n"
        "  Already known (Drive ID): %d  (skipped, not downloaded)\n"
        "  Rejected by intake:       %d  (not downloaded; %d rejected in total)\n"
        "  Accepted and downloaded:  %d\n"
        "  Check: already + rejected + accepted = listed: %s\n"
        "\n"
        "SCREENING OF ACCEPTED FILES (each file counted once)\n"
        "  Exact duplicates:         %d  (identical content hash, skipped)\n"
        "  Near duplicates:          %d  (image fingerprint, skipped)\n"
        "  Same book, valid ISBN:    %d  (skipped, original kept)\n"
        "  Processed:                %d  (OCR + identification attempted)\n"
        "  Check: skipped + processed = accepted: %s\n"
        "\n"
        "REVIEW FLAGS (subsets of processed; nothing discarded)\n"
        "  Possible near duplicate:  %d  (recorded and flagged)\n"
        "  Possible same book:       %d  (recorded and flagged)\n"
        "\n"
        "BOOKS IN DATABASE (cumulative)\n"
        "  Identified:               %d\n"
        "  Manual review:            %d\n"
        "  Failed:                   %d\n"
        "  Pending / retrying:       %d / %d\n"
        "\n"
        "PROVIDER WORK THIS CYCLE\n"
        "  OCR calls:                %d\n"
        "  Metadata lookups:         %d\n"
        "  Cover downloads:          %d\n"
        "\n"
        "EXPORTS (cumulative):       %s\n"
        "\n"
        "Skipped repeated work:      %d\n"
        "========================================",
        discovered,
        already_known,
        rejected_cycle,
        rejected_total,
        accepted,
        discovery_check,
        exact,
        near,
        same_book,
        processed,
        screening_check,
        possible,
        possible_book,
        queue.get("completed", 0),
        queue.get("manual_review", 0),
        queue.get("failed", 0),
        queue.get("pending", 0),
        queue.get("retrying", 0),
        ocr_calls,
        metadata_lookups,
        cover_downloads,
        export_line,
        skipped_repeated,
    )


def run_sync_engine(config: Config, logger) -> None:
    """Run the synchronization engine loop.

    Each cycle:
    1. Lists all files in the Drive folder.
    2. Downloads new files (not yet tracked in SQLite).
    3. Creates a processing job for each downloaded file.
    4. Sleeps 30 seconds before the next cycle.

    A ProcessingWorker runs in a background thread, polling for
    pending jobs and processing them.
    """
    from repositories.book_metadata_repository import (
        BookMetadataRepository,
    )  # noqa: E402
    from repositories.cover_repository import CoverRepository  # noqa: E402
    from repositories.duplicate_repository import DuplicateRepository  # noqa: E402
    from repositories.export_repository import ExportRepository  # noqa: E402
    from repositories.intake_repository import IntakeRepository  # noqa: E402
    from repositories.job_repository import JobRepository  # noqa: E402
    from repositories.sqlite_repository import SQLiteRepository  # noqa: E402
    from services import intake_policy  # noqa: E402
    from services.book_lookup_service import BookLookupService  # noqa: E402
    from services.candidate_ranker import CandidateRanker  # noqa: E402
    from services.clean_cover_service import CleanCoverService  # noqa: E402
    from services.confidence_service import ConfidenceService  # noqa: E402
    from services.cover_download_service import CoverDownloadService  # noqa: E402
    from services.cover_resolver_service import CoverResolverService  # noqa: E402
    from services.drive_service import DriveService  # noqa: E402
    from services.export_service import ExportService  # noqa: E402
    from services.google_sheets_service import GoogleSheetsService  # noqa: E402
    from services.image_preprocessor import ImagePreprocessor  # noqa: E402
    from services.job_service import JobService  # noqa: E402
    from services.metadata_enrichment_service import (  # noqa: E402
        MetadataEnrichmentService,
    )
    from services.metadata_service import MetadataService  # noqa: E402
    from services.metadata_validation_service import (  # noqa: E402
        MetadataValidationService,
    )
    from services.ocr_service import OCRService  # noqa: E402
    from services.text_normalizer import TextNormalizer  # noqa: E402
    from workers.export_worker import ExportWorker  # noqa: E402
    from workers.processing_worker import ProcessingWorker  # noqa: E402

    data_dir = config.data_directory
    db_path = os.path.join(data_dir, "app.db")
    jobs_db_path = os.path.join(data_dir, "jobs.db")
    metadata_db_path = os.path.join(data_dir, "metadata.db")
    downloads_dir = os.path.join(data_dir, "downloads")
    processed_dir = os.path.join(data_dir, "processed")
    covers_dir = os.path.join(data_dir, "covers")

    os.makedirs(downloads_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)
    os.makedirs(covers_dir, exist_ok=True)

    drive_service = DriveService(config)
    authenticated = drive_service.authenticate()

    if not authenticated:
        logger.error("Cannot authenticate with Google Drive. Check credentials.")
        return

    with SQLiteRepository(db_path) as file_repo:
        with JobRepository(jobs_db_path) as job_repo:
            job_service = JobService(job_repo, config=config)

            # Recover interrupted jobs from previous runs
            recovered = job_service.recover_stuck_jobs()
            logger.info("Recovered %d stuck job(s) on startup", recovered)

            # Build metadata service with configurable confidence threshold
            confidence_threshold = getattr(config, "confidence_threshold", 0.85)
            confidence_svc = ConfidenceService(
                threshold=confidence_threshold,
                manual_review_threshold=getattr(
                    config, "confidence_manual_review_threshold", 0.50
                ),
                rejected_threshold=getattr(
                    config, "confidence_rejected_threshold", 0.50
                ),
            )
            ocr_confidence_threshold = getattr(config, "ocr_confidence_threshold", 0.5)
            metadata_service = MetadataService(
                image_preprocessor=ImagePreprocessor(),
                ocr_service=OCRService(
                    languages=config.ocr_language.split(","),
                    use_gpu=getattr(config, "ocr_use_gpu", False),
                    confidence_threshold=ocr_confidence_threshold,
                ),
                text_normalizer=TextNormalizer(),
                book_lookup=BookLookupService(
                    google_books_api_key=getattr(config, "google_books_api_key", None),
                    open_library_enabled=getattr(config, "open_library_enabled", True),
                    google_books_timeout=getattr(config, "google_books_timeout", 15),
                    open_library_timeout=getattr(config, "open_library_timeout", 15),
                ),
                candidate_ranker=CandidateRanker(),
                confidence_service=confidence_svc,
                data_directory=data_dir,
            )
            # Create metadata repository and inject into the metadata service
            metadata_repo = BookMetadataRepository(metadata_db_path)
            metadata_service.set_metadata_repo(metadata_repo)

            # Initialize Milestone 5 repositories and config
            from models.export_config import ExportConfig  # noqa: E402

            export_config = ExportConfig.from_env()
            # export_queue lives in jobs.db (alongside jobs table)
            # export_records in metadata.db (alongside metadata + covers)
            export_repo = ExportRepository(jobs_db_path, metadata_db_path)
            cover_repo = CoverRepository(metadata_db_path)

            # Inject export_repo into metadata_service so it creates export
            # queue entries when metadata is persisted
            metadata_service.set_export_repo(export_repo)

            # Duplicate protection and intake rejections share jobs.db so a
            # single file backup covers all processing state.
            duplicate_repo = DuplicateRepository(jobs_db_path)
            intake_repo = IntakeRepository(jobs_db_path)
            metadata_service.set_duplicate_repo(duplicate_repo)
            max_intake_bytes = intake_policy.max_size_bytes_from_mb(
                getattr(config, "image_max_file_size_mb", 20.0)
            )

            # Start the processing worker in a background thread
            worker = ProcessingWorker(
                job_service, metadata_service, metadata_repo=metadata_repo
            )
            worker_thread = threading.Thread(
                target=worker.run,
                daemon=True,
                name="ProcessingWorker",
            )
            worker_thread.start()
            logger.info("Worker started in background thread")

            # Create export services

            book_lookup = BookLookupService(
                google_books_api_key=getattr(config, "google_books_api_key", None),
                open_library_enabled=getattr(config, "open_library_enabled", True),
                google_books_timeout=getattr(config, "google_books_timeout", 15),
                open_library_timeout=getattr(config, "open_library_timeout", 10),
                exa_api_key=getattr(config, "exa_api_key", "")
                or os.getenv("EXA_API_KEY", ""),
            )
            validation_service = MetadataValidationService(
                confidence_threshold=getattr(config, "confidence_threshold", 0.85)
            )
            cover_resolver = CoverResolverService(book_lookup_service=book_lookup)
            cover_downloader = CoverDownloadService(
                cover_repo=cover_repo,
                covers_dir=covers_dir,
                timeout=export_config.covers_timeout,
                min_width=export_config.cover_min_width,
                min_height=export_config.cover_min_height,
            )
            sheets_service = GoogleSheetsService(
                credentials_file=export_config.google_sheets_credentials,
                sheets_id=export_config.google_sheets_id,
                tab_name=export_config.google_sheets_tab_name,
            )
            enrichment_service = MetadataEnrichmentService(
                book_lookup=book_lookup,
            )
            clean_cover_service = CleanCoverService(
                book_lookup=book_lookup,
                covers_dir=os.path.join(config.data_directory, "covers"),
            )
            export_service = ExportService(
                metadata_repo=metadata_repo,
                cover_repo=cover_repo,
                export_repo=export_repo,
                validation_service=validation_service,
                cover_resolver=cover_resolver,
                cover_downloader=cover_downloader,
                sheets_service=sheets_service,
                config=export_config,
                enrichment_service=enrichment_service,
                clean_cover_service=clean_cover_service,
            )

            # Start the export worker in a separate background thread
            export_worker = ExportWorker(
                export_service=export_service,
                export_repo=export_repo,
            )
            export_worker_thread = threading.Thread(
                target=export_worker.run,
                daemon=True,
                name="ExportWorker",
            )
            export_worker_thread.start()
            logger.info("ExportWorker started in background thread")

            # Main sync loop
            try:
                while True:
                    cycle_started_iso = datetime.now(timezone.utc).isoformat()
                    metadata_service.reset_stats()
                    for counter_owner in (book_lookup, cover_downloader):
                        reset_stats = getattr(counter_owner, "reset_stats", None)
                        if callable(reset_stats):
                            reset_stats()
                    logger.info("Starting synchronization cycle")
                    try:
                        all_drive_files = drive_service.list_all_files()
                    except Exception as exc:
                        logger.error("Failed to list files from Drive: %s", exc)
                        time.sleep(30)
                        continue

                    downloaded_ids = set(file_repo.get_all_file_ids())
                    new_files = [
                        f for f in all_drive_files if f.file_id not in downloaded_ids
                    ]
                    already_known = len(all_drive_files) - len(new_files)

                    logger.info("Found %d files in Drive", len(all_drive_files))
                    logger.info("%d already downloaded", already_known)
                    logger.info("%d new files found", len(new_files))

                    # Intake gate: reject unsupported/oversized files from Drive
                    # metadata alone -- no download, no job, no retries.
                    accepted_files = []
                    rejected_this_cycle = 0
                    for drive_file in new_files:
                        decision = intake_policy.evaluate(
                            drive_file, max_size_bytes=max_intake_bytes
                        )
                        if decision.accepted:
                            accepted_files.append(drive_file)
                            continue
                        rejected_this_cycle += 1
                        first_time = intake_repo.record(
                            drive_file_id=drive_file.file_id,
                            file_name=drive_file.name,
                            reason=decision.reason or "REJECTED_UNKNOWN",
                            mime_type=decision.mime_type,
                            size_bytes=decision.size_bytes,
                            detail=decision.detail,
                        )
                        if first_time:
                            logger.warning(
                                "Rejected %s (%s): %s",
                                drive_file.name,
                                decision.reason,
                                decision.detail,
                            )

                    if accepted_files:
                        logger.info("Downloading:")
                    for drive_file in accepted_files:
                        local_filename = safe_local_filename(
                            drive_file.file_id, drive_file.name
                        )
                        local_path = os.path.join(downloads_dir, local_filename)

                        try:
                            drive_service.download_file(drive_file, local_path)
                            file_repo.insert_file(
                                drive_file=drive_file,
                                local_filename=local_filename,
                                download_time=str(int(time.time())),
                            )
                            logger.info("Saved: %s", local_path)

                            # Create a processing job for the new file
                            job = job_service.create_job_for_file(
                                drive_file, local_path
                            )
                            if job:
                                logger.info(
                                    "[Job %s] Created job for " "Drive ID %s",
                                    job.job_id,
                                    drive_file.file_id,
                                )
                            else:
                                logger.warning(
                                    "Could not create job for " "Drive ID %s",
                                    drive_file.file_id,
                                )

                        except Exception as exc:
                            logger.error(
                                "Failed to download %s: %s",
                                drive_file.name,
                                exc,
                            )

                    logger.info("Synchronization complete")
                    try:
                        stats = job_service.get_queue_status()
                    except Exception as exc:
                        logger.warning("Queue status read skipped (non-fatal): %s", exc)
                        stats = {}

                    try:
                        duplicate_counts = dict(
                            duplicate_repo.count_by_kind_since(cycle_started_iso)
                        )
                    except Exception as exc:
                        logger.warning(
                            "Duplicate counters unavailable (non-fatal): %s", exc
                        )
                        duplicate_counts = {}
                    try:
                        export_counts = export_repo.count_export_records()
                    except Exception as exc:
                        logger.warning(
                            "Export counters unavailable (non-fatal): %s", exc
                        )
                        export_counts = {}

                    log_sync_summary(
                        logger,
                        discovered=len(all_drive_files),
                        new_files=len(new_files),
                        already_known=already_known,
                        rejected_cycle=rejected_this_cycle,
                        rejected_total=intake_repo.count(),
                        duplicate_counts=duplicate_counts,
                        queue=stats,
                        ocr_calls=metadata_service.ocr_call_count,
                        metadata_lookups=getattr(book_lookup, "query_count", 0),
                        cover_downloads=getattr(cover_downloader, "download_count", 0),
                        exports=export_counts,
                    )

                    # Milestone 8.5: Periodic debug artifact cleanup
                    retention_days = getattr(config, "debug_retention_days", 3)
                    if retention_days > 0:
                        try:
                            removed = metadata_service.cleanup_old_debug_artifacts(
                                retention_days=retention_days
                            )
                            if removed > 0:
                                logger.info(
                                    "Debug cleanup: removed %d old debug dir(s)",
                                    removed,
                                )
                        except Exception as exc:
                            logger.warning("Debug cleanup failed (non-fatal): %s", exc)

                    time.sleep(30)

            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt -- shutting down")
            finally:
                # Stop the workers, join their threads, then clean up repositories
                worker.shutdown()
                worker_thread.join(timeout=10)
                export_worker.shutdown()
                export_worker_thread.join(timeout=10)
                duplicate_repo.close()
                intake_repo.close()
                metadata_repo.close()
                export_repo.close()
                cover_repo.close()
                import gc  # noqa: E402

                gc.collect()  # Ensure SQLite file handles are released
                logger.info("Workers stopped")


def print_status_report(config: Config, logger) -> None:
    """Print a comprehensive application status report.

    Shows configuration, repository stats, and export queue status.
    """
    from repositories.book_metadata_repository import (
        BookMetadataRepository,
    )  # noqa: E402
    from repositories.export_repository import ExportRepository  # noqa: E402
    from repositories.job_repository import JobRepository  # noqa: E402
    from repositories.sqlite_repository import SQLiteRepository  # noqa: E402
    from services.job_service import JobService  # noqa: E402

    data_dir = config.data_directory
    db_path = os.path.join(data_dir, "app.db")
    jobs_db_path = os.path.join(data_dir, "jobs.db")
    metadata_db_path = os.path.join(data_dir, "metadata.db")
    downloads_dir = os.path.join(data_dir, "downloads")
    processed_dir = os.path.join(data_dir, "processed")
    covers_dir = os.path.join(data_dir, "covers")
    debug_dir = os.path.join(data_dir, "debug")

    print("\n" + "=" * 40)
    print("Book Processor Status Report")
    print(f"  Version:     {VERSION}")
    print(f"  Environment: {config.environment}")
    print(f"  Data Dir:    {data_dir}")
    print("=" * 40)

    # Config status
    report = config.get_status_report()
    print("\nConfiguration:\n")
    if report["present"]:
        print("  Present:")
        for item in report["present"]:
            print(f"    {item}")
    if report["missing"]:
        print("  Missing:")
        for item in report["missing"]:
            print(f"    {item}")
    if report["future"]:
        print("  Future:")
        for item in report["future"]:
            print(f"    {item}")
    print()

    # Repository stats
    print("Repositories:\n")
    with SQLiteRepository(db_path) as file_repo:
        file_count = file_repo.count_files()
        print(f"  Tracked Drive files: {file_count}")

    with JobRepository(jobs_db_path) as job_repo:
        job_service = JobService(job_repo)
        job_status = job_service.get_queue_status()
        print(
            f"  Jobs: pending={job_status['pending']}, "
            f"processing={job_status['processing']}, "
            f"completed={job_status['completed']}, "
            f"failed={job_status['failed']}, "
            f"retrying={job_status['retrying']}, "
            f"manual_review={job_status['manual_review']}"
        )

    with BookMetadataRepository(metadata_db_path) as metadata_repo:
        metadata_count = metadata_repo.count_metadata()
        print(f"  Identified books: {metadata_count}")

    export_repo = ExportRepository(jobs_db_path, metadata_db_path)
    pending_exports = export_repo.get_pending_exports()
    retrying_exports = export_repo.get_retrying_exports()
    dead_letters = export_repo.get_dead_letter_exports()
    print(
        f"  Export queue: pending={len(pending_exports)}, "
        f"retrying={len(retrying_exports)}, "
        f"dead_letter={len(dead_letters)}"
    )
    export_repo.close()

    # Directory contents
    print("\nDirectories:\n")
    for name, path in [
        ("Downloads", downloads_dir),
        ("Processed", processed_dir),
        ("Covers", covers_dir),
        ("Debug", debug_dir),
    ]:
        if os.path.isdir(path):
            count = sum(1 for _ in os.scandir(path)) if os.path.isdir(path) else 0
            print(f"  {name}: {path} ({count} items)")
        else:
            print(f"  {name}: {path} (does not exist)")

    # Dead letter details
    if dead_letters:
        print("\nDead-Letter Exports (failed, retries exhausted):\n")
        for dl in dead_letters[:10]:
            print(
                f"  drive_file_id={dl['drive_file_id']}, "
                f"retry_count={dl.get('retry_count', 0)}, "
                f"last_error={dl.get('last_error', '')[:80]}"
            )
        if len(dead_letters) > 10:
            print(f"  ... and {len(dead_letters) - 10} more")

    print()


def run_requeue_incomplete(config: Config, logger) -> None:
    """Requeue exports that finished without a validated cover or identifier.

    The export worker only polls ``pending``/``retrying`` rows, so a book whose
    cover or ISBN never resolved at first export is never revisited. This
    command puts those records back on the queue; the worker then resumes from
    the persisted metadata and repeats only the cover lookup and sheet write —
    OCR, text extraction and enrichment are not re-run.

    Usage:
        python app.py --requeue-incomplete
    """
    from repositories.export_repository import ExportRepository  # noqa: E402

    data_dir = config.data_directory
    jobs_db_path = os.path.join(data_dir, "jobs.db")
    metadata_db_path = os.path.join(data_dir, "metadata.db")

    print("\n" + "=" * 40)
    print("Requeue Incomplete Exports")
    print("=" * 40)

    with ExportRepository(jobs_db_path, metadata_db_path) as export_repo:
        requeued = export_repo.requeue_incomplete_exports()

    if not requeued:
        print("No incomplete exports found - nothing to requeue.")
        return

    logger.info("Requeued %d incomplete export(s)", len(requeued))
    print(f"Requeued {len(requeued)} export(s) for another pass:")
    for drive_file_id in requeued:
        print(f"  {drive_file_id}")
    print(
        "\nThe export worker will resume them from persisted metadata "
        "on its next poll."
    )


def run_refresh_sheet_status(config: Config, logger) -> None:
    """Re-label the Status cell of every spreadsheet row that already exists.

    This is the completion step of the Status-column migration: it writes only
    the Status cell (RAW input) of rows it can match by title/author. It never
    appends, rewrites other cells or deletes anything.

    Usage:
        python app.py --refresh-sheet-status
    """
    from repositories.book_metadata_repository import (  # noqa: E402
        BookMetadataRepository,
    )
    from services.google_sheets_service import GoogleSheetsService  # noqa: E402

    metadata_db_path = os.path.join(config.data_directory, "metadata.db")

    print("\n" + "=" * 40)
    print("Refresh Sheet Status Labels")
    print("=" * 40)

    with BookMetadataRepository(metadata_db_path) as metadata_repo:
        books = metadata_repo.list()
    print(f"Books in local database: {len(books)}")

    sheets = GoogleSheetsService(
        credentials_file=config.google_sheets_credentials,
        sheets_id=config.google_sheet_id,
        tab_name=config.google_sheets_tab_name,
    )
    with sheets:
        counts = sheets.refresh_statuses(books)

    print(f"Rows updated:  {counts['updated']}")
    print(f"Already right: {counts['unchanged']}")
    print(f"Not in sheet:  {counts['missing']}")
    print("\nOnly Status cells were written; no rows were added or removed.")


def run_requeue_failed_jobs(config: Config, logger) -> None:
    """Requeue jobs that exhausted their retries.

    Only jobs in the ``failed`` state are touched. Each requeued job re-runs the
    normal pipeline from preprocessing, so it consumes one OCR call per photo;
    completed books, skipped duplicates and books in review are never touched.

    Usage:
        python app.py --requeue-failed-jobs
        python app.py --requeue-failed-jobs --drive-id <DRIVE_FILE_ID>
    """
    from repositories.job_repository import JobRepository  # noqa: E402
    from services.job_service import JobService  # noqa: E402

    jobs_db_path = os.path.join(config.data_directory, "jobs.db")

    drive_file_id = None
    if "--drive-id" in sys.argv:
        index = sys.argv.index("--drive-id")
        if index + 1 >= len(sys.argv) or sys.argv[index + 1].startswith("--"):
            print("Error: --drive-id requires a Google Drive file id.")
            return
        drive_file_id = sys.argv[index + 1]

    print("\n" + "=" * 40)
    print("Requeue Failed Jobs")
    print("=" * 40)

    with JobRepository(jobs_db_path) as job_repo:
        job_service = JobService(job_repo, config=config)
        candidates = [
            job
            for job in job_repo.get_failed_jobs()
            if drive_file_id is None or job.file_id == drive_file_id
        ]
        job_ids = job_service.requeue_failed_jobs(drive_file_id)

    if not job_ids:
        scope = f" for Drive ID {drive_file_id}" if drive_file_id else ""
        print(f"No failed jobs found{scope} - nothing to requeue.")
        return

    by_id = {job.job_id: job for job in candidates}
    print(f"Requeued {len(job_ids)} failed job(s):")
    for job_id in job_ids:
        job = by_id.get(job_id)
        if job is None:
            print(f"  {job_id}")
            continue
        print(f"  {job.file_name} (Drive ID {job.file_id})")
    print(
        "\nStart (or keep running) `python app.py`: the worker picks these up "
        "from the persisted downloads and re-runs OCR + lookup for each one."
    )


def run_gemini_recovery(config: Config, logger) -> None:
    """Run Gemini recovery for low-confidence books.

    Reads all rows from the Google Sheet, identifies rows with confidence
    below 0.70, downloads their Drive images, sends to Gemini for visual
    extraction, re-runs the existing MetadataService pipeline with Gemini's
    title/author, and updates existing Sheet rows.

    Usage:
        python app.py --gemini-recovery

    Gemini is only called for books with confidence < 0.70.
    Confidence thresholds are NOT lowered.
    """
    from services.drive_service import DriveService
    from services.gemini_recovery_orchestrator import GeminiRecoveryOrchestrator
    from services.gemini_recovery_service import GeminiRecoveryService
    from services.google_sheets_service import GoogleSheetsService
    from services.metadata_service import MetadataService

    if not config.gemini_recovery_enabled:
        print("Gemini recovery is not enabled (GEMINI_RECOVERY_ENABLED=false)")
        return

    if not config.gemini_api_key:
        print("Gemini API key not configured (GEMINI_API_KEY not set)")
        return

    print(f"{'=' * 40}")
    print("Gemini Low-Confidence Recovery")
    print(f"{'=' * 40}")
    print()

    # Initialize services
    sheets_service = GoogleSheetsService(
        credentials_file=config.google_sheets_credentials,
        sheets_id=config.google_sheet_id,
        tab_name=config.google_sheets_tab_name,
    )

    drive_service = DriveService(
        credentials_file=config.google_service_account_file,
        folder_id=config.google_drive_folder_id,
    )

    metadata_service = MetadataService()

    gemini_service = GeminiRecoveryService(
        api_key=config.gemini_api_key,
        daily_max_requests=config.gemini_daily_max_requests,
        rpm_limit=config.gemini_rpm_limit,
    )

    orchestrator = GeminiRecoveryOrchestrator(
        gemini_service=gemini_service,
        metadata_service=metadata_service,
        sheets_service=sheets_service,
        drive_service=drive_service,
    )

    print("Reading rows from Google Sheet...")
    stats = orchestrator.run()

    print()
    print(f"Total Sheet rows:      {stats.total_rows}")
    print(f"Low-confidence rows:    {stats.low_confidence_rows}")
    print(f"Processed:              {stats.processed}")
    print(f"Gemini calls:           {stats.gemini_calls}")
    print(f"Gemini successes:       {stats.gemini_successes}")
    print(f"Successfully re-identified: {stats.re_identified}")
    print(f"Still unresolved:       {stats.unresolved}")
    print(f"Retries:                {stats.retries}")
    print(f"API errors:             {len(stats.api_errors)}")
    if stats.api_errors:
        print(f"  Errors: {stats.api_errors[:5]}")
    print()
    print("Recovery complete.")


def main() -> None:
    """Application entry point.

    Usage:
        python app.py                      # Run the sync engine
        python app.py --status             # Print status report and exit
        python app.py --requeue-incomplete # Requeue unresolved exports and exit
        python app.py --requeue-failed-jobs # Requeue jobs out of retries and exit
        python app.py --refresh-sheet-status # Re-label existing Sheet rows and exit
    """
    config = Config.from_env()
    config.validate()
    logger = setup_logger(APP_NAME)

    # Check for --status flag
    if "--status" in sys.argv or "-s" in sys.argv:
        print_status_report(config, logger)
        return

    # Check for --requeue-incomplete flag
    if "--requeue-incomplete" in sys.argv:
        run_requeue_incomplete(config, logger)
        return

    # Check for --refresh-sheet-status flag
    if "--refresh-sheet-status" in sys.argv:
        run_refresh_sheet_status(config, logger)
        return

    # Check for --requeue-failed-jobs flag
    if "--requeue-failed-jobs" in sys.argv:
        run_requeue_failed_jobs(config, logger)
        return

    # Check for --gemini-recovery flag
    if "--gemini-recovery" in sys.argv:
        run_gemini_recovery(config, logger)
        return

    print("=" * 33)
    print(f"{APP_NAME} Started")
    print(f"Version: {VERSION}")
    print(f"Environment: {config.environment}")
    print("=" * 33)
    print()
    print("Configuration loaded successfully.")
    print()
    print("Logger initialized.")
    print()
    print("Application ready.")
    print()
    print("Waiting for services...")
    print()
    print("No errors.")
    print()

    report = config.get_status_report()
    print_startup_report(report)

    logger.info("Application starting")
    run_sync_engine(config, logger)


if __name__ == "__main__":
    main()
