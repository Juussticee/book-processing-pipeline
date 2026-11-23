# CURRENT PROJECT STATUS REPORT — Book Processor

Project Path: C:\Users/hashe\Documents\Book Auth\
Last Verified: 2026-08-09

---

## A. CURRENT ARCHITECTURE

The project is a single-process Python application (`app.py`) that runs a
continuous synchronization loop. It is NOT a multi-service or distributed
system — everything runs in-process with background daemon threads.

### Components and Data Flow

```
Google Drive API
    |
    v
DriveService (list_all_files, download_file)
    |
    v
SQLiteRepository (app.db: files table)  <-- tracks what's already downloaded
    |
    v
JobService -> JobRepository (jobs.db: jobs table)
    |          - Partial unique index: one active job per Drive File ID
    |          - Status: pending -> processing -> completed/failed/manual_review
    |
    v
ProcessingWorker (background daemon thread, polls every 5s)
    |
    v
MetadataService.identify_book(image_path)
    |
    +---> ImagePreprocessor: load, EXIF auto-rotate, resize, grayscale, denoise
    +---> OCRService: EasyOCR (lazy import), paragraph=False, returns (bbox, text, conf)
    +---> TextNormalizer: NFKC, smart quotes, OCR char fixes, ISBN extraction (checksum validated)
    +---> BookTextExtractor: structured field extraction (title, author, ISBN, publisher)
    +---> BookLookupService: multi-source search
    |       1. Google Books API (by ISBN, then title+author)
    |       2. Open Library (fallback, by ISBN then title+author)
    +---> CandidateRanker: RapidFuzz token_sort_ratio for title/author/publisher
    |       isbn_state: MATCH / CONFLICT / UNAVAILABLE
    +---> ConfidenceService: adaptive weighted scoring
    |       ISBN 50% + Title 25% + Author 15% + Publisher 5% + OCR 5% + Source bonus 5%
    |       >= 0.85: identified (auto-accept)
    |       0.50-0.85: needs_review (manual review)
    |       < 0.50: rejected
    +---> _validate_overlap: title/author overlap check (rejects false positives)
    +---> _persist_metadata: BookMetadataRepository.upsert_metadata (metadata.db)
    |       Also creates export queue entry (Milestone 5)
    +---> _save_debug_artifacts: writes OCR text, candidate results, final decision
    |
    v
JobService.complete_job() or set_manual_review()
    |
    v
ExportWorker (background daemon thread, polls every 5s)
    |
    v
ExportService.process_export(drive_file_id, job_id)
    |
    +---> MetadataValidationService: validate confidence/presence thresholds
    +---> CoverResolverService: priority-ordered URL list
    |       1. metadata.cover_url
    |       2. metadata.image_links (extraLarge -> small)
    |       3. metadata.thumbnail_url
    |       4. ISBN-based Google Books / Open Library search
    |       5. Title+author Open Library search
    |       6. Direct Open Library ISBN URL (-L, -M, default)
    +---> CoverDownloadService: try each URL, validate dimensions/format
    |       Uses PIL to verify image is not a placeholder (1x1, tiny, invalid)
    |       Persists to cover_downloads table (metadata.db)
    +---> GoogleSheetsService: get_or_create_row (update or insert by Drive File ID)
    +---> ExportRepository: update_export_status + update_export_record

### Databases (3 SQLite files in data/)

1. **app.db** — SQLiteRepository: `files` table (Drive file tracking)
2. **jobs.db** — JobRepository: `jobs` table (+ `export_queue` table for Milestone 5)
3. **metadata.db** — BookMetadataRepository: `book_metadata` table
              — CoverRepository: `cover_downloads` table
              — ExportRepository: `export_records` table

### Key Design Decisions Verified in Code

- **isbn_state UNAVAILABLE is neutral** — when OCR has ISBN but candidate
  doesn't, it's treated as MATCH (not penalized). This is the fix that resolved
  the "low confidence problem" for "Le cœur vert". Verified in
  `candidate_ranker.py:128-133`.
- **Confidence is normalized over available evidence** — missing fields don't
  cap the max score. Verified in `confidence_service.py:138-163`.
- **Title+Author minimum** — confidence capped at 0.60 if either is missing.
  Verified in `confidence_service.py:166-172`.
- **Dual-database export** — `export_queue` in jobs.db, `export_records` in
  metadata.db, with proper cross-db sync in `update_export_status`.
  Verified in `repositories/export_repository.py:103-108`.
- **Graceful degradation** — cover download failure doesn't block export;
  export proceeds without cover, logs warning. Verified in
  `export_service.py:120-133`.

---

## B. COMPLETED MILEONES (VERIFIED AGAINST CODE)

### Milestone 1: Project Scaffolding + Drive Sync
- **What it was supposed to implement**: Basic project structure, config loading,
  Drive sync to download new files.
- **What is actually implemented**: `core/config.py` (Config dataclass with from_env),
  `core/constants.py`, `core/logger.py`, `services/drive_service.py`,
  `repositories/sqlite_repository.py`, `models/drive_file.py`, `app.py` with
  sync loop.
- **Evidence**: Tests at `tests/unit/test_config.py`, `tests/unit/test_drive_service.py`,
  `tests/unit/test_sqlite_repository.py`, `tests/integration/test_drive_sync.py`.
- **Status: COMPLETE**

### Milestone 2: Job Queue System (placeholder processing)
- **What it was supposed to implement**: SQLite-backed job queue with worker
  thread, retry logic, crash recovery, duplicate prevention.
- **What is actually implemented**: `models/processing_job.py` (JobStatus enum:
  PENDING, PROCESSING, COMPLETED, FAILED, RETRYING, CANCELLED, MANUAL_REVIEW),
  `repositories/job_repository.py` (partial unique index, status transitions),
  `services/job_service.py` (MAX_RETRIES=3, recovery, queue status),
  `workers/base_worker.py` (polling loop, health check, graceful shutdown),
  `workers/processing_worker.py` (initial placeholder).
- **Evidence**: `tests/unit/test_job_repository.py` (23 tests),
  `tests/unit/test_job_service.py` (16 tests),
  `tests/unit/test_processing_worker.py` (15 tests),
  `tests/integration/test_processing_pipeline.py` (7 scenarios).
  Documented in `docs/milestone-3-report.md` (covers both M2 and M3).
- **Status: COMPLETE**

### Milestone 3: Job Queue + Processing Worker (integration)
- **What it was supposed to implement**: Full integration of job queue into
  the application startup sequence, worker thread management, sync engine loop.
- **What is actually implemented**: `app.py` lines 194-302: worker thread
  started, sync loop with 30s cycle, job creation on new files, logging of
  queue statistics. Graceful shutdown in finally block with thread joins.
- **Evidence**: `tests/integration/test_processing_pipeline.py` (5 scenarios).
  REFACTOR-LOG.md Phase 1 mentions Milestone 3 completion.
- **Status: COMPLETE** (covered by the milestone-3-report.md documentation
  above)

### Milestone 4: Book Identification Pipeline (OCR + Lookup + Confidence)
- **What it was supposed to implement**: Real OCR via EasyOCR, multi-source
  book lookup (Google Books + Open Library), candidate ranking with RapidFuzz,
  confidence scoring, title/author overlap validation, MANUAL_REVIEW status,
  debug artifacts persistence.
- **What is actually implemented**:
  - `services/image_preprocessor.py` — PIL-based preprocessing (auto-rotate,
    resize, grayscale, contrast, denoise). SUPPORTED_EXTENSIONS filter.
  - `services/ocr_service.py` — EasyOCR wrapper. **paragraph=False** fix applied
    (refactor-log line 33: "Fixed paragraph=True to paragraph=False").
    Lazy import guard. Confidence filtering.
  - `services/text_normalizer.py` — NFKC normalization, smart quote/dash fixes,
    OCR char corrections, ISBN extraction with checksum validation (both
    ISBN-10 and ISBN-13).
  - `services/book_text_extractor.py` — Rule-based structured field extraction
    (title from first line, author from last lines, ISBN regex, publisher).
  - `services/book_lookup_service.py` — Google Books (primary) + Open Library
    (fallback). Parses both APIs into BookMetadata objects. Extracts all
    imageLinks sizes into `image_links` dict.
  - `services/candidate_ranker.py` — RapidFuzz token_sort_ratio scoring.
    **isbn_state fix applied** (lines 128-133): OCR-has-ISBN-but-candidate-doesn't
    is MATCH, not CONFLICT.
  - `services/confidence_service.py` — Adaptive scoring. ISBN 50%, Title 25%,
    Author 15%, Publisher 5%, OCR 5%, Source bonus 5%. Normalized over
    available evidence. Title+Author minimum (cap 0.60 if either missing).
    >=0.85 auto-accept, 0.50-0.85 manual review, <0.50 rejected.
  - `services/metadata_service.py` — Full orchestration. Multi-source candidate
    generation (ISBN → title+author → title → author). Overlap validation
    (rejects false positives). Debug artifacts (ocr_raw.txt, ocr_processed.txt,
    candidate_results.json, final_decision.json).
- **Evidence**: 83 new tests added (text_normalizer: 16, ocr_service: 13,
  image_preprocessor: 13, book_lookup_service: 12, candidate_ranker: 12,
  confidence_service: 14, metadata_service: 8, processing_pipeline: 7).
  Live test: "Le cœur vert" by Jean-Louis Kuffer identified with confidence=0.991.
- **Status: COMPLETE** — All components present and verified. Live test succeeded.
  Quality gates: black, ruff, mypy all clean. 189 tests pass (106 + 83).

### Milestone 5: Export Pipeline (Cover Resolution + Google Sheets)
- **What it was supposed to implement**: Cover URL resolution, cover download
  with validation, Google Sheets export, export queue with retry.
- **What is actually implemented**:
  - `services/cover_resolver_service.py` — 7-level priority URL resolution
    (cover_url → image_links → thumbnail → ISBN search → title+author search
    → OL direct ISBN → OL title URL). Returns ALL candidates for downloader.
  - `services/cover_download_service.py` — Downloads images, validates
    dimensions (>= 200x200), format check via PIL. Persists to cover_downloads
    table. Handles "all URLs exhausted" gracefully.
  - `services/export_service.py` — Full pipeline orchestration: validate →
    resolve → download → sheets export. Continues even if cover fails.
  - `services/google_sheets_service.py` — gspread integration. get_or_create_row
    (matches by Drive File ID column). 14-column export with proper mapping.
    Credentials never logged.
  - `models/export_config.py` — ExportConfig dataclass from env.
  - `models/export_record.py` — ExportRecord + ExportStatus enum.
  - `repositories/export_repository.py` — **Full rewrite** (documented in
    refactor-log Phase 2). Dual-database support: export_queue in jobs.db,
    export_records in metadata.db. Cross-db sync in update_export_status.
  - `repositories/cover_repository.py` — CoverDownload tracking with checksum
    deduplication.
  - `services/metadata_validation_service.py` — Validates metadata quality
    before export.
- **Evidence**: Live test on 2026-08-07: Dune book identified, cover download
  failed (1x1 placeholder), export succeeded to Sheets row 3.
  `tests/integration/test_export_pipeline.py` (15 tests). Live test on
  2026-08-09: "Le cœur vert" — identification succeeded, cover download
  failed (43-byte GIF placeholder), metadata persisted with status=identified.
- **Status: COMPLETE** — All components present, live-tested. The cover download
  failure is a data issue (Open Library returns placeholders for this book),
  not a code bug. Pipeline handles it gracefully.

### Milestone 6: Cover Resolution, Validation & Final Metadata Enrichment
- **What it was supposed to implement**: Multi-size image_links extraction
  from Google Books and Open Library, priority-based cover resolution,
  image quality validation (dimensions, format), metadata enrichment
  (image_links, cover_url storage).
- **What is actually implemented**:
  - `models/book_metadata.py` — Added `image_links: dict` field (line 132
    of refactor-log).
  - `repositories/book_metadata_repository.py` — Added `image_links TEXT`
    column, `_parse_json` helper. Migration in `_migrate_schema()`.
  - `services/book_lookup_service.py` — `_parse_google_books_item` extracts
    ALL imageLinks (extraLarge, large, medium, small, thumbnail, smallThumbnail).
    `_parse_openlibrary_doc` constructs multi-size URLs (-L, -M, -S).
  - `services/cover_resolver_service.py` — Priority cascade: cover_url →
    image_links (extraLarge→small, thumbnail, smallThumbnail) → thumbnail_url
    → ISBN-based search → title+author search → OL direct ISBN URLs →
    OL title URL. All http→https normalized. Deduplication.
- **Evidence**: Live test on 2026-08-09 verified: BookLookupService found 1
  candidate via Open Library, image_links populated with 3 sizes. CoverResolver
  generated 4 candidate URLs. All rejected (placeholder images).
  `tests/unit/test_cover_resolver_service.py` — 21 tests.
  `tests/unit/test_cover_download_service.py` — 5 new tests (network failure,
  empty response, oversized rejection, invalid format rejection, failed record
  persistence).
  `tests/integration/test_export_pipeline.py` — 5 new tests including
  `test_image_links_stored_in_db`, `test_cover_download_failure_marks_cover_failed_not_metadata`.
- **Status: COMPLETE** — All features implemented and live-tested.

### Milestone 7: Production Batch Workflow, Manual Review & Reliability
- **What it was supposed to implement**: Batch processing reliability,
  manual review metadata storage, processing statistics logging, MANUAL_REVIEW
  duplicate prevention fix.
- **What is actually implemented**:
  - `repositories/job_repository.py` — Added `metadata_json TEXT` column to
    `jobs` table schema + ALTER TABLE migration. `set_manual_review()` now
    serializes full metadata (candidates, scores, confidence, OCR, rejection
    reason) to metadata_json. Added `get_manual_review_metadata()` to
    retrieve. Added `get_queue_status()` returning counts per JobStatus.
  - `workers/processing_worker.py` — `_build_candidate_summary()` includes
    full evidence: ocr_text, extracted fields, match_score, confidence,
    isbn_state, rejection_reason, source, candidates_count, candidates list
    with per-field scores. `process()` populates review_metadata.
  - `app.py` — Lines 316-325: Processing statistics logging at end of each
    sync cycle (pending, processing, completed, failed, retrying,
    manual_review, cancelled counts).
  - **Bug fix**: `services/job_service.py:59-63` — Added
    `JobStatus.MANUAL_REVIEW` to active-status tuple in `create_job_for_file`
    to prevent duplicate jobs for files in manual_review state.
- **Evidence**: `tests/unit/test_milestone7_batch.py` — 23 tests covering
  FIFO, thread safety, duplicate prevention, processing states, restart
  recovery, failure isolation.
  `tests/unit/test_processing_worker.py` — Added
  `test_process_manual_review_stores_full_metadata`.
- **Status: COMPLETE** — 428 tests pass. Quality gates: ruff ✓, black ✓,
  mypy ✓ (2 pre-existing test-only errors unchanged). REFACTOR-LOG.md
  confirms completion.

---

## C. LIVE PIPELINE (CURRENT BEHAVIOR)

### What happens when a book image is placed in the Drive folder:

1. **Sync Engine** (`app.py:256-326`): Every 30 seconds, lists all files in
   the Drive folder via `DriveService.list_all_files()`.

2. **New File Detection**: Compares Drive file IDs against `SQLiteRepository.get_all_file_ids()`.
   Files not in `app.db` are "new".

3. **Download**: Each new file is downloaded to `data/downloads/{sanitized_id}.{ext}`
   via `DriveService.download_file()`.

4. **File Tracking**: `SQLiteRepository.insert_file()` records the file in
   `app.db` (files table) with Drive file ID, name, download time.

5. **Job Creation**: `JobService.create_job_for_file()` checks for existing
   active job (any status except completed/failed/cancelled). If none exists,
   creates a job in `jobs.db` with a UUID job_id, status=PENDING.

6. **Processing**: `ProcessingWorker` (daemon thread, polls every 5s) picks up
   the PENDING job, marks it PROCESSING, calls `MetadataService.identify_book()`:
   a. Preprocess image (load → EXIF auto-rotate → resize if >1600px → grayscale → contrast → denoise → temp file)
   b. OCR via EasyOCR (languages from config, GPU if enabled)
   c. Normalize text (NFKC, smart quotes, OCR char fixes)
   d. Extract ISBN(s) with checksum validation
   e. Extract structured fields (title from first line, author from last lines)
   f. Multi-source book lookup: ISBN → Google Books → Open Library fallback → title+author → title → author
   g. Rank candidates with RapidFuzz (title, author, ISBN, publisher similarity)
   h. Calculate confidence (weighted + adaptive normalization)
   i. Title/author overlap validation (rejects false positives <0.3 title sim or <0.2 author sim)
   j. Persist metadata to `metadata.db` (book_metadata table, upsert by drive_file_id)
   k. Create export queue entry in `jobs.db` (export_queue table)
   l. Save debug artifacts to `data/debug/{job_id}/`
   m. Returns: Best metadata if confidence >= 0.85 (auto-accept)
      — None if confidence 0.50-0.85 (manual_review, still persisted)
      — None if confidence < 0.50 (rejected, still persisted)

7. **Job Completion**: `JobService.complete_job()` marks the job COMPLETED.
   If confidence was below threshold, `JobService.set_manual_review()` marks
   it MANUAL_REVIEW with full metadata stored in `metadata_json` column.

8. **Export**: `ExportWorker` (separate daemon thread, polls every 5s) picks up
   the export queue entry, calls `ExportService.process_export()`:
   a. Loads metadata from `metadata.db`
   b. Validates (MetadataValidationService)
   c. Resolves cover URLs (CoverResolverService — 7-level priority)
   d. Downloads cover (CoverDownloadService — tries each URL, validates dimensions >= 200px)
   e. Exports to Google Sheets (GoogleSheetsService — get_or_create_row)
   f. Updates export_records table
   g. Increments retry_count on failure, marks FAILED after max_retries

9. **Logging**: Throughout the cycle, statistics are logged (pending,
   processing, completed, failed, retrying, manual_review, cancelled counts).

### Verified Live Test (last known):

- Drive file: "WhatsApp Image 2026-07-31 at 6.31.50 PM.jpeg"
- OCR: Title="Le Cœur Vert", Author="Jean-Louis Kuffer", OCR confidence=0.9194
- Identification: confidence=0.991, status="identified"
- Export: Succeeded (metadata written to Google Sheets)
- Cover: Failed (placeholder images from external APIs) — handled gracefully

---

## D. CURRENT LIMITATIONS

### 1. Single-File-Per-Run Processing
The ProcessingWorker processes only `pending_jobs[0]` (FIFO, one job at a time).
There is no parallel processing. Multiple worker threads could be spawned but
the current `app.py` only starts one.

**Impact**: Throughput is limited. If you drop 100 book images in Drive, each
30-second sync cycle downloads new files and creates jobs, but processing happens
one-at-a-time with a 5-second poll interval between jobs.

### 2. Hardcoded Confidence Thresholds
`ConfidenceService.DEFAULT_THRESHOLD = 0.85` and `MANUAL_REVIEW_THRESHOLD = 0.50`
are hardcoded constants. The `Config` object reads `confidence_threshold` from
env (default 0.85) and passes it to `ConfidenceService(threshold=...)`, but
`MANUAL_REVIEW_THRESHOLD` and `REJECTED_THRESHOLD` are not configurable.

**Impact**: Cannot tune the review/reject boundary without code changes.

### 3. Source Priority is Hardcoded (Not Configurable)
The multi-source lookup order in `MetadataService.identify_book()` is fixed:
ISBN → title+author → title → author. The thresholds for when each source
fires (e.g., "only search title if no ISBN candidates") are baked into the
control flow.

**Impact**: Cannot prioritize Open Library over Google Books, or adjust fallback
behavior, without code changes.

### 4. No Dead Letter Queue for Permanently Failed Jobs
When a job exhausts all retries (MAX_RETRIES=3), it's marked FAILED in the
database. There is no alerting, no dashboard, no automatic notification.
The job stays in the database indefinitely.

**Impact**: Failed jobs require manual database inspection to identify.

### 5. Export Worker Retry Delay is Hardcoded
`BaseWorker.POLL_INTERVAL = 5` is the same for both ProcessingWorker and
ExportWorker. There's no configurable backoff. The `ExportConfig` has
`export_retry_delay` (default 60s) but `BaseWorker` doesn't use it — it
always polls every 5 seconds regardless.

**Impact**: Failed exports retry aggressively (every 5s, up to max_retries=3).
This could strain external APIs.

### 6. Google Books API Key Not Required (Degraded Mode)
`GOOGLE_BOOKS_API_KEY` is optional in config. Without it, the Google Books API
may return limited results, and the pipeline falls back to Open Library
immediately. This is "graceful degradation" but means the primary source is
effectively bypassed.

**Impact**: May miss books that are only in Google Books, not Open Library.

### 7. No Cover Image Enrichment for Manual Review
When identification succeeds (confidence >= 0.85) but cover download fails,
the metadata is still persisted with `cover_url=None`. The export proceeds
without a cover. There is no retry mechanism specifically for cover downloads
that is separate from the export retry.

**Impact**: Books identified correctly but without covers in the spreadsheet.
The export is marked COMPLETED even though the cover is missing.

### 8. No Image Format Validation Before OCR
`ImagePreprocessor.preprocess()` checks the file extension
(SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}) but does not
validate the actual image content. A corrupted file with a .jpg extension
would crash EasyOCR.

**Impact**: A single corrupt image can crash the worker thread, though
`_process_job` catches exceptions and marks the job as FAILED with retry.

### 9. Debug Artifacts Never Cleaned Up
`_save_debug_artifacts()` writes to `data/debug/{job_id}/` indefinitely.
There is no cleanup policy or rotation.

**Impact**: Disk space grows unbounded over time for long-running deployments.

### 10. No Duplicate Metadata Prevention Across Different Files
If the same book is photographed twice and uploaded as two separate Drive
files, the system creates two separate `book_metadata` rows (one per
`drive_file_id`) with no deduplication. There is no "canonical book"
concept.

**Impact**: Duplicate entries in the spreadsheet for the same physical book.

---

## E. NEXT MILESTONE

### Milestone 8: Batch Processing Efficiency & Observability

#### Objective
Address the throughput limitation (M2: single-job-at-a-time processing) and
the observability gap (M3-M7: no monitoring dashboard, no dead letter queue).
Add parallel processing capacity and basic operational visibility.

#### Rationale
This is the natural next step because:
1. The single-worker model (M2) is the primary throughput bottleneck — the
   system can only process one book at a time, polling every 5 seconds.
2. Failed jobs accumulate in the database with no visibility (M5-M6 retry
   logic exists but is invisible to operators).
3. The confidence thresholds, source priority, and retry delays are hardcoded
   and should be made configurable for tuning.
4. No new external API dependencies or model downloads are needed — this
   milestone is purely architectural and operational.

#### Features
1. **Parallel ProcessingWorker**: Support spawning N worker threads (configurable
   via `PROCESSING_WORKER_COUNT` env var, default 1). Each worker pulls from
   the same FIFO queue (job_repository.get_pending_jobs() already returns
   multiple jobs). Existing tests with single worker should still pass;
   new concurrency tests verify thread safety.

2. **Configurable Retry/Backoff**: Use `ExportConfig.export_retry_delay` and
   a new `PROCESSING_POLL_INTERVAL` env var for worker poll intervals.
   Implement exponential backoff for failed exports (delay grows per retry).

3. **Dead Letter Queue Visibility**: Add `ExportRepository.get_dead_letter_exports()`
   that returns all FAILED export records with their last_error and retry_count.
   Add a `/health` or status endpoint (or CLI command) that reports queue
   status including dead-letter counts.

4. **Configurable Thresholds**: Move `ConfidenceService.MANUAL_REVIEW_THRESHOLD`
   and `REJECTED_THRESHOLD` to config (env vars: `CONFIDENCE_MANUAL_REVIEW_THRESHOLD`,
   `CONFIDENCE_REJECTED_THRESHOLD`).

5. **Processing Statistics Dashboard** (CLI): Add a `python app.py --status`
   mode that prints the current queue status (pending, processing, completed,
   failed, manual_review, retrying) and dead-letter count. Reuses existing
   `JobService.get_queue_status()` and the new dead-letter query.

#### Files/Components Likely Involved
- `app.py` — Add worker thread pool, `--status` CLI mode, configurable poll interval
- `workers/processing_worker.py` — Already supports `poll_interval` via `BaseWorker`
  `POLL_INTERVAL`; extend `BaseWorker.__init__` to accept `poll_interval` param
- `workers/base_worker.py` — Add `poll_interval` parameter
- `workers/export_worker.py` — Add exponential backoff, use configurable delay
- `core/config.py` — Add `PROCESSING_WORKER_COUNT`, `PROCESSING_POLL_INTERVAL`,
  `CONFIDENCE_MANUAL_REVIEW_THRESHOLD`, `CONFIDENCE_REJECTED_THRESHOLD`
- `services/confidence_service.py` — Accept manual_review/rejected thresholds
  in constructor
- `repositories/export_repository.py` — Add `get_dead_letter_exports()` method
- `workers/export_worker.py` — Exponential backoff between retries

#### Tests Needed
- Unit tests for parallel worker pool (thread safety, job distribution)
- Unit tests for `get_dead_letter_exports()` (filter by FAILED status)
- Unit tests for exponential backoff calculation
- Unit tests for configurable threshold injection
- Integration test: 5 jobs dispatched, 2 workers process them concurrently
  (verify no duplicate processing, no race conditions)

#### Live Verification Needed
- Start app with `PROCESSING_WORKER_COUNT=2`, place 3 images in Drive folder
- Verify all 3 jobs are picked up concurrently (log timestamps within 5s of each
  other, vs sequential which would be ~10s apart)
- Verify no duplicate metadata rows, no race conditions on export queue entries
- Verify `python app.py --status` prints correct queue counts

#### Expected Result
- Test count increases by ~15-20 new tests (428 → ~445)
- Quality gates: ruff ✓, black ✓, mypy ✓
- Live test: Multi-worker processing confirmed, status CLI works
- Throughput: 2x baseline with 2 workers (linear scaling expected up to ~4)

### Alternative: Milestone 8 (Metadata Quality Gate)

An alternative would be to add a **metadata quality pre-filter** before book lookup:
if OCR extracts no ISBN, no title, and no author, skip the API lookup entirely
and mark the job as FAILED early. This saves API calls and bandwidth for images
that are clearly not book covers (e.g., photos of cats).

This would be bandwidth-friendly (<100 MB, no new downloads) but is lower
priority — the current system already handles this gracefully (just wastes
an API call or two).

---

## F. BANDWIDTH PLAN

### Current environment state
- Python venv at `.venv/` (already created, all deps installed)
- EasyOCR models cached (already downloaded in previous milestones)
- No Docker images needed
- SQLite databases local to project

### Milestone 8 (Parallel Processing & Observability) — Network Usage

| Operation | Estimated Download |
|---|---|
| No new dependencies | 0 MB |
| No new model downloads | 0 MB |
| No new API calls during implementation | 0 MB |
| Tests use isolated temp SQLite DBs + mocks | 0 MB |
| Live test reuses existing test image ("WhatsApp Image...") | ~0 MB (already on disk) |
| **TOTAL** | **0 MB** |

### Implementation approach
- All changes are Python code changes — no new packages added
- Tests use `tmp_path` fixtures and mock external services (existing pattern)
- No Docker, no model downloads, no dataset downloads
- Live test uses the existing cached test image in `data/downloads/`

### Risk assessment
**Safe** — zero network usage for implementation and testing. The only
network touchpoints are the existing Google Drive/Drive API calls (unchanged
from current state) and the existing Google Books/Open Library API calls
(already cached or mocked in tests).

---

*Report generated from direct code inspection, not from summary documents.*
*Key files verified: app.py (377 lines), metadata_service.py (531 lines),
candidate_ranker.py (189 lines), confidence_service.py (307 lines),
export_repository.py (556 lines), export_service.py (253 lines),
cover_resolver_service.py (239 lines), job_repository.py (446 lines).*
