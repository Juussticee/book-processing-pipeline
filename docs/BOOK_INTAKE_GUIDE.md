# Book intake guide

How to get book-cover photos into the pipeline, how to follow their progress, and
what appears in the Google Sheet. Commands are PowerShell from the project root
(`C:\Users\hashe\Documents\Book Auth`); see `docs/RUNBOOK.md` for the full
operator commands.

---

## 1. Where the photos go

There is exactly **one** monitored Google Drive folder, configured by
`GOOGLE_DRIVE_FOLDER_ID`. Print it with:

```powershell
.venv\Scripts\python.exe -c "from core.config import Config; print(Config.from_env().google_drive_folder_id)"
```

Facts that follow from the implementation (`services/drive_service.py`):

- The system lists files whose parent is that folder only
  (`'<folder id>' in parents and trashed = false`).
- **Subfolders are not scanned.** A subfolder appears in the listing, is rejected
  as `REJECTED_DIRECTORY`, and nothing inside it is ever discovered. Put photos
  directly in the configured folder, flat.
- One folder means one collection. If you need to separate batches, rename files
  with a prefix rather than creating subfolders.
- Trashed files are ignored. Deleting a photo from Drive after processing does
  not remove its book record or its sheet row.

Share the folder with the service-account address as **Viewer** (see
`docs/RUNBOOK.md` §9 for the command that prints it). Uploading is done by you
from the normal Google Drive web UI, the Drive desktop app, or the Drive mobile
app — the system only reads.

## 2. Accepted and rejected files

Accepted (decided from Drive metadata before anything is downloaded):

| Format | MIME | Extensions |
|---|---|---|
| JPEG | `image/jpeg`, `image/jpg` | `.jpg`, `.jpeg` |
| PNG | `image/png` | `.png` |
| WebP | `image/webp` | `.webp` |
| TIFF | `image/tiff` | `.tiff` |
| BMP | `image/bmp` | `.bmp` |

Rejected, with the reason recorded in `intake_rejections`:

| Reason | Meaning |
|---|---|
| `REJECTED_HEIC_UNSUPPORTED` | iPhone HEIC/HEIF. Switch the camera to **JPEG** (or export as JPEG) and re-upload |
| `REJECTED_FILE_TOO_LARGE` | larger than 20 MB (`IMAGE_MAX_FILE_SIZE_MB`) |
| `REJECTED_UNSUPPORTED_TYPE` | PDF, DOCX, Google Docs/Sheets/Slides, video, etc. |
| `REJECTED_DIRECTORY` | a subfolder inside the monitored folder |
| `REJECTED_BLOCKED_NAME` | OS junk: `Thumbs.db`, `desktop.ini`, `.DS_Store`, `Icon\r`, `.localized` |

A rejected file is never downloaded, never becomes a job, and never retries —
fix or convert the file, re-upload it, and it is picked up as new.

## 3. Photo quality that the pipeline can actually read

Not enforced by the code, but this is what the OCR and extractor need:

- The **front cover fills most of the frame**, roughly upright. Tilted or
  perspective shots are fine (`angle.jpg` was identified live), as are
  upside-down ones (`rot180.jpg` was identified live at confidence 0.89) — but
  sideways 90°/270° shots are unproven on real photos.
- Title and author legible at normal screen size; avoid motion blur, heavy
  glare from shrink-wrap, and long shadows.
- One book per photo. Two covers in one frame will confuse both OCR and the
  metadata lookup.
- 1000–2500 px on the long edge is plenty. `ImagePreprocessor` downscales the
  long edge to 1600 px before OCR, so bigger originals work but gain nothing.

## 4. First-time bulk upload and processing

1. Open the Drive folder from §1 in a browser or the Drive app.
2. Upload **all** cover photos directly into it. Do not create subfolders. Keep
   the originals in Drive — the system never modifies or deletes them.
3. Make sure `.env` has the four Google values and `PADDLEOCR_API_TOKEN`, then
   start the system in Terminal A:

   ```powershell
   cd "C:\Users\hashe\Documents\Book Auth"
   .venv\Scripts\python.exe app.py
   ```

   That is the whole "batch command". On its first cycle (within 30 seconds) it
   lists the entire folder, applies the intake gate, downloads every accepted
   photo, and creates one job per photo. There is no separate import, scan or
   batch trigger, and no need to touch SQLite.

4. Watch the first cycle in the log (**[read-only]**):

   ```powershell
   Get-Content .\logs\app.log -Tail 40 -Wait
   ```

   Expected for a large upload:

   ```
   Starting synchronization cycle
   Found 120 files in Drive
   0 already downloaded
   120 new files found
   Downloading:
   Saved: data\downloads\<id>_cover-001.jpg
   [Job …] Created job for Drive ID …
   Rejected IMG_0421.HEIC (REJECTED_HEIC_UNSUPPORTED): …
   Synchronization complete
   ```

   Downloads happen in that one cycle; OCR then runs one book at a time in the
   background. A book typically takes 30–70 seconds (OCR ≈ 20–35 s plus lookup
   and cover retrieval), so 100 books is roughly 1–2 hours.

5. Follow progress from Terminal B with `app.py --status` (§6) — `pending` drains,
   `completed`/`manual_review` grows, `Identified books` grows.

## 5. Ongoing (daily) workflow

1. Upload new photos into the same folder, from phone or computer. Nothing else
   to trigger: the running app discovers them on the next 30-second cycle.
2. If the app is not running, start it (§4.3); the first cycle picks up
   everything uploaded while it was down.
3. Check `app.py --status` at the end of the day and read the sheet.

Uploading the **same file twice** is handled: an identical image under a new
Drive id is cancelled as `exact_duplicate_of:<original>` with no OCR call and no
second row. A *different photograph of the same physical book* is processed and
kept, and flagged internally as `possible_same_book` (two rows are expected in
that case — the system does not merge distinct photos).

## 6. Knowing what happened to each photo

| Question | Where to look |
|---|---|
| Was it accepted? | `Saved: data\downloads\…` in `logs\app.log`, then a row in the sheet |
| Was it rejected, and why? | `Rejected <name> (<REASON>): <detail>` in the log, or the read-only query in `docs/RUNBOOK.md` §8 (`intake_rejections` table) |
| Waiting for OCR? | `--status`: `pending` / `processing` are non-zero |
| Waiting for the sheet? | `--status`: `Export queue: pending=…`; the `ExportWorker` polls continuously |
| Cover missing? | the `Open Cover` cell is empty; `cover_status` in `data/metadata.db` says `not_found` / `pending_retry` |
| Needs a human decision? | `--status` line `manual_review=…`, and the query in `docs/RUNBOOK.md` §8 |
| Failed outright? | `--status` `failed=…`, `Dead-Letter Exports` section, and `ERROR` lines in the log |
| Retrying automatically? | `retrying` counts; job retries back off 5 s → 300 s, exports retry up to 3 times |

Is the whole batch finished? All of these at once:

- `--status` shows `pending=0, processing=0, retrying=0` and
  `Export queue: pending=0, retrying=0, dead_letter=0`;
- the log's latest cycle reports `0 new files found` and `Synchronization complete`;
- the number of data rows in the sheet equals `Identified books`.

## 7. What appears in the Google Sheet

One row per identified photo, in this column order
(`services/google_sheets_service.py::COLUMNS`):

| Column | Source | Can be empty? |
|---|---|---|
| `Status` | derived from the stored record | never empty after export |
| `Title` | OCR + metadata lookup | only for review rows (may show `(unreadable)`) |
| `Authors` | lookup/extraction | yes, if the cover has no author |
| `ISBN-13` | lookup | **often** — many older French/Swiss books have no ISBN in Google Books / Open Library |
| `Publisher` | lookup | sometimes (e.g. the live *Le mal été* record had none) |
| `Published Date` | lookup | sometimes |
| `Language` | lookup | sometimes |
| `Categories` | lookup | often empty for small publishers |
| `Description` | lookup | often empty |
| `Open Cover` | `=IMAGE("…")` when resolved | yes, when no cover was found |

### What `Status` tells you

| `Status` | Meaning | What to do |
|---|---|---|
| `Confirmed` | The book was identified and the row has an ISBN, a publisher and a cover. | Nothing. |
| `Incomplete` | The book was identified, but the publisher or the cover is missing. Usually the catalogue record is thin — the book itself is correct. A missing ISBN does **not** make a row Incomplete. | Optional: `app.py --requeue-incomplete` retries the metadata lookup (no OCR is repeated). |
| `Needs Review` | The pipeline was not sure, or a human marked it for review. Fields may look complete — trust the label, not the fields. | Check §8 (manual review) and the DB; the row is never presented as confirmed. |

A blank `Status` means the row was written before the column existed. It is
**not** `Confirmed`; relabel existing rows once with
`python app.py --refresh-sheet-status` (only Status cells are written — no row
is added, moved or deleted).

Missing description, published date, language or categories never change the
label: those are enrichment fields and are often empty for older books.

Notes that follow from the code:

- Metadata cells are written with `RAW`, so they stay text — nothing is
  interpreted as a formula, and leading zeros survive.
- Only the cover cell uses `USER_ENTERED` and contains exactly
  `=IMAGE("https://…")`. Google fetches the image itself; a blank of that cell
  means no cover URL was resolved, and a `#N/A` inside it means Google could not
  fetch the URL.
- **Page count is stored in the database but is not a sheet column**, so 148
  pages for *Le cœur vert* exists in `data\metadata.db` only.
- Updates never append: re-exporting a book rewrites its existing row (the row is
  matched and its title verified first). Two identical photos produce one row.
- **Review rows are exported and marked.** The exporter writes review records
  too, but their `Status` is `Needs Review`, so a photo the pipeline was unsure
  about can never be mistaken for a confirmed book — even when its title looks
  plausible or is a placeholder such as `(unreadable)`.

Typical row states:

| What you see | Meaning |
|---|---|
| `Confirmed` with title, author, publisher and cover | confidently identified and exported |
| `Incomplete` with title + author, `Publisher`/cover empty | identified, but the catalogue record is thin — normal |
| `Confirmed` with an empty `ISBN-13` | identified and complete; the book simply has no ISBN |
| `Needs Review`, maybe `(unreadable)` or a fragment of spine text | not accepted automatically; the photo may be unusable |
| Row missing although the photo was uploaded | still queued (check `--status`) or rejected by the intake gate (check §6) |

## 8. Recovering a specific failure

1. `app.py --status` → note the failed count and the dead-letter list.
2. For missing covers/identifiers on books that already have a row:
   `app.py --requeue-incomplete` (no OCR is repeated).
3. For a photo whose job exhausted its retries, requeue just that job — no
   re-download, one OCR call:

   ```powershell
   .venv\Scripts\python.exe app.py --requeue-failed-jobs --drive-id <DRIVE_FILE_ID>
   ```

   `app.py --requeue-failed-jobs` on its own requeues every failed job. If the
   photo itself is unusable, upload a better shot under a new name instead: it
   becomes a new Drive id, and the old failure stays in the history without
   blocking anything.
4. For a rejected format/size: convert and re-upload — nothing else needed.
5. Never edit the SQLite files by hand and never call the OCR API directly; the
   worker owns retries and the duplicate gate.

## 9. Who does what

Everyday work is uploads and reading the sheet. Everything else belongs to the
person who runs the host.

**Operators can do this alone**

| Task | How |
|---|---|
| Upload photos | §1 |
| Check whether a photo was processed | `python app.py --status` (§6), then the sheet's `Status` column |
| Ask for a retry of a failed photo | `python app.py --requeue-failed-jobs --drive-id <DRIVE_FILE_ID>` (§8) |
| Fill in missing covers for rows that already exist | `python app.py --requeue-incomplete` (§8) |
| Read the application log | `logs\app.log` (§6) |

Those commands only touch the local queue. They do not re-run OCR on books that
already succeeded and they never delete rows.

**Administrator only — do not attempt these**

- Editing the SQLite files in `data\` by hand, or running SQL against them.
- Deleting or reordering columns in the Google Sheet, or editing rows by hand.
  The application matches rows by title and author; a hand-edited title creates
  a duplicate row on the next export.
- Changing `.env`, API keys, the service-account JSON, or sharing settings on
  the Drive folder / spreadsheet. All three are credential changes.
- Installing, stopping or restarting the background service
  (`nssm restart BookAuth` on Windows, `systemctl restart book-auth` on Linux).
  Only the administrator restarts it — a restart in the middle of a batch is
  safe, but it is their call.
- Replacing the OCR provider or any code. The production OCR provider is fixed
  to the hosted PaddleOCR-VL API.
- Running `scripts/reset_pipeline.py` — it deletes records.

**If something looks wrong**

1. Note the sheet row (title) and the photo's file name.
2. Run `python app.py --status` and keep the output.
3. Send both to the technical maintainer. Do not try to fix the database.

The technical maintainer is the person who installed the application; their
contact details are recorded by the owner outside this repository. Do not put
phone numbers or e-mail addresses in this file — it is version-controlled.

**Signs that processing has stopped**

- `--status` shows `pending` growing while `completed` does not move.
- The sheet keeps the same row count across a working day with photos arriving.
- `logs\app.log` has no new cycle lines, or repeats the same error.

In all three cases, contact the administrator — do not restart or reinstall
anything yourself.
