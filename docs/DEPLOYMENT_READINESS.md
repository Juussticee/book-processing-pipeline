# Deployment readiness

Current state of the book-cover pipeline for deployment: what it needs, what has
been verified, and what remains. Written 2026-11-13 against the working tree on
branch `main` (HEAD `6d67ef1` plus the uncommitted hardening work).

**Nothing is deployed.** No host was provisioned, no DNS changed, no production
workbook or Drive file was modified by this phase.

Companion documents:

- `docs/RUNBOOK.md` — exact PowerShell start/stop/status/log commands.
- `docs/BOOK_INTAKE_GUIDE.md` — how to upload photos and read the output.

---

## 1. Architecture as implemented

```
Google Drive folder  (single folder, not recursive)
        │  service account, Viewer access
        ▼
`python app.py`  ── one long-running process, three threads:
        ├─ sync loop            every 30 s: list folder → intake gate →
        │                       download → jobs.db → create job
        ├─ ProcessingWorker     jobs.db queue → preprocess → duplicate gate →
        │                       PaddleOCR-VL (Baidu) → extraction → lookup/ranking
        │                       → metadata.db → export queue row
        └─ ExportWorker         export queue → enrichment → cover download →
                                Google Sheets write (RAW values + IMAGE formula)
        │
        ├──► Baidu AI Studio  https://paddleocr.aistudio-app.com (PaddleOCR-VL-1.6)
        ├──► Google Books / Open Library (metadata candidates)
        ├──► books.google.com / covers.openlibrary.org (cover images)
        └──► Google Sheets (customer-visible output)
```

SQLite on local disk: `data/app.db` (Drive file tracking), `data/jobs.db`
(jobs, export queue, duplicates, intake rejections), `data/metadata.db`
(book metadata, covers, export records).

There is **no web UI, no HTTP API and no scheduler**. Discovery is the sync loop
inside `app.py`; nothing else needs to run. `drive_batch.py` is a legacy
one-shot batch script that bypasses the job queue and writes rows directly, and
`app.py --gemini-recovery` is an optional, disabled-by-default Gemini path — the
supported production workflow is `app.py` alone. Do not run two `app.py`
instances against the same `data/` directory.

## 2. Runtime requirements

| Item | Requirement | Evidence |
|---|---|---|
| Python | **3.11** (verified on 3.11.9) | `.venv/Scripts/python.exe -V`. `google-api-core` warns 3.11 support ends 2027-10-24; 3.12+ is untested here |
| OS | Windows 10/11 (verified) or Linux | OpenCV uses the **headless** wheel, so no libGL/X11 needed on Linux |
| CPU/RAM | 2 vCPU / 4 GB is ample; OCR is remote | no local model runs unless a vision key is configured |
| Disk | `data/` grows ~2–6 MB per photo plus covers; budget 5 GB per 500 books + backups | `data/downloads` (19 items), `data/covers` (15 items) on this machine |
| Outbound HTTPS | `drive.googleapis.com`, `sheets.googleapis.com`, `oauth2.googleapis.com`, `www.googleapis.com/books`, `openlibrary.org`, `books.google.com`, `covers.openlibrary.org`, `paddleocr.aistudio-app.com` | endpoints referenced in the service modules |
| Inbound | none | no server socket, no webhook |
| Process supervision | required for unattended running (Windows Service via NSSM/Task Scheduler, systemd, or a kept-open terminal) | the process does not daemonise itself |

Python packages: `requirements.txt` (completed in this phase — it previously
omitted `gspread`, `opencv-python-headless`, `Pillow`, `numpy` and `requests`,
so a fresh install could not have started the app). Versions installed in the
verified environment:

```
python-dotenv 1.2.2   Pillow 12.3.0   numpy 2.4.6   opencv-python-headless 5.0.0.93
google-auth 2.56.3    google-api-python-client 2.198.0   gspread 6.2.1
requests 2.34.2       RapidFuzz 3.14.5
```

## 3. Configuration

Loaded from `.env` in the project root by `core/config.py` (`load_dotenv()`), so
the working directory must be the project root. Never commit `.env`
(`.gitignore` already excludes `.env`, `credentials/`, `data/`, `logs/`).

### Required

| Variable | Purpose | Current status |
|---|---|---|
| `GOOGLE_DRIVE_FOLDER_ID` | the one folder that is scanned | configured |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | service-account JSON for Drive | configured, file exists |
| `GOOGLE_SHEET_ID` | destination spreadsheet (production workbook) | configured |
| `GOOGLE_SHEETS_CREDENTIALS` | service-account JSON for Sheets (same file here) | configured, file exists |
| `PADDLEOCR_API_TOKEN` | Baidu AI Studio token for PaddleOCR-VL | configured (`is_configured()` → True) |
| `DATA_DIRECTORY` | SQLite + images root (default `data`) | default |
| `LOG_DIRECTORY` | log root (default `logs`) | default |

### Behavioural (defaults are production-safe; do not change without a reason)

`CONFIDENCE_THRESHOLD=0.85`, `CONFIDENCE_MANUAL_REVIEW_THRESHOLD=0.50`,
`OCR_CONFIDENCE_THRESHOLD=0.5`, `OCR_LANGUAGE=en,fr,ru`, `OCR_USE_GPU=false`,
`PROCESSING_TIMEOUT_SECONDS=900`, `IMAGE_MAX_FILE_SIZE_MB=20`,
`IMAGE_MAX_WIDTH=4000`, `IMAGE_MAX_HEIGHT=4000`, `EXPORT_MAX_RETRIES=3`,
`EXPORT_RETRY_DELAY=60`, `COVER_MIN_WIDTH/HEIGHT=200`,
`DEBUG_RETENTION_DAYS=3`, `GOOGLE_SHEETS_TAB_NAME=Sheet1`,
`RETRY_INITIAL_DELAY_SECONDS=5`, `RETRY_MAX_DELAY_SECONDS=300`,
`RETRY_BACKOFF_FACTOR=2`. These have safe defaults in `core/config.py` and
are intentionally absent from `.env.example`; set them only with a reason.

Optional and off by default: `GEMINI_API_KEY` / `GEMINI_RECOVERY_ENABLED`,
`EXA_API_KEY`, `GOOGLE_BOOKS_API_KEY` (Google Books works without a key).

### Credential files

Both Sheets and Drive currently use the **same** service-account JSON under
`credentials/` (gitignored). The credentials are a service account, not a user
OAuth token: it owns no Drive files, so every spreadsheet it must write to has
to be shared with its `client_email` as **Editor**; the source folder as
**Viewer** is enough. Print the address with:

```powershell
.venv\Scripts\python.exe -c "import json; from core.config import Config; print(json.load(open(Config.from_env().google_sheets_credentials))['client_email'])
```

(Reads the file path from `.env`; `$env:GOOGLE_SHEETS_CREDENTIALS` is usually
empty in a fresh PowerShell session.)

Scopes used by the code (unchanged, no broadening done): `spreadsheets` and
`drive.readonly` for Sheets, `drive.readonly` for Drive listing/downloading.

## 4. What has been verified, and how

| Area | Evidence | Kind |
|---|---|---|
| Live OCR through the real provider | 3 photos, 3 calls: `angle.jpg` → *Le coeur vert* / Jean-Louis Kuffer (148 pp., publisher `L'AGE D'HOMME`), `upright.jpg` → *Le mal été* / Jean Vuilleumier, `rot180.jpg` → *Vie et mort de la Yougoslavie* / Paul Garde (444 pp.) | **live** |
| Persistence, duplicate gate, integrity | 3 rows, 3 export-queue rows, `exact_duplicate_of` on a byte copy with 0 OCR calls, `PRAGMA integrity_check` ok, source SHA-256 unchanged | **live-adjacent** (temp DBs, real photos) |
| Duplicate detection, same-book, distinct books, EXIF normalisation, retry/recovery, export payload | `tests/integration/test_real_photo_acceptance.py` (16), `test_pipeline_end_to_end.py` (10), `tests/unit/test_orientation_geometry.py` (12), `test_image_preprocessor.py` | controlled harness |
| OCR bounding-box parsing | `tests/unit/test_ocr_service.py::TestProviderBoundingBoxes` (3) — the defect fixed in Phase 46 | mocked provider |
| Sheet payload shape | header order (`Status` first, 10 columns), `RAW` metadata, `USER_ENTERED` cover, `=IMAGE("…")`, verified from live metadata through the real service | **local only** |
| Cover URL reachability | `http://` and `https://` Google Books URLs both return `200 image/jpeg`, 9 146 bytes, identical bytes, no redirect — the `http://` URL stored for `angle` loads fine | **live** |
| Google Sheets rendering | **not verified live** — see §6 | — |
| 90°/270° real photos | **no fixture exists**; the geometry gate is covered by unit tests on synthetic lines | — |

## 5. Deployment checklist

1. Host with persistent disk; Python 3.11 installed.
2. `git clone` / copy the project, then `cd` into it.
3. `python -m venv .venv` and `.\.venv\Scripts\Activate.ps1`.
4. `pip install -r requirements.txt`.
5. Create `.env` from `.env.example`; fill the seven required values; set
   `ENVIRONMENT=Production`.
6. Put the service-account JSON in `credentials/` (outside version control).
7. Share the Drive source folder with the service-account email (Viewer) and
   the target spreadsheet (Editor).
8. Confirm with the configuration check and `app.py --status` (see
   `docs/RUNBOOK.md`).
9. Start `python app.py` in the foreground once and watch one full cycle finish
   before wiring a service supervisor.
10. Only then register it as a Windows service / systemd unit, and set a backup
    job for `data/`.
11. Decide the `Status` column state before the first production cycle: the live
    workbook already has the header with blank labels (§6), so either run
    `app.py --refresh-sheet-status` after a backup, or roll the column back
    first (`docs/RUNBOOK.md` §13.1). Both need the owner's approval.

## 6. Remaining limitations and blockers

### Blocker for a customer pilot: isolated Sheets rendering not verified

The spreadsheet workflow itself is exercised by tests, but no live write has
been performed to an isolated spreadsheet, so **image rendering, cell-error
status and update-in-place are unverified against Google**. This needs exactly
one input from the operator:

1. Create a new, empty Google spreadsheet.
2. Share it with the service-account `client_email` (§3) as **Editor**.
3. Run `scripts/sheets_acceptance_check.py --spreadsheet-id <that id>` (see
   `docs/RUNBOOK.md` §9).

The harness now asserts the full customer-facing contract: `Status` is the
first column; a complete record is `Confirmed`; a record without ISBN is
`Incomplete`; a review record is `Needs Review` and is promoted to `Confirmed`
**in the same row** once resolved (row count unchanged); a repeated export does
not append; metadata re-reads as text (including a leading-zero ISBN); the
cover cell stays an `=IMAGE("…")` formula; no written cell reports a formula
error; and the PDF export embeds at least one image.

The script refuses to run against `GOOGLE_SHEET_ID` and never touches the
production workbook. The service account cannot create the spreadsheet itself
(it owns no Drive storage), which is why the spreadsheet must be created by a
human and shared. No scope change is needed: `drive.readonly` already permits
the PDF export used as the rendering check.

### Status column (implemented): review rows are labelled, not hidden

Review records are still exported (that behaviour is unchanged and deliberate),
but they are now visibly marked. `Status` is the **first** column of the sheet
(`GoogleSheetsService.COLUMNS`), with exactly three labels:

| Label | Rule (derived only from persisted state, never guessed) |
|---|---|
| `Needs Review` | `metadata.status` is not `identified`, **or** the title is blank/`(…)`, **or** `confidence < 0.50`. Applied even when every field is populated. |
| `Incomplete` | Identity accepted, but useful expected metadata or the cover is missing: no publisher, or no usable cover image. |
| `Confirmed` | Identity accepted, the publisher is known, and the cover is present. The ISBN is **optional**. |

An ISBN is not guaranteed for every book, so its absence alone never downgrades
a correctly identified record: no-ISBN rows are `Confirmed` when the publisher
and cover are present. Description, published date, language and categories are
**enrichment** fields with the same rule — they never change the label, because
their absence is normal for older French/Swiss books.

An export succeeding never makes a book `Confirmed`: the label is computed from
the persisted record before the row is written, so a `needs_review` row stays
`Needs Review` on the sheet however cleanly its export completes.

The label is computed in `GoogleSheetsService.export_status()` (fresh rows) and
`_status_from_row()` (existing rows). `_status_from_row` judges completeness on
the *merged* row, so a re-export that arrives with poorer provider data cannot
downgrade a row that already holds a publisher or a cover. `ExportService`
reports the same label in its `exported_data["status"]`. An unknown or missing
persisted status always becomes `Needs Review` — a record is never labelled
`Confirmed` on ambiguous evidence.

#### Row preservation during the layout change

`_ensure_layout()` runs once per service instance on a **write path**, and only
when the existing header has no `Status` cell. It inserts one column at position
A (`worksheet.insert_cols([["Status"]], 1)`): Google moves the existing cells
right with their values and formulas intact, so no row is rewritten, duplicated
or shifted by the application. Nothing else is written in that pass, and no
database migration is involved. The insert is idempotent — a second run sees
`Status` and does nothing.

A read must never migrate the workbook. `_get_sheet(ensure_layout=False)` and
`read_all_rows(ensure_layout=False)` exist for diagnostic callers, and
`scripts/read_sheet_state.py` uses them. Before that guard existed, merely
reading the sheet through the service created the column.

#### Migration status of the live workbook (verified read-only, 2026-11-23)

The production `Sheet1` **already carries the `Status` header**. It was created
by a diagnostic read that ran before the read-only guard above was added.
Re-verified on 2026-11-23 with a strictly read-only call
(`_get_sheet(ensure_layout=False)` + `get_all_values(value_render_option="FORMULA")`):

| Measurement | Value |
|---|---|
| Rows | 19 (1 header + 18 data) |
| Columns | 10: `Status`, `Title`, `Authors`, `ISBN-13`, `Publisher`, `Published Date`, `Language`, `Categories`, `Description`, `Open Cover` |
| `Status` column | 18 cells, **all blank**, no formulas |
| Metadata columns | 18 non-empty titles/authors, 13 publishers, 16 dates, 18 languages, 10 categories, 3 descriptions, 6 ISBN-13 |
| Cover column | 14 cells, all `=IMAGE(…)` formulas |
| Metadata formulas | none (values only), so the column insert reinterpreted nothing |

All 18 original records are present and intact, one column to the right of where
they were. The header order now matches the application's canonical `COLUMNS`
exactly, so the workbook is structurally ready for the application; only the
labels are missing.

- This happened outside the approved sequence and is recorded as an incident
  (`docs/INCIDENT-STATUS-COLUMN.md`), not as a completed migration.
- It is recoverable: see the rollback in `docs/RUNBOOK.md` §13.1.
- The remaining production step is labelling the existing rows. That write is
  **not authorized** and has not been performed.

Rows that existed before the change keep a **blank** Status cell until they are
re-exported or explicitly relabelled with
`python app.py --refresh-sheet-status` (writes only Status cells, RAW, for rows
matched by title/author; never appends, never deletes, never rewrites other
cells). A blank cell is never interpreted as `Confirmed`.

Current data: 18 identified records, 9 of them `needs_review`; after a refresh
the sheet shows the 18 accepted records as `Confirmed` (`Incomplete` only where
no publisher or cover was resolved) plus 9 `Needs Review` rows.

### The five vision-path integration failures — resolved (2026-11-23)

The five failures reported by the earlier acceptance run are fixed. None of them
was solved by deleting, skipping or weakening a test, and the production OCR
provider is still PaddleOCR-VL only.

| Test | Root cause | Treatment |
|---|---|---|
| `test_vision_first_routing::TestVisionFirstRouting::test_good_ocr_does_not_trigger_vision` | Asserted the pre-Phase-44 policy ("vision is never called for good OCR"). The settled policy is that vision is the pipeline's FINAL ANALYSIS step and runs for every book. | Renamed to `test_good_ocr_result_is_not_overridden_by_vision` and made stricter: it now asserts the final-analysis step ran **and** that the accepted title/author/publisher/status were not altered. |
| `...::test_vision_not_called_for_fake_image` | Same stale premise; with vision always running, `total_calls == 0` can never hold. The real invariant is cache safety. | Renamed to `test_fake_drive_id_never_matches_the_vision_cache`; asserts `cache_hits == 0` against a **populated** cache directory, which is the actual collision guard. |
| `...::TestVisionRecoveryResults::test_yugoslavia_vision_extraction` | Read `data/vision_model_responses/13oww9ct1us7aG….json`, a gitignored hand-written benchmark file whose author (`Pavle Ivić`) is wrong for that image. | Now uses the repo-visible `VISION_FIXTURES` payload written to a temporary cache; the assertion on the author is unchanged and passes. |
| `...::TestRegressionCases::test_vision_extracts_all_problem_books` | Same dependency on the local cache for seven drive ids. | Iterates `VISION_FIXTURES` (seven synthetic ids, same title/author expectations). No real Drive id remains in the suite. |
| `test_vision_benchmark::TestBenchmark::test_no_books_silently_lost` | Asserted "a correct vision title may never end as `needs_review`", which contradicts the approved rule that a vision-only identification is never auto-accepted. | Rewritten to the invariant that actually matters: all 14 books produce a record, none raises, and a correct vision extraction is never discarded on the way into the record (it may still be persisted as `needs_review`). |

Supporting defects fixed while investigating:

- The vision cache is only consulted for the **synthetic fixtures in a temporary
directory** in tests. The 14 hand-written cached extractions in
`data/vision_model_responses/` are still local benchmark data and must **never**
be copied to a production host: with that directory present, those ids resolve
from the cache and skip live extraction.
- `VisionModelService._try_gemini()` constructed `GeminiRecoveryService()` without
the required `api_key` and called a non-existent `extract()` method, so the
"disabled" branch was actually an exception swallowed once per book. It now checks
`GEMINI_RECOVERY_ENABLED` and the key first, respects the recovery confidence
gate, calls `extract_from_image()`, and caches its own unavailability. Gemini is
still **not** an OCR provider and is still off by default.
- The local-Ollama availability probe is now memoised per service instance, so a
host without Ollama pays one probe instead of one per book.
- The 14-image benchmark module is now guarded: without the local corpus
(`data/downloads`, `data/vision_model_responses`, `data/clean_run_results.json`)
it reports a skip with that reason instead of raising `FileNotFoundError` during
collection. On the development machine, where the corpus exists, it runs.

### Private photograph fixtures are no longer tracked

`tests/fixtures/real/` contained eight photographs of the owner's collection plus
a manifest with real Drive file ids and titles. Publishing them would have
published a private library. They now live in `data/fixtures/real/` (gitignored)
and `tests/conftest.py` exposes `REAL_PHOTO_FIXTURES` plus a module-level guard:
the three affected modules run normally when the photographs are present and
report a documented skip when they are not. The same applies to the
`data/vision_model_responses/` benchmark cache, which was already gitignored.

### Other limitations

- **90°/270° real photographs**: no such capture exists in the cache; not
  fabricated. The rotation gate is verified only by unit tests on synthetic
  line images and by the 180° live result.
- **OCR coverage**: three live photos, all French covers, one publisher. Blurry,
  glare, ISBN-bearing, Cyrillic and other-language covers are covered only by
  mocked/controlled tests, never by the live provider.
- **`http://` cover URLs**: Google Books returns `http://` and it loads (see
  §4). No production change was made. If a future environment blocks plain HTTP,
  the cover formula would silently render blank; the fix would belong in the
  cover-URL resolver and is out of scope here.
- **Retry recovery**: jobs interrupted mid-flight are recovered on startup, and
  jobs that exhausted their retries stay `failed` until
  `app.py --requeue-failed-jobs` returns them to the queue (optionally a single
  photo with `--drive-id`). A requeue re-runs OCR for those photos and never
  touches completed books, review records or skipped duplicates.
- **Single OCR provider, no local fallback by design.** If Baidu is unreachable,
  jobs retry and end in `retrying`/`failed`; no book is silently dropped, but
  nothing is processed until the API is reachable.
- **`scripts/reset_pipeline.py` is destructive** (clears `metadata.db` rows and
  the production sheet's data rows). It is intentionally not part of any
  documented procedure.

## 7. Persistence, backup, restart

Back these up (they are the only state that is not reproducible from Drive):

```
data/app.db        which Drive files were downloaded
data/jobs.db       jobs, export queue, duplicates, intake rejections
data/metadata.db   identified books, covers, export records
data/covers/       downloaded cover images referenced by the sheet
```

`data/downloads/` and `data/debug/` are regenerable (debug artifacts are deleted
after `DEBUG_RETENTION_DAYS`). SQLite with `-wal`/`-shm` siblings should be copied
with the application stopped, or with `sqlite3.Connection.backup` while running.

Restart safety: on startup `app.py` calls `recover_stuck_jobs()`, which requeues
any job whose lease (heartbeat) expired beyond `PROCESSING_TIMEOUT_SECONDS`
(900 s) and fails those out of retries. Pending jobs are untouched, so a crashed
or killed process resumes the queue without re-downloading or re-OCRing anything
already completed.

## 8. Verdict

**READY TO DEPLOY WITH TWO OUTSTANDING LIVE STEPS**

The pipeline is deployment-ready: one process, clear config, complete
dependencies, live-proven OCR, proven persistence, duplicate handling and a
customer-facing `Status` column that marks every uncertain record. What is still
unverified is Google's own rendering of the sheet (cover image + status cells on
a real spreadsheet) — the isolated acceptance run in §6, which needs one
spreadsheet shared by you.

Separately, the live workbook is in a **half-migrated** state: the `Status`
header exists, all 18 rows are intact, and no label has been written (§6). That
is not a code defect, but it is customer-visible, so it needs one decision:
finish it (`--refresh-sheet-status`, after a backup) or delete column A.

Everything else in §6 is a documented limitation, not a defect. Code readiness
and live-sheet verification are separate claims: the first is done, the second
needs the two steps above.

## 9. Recommended hosting and deployment preparation

### Why this shape of host

The system is **one long-running process** with **state on local disk** and
**outbound-only networking**:

- the Drive sync loop must keep running (30 s cycle) — a sleeping/scale-to-zero
  PaaS or a serverless function cannot host it;
- `data/app.db`, `data/jobs.db`, `data/metadata.db` and `data/covers/` are the
  only non-reproducible state (see §7), so the disk must be **persistent**;
- SQLite on a network share or an object store is not supported;
- no inbound port, no web server, no container orchestration is needed.

### Option A — recommended: a Windows 10/11 PC or Windows Server 2022 VM

This is the environment the code was verified on (Windows 10/11, Python 3.11.9).
A 2 vCPU / 4 GB / 20 GB SSD instance is ample; OCR is remote so no GPU is used.
It must be always on (a small VM, a mini-PC, or a workstation that is not
shut down at night).

```powershell
# 1. Python 3.11 (once)
winget install --id Python.Python.3.11 -e
# verify
py -3.11 -V

# 2. Get the project
cd C:\
git clone git@github.com:Juussticee/book-processing-pipeline.git book-auth
cd C:\book-auth

# 3. Isolated environment + dependencies
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 4. Configuration: copy the template and fill it in (never commit .env)
Copy-Item .env.example .env
notepad .env

# 5. Credentials folder + service-account JSON
New-Item -ItemType Directory -Force credentials | Out-Null
# copy your service-account .json into C:\book-auth\credentials\service_account.json

# 6. First run in the foreground (creates data/ and the SQLite files)
#    Run the configuration check first: docs/RUNBOOK.md §3 (one PowerShell one-liner)
python app.py
#   -> wait for "Synchronization complete", then Ctrl+C

# 7. Register as a service so it survives logoff/reboot (NSSM)
#    Download NSSM from https://nssm.cc/download, extract win64\nssm.exe
.\nssm.exe install BookAuth "C:\book-auth\.venv\Scripts\python.exe" "C:\book-auth\app.py"
.\nssm.exe set BookAuth AppDirectory "C:\book-auth"
.\nssm.exe set BookAuth AppStdout "C:\book-auth\logs\service-out.log"
.\nssm.exe set BookAuth AppStderr "C:\book-auth\logs\service-err.log"
.\nssm.exe set BookAuth Start SERVICE_AUTO_START
.\nssm.exe start BookAuth
.\nssm.exe status BookAuth   # "SERVICE_RUNNING"; Get-Service BookAuth also works
```

Backup task (nightly, 02:00), with the service stopped for the copy — see
`docs/RUNBOOK.md` §12 for the exact `Copy-Item` commands:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -Command "Stop-Service BookAuth; robocopy C:ook-auth\data C:\Backupsookauth\data /MIR; Start-Service BookAuth"'
$trigger = New-ScheduledTaskTrigger -Daily -At 02:00
Register-ScheduledTask -TaskName "BookAuth backup" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest
```

NSSM restarts a crashed process only once that is configured explicitly:

```powershell
.\nssm.exe set BookAuth AppExit Default Restart
.\nssm.exe set BookAuth AppRestartDelay 10000   # 10 s, so SQLite handles are released
.\nssm.exe set BookAuth AppThrottle 5000
```

### Option B — Linux VM (Ubuntu 22.04/24.04) with systemd

OpenCV is the headless wheel, so there is no GUI dependency. Python **3.11** is
required (3.12 is untested here).

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git
sudo useradd -r -m -d /opt/book-auth -s /bin/bash bookauth
sudo -u bookauth git clone git@github.com:Juussticee/book-processing-pipeline.git /opt/book-auth
cd /opt/book-auth
sudo -u bookauth python3.11 -m venv .venv
sudo -u bookauth .venv/bin/pip install -r requirements.txt
sudo -u bookauth cp .env.example .env   # then edit .env and add credentials/
# /etc/systemd/system/book-auth.service
#   [Unit]
#   Description=Book Auth worker
#   After=network-online.target
#   [Service]
#   User=bookauth
#   WorkingDirectory=/opt/book-auth
#   ExecStart=/opt/book-auth/.venv/bin/python app.py
#   Restart=always
#   RestartSec=10
#   [Install]
#   WantedBy=multi-user.target
sudo systemctl daemon-reload && sudo systemctl enable --now book-auth
journalctl -u book-auth -f
```

Do not put `data/` on NFS/SMB: SQLite locking over network shares corrupts state.

### Upgrade procedure (both options)

1. Back up (§7, `docs/RUNBOOK.md` §12) — the upgrade is reversible only with it.
2. Stop the service (`nssm.exe stop BookAuth` / `systemctl stop book-auth`).
3. Replace the code, keeping `.env`, `credentials/` and `data/` untouched:
   `git pull --ff-only` on the host, or copy the new tree over it.
4. `.venv\Scripts\python.exe -m pip install -r requirements.txt` (only when the
   file changed).
5. `python app.py --status` — configuration must be complete before starting.
6. Start the service and watch one full cycle in `logs\app.log`.
7. If the upgrade changes the sheet layout, read §6 first: the export path may
   alter the customer-visible header.

Downgrade is the same procedure with the previous tree plus the matching backup
restored (`docs/RUNBOOK.md` §12.1).

### Health check after a deploy

| Check | Expected |
|---|---|
| `Get-Service BookAuth` / `systemctl status book-auth` | running |
| `logs\app.log` last 50 lines | a `Synchronization complete` line, no repeated traceback |
| `python app.py --status` | `pending` not growing, no `Missing` configuration |
| Export queue | `pending`/`retrying` near zero after one cycle |
| The sheet | one new row per new photo, no duplicates |

### What must persist on the host

| Path | Why | Backup |
|---|---|---|
| `data/app.db`, `data/jobs.db`, `data/metadata.db` | all pipeline state | yes, nightly |
| `data/covers/` | images referenced by the sheet | yes, nightly |
| `.env`, `credentials/*.json` | secrets; recreate manually if lost | store in your password manager, **never** in Git |
| `logs/` | diagnostics (`DEBUG_RETENTION_DAYS=3` prunes debug images, not logs) | optional |
| `data/downloads/`, `data/debug/` | regenerable | no |

### Giving the other person access

They never need server access. For their work they need the **Google Sheet**
(share the spreadsheet with their Google account as Editor) and, if they upload
photos, the **Drive folder** (share as Contributor). If they must also restart the
service, give them a Windows/RDP account on the host plus the commands in
`docs/RUNBOOK.md` §5, §10 and §11 — nothing else.
