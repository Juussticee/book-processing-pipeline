# Runbook (Windows PowerShell)

Exact commands to run the book-cover pipeline on this machine. Every command was
checked against the code and, where marked, executed. Nothing here requires
administrator rights.

Legend:

- **[read-only]** touches nothing, safe to run any time, including while the
  system is running.
- **[writes data]** modifies `data/*.db`, `data/covers`, logs or Drive state.
- **[needs input]** asks for a value only you can supply.

Terminal model: **Terminal A** runs the system (one long-running process).
**Terminal B** is your operator terminal for status, logs and checks. Do not run
two `app.py` instances against the same `data/` folder.

---

## 1. Open the project and activate the environment

```powershell
cd "C:\Users\hashe\Documents\Book Auth"
.\.venv\Scripts\Activate.ps1
```

Expected: the prompt gains `(.venv)`. Failure: `running scripts is disabled on
this system` → allow it for this session only, then retry:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

If you prefer not to activate, every command below also works as
`.venv\Scripts\python.exe …` from the project root.

The project root matters: `core/config.py` loads `.env` from the current
directory. All commands assume you are in `C:\Users\hashe\Documents\Book Auth`.

## 2. Install / verify dependencies — **[writes data]** (into `.venv` only)

```powershell
python -m pip install -r requirements.txt
```

Expected: `Successfully installed …` or `Requirement already satisfied`.
`requirements.txt` was completed in this phase (it previously lacked `gspread`,
`opencv-python-headless`, `Pillow`, `numpy`, `requests`). Safe to rerun.

## 3. Check configuration without exposing secrets — **[read-only]**

```powershell
.venv\Scripts\python.exe -c "import os; from core.config import Config; from services.paddleocr_vl_service import PaddleOCRVlService; c=Config.from_env(); print('drive folder id set:', bool(c.google_drive_folder_id)); print('service account file exists:', os.path.exists(c.google_service_account_file or '')); print('sheet id set:', bool(c.google_sheet_id)); print('sheets credentials file exists:', os.path.exists(c.google_sheets_credentials or '')); print('paddleocr configured:', PaddleOCRVlService().is_configured())"
```

Expected:

```
drive folder id set: True
service account file exists: True
sheet id set: True
sheets credentials file exists: True
paddleocr configured: True
```

Any `False` is a blocker; the variable to fix is named on that line. This
prints booleans only — never a token, key or file content.

The folder being monitored:

```powershell
.venv\Scripts\python.exe -c "from core.config import Config; print(Config.from_env().google_drive_folder_id)"
```

## 4. Database initialisation — **not required**

No separate init step exists. `app.py` creates `data/downloads`,
`data/processed`, `data/covers` and opens/creates `data/app.db`, `data/jobs.db`,
`data/metadata.db` with `CREATE TABLE IF NOT EXISTS` on startup. Do **not** run
migrations or create tables by hand.

## 5. Start the system — Terminal A — **[writes data]**

```powershell
cd "C:\Users\hashe\Documents\Book Auth"
.venv\Scripts\python.exe app.py
```

One process runs the Drive sync loop (30 s cycle), the processing worker and the
export worker. There is no separate API, worker or discovery command to start.

Expected output, in order:

```
=================================
Book Processor Started
Version: 0.1.0
Environment: Development
=================================
...
Application ready.
Waiting for services...
...
Recovered N stuck job(s) on startup
Worker started in background thread
ExportWorker started in background thread
Starting synchronization cycle
Found N files in Drive
X already downloaded
Y new files found
Synchronization complete
```

Failure signs: a traceback ending in `FileNotFoundError` (missing credentials
file), `Configuration … missing` (an unset required variable), `401`/`403`
(service account lacks access to the folder or the sheet), or
`PaddleOCR-VL (Baidu OCR) failed` on every book (token invalid or quota
exhausted). The process keeps running through per-file errors by design.

Leave this window open. Start it again after any crash — it is idempotent and
resumes the queue.

## 6. Health check — Terminal B — **[read-only]**

```powershell
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, StartTime, CPU
```

Expected: at least one `python` process with a recent `StartTime`. Failure: no
process → it stopped; restart with §5.

A stronger check: the log's last line should be less than a minute old (§7), and
each 30 s cycle prints `Synchronization complete`.

## 7. View logs — Terminal B — **[read-only]**

```powershell
Get-Content .\logs\app.log -Tail 40 -Wait
```

Press `Ctrl+C` to stop following (this does not stop the application).
Rotated files are `logs\app.log.1`, `.2`, `.3`.

```powershell
# only the cycle summaries
Select-String -Path .\logs\app.log -Pattern "Synchronization complete|new files found|Rejected " | Select-Object -Last 20

# only errors from the last 2000 lines
Get-Content .\logs\app.log -Tail 2000 | Select-String -Pattern "ERROR|Traceback"
```

## 8. Counts: pending, completed, failed, manual review — **[read-only]**

```powershell
.venv\Scripts\python.exe app.py --status
```

Expected (real output from this machine):

```
Repositories:

  Tracked Drive files: 19
  Jobs: pending=0, processing=0, completed=9, failed=1, retrying=0, manual_review=9
  Identified books: 18
  Export queue: pending=0, retrying=0, dead_letter=0
```

It also prints the configuration status, directory item counts and a
`Dead-Letter Exports` section when exports exhausted their retries. Works whether
or not the application is running (it only reads those SQLite files; it may create
missing database files, nothing else).

If non-Latin titles print as question marks, put the console on UTF-8 first:
`chcp 65001 > $null; $env:PYTHONIOENCODING = "utf-8"` (PowerShell 7 already is).

Books that need a human decision (read-only query):

```powershell
@'
import sqlite3
con = sqlite3.connect("data/metadata.db")
for row in con.execute(
    "SELECT drive_file_id, title, confidence FROM book_metadata "
    "WHERE status='needs_review' ORDER BY confidence"
):
    print(row)
'@ | .venv\Scripts\python.exe -
```

Photos rejected by the intake gate, with the reason (read-only):

The table is created the first time `app.py` starts, so a fresh install prints
`no intake_rejections table yet` until then.

```powershell
@'
import sqlite3
con = sqlite3.connect("data/jobs.db")
try:
    rows = list(con.execute("SELECT reason, COUNT(*) FROM intake_rejections GROUP BY reason"))
except sqlite3.OperationalError:
    print("no intake_rejections table yet - run app.py once to create it")
else:
    print("by reason:", rows)
    for row in con.execute("SELECT file_name, reason, detail FROM intake_rejections LIMIT 20"):
        print(" ", row)
'@ | .venv\Scripts\python.exe -
```

## 9. Isolated Google Sheets acceptance — **[needs input]**

This is the one outstanding live verification. It needs a spreadsheet the
project does not own:

1. Create a new, empty spreadsheet in Google Sheets (any name, e.g.
   `Book Auth acceptance`).
2. Share it with the service-account address, role **Editor**:

   ```powershell
.venv\Scripts\python.exe -c "import json; from core.config import Config; print(json.load(open(Config.from_env().google_sheets_credentials))['client_email'])
   ```

3. Copy the id from the spreadsheet URL
   (`https://docs.google.com/spreadsheets/d/<ID>/edit`).
4. Run (**[writes data]**, isolated spreadsheet only):

```powershell
.venv\Scripts\python.exe scripts\sheets_acceptance_check.py --spreadsheet-id <ID> --tab Acceptance --pdf-out data\sheets_acceptance.pdf
```

Expected: a list of `PASS` lines and `RESULT: LIVE ISOLATED SHEETS ACCEPTANCE
PASSED`. The checks cover the customer-facing `Status` column end to end:
`Status` is column A; a complete record is `Confirmed`; a record without an
ISBN is `Incomplete`; a review record is `Needs Review` and is promoted to
`Confirmed` in the **same row** (row count unchanged); a repeated export does
not append a duplicate; the leading-zero ISBN cell still reads as text; the
cover cell is still `=IMAGE("…")`; no written cell shows a formula error; and
the PDF contains embedded images.

It refuses to run if the id equals `GOOGLE_SHEET_ID`, and a wrong or unshared id
prints `BLOCKED: SpreadsheetNotFound …` (exit code 2). Safe to rerun: it reuses
the same rows instead of appending. If it fails on the *first* run in an empty
spreadsheet, copy the `FAIL` lines into the acceptance record — they name the
exact cell that disagrees.

The PDF at `data\sheets_acceptance.pdf` is Google's own rendering — open it to
see the cover image. The script also reports how many embedded images the PDF
contains.

Delete the spreadsheet by hand afterwards if you wish; the script never deletes
anything.

## 10. Stop the system — Terminal A — **[writes data]** (flushes state)

Press `Ctrl+C` once in Terminal A. Expected:

```
Received keyboard interrupt -- shutting down
Workers stopped
```

The workers are shut down and the SQLite handles closed, so pending jobs and
queue rows survive. If the window is unresponsive, close it or
`Stop-Process -Id <pid>` with the id from §6; a kill is still safe, because job
leases expire and are recovered on the next start.

## 11. Restart or recover after an interruption — **[writes data]**

```powershell
# 1. stop if anything is still running (Ctrl+C in Terminal A)
# 2. check what the previous run left behind
.venv\Scripts\python.exe app.py --status

# 3. start again
.venv\Scripts\python.exe app.py
```

On startup the app calls `recover_stuck_jobs()`: jobs whose heartbeat stopped for
more than `PROCESSING_TIMEOUT_SECONDS` (900 s) go back to `pending`, and jobs out
of retries are marked `failed`. Completed books are never reprocessed, and no
photo is downloaded or OCR'd twice. Expected log line:
`Recovered N stuck job(s) on startup` (N may be 0).

If a book's sheet row never got a cover or an identifier, requeue only the
exports — no OCR is repeated (**[writes data]**):

```powershell
.venv\Scripts\python.exe app.py --requeue-incomplete
```

Expected: `Requeued N export(s) for another pass:` or
`No incomplete exports found - nothing to requeue.` The running export worker
picks them up within one poll. Safe to rerun.

A job that exhausted its retries stays `failed` until you requeue it
(**[writes data]** — the file is not downloaded again, but each requeued photo
costs one OCR call):

```powershell
# every failed job
.venv\Scripts\python.exe app.py --requeue-failed-jobs

# a single photo
.venv\Scripts\python.exe app.py --requeue-failed-jobs --drive-id <DRIVE_FILE_ID>
```

Expected: `Requeued N failed job(s):` followed by
`<file name> (Drive ID <id>)`, or `No failed jobs found - nothing to requeue.`
Only `failed` jobs are touched — completed books, manual-review records and
skipped duplicates keep their state — and each one restarts with a fresh retry
budget (retry count, error message and timestamps cleared). The worker resumes
from the file already in `data\downloads`, so nothing is downloaded twice.
Safe to rerun; if the photo itself is unusable, requeueing only spends an OCR
call — upload a better shot instead (new Drive id).

## 12. Backup — **[read-only]** for the source, **[writes data]** for the copy

Stop the application first (§10), then:

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest  = "C:\Backups\bookauth-$stamp"
New-Item -ItemType Directory -Path $dest | Out-Null
Copy-Item .\data\app.db, .\data\jobs.db, .\data\metadata.db -Destination $dest
Copy-Item .\data\covers -Destination $dest -Recurse
```

`data\downloads\` and `data\debug\` are regenerable and do not need backing up.
Also back up `.env` and the `credentials\` folder (keep them out of the shared
backup share; they are secrets) and `data\covers\` — the sheet formulas point at
remote cover URLs, but `covers\` is what the exporter checks when deciding
`Incomplete`.

Retention: keep 7 daily copies plus one copy after every upgrade. A backup is
worth nothing until it has been opened once.

### 12.1 Restore — **[writes data]**

1. Stop the application (§10) and confirm no `python` process is running:
   `Get-Process python -ErrorAction SilentlyContinue`.
2. Move the current `data\` aside instead of deleting it:
   `Rename-Item .\data ("data.before-restore-" + (Get-Date -Format yyyyMMdd-HHmmss))`.
3. Copy the chosen backup back: `Copy-Item <backup>\* .\data -Recurse`.
4. Put `.env` and `credentials\` back from your secret store, not from a
   shared folder.
5. Verify integrity before starting:

   ```powershell
   .venv\Scripts\python.exe -c "import sqlite3; [print(f, sqlite3.connect(f'file:{f}?mode=ro', uri=True).execute('PRAGMA integrity_check').fetchone()[0], sqlite3.connect(f'file:{f}?mode=ro', uri=True).execute('PRAGMA foreign_key_check').fetchall()) for f in ('data/app.db','data/jobs.db','data/metadata.db')]"
   ```

   Expected: `ok []` for each file.
6. Start the application (§5) and check `app.py --status`: counts must match the
   backup's date, and the export queue must be empty or explainable.
7. Let one cycle finish. Do **not** restore a `data\` folder into a running
   process, and do not copy only one of the three databases — the ids in
   `jobs.db` and `metadata.db` reference each other.

## 13. Optional: check the Google Sheet contents from the command line — **[read-only]**

```powershell
@'
import sys
sys.path.insert(0, ".")
from core.config import Config
from services.google_sheets_service import GoogleSheetsService
c = Config.from_env()
svc = GoogleSheetsService(
    credentials_file=c.google_sheets_credentials,
    sheets_id=c.google_sheet_id,
    tab_name=c.google_sheets_tab_name,
)
with svc:
    rows = svc.read_all_rows(ensure_layout=False)
print("rows:", len(rows))
for row in rows[-5:]:
    print(row.get("Title"), "|", row.get("Authors"), "|", (row.get("Open Cover") or "")[:40])
'@ | .venv\Scripts\python.exe -
```

This only reads. `ensure_layout=False` is what makes that true: without it the
service inserts the `Status` column as a side effect of opening the worksheet.
`scripts/read_sheet_state.py` is the safer version of the same check and prints
the real header.

Do not use `scripts/reset_pipeline.py`: it clears `metadata.db` and the
production sheet's data rows.

### 13.1 Migration state of the live workbook

The production tab **already has the `Status` column**. Re-verified read-only on
2026-11-23: 19 rows (1 header + 18 records), 10 columns in canonical order, all 18
Status cells blank, 14 cover `=IMAGE(…)` formulas intact, no metadata formula
anywhere. See `docs/INCIDENT-STATUS-COLUMN.md` for how that happened and
`docs/DEPLOYMENT_READINESS.md` §6 for the measurement table. Two options, both
need the owner's approval because both touch customer-visible data:

- **Finish it** — back up (§12), stop the app (§10), run
  `python app.py --refresh-sheet-status`, then verify 18 Status cells are
  populated and no other cell changed.
- **Roll it back** to the original nine columns — in the Sheets UI select column
  A and *Delete column*. Nothing but the header lives there, so no data is lost.
  Confirm `A1` is again `Title`, that `Open Cover` is the 9th column and that
  every row's values/formulas are unchanged. Take the §12 backup first, and
  re-run §13 afterwards to confirm the header is back to the nine original
  columns.

### Re-label existing rows with the Status column — **[writes data]**

When the `Status` column is added to an existing spreadsheet, every row that
pre-dates it keeps a blank Status cell. A blank cell is never treated as
`Confirmed`. Fill them once, after the application has been stopped (§10) and
after a backup (§12):

```powershell
.venv\Scripts\python.exe app.py --refresh-sheet-status
```

Expected output:

```
Books in local database: 27
Rows updated:  18
Already right: 0
Not in sheet:  9
```

- **Only Status cells are written** (RAW), one cell per matched row, matched by
title and authors. No row is appended, moved or deleted and no other cell is
touched — covers, ISBNs and descriptions are left exactly as they are.
- `Not in sheet` counts records whose title/author do not appear in the sheet;
  they are booked by the next normal export run, not by this command.
- Safe to rerun (idempotent). Run it **after** upgrading and **before** telling
  a customer the sheet is current; the file `data\metadata.db` is the source of
truth, so re-running after new data always gives the newer label.
- The column itself is created by Google the first time the upgraded code
  touches the sheet (`insert_cols` at column A, once only). Rows shift right
  with their values and formulas intact; the app rewrites nothing during that
  step. Verify once in the browser: `Status` is column A, every existing title /
  ISBN / cover is one column further right and unchanged, and no row is
  duplicated.
- **Approval required — this is a customer-visible schema change.** The
  insert happens automatically on the first export after the upgrade, so
  take the backup in §12 and tell the customer before restarting the app
  against the production sheet. If you are not ready for the column yet,
  stop the app rather than letting a cycle run (there is no dry-run flag).
- `Needs Review` rows are never turned into `Confirmed` by this command: the
  label comes from `data\metadata.db`, so resolve the book there first (or
  reprocess it) and re-run.

## 14. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `Missing: GOOGLE_DRIVE_FOLDER_ID` at startup | `.env` not loaded (wrong working directory) or variable empty | `cd` to the project root, re-run §3 |
| `/` persists as `403 The caller does not have permission` | folder or spreadsheet not shared with the service account | share folder (Viewer) and sheet (Editor) with the address from §9.2 |
| Every book ends `(unreadable)` / `needs_review` | `PADDLEOCR_API_TOKEN` invalid/expired, or Baidu quota exhausted | check `logs\app.log` for `PaddleOCR-VL (Baidu OCR) failed`; replace the token in `.env` |
| `Rejected … REJECTED_HEIC_UNSUPPORTED` | iPhone HEIC upload | switch the camera to JPEG, re-upload |
| `Rejected … REJECTED_FILE_TOO_LARGE` | photo over 20 MB | compress or resize, re-upload |
| Sheet rows missing but `Identified books` grows | export queue retrying/dead-letter | `app.py --status`, then `--requeue-incomplete` |
| `database is locked` | two `app.py` processes | stop one (§6, §10) |
| Job stuck at `failed` | retries exhausted, or a permanent error such as an unreadable image | `app.py --requeue-failed-jobs` (add `--drive-id <id>` for one photo) |
| Cover cell blank in the sheet | cover not resolved (`cover_status` not `found`) | `--requeue-incomplete` after the app has run once more |
