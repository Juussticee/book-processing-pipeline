> **Note (2026-11-13):** superseded by `docs/RUNBOOK.md` (exact PowerShell commands,
> verified against the code). Kept for history.

# Deployment runbook

Operational procedure for running the book-processing system for a customer.
This document is for the operator, not the customer (customers get
`docs/CUSTOMER_UPLOAD_GUIDE.md`).

**Status:** prepared for a controlled pilot on a persistent host. Not deployed.
Vercel alone cannot host this workload (see `docs/DEPLOYMENT_READINESS_REPORT.md`).

---

## 1. What runs where

| Component | Runs on | Notes |
|---|---|---|
| Sync loop, processing worker, export worker | Persistent host (VM, mini-PC, container) | One long-running process: `python app.py` |
| SQLite databases (`app.db`, `jobs.db`, `metadata.db`) | Same host, persistent disk | Must not be on a network share |
| Downloaded images, covers, debug artifacts | Same host, under `DATA_DIRECTORY` | Regenerable from Drive |
| Google Drive | Google | Source of truth for customer photographs |
| Google Sheets | Google | Customer-visible output |
| Baidu AI Studio OCR | Cloud API | Credentials via env |
| Vercel frontend | Optional / future | No frontend exists today |

---

## 2. Host requirements

- **Python 3.11** (tested on 3.11.9).
- 2 vCPU / 4 GB RAM minimum for OCR API usage; the local vision model is
  disabled unless explicitly configured.
- Persistent disk with room for `DATA_DIRECTORY`: images are ~2–6 MB each;
  budget 5 GB per 500 books plus backups.
- Outbound HTTPS to `drive.googleapis.com`, `sheets.googleapis.com`,
  `www.googleapis.com/books`, `openlibrary.org`, the Baidu OCR endpoint, and
  cover-image hosts.
- A process supervisor (systemd, NSSM on Windows, supervisord, or Docker
  restart policy). The application has no built-in daemonisation.

System packages: none beyond the Python wheels in `requirements.txt`
(`opencv-python`, `numpy`, `Pillow`, `paddleocr` dependencies). On Linux,
install `libgl1` and `libglib2.0-0` for OpenCV.

---

## 3. Environment variables

Copy `.env.example` to `.env` on the host and fill it in. Never commit `.env`.

### Required

| Variable | Purpose |
|---|---|
| `GOOGLE_DRIVE_FOLDER_ID` | The customer folder to scan |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the service-account JSON key |
| `GOOGLE_SHEET_ID` | Destination spreadsheet |
| `GOOGLE_SHEETS_CREDENTIALS` | Credentials used by the Sheets client |
| `PADDLEOCR_API_TOKEN` | Baidu AI Studio OCR token |
| `DATA_DIRECTORY` | Persistent data root (e.g. `/srv/bookauth/data`) |
| `LOG_DIRECTORY` | Log directory (e.g. `/srv/bookauth/logs`) |

### Behavioural (defaults are production-safe)

| Variable | Default | Notes |
|---|---|---|
| `PROCESSING_TIMEOUT_SECONDS` | `900` | Jobs stuck in `processing` longer than this are reset to `pending` on restart |
| `IMAGE_MAX_FILE_SIZE_MB` | `20` | Files larger than this are rejected before download |
| `CONFIDENCE_THRESHOLD` | `0.85` | Auto-accept threshold |
| `CONFIDENCE_MANUAL_REVIEW_THRESHOLD` | `0.50` | Below this, still recorded but flagged |
| `EXPORT_MAX_RETRIES` / `EXPORT_RETRY_DELAY` | `3` / `60` | Sheet export retry policy |
| `DEBUG_RETENTION_DAYS` | `3` | Debug artifact cleanup |
| `OCR_LANGUAGE` | `en,fr,ru` | OCR language hints |

Secrets (`GOOGLE_SERVICE_ACCOUNT_FILE`, `PADDLEOCR_API_TOKEN`,
`GOOGLE_BOOKS_API_KEY`, `GEMINI_API_KEY`, `EXA_API_KEY`) must be readable only
by the service account running the process and must never appear in logs,
the repository, or the sheet.

---

## 4. First start

```bash
cd /srv/bookauth
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
.venv/bin/python app.py --status   # configuration + database sanity check
.venv/bin/python app.py            # start the sync loop (foreground)
```

On Windows:

```powershell
.venv\Scripts\python.exe app.py --status
.venv\Scripts\python.exe app.py
```

At startup the process:

1. opens/creates `data/app.db`, `data/jobs.db`, `data/metadata.db`;
2. recovers jobs stuck in `processing` for longer than
   `PROCESSING_TIMEOUT_SECONDS`;
3. starts the processing worker and the export worker threads;
4. begins the 30-second sync loop and logs a `BOOK PROCESSING SUMMARY` block
   at the end of each cycle.

---

## 5. Process supervision (systemd example)

```ini
# /etc/systemd/system/bookauth.service
[Unit]
Description=Book Auth processor
After=network-online.target

[Service]
Type=simple
User=bookauth
WorkingDirectory=/srv/bookauth
EnvironmentFile=/srv/bookauth/.env
ExecStart=/srv/bookauth/.venv/bin/python app.py
Restart=always
RestartSec=10
StandardOutput=append:/srv/bookauth/logs/stdout.log
StandardError=append:/srv/bookauth/logs/stderr.log
# graceful shutdown so workers finish the current job
KillSignal=SIGINT
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bookauth
sudo systemctl status bookauth
journalctl -u bookauth -f
```

Windows equivalent: run under NSSM with the same working directory and
`.env` loading, or a scheduled task with "restart on failure".

---

## 6. Health check

There is no HTTP health endpoint (the process has no web server). Use:

```bash
.venv/bin/python app.py --status
```

which reports tracked Drive files, job counts by state, identified books and
export queue depth. A healthy pilot host shows `processing=0` between cycles
and a growing `completed` count.

Log-based check: the last `BOOK PROCESSING SUMMARY` block shows
`Files discovered`, `New files`, `Rejected`, duplicate counts, and
`Failed`. Failures should stay at zero or match known-bad customer photos.

---

## 7. Backups

Everything the system cannot regenerate lives in `DATA_DIRECTORY`:

- `jobs.db` — job state, duplicate fingerprints, duplicate decisions,
  intake rejections, export queue
- `metadata.db` — book records, cover records, export records
- `app.db` — tracked Drive file IDs

`downloads/`, `covers/`, `processed/`, `debug/` are regenerable or
disposable.

### Nightly online backup (SQLite is safe to copy with the backup API)

```bash
#!/bin/sh
set -eu
DEST=/srv/bookauth/backups/$(date +%F)
mkdir -p "$DEST"
for db in app jobs metadata; do
  sqlite3 "/srv/bookauth/data/$db.db" ".backup '$DEST/$db.db'"
done
# keep 14 days
find /srv/bookauth/backups -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
```

Verify a backup by opening it read-only:

```bash
sqlite3 "file:/srv/bookauth/backups/2026-11-06/jobs.db?mode=ro" "PRAGMA integrity_check;"
```

### Restore

```bash
sudo systemctl stop bookauth
cp -a /srv/bookauth/data /srv/bookauth/data.before-restore
cp /srv/bookauth/backups/2026-11-06/*.db /srv/bookauth/data/
sudo systemctl start bookauth
.venv/bin/python app.py --status
```

Restore is idempotent with respect to the customer folder: Drive remains the
source of truth for images, and files already recorded in `app.db` are not
re-downloaded. Deleting `app.db` intentionally re-scans the whole folder, but
content dedupe still prevents re-OCRing identical photographs.

---

## 8. Restart and recovery behaviour

| Situation | Behaviour |
|---|---|
| Clean shutdown (Ctrl+C / SIGINT) | Workers stop, repositories close |
| Hard crash during a job | Job stays `processing`; on restart, reset to `pending` after `PROCESSING_TIMEOUT_SECONDS` (15 min) |
| Sheets unavailable | Export retries with backoff; queue survives restart |
| OCR/metadata provider outage | Job retries up to 3 times, then `failed` with the reason recorded |
| One corrupt photograph | That job fails; the rest of the batch continues |

To force recovery immediately after an unclean crash, stop the service, wait
for the timeout to elapse, then restart — or reset manually:

```sql
UPDATE jobs SET status='pending', started_at=NULL
WHERE status='processing' AND started_at <= datetime('now','-15 minutes');
```

---

## 9. Routine operations

```bash
# queue and export status
.venv/bin/python app.py --status

# re-queue exports that never completed
.venv/bin/python app.py --requeue-incomplete

# one-off batch run for a folder (no job queue, no resume)
.venv/bin/python drive_batch.py
```

Log rotation is external (logrotate / systemd journal). The application writes
to `LOG_DIRECTORY` and does not truncate its own logs.

---

## 10. Security checklist before handing over

- [ ] `.env` and the service-account JSON are `chmod 600`, owned by the
      service user, and excluded from version control.
- [ ] The customer Drive folder is shared **Viewer** only.
- [ ] The spreadsheet is shared only with the customer and the service account.
- [ ] Logs are checked for secrets: `grep -iE "token|key|secret" logs/*` should
      show only variable names, never values.
- [ ] No database file is world-readable.
- [ ] Debug artifacts (`data/debug/<job_id>/`) are cleaned by
      `DEBUG_RETENTION_DAYS`.
- [ ] Backups are stored outside the application directory.

---

## 11. Known operational limits

- One folder per deployment (`GOOGLE_DRIVE_FOLDER_ID`); no multi-tenant mode.
- Sub-folders are not scanned.
- HEIC is rejected (no decoder installed); customers must upload JPEG/PNG.
- SQLite is single-host: do not run two instances against the same data
  directory.
- No HTTP API or web UI: monitoring is via `--status` and the log summary.
