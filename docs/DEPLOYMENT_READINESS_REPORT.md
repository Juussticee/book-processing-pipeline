> **Note (2026-11-13):** superseded by `docs/DEPLOYMENT_READINESS.md`, which reflects the
> current implementation and verification state. Kept for history.

# Deployment readiness report

Prepared as part of the customer-ready release. **Nothing has been deployed**:
no VPS provisioned, no DNS changed, no production database migrated, no
customer Drive or Sheet touched.

---

## 1. Target architecture

```
Customer (browser / Drive app)
        │  uploads photographs
        ▼
Google Drive folder  ──────────────┐
        │  (service account, Viewer)│
        ▼                          │
Persistent host: python app.py      │  source of truth for images
  ├─ sync loop (30 s)               │
  ├─ processing worker thread       │
  ├─ export worker thread           │
  └─ SQLite: app.db / jobs.db / metadata.db   (persistent disk)
        │
        ├──────────► Baidu AI Studio (PaddleOCR-VL)
        ├──────────► Open Library / Google Books (metadata)
        └──────────► Google Sheets (customer-visible output)
```

**Vercel:** not used for the worker. A Vercel serverless function has an
ephemeral, mostly read-only filesystem, no long-running threads, and no
persistent local database, so the sync loop and workers cannot run there.
There is currently **no web frontend or API**, so nothing is deployed to
Vercel either; when a UI is built, it belongs on Vercel while calling a
persistent API/worker host, exactly as in the diagram above.

**Docker:** a Dockerfile was deliberately not added. The pilot target is a
single VM or mini-PC with a systemd/NSSM service, where a container adds a
build-and-mount layer without solving anything. If the host is later
containerised, the requirements are: base `python:3.11-slim`, install
`requirements.txt`, mount `DATA_DIRECTORY` as a volume, run `python app.py`
as PID 1 (no model downloads at build time, no credentials in the image),
and a health check that runs `python app.py --status`.

---

## 2. Host requirements

| Item | Requirement |
|---|---|
| Python | 3.11 (tested 3.11.9) |
| CPU / RAM | 2 vCPU / 4 GB |
| Disk | 5 GB per 500 books + backups |
| OS packages | `libgl1`, `libglib2.0-0` (OpenCV); no GPU required |
| Network | HTTPS to Drive, Sheets, Baidu OCR, Open Library, Google Books, cover hosts |
| Supervisor | systemd, NSSM, supervisord or similar (no built-in daemon mode) |

---

## 3. Paths and state

| Path | Purpose | Persistent | Backup |
|---|---|---|---|
| `$DATA_DIRECTORY/app.db` | Tracked Drive file IDs | Yes | Yes |
| `$DATA_DIRECTORY/jobs.db` | Jobs, duplicate fingerprints, duplicate decisions, intake rejections, export queue | Yes | Yes |
| `$DATA_DIRECTORY/metadata.db` | Book records, cover records, export records | Yes | Yes |
| `$DATA_DIRECTORY/downloads/` | Downloaded originals | Regenerable | No |
| `$DATA_DIRECTORY/covers/` | Downloaded cover images | Regenerable | No |
| `$DATA_DIRECTORY/debug/` | Per-job debug artifacts | Disposable | No |

No state is stored in memory only: a restart loses at most the job currently
in flight. That job keeps its retry count and error history and is requeued
once its lease expires — 900 s after the last heartbeat
(`PROCESSING_TIMEOUT_SECONDS`), so a job whose worker died mid-run is picked
up without an operator, while a slow job that is still sending heartbeats is
left alone. A job that exceeds `MAX_RETRIES` (3) is marked `failed` with a
reason instead of looping. Completed, cancelled and permanently failed jobs
are never touched by recovery.

---

## 4. Commands

```bash
# worker + sync loop (single long-running process, supervised)
python app.py

# configuration and queue status (also the health check)
python app.py --status

# requeue exports that never completed
python app.py --requeue-incomplete

# one-off folder batch (no queue, no resume)
python drive_batch.py
```

**API start command:** none — no HTTP API exists in this release.

---

## 5. Environment variables

See `docs/DEPLOYMENT_RUNBOOK.md` §3 for the full table. Required:
`GOOGLE_DRIVE_FOLDER_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEET_ID`,
`GOOGLE_SHEETS_CREDENTIALS`, `PADDLEOCR_API_TOKEN`, `DATA_DIRECTORY`,
`LOG_DIRECTORY`. Secrets stay in `.env` (mode 600) or the supervisor's secret
store; they are never logged and never committed.

---

## 6. Health, logging, backups

- **Health check:** `python app.py --status` — exit status 0 and a status
  report; a supervisor can run it every minute.
- **Logging:** application logs under `LOG_DIRECTORY`; each sync cycle ends
  with a `BOOK PROCESSING SUMMARY` block split into discovery and screening
  stages, each followed by an arithmetic check (`OK` / `MISMATCH`, the latter
  also logged at WARNING). Duplicate flags are reported as subsets of the
  processed count, so a file is never counted in two places.
- **Backups:** nightly `sqlite3 .backup` of the three databases, 14-day
  retention, restore procedure and integrity check in the runbook §7.

---

## 7. Blockers to production (not to a controlled pilot)

1. **No customer-facing UI/API.** The customer submits via a shared Drive
   folder and reads the Sheet. Acceptable for a pilot, not for self-service.
2. **Single folder / single tenant.** One `GOOGLE_DRIVE_FOLDER_ID` per
   deployment; multi-customer isolation would need a data-model change.
3. **HEIC not supported.** No decoder is installed; the policy is explicit
   rejection with instructions, which is acceptable if the customer confirms
   their phone saves JPEG.
4. **SQLite is single-host.** No horizontal scaling; a second instance must
   not share the data directory.

## 8. Ready-for-pilot assessment

| Area | Status |
|---|---|
| Persistent worker host defined | Yes (VM / mini-PC + supervisor) |
| Vercel limited to future frontend | Yes |
| Environment variables documented | Yes |
| Startup commands documented | Yes |
| Health check documented | Yes |
| No ephemeral-filesystem dependency for persistent state | Yes |
| Backup + restore procedure | Yes |
| Secrets handling | Yes (`.env`, excluded from git) |
| Stale-job recovery | Yes — heartbeat lease (900 s), single-winner claim, retry limit |
| Batch counters reconcilable | Yes — discovery/screening split with arithmetic checks |
