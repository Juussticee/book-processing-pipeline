# Architecture

## Overview

Book Processor follows a clean architecture with the following layers:

- core/ - Shared foundation: configuration, constants, exceptions, logging
- services/ - Business logic services
- processors/ - Data processing pipelines
- repositories/ - Data access layer
- models/ - Domain models and data structures
- utils/ - Utility helpers (logger, etc.)

## Configuration

Configuration is loaded from .env using python-dotenv and managed via a frozen dataclass in core/config.py. Environment variables override defaults defined in core/constants.py.

The Config class provides:
- from_env() - Build configuration from environment variables
- validate() - Check that required fields are present, raise ConfigurationError if missing
- get_status_report() - Return a structured report of what is configured and what is missing

## Logging

Logging is set up in core/logger.py using Python standard logging module. It supports:
- Console output
- Rotating file output (5 MB max, 3 backups)
- Timestamps and log levels

## Validation

At startup, app.py calls config.validate(). If required configuration is missing, a human-readable error is printed (no Python tracebacks) and the application exits with code 1. The error message points to docs/environment.md for guidance.

## Startup Report

After successful validation, the application prints a configuration summary showing:
- Present items (Environment, Logging, Paths, Google Credentials if file exists)
- Missing items (Google Drive Folder ID, Google Sheet ID, etc.)
- Future features (OCR, Google Books)

Secrets and API keys are never printed - only their presence/absence is indicated.

## Testing

Tests live under tests/ with subdirectories for unit, integration, and e2e tests. Pytest is used as the test runner.

## Google Drive Sync Architecture (Milestone 2)

The Google Drive File Synchronization service detects new files in a monitored Google Drive folder, downloads each file once, and tracks it in SQLite to avoid reprocessing.

### Components

- `services/drive_service.py` - Drives the sync cycle: lists files in the monitored folder, downloads new files, and delegates storage to the repository.
- `repositories/sqlite_repository.py` - Persists file records keyed by `drive_file_id`. Files are tracked by their Drive ID (not filename) so renames are handled correctly.
- `models/drive_file.py` - Frozen dataclass representing a Google Drive file with `file_id`, `name`, `mime_type`, `modified_time`, and `size`.
- `app.py` - Contains the sync engine loop (`run_sync_engine`) that calls `DriveService.list_files()` on a configurable interval (default 30 seconds).

### Sync Flow

1. `DriveService.list_files()` calls the Google Drive API `files.list` endpoint.
2. New files (not in the SQLite DB) are downloaded to `data/downloads/<drive_file_id>`.
3. Each downloaded file is recorded in SQLite via `SQLiteRepository.add_file()`.
4. On subsequent sync cycles, already-tracked files are skipped.
5. Transient failures are retried with exponential backoff.

### Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_DRIVE_FOLDER_ID` | Yes | (empty) | ID of the Drive folder to monitor |
| `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE` | Yes | `service_account.json` | Path to the service account JSON key |
| `GOOGLE_DRIVE_DOWNLOAD_DIR` | No | `data/downloads` | Where downloaded files are stored |
| `GOOGLE_DRIVE_PROCESSED_DIR` | No | `data/processed` | Where processed files are moved |

### Database Schema

Files are stored in `files` table keyed by `drive_file_id`:
- `drive_file_id` (TEXT, PK) - The Google Drive file ID
- `name` (TEXT) - The file name
- `mime_type` (TEXT) - MIME type (e.g. `application/pdf`)
- `size` (INTEGER) - File size in bytes
- `local_path` (TEXT) - Path to the downloaded file
- `downloaded_at` (TEXT) - ISO timestamp of download
- `modified_time` (TEXT) - Last modified time from Drive

## Milestone 3 — Processing Pipeline

Once files are downloaded by the sync engine, they enter the job processing pipeline. Each downloaded file gets exactly one `ProcessingJob`, which is picked up by a `ProcessingWorker` and processed in FIFO order.

### Pipeline Flow

```
Google Drive

  |  list_all_files() / download_file()

  v

SQLite Files (app.db)
  files table: drive_file_id PK

  |  create_job_for_file()

  v

SQLite Jobs (jobs.db)
  jobs table: job_id PK, file_id

  |  get_pending_jobs() (FIFO)

  v

Processing Worker (background thread)

  |  process() — placeholder

  v

COMPLETED / FAILED
```

### Database Relationships

The pipeline uses two SQLite databases with no foreign key relationship between them — they share Drive File ID by convention only:

| Database | Table | Scope |
|---|---|---|
| `data/app.db` | `files` | Tracks downloaded Drive files |
| `data/jobs.db` | `jobs` | Tracks processing jobs |

Both tables share `drive_file_id` (files table PK) / `file_id` (jobs table) as the common identifier. The jobs table does NOT have a foreign key to the files table — the relationship is managed by application logic in `JobService.create_job_for_file()`.

### Why Google Drive File ID Is the Permanent Identity

Google Drive File IDs are:
- **Immutable** — They never change for a given file, even if the file is renamed or moved
- **Globally unique** — Within a Drive scope, they uniquely identify a file
- **Stable across restarts** — The ID is returned by the Drive API on every sync cycle

The pipeline uses Drive File ID as the key in:
- `files` table: `drive_file_id` (PK) in `app.db`
- `jobs` table: `file_id` (non-unique, but constrained by partial unique index)

This means renaming a file on Drive won't create duplicate jobs or lose processing state.

### State Machine

```
PENDING

  |  Worker claims job (start_job)

  v

PROCESSING

  |  process() succeeds

  v

COMPLETED (terminal)

  |  process() fails, retry_count < 3

  v

RETRYING

  |  next poll cycle (reset_retrying_jobs)

  v

PENDING (back to top)

  |  process() fails, retry_count >= 3

  v

FAILED (terminal)
```

States:
- `PENDING` — Awaiting processing. Assigned by startup recovery, retry reset, or initial creation.
- `PROCESSING` — Currently being processed by a worker.
- `RETRYING` — Processing failed, will be retried on next poll cycle.
- `COMPLETED` — Successfully processed. Terminal state.
- `FAILED` — All retries exhausted. Terminal state.
- `CANCELLED` — Manually cancelled (reserved for future use).

### Queue Lifecycle

1. **Job creation**: `JobService.create_job_for_file()` verifies the file exists on disk and no active job exists for the Drive File ID, then creates a `PENDING` job in `jobs.db`.
2. **Job claim**: `BaseWorker.run()` calls `JobService.get_pending_jobs()` which resets `RETRYING` → `PENDING`, then returns pending jobs ordered by `priority DESC, created_at ASC` (FIFO within priority).
3. **Processing**: Worker claims the first job, marks it `PROCESSING` via `JobService.start_job()`, runs `ProcessingWorker.process()`.
4. **Completion**: On success, `JobService.complete_job()` marks it `COMPLETED`.
5. **Failure**: On exception, `BaseWorker._handle_failure()` calls `JobService.fail_job()` which increments `retry_count`. If under the limit (3), marks `RETRYING`; otherwise `FAILED`.

### Retry Flow

- `JobService.MAX_RETRIES = 3`
- Each failure calls `increment_retry()` which bumps `retry_count` by 1
- If `retry_count < MAX_RETRIES`: status set to `RETRYING`, job re-enters queue
- If `retry_count >= MAX_RETRIES`: status set to `FAILED`, job stays terminal
- `RETRYING` jobs are reset to `PENDING` during `get_pending_jobs()` — no separate timer needed

### Crash Recovery

On startup, `app.py` calls `JobService.recover_stuck_jobs()` which:
1. Finds all jobs with status `PROCESSING`
2. Resets them to `PENDING`
3. Logs the count

This guarantees no job is permanently locked:
- A crash mid-processing leaves the job in `PROCESSING`
- On restart, the job is reset to `PENDING`
- The worker picks it up and re-processes

Since `retry_count` is preserved across crashes, a job that failed 2 times before the crash will be on its 3rd attempt after recovery — retries are not wasted.

### Restart Recovery

Two recovery paths exist:
- `recover_stuck_jobs()`: Called at startup. Resets `PROCESSING` → `PENDING`.
- `reset_retrying_jobs()`: Called on every poll cycle. Resets `RETRYING` → `PENDING`.

Both are idempotent — calling them on a clean database is a no-op.

### Duplicate Prevention

Enforced at two layers:
1. **Application logic**: `JobService.create_job_for_file()` calls `get_job_by_drive_file_id()` and refuses to create a job if an active job (PENDING, PROCESSING, RETRYING) already exists for the same Drive File ID.
2. **Database**: A partial unique index `idx_jobs_active_per_file` on `file_id` ensures that only one active job exists per Drive File ID at the SQL level. Terminal states (COMPLETED, FAILED, CANCELLED) are excluded from the index, allowing job recreation after terminal state.

### Worker Lifecycle

```
BaseWorker.run()
  |
  v
initialize()  — setup resources

  |
  v
[Loop: while running]
  1. get_pending_jobs() → [job]  (FIFO, one at a time)
  2. _process_job(job):
     a. start_job(job_id) → PROCESSING
     b. process(job) → success/fail
     c. complete_job or _handle_failure
  3. sleep(POLL_INTERVAL=5s)

  |
  v
shutdown()  — signal stop, join thread, release resources
```

### Health Checks

**Worker health (`BaseWorker.health_check()`):**
- `worker_name` — Worker class name
- `running` — Boolean, whether the polling loop is active
- `processed_count` — Total jobs completed successfully
- `failed_count` — Total jobs that failed (all retries exhausted)
- `consecutive_failures` — Current consecutive failure streak (resets on success)
- `poll_interval` — Seconds between polling cycles

**Queue health (`JobService.get_queue_status()`):**
- `pending` — Count of PENDING jobs
- `processing` — Count of PROCESSING jobs
- `completed` — Count of COMPLETED jobs
- `failed` — Count of FAILED jobs
- `retrying` — Count of RETRYING jobs
- `cancelled` — Count of CANCELLED jobs

### Database Schema — jobs table

Jobs are stored in the `jobs` table in a separate SQLite database (`data/jobs.db`) to avoid schema conflicts with the `files` table in `app.db`.

#### Jobs Table Schema

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | INTEGER | No (PK) | AUTOINCREMENT | Internal row ID |
| `job_id` | TEXT | No (UNIQUE) | UUID | Application-level job ID (UUID4 string) |
| `file_id` | TEXT | No | — | Google Drive File ID |
| `file_name` | TEXT | No | — | Original filename from Drive |
| `local_path` | TEXT | No | — | Local filesystem path to the downloaded file |
| `status` | TEXT | No | — | Current status (see JobStatus enum values) |
| `retry_count` | INTEGER | No | 0 | How many processing attempts have been made |
| `priority` | INTEGER | No | 0 | Higher = processed first within FIFO |
| `created_at` | TEXT | No | — | ISO timestamp when the job was created |
| `started_at` | TEXT | Yes | NULL | ISO timestamp when PROCESSING began |
| `completed_at` | TEXT | Yes | — | ISO timestamp when job reached terminal state |
| `error_message` | TEXT | Yes | NULL | Error description if processing failed |
| `worker_name` | TEXT | Yes | NULL | Name of the worker that processed the job |

#### JobStatus Values

| Enum | Value (stored in DB) | Description |
|---|---|---|
| `JobStatus.PENDING` | `"pending"` | Waiting to be processed |
| `JobStatus.PROCESSING` | `"processing"` | Currently being processed |
| `JobStatus.COMPLETED` | `"completed"` | Successfully processed (terminal) |
| `JobStatus.FAILED` | `"failed"` | All retries exhausted (terminal) |
| `JobStatus.RETRYING` | `"retrying"` | Failed, will retry on next poll |
| `JobStatus.CANCELLED` | `"cancelled"` | Manually cancelled (terminal, reserved) |

#### Indexes

- `idx_jobs_status` — Index on `status` for fast queue queries
- `idx_jobs_file_id` — Index on `file_id` for quick lookups by Drive file
- `idx_jobs_created_at` — Index on `created_at` for FIFO ordering
- `idx_jobs_active_per_file` — **Partial unique index** on `file_id` where `status NOT IN ('completed', 'failed', 'cancelled')`

The partial unique index enforces "one active job per Drive file" at the database level. It only covers non-terminal statuses (PENDING, PROCESSING, RETRYING), so recreating a job is allowed after a file reaches COMPLETED, FAILED, or CANCELLED.

### Future Extension Points for OCR

The pipeline is designed so Milestone 4 (Book Identification) can be added with minimal changes:

1. **`ProcessingWorker.process()`** — Replace the placeholder logic (file existence check + sleep) with actual OCR/identification logic. The method signature (`process(job) -> bool`) stays the same.
2. **`workers/` package** — Supports adding multiple worker types (e.g., `OCRWorker`, `CoverWorker`) — all inherit from `BaseWorker` and the polling loop is reusable.
3. **`JobService`** — No changes needed; the retry/crash-recovery logic is agnostic to what `process()` does.
| The `OCR_LANGUAGE` config variable... would be added to `Config` and `docs/environment.md`.

### OCR Configuration

Milestone 4 adds two new configuration variables for OCR:

- `OCR_CONFIDENCE_THRESHOLD` (default `0.5`) — Filters low-confidence OCR words
- `OCR_USE_GPU` (default `false`) — Whether to use GPU acceleration for EasyOCR

### Startup Sequence

1. Initialize (Config, Logger)
2. Recover stuck jobs: `JobService.recover_stuck_jobs()` (PROCESSING → PENDING)
3. Start `ProcessingWorker` in a background thread
4. Run the sync engine loop (downloads new files, creates processing jobs via `JobService`)
5. On shutdown: signal worker to stop (`shutdown()`), join thread, close repositories

### No New Configuration Variables

The job processing pipeline uses the existing `DATA_DIRECTORY` configuration variable. The job database path is `data/jobs.db` (derived from `DATA_DIRECTORY`). No new environment variables are required for Milestone 3.
