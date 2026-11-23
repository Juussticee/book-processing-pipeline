# Book Processing System — Customer Readiness Audit

**Status:** Stage 1 (code + offline verification) complete. Stage 2 (real photographs) blocked on
user-supplied images. No live OCR, no live Drive scan, no production Sheet writes were performed.

**Date:** 2026-11-06
**Scope:** Google Drive folder → duplicate check → orientation → OCR → identification →
persistence → Google Sheets export, for both processing pathways in this repository.

**Update (customer-ready release):** the gaps marked FAIL / NOT IMPLEMENTED below
were addressed afterwards. The matrix in §2 records the state at audit time;
see §9 for the current status of each item and the test that proves it.

**Verdict at audit time:** **NOT READY for a paying customer without supervision.**
The identification core is solid (known-good book still resolves `identified` / confidence
`0.9965` fully offline, see §4.1). The blockers are around the *edges* of the workflow: there is no
customer-facing entry point, no protection against near-duplicate or same-book submissions, and the
deployment target (SQLite + long-running worker threads on a local disk) cannot run on Vercel.

---

## 1. What was inspected

| Area | File |
|---|---|
| Sync loop, worker startup, status report | `app.py` (`run_sync_engine`, `main`) |
| Drive access | `services/drive_service.py` |
| Job lifecycle, dedupe, retries, crash recovery | `services/job_service.py`, `repositories/job_repository.py` |
| Worker loop, failure isolation | `workers/base_worker.py`, `workers/processing_worker.py` |
| Image normalisation and EXIF rotation | `services/image_preprocessor.py` |
| OCR + orientation fallback | `services/metadata_service.py` (`identify_book`, `_try_orientation_correction`) |
| Standalone batch path | `drive_batch.py` |
| Persistence | `repositories/book_metadata_repository.py`, `repositories/export_repository.py` |
| Sheets export | `services/export_service.py`, `services/google_sheets_service.py` |

Two independent pathways exist and were audited separately, because they behave differently:

1. **Worker pipeline** (`app.py`): Drive → jobs table → 2 background worker threads → DB → export queue → Sheets.
2. **Batch script** (`drive_batch.py`): Drive → in-memory processing → Sheets + local audit docs. No jobs table, no DB persistence, no resume.

---

## 2. Feature matrix

Statuses: **PASS** (verified offline with evidence), **PARTIAL**, **FAIL**, **NOT IMPLEMENTED**,
**NOT VERIFIED** (needs the real-data stage).

| # | Capability | Status | Evidence / reason |
|---|---|---|---|
| 1 | Discover images in the configured Drive folder | PASS | `DriveService.list_all_files` paginated listing; mocked in `test_pagination_250_files_exactly_once` |
| 2 | Paginate large folders without duplicates or gaps | PASS | 250 files / 3 pages → each Drive ID seen exactly once |
| 3 | Incremental scan (only new files) | PASS | 100 files then 120 → exactly 20 new jobs (`test_incremental_scan_adds_only_new_files`) |
| 4 | Rescan after restart does not duplicate work | PASS | Restart scan creates 0 new jobs (`test_restart_rescan_creates_no_new_jobs`) |
| 5 | Detect *modified* files (replaced photo, same Drive ID) | **NOT IMPLEMENTED** | Sync loop skips any `drive_file_id` already in `book_metadata`; `modifiedTime` is never compared. A re-uploaded/corrected photo is ignored permanently |
| 6 | Nested sub-folders | **NOT IMPLEMENTED** | Query is direct children only (`'<folderId>' in parents`); sub-folders are skipped silently |
| 7 | Non-image files in the folder | **FAIL** | `app.py` applies **no MIME filter**: every file in the folder is downloaded and turned into a job, which the worker then fails (and retries 3×) because the extension is unsupported. `drive_batch.py` *does* filter (`jpeg/jpg/png/webp`), so the two paths disagree |
| 8 | Supported formats | PARTIAL | Worker: `.jpg .jpeg .png .tiff .bmp .webp`; batch: 4 MIME types only. No ZIP, no HEIC (typical iPhone default), no PDF |
| 9 | Oversized image protection | PARTIAL | 20 MB post-download check (`MAX_FILE_SIZE_BYTES`); the `IMAGE_MAX_WIDTH/HEIGHT` config keys are declared but never read (the preprocessor uses a hardcoded 1600 px cap). No pre-download size check, so a 500 MB file is fully downloaded first |
| 10 | EXIF orientation correction (tags 3/6/8) | PASS | Real JPEGs written with each tag: output upright, original untouched, no cumulative rotation on repeat runs |
| 11 | Mirror / tag 1 / tag 2 / tag 4 / tag 5 / tag 7 | FAIL (by design) | Only 3/6/8 handled; flipped or transposed EXIF tags are ignored |
| 12 | Visual rotation fallback (no EXIF, sideways/upside-down) | PARTIAL | Works, but only fires when OCR yields <3 words **or** confidence <0.30. A sideways cover that OCRs into 4+ junk words at ≥0.30 confidence is never re-oriented. Simulated pass for 90/180/270 (`test_upside_down_or_sideways_image_is_reoriented`) |
| 13 | Upright image not needlessly re-OCR'd | PASS | Fallback skipped when OCR quality is sufficient (`test_upright_image_is_not_reoriented`) |
| 14 | Real-world blur / glare / angle / shadow | **NOT VERIFIED** | Needs real photographs. No fixture images exist in the repo (`tests/fixtures` is empty) |
| 15 | Duplicate by Drive file ID | PASS | Single job for repeated sync cycles of the same ID; `book_metadata.drive_file_id UNIQUE`; export tables unique on `drive_file_id`/`job_id` |
| 16 | Duplicate by identical bytes (same photo, two Drive IDs — copy, re-upload, shared twice) | **NOT IMPLEMENTED** | Two files with identical bytes but different IDs produce **2 jobs and 2 sheet rows** (`test_identical_bytes_different_ids_not_deduplicated`). `DriveService.get_file_checksum` (MD5) exists but is never used for dedupe |
| 17 | Near-duplicate (same photo: resized, re-compressed, cropped) | **NOT IMPLEMENTED** | `CoverVerifier` has a dHash, but it is only used to score *candidate cover images* against the submitted photo — never to compare submissions with each other |
| 18 | Same book submitted twice (different photo of the same copy) | **NOT IMPLEMENTED** | Rows are keyed by Drive ID only; no ISBN/title+author merge or "already in sheet" warning at intake |
| 19 | Job queue, FIFO order, one at a time | PASS | `test_scenario_4_fifo_processing_order`, batch audit tests |
| 20 | Retry with backoff, max 3 attempts | PASS | 100/250/500-file batches with one failing file → `completed = size-1`, failing job `retrying` then `failed` after `MAX_RETRIES` |
| 21 | One bad file does not stop the batch | PASS | Same tests; worker loop continues, consecutive-failure circuit breaker only after repeated failures |
| 22 | Crash recovery on restart | PASS (with caveat) | `recover_stuck_jobs` resets `processing` → `pending`, but **only after 7200 s (2 h)**, so a crash-restart within 2 h leaves the file stuck until the timeout passes (`test_stuck_job_is_recovered_after_restart`, `test_recently_started_job_is_not_prematurely_recovered`) |
| 23 | Completed work never reprocessed | PASS | Sync loop skips known IDs; `create_job_for_file` may create a fresh job only if called directly, which the loop never does |
| 24 | Unidentifiable book → manual review, never a hard failure | PASS | Low confidence routes to `manual_review` with a candidate summary |
| 25 | Known-good regression: *Le cœur vert* / Jean-Louis Kuffer | PASS | Offline end-to-end with real extractor/ranker/confidence: `title="Le cœur vert"`, `authors=["Jean-Louis Kuffer"]`, `status="identified"`, `confidence=0.9965`, job `completed` |
| 26 | Persistence of metadata, ISBN with leading zeros | PASS | Existing unit coverage; ISBN kept as text end-to-end |
| 27 | Sheets export (mixed RAW / `USER_ENTERED`, `=IMAGE()` covers) | PASS | Prior milestone verification: 12 `=IMAGE()` formulas, 0 `#ERROR!`; regression tests for human-edited title rows |
| 28 | Export failure isolation / idempotency | PASS | `export_queue` / `export_records` unique keys, retry on failure |
| 29 | Structured run summary for the customer ("N found, M new, K duplicates") | PARTIAL | Batch script writes `BatchAudit` + local audit files; the worker pipeline only logs. No durable per-run report |
| 30 | Customer-facing frontend / upload UI / status page | **NOT IMPLEMENTED** | No web app, no API, no auth. Customer must share a Drive folder with the service account and read the Sheet |
| 31 | Multi-tenant (several customers / folders) | **NOT IMPLEMENTED** | One `GOOGLE_DRIVE_FOLDER_ID` per deployment |
| 32 | Deployment to the planned Vercel target | **FAIL** | SQLite file + 2 long-running worker threads + local temp files. Vercel serverless has an ephemeral, read-only filesystem and no background threads. No Dockerfile/Procfile/Vercel config exists |

---

## 3. Critical findings (ordered by customer impact)

1. **No content-based duplicate protection (matrix 16–18).** The most likely real-world failure:
   the customer photographs a cover, doesn't see it in the Sheet yet, and photographs it again.
   Result: a second job, a second OCR call (cost), and a duplicate row. Near-duplicates and
   "same book, different photo" are entirely undefended.
2. **Non-image files become failing jobs (matrix 7).** Any PDF, invoice, `.DS_Store`, or
   `Thumbs.db` dropped in the folder is downloaded, queued, and retried 3× before failing. In a
   shared folder this is guaranteed to happen.
3. **No HEIC support (matrix 8).** iPhones upload HEIC by default; those files are silently
   unsupported (batch path) or fail as jobs (worker path).
4. **Orientation fallback can be skipped on sideways photos (matrix 12).** The gate is OCR *quality*,
   not geometry. Sideways covers that produce junk-but-plausible text are accepted as-is.
   Needs real-photograph confirmation.
5. **2-hour stuck-job window (matrix 22).** After an unclean shutdown, the operator must wait up to
   2 h (or reset manually) before those files are retried.
6. **Cannot be hosted on Vercel as designed (matrix 32).** Architecture change (object storage +
   queue + stateless API) or a long-running host (Fly/Render/VM) is required before any customer
   deployment.
7. **Minor, non-blocking:** `_save_debug_artifacts` uses the platform default encoding and raises
   `UnicodeEncodeError: 'charmap' … '\xc9'` on Windows when OCR text contains accented capitals; it
   is caught and logged, so processing continues, but debug artifacts are lost for French titles.

---

## 4. Evidence

All evidence below is reproducible offline (no network, no credentials, no Drive access).

### 4.1 Audit test file

`tests/unit/test_customer_readiness_audit.py` — new file, does not modify existing tests.

```bash
.venv/Scripts/python.exe -m pytest tests/unit/test_customer_readiness_audit.py -q
# 22 passed, 1 warning in 56.15s   (log: docs/customer-readiness/evidence/pytest-audit.txt)
.venv/Scripts/python.exe -m ruff check tests/unit/test_customer_readiness_audit.py   # All checks passed
.venv/Scripts/python.exe -m black --check tests/unit/test_customer_readiness_audit.py # unchanged
```

| Test | Proves |
|---|---|
| `test_pagination_250_files_exactly_once` | 3-page pagination, no gaps/dupes |
| `test_incremental_scan_adds_only_new_files` | 100 → 120 files = 20 jobs |
| `test_restart_rescan_creates_no_new_jobs` | restart is safe |
| `test_same_drive_id_is_processed_once` | ID dedupe |
| `test_identical_bytes_different_ids_not_deduplicated` | **gap**: byte-identical copies both processed |
| `test_exif_orientation_is_applied[1/3/6/8]` | real JPEG EXIF handling |
| `test_original_image_is_never_modified` | source file untouched |
| `test_repeated_preprocessing_does_not_accumulate_rotation` | idempotent normalisation |
| `test_upside_down_or_sideways_image_is_reoriented[90/180/270]` | visual fallback finds the upright angle |
| `test_upright_image_is_not_reoriented` | no wasted OCR |
| `test_one_bad_file_does_not_stop_the_batch[100/250/500]` | isolation + retry accounting |
| `test_stuck_job_is_recovered_after_restart` | crash recovery (backdated 3 h) |
| `test_recently_started_job_is_not_prematurely_recovered` | recovery does not steal live work |
| `test_completed_job_is_not_reprocessed` | sync-loop skip |
| `TestKnownGoodBook::test_identified_with_high_confidence_and_completed` | *Le cœur vert* regression end-to-end |

### 4.2 Existing suites (unmodified)

```bash
.venv/Scripts/python.exe -m pytest tests/unit/test_drive_service.py tests/unit/test_image_preprocessor.py \
  tests/unit/test_milestone7_batch.py tests/unit/test_job_service.py \
  tests/integration/test_drive_sync.py tests/integration/test_processing_pipeline.py \
  tests/integration/test_export_pipeline.py -q
# 105 passed in 25.40s  (log: docs/customer-readiness/evidence/pytest-existing-pipeline.txt)

.venv/Scripts/python.exe -m pytest tests/unit/test_phase43_regression.py -q
# 16 passed
```

Known baseline for reference (unchanged by this audit): integration suite `93 passed` + 5
pre-existing vision-model failures; full unit suite `1113 passed, 2 skipped`.

### 4.3 Verification of this hardening pass

All commands run on Windows, Python 3.11.9, `.venv/Scripts/python.exe`, no network.

| Command | Result | Log |
|---|---|---|
| `pytest tests/unit/ -q` | **1272 passed, 2 skipped** | `evidence/pytest-unit-final2.log` |
| `pytest tests/integration/ -q` | 93 passed, **5 failed** (the documented vision baseline, unchanged) | `evidence/pytest-integration-final2.log` |
| `pytest tests/ --tb=short -q` | **1365 passed, 2 skipped, 5 failed** (same five) | `evidence/pytest-full-final2.log` |
| `pytest tests/unit/test_batch_acceptance.py -q` | 5 passed (100 / 250 / 500 photos) | — |
| Offline batch breakdown, 3 folder sizes | non-overlapping categories that reconcile at each stage | `evidence/batch-stats.txt`, `evidence/batch-breakdown.md` |
| `ruff check .` | 912 pre-existing findings repo-wide; **0 in the files changed here** | — |
| `black --check .` | 58 pre-existing files would be reformatted, 1 cannot be parsed (`generate_benchmark.py`); **every file changed here is clean** (the remaining `services/job_service.py` diff is on untouched retry lines that were already unformatted at `HEAD`) | — |
| `mypy .` | **blocked**: `generate_benchmark.py:145` has a pre-existing f-string syntax error, so mypy stops before checking anything | — |
| `mypy app.py services repositories workers` | 57 pre-existing errors in 23 files; none in the modules changed here (the two hits in `base_worker.py`/`app.py` are untouched lines) | — |

Test-count delta versus the pre-pass baseline (1315 → 1365 passed) is the new
coverage for the same-book policy, EXIF/corner matrix, lease recovery, and the
batch-accounting rewrites. No existing test was deleted, skipped or weakened.

The five failures are the same five before and after this pass:

```
tests/integration/test_vision_benchmark.py::TestBenchmark::test_no_books_silently_lost
tests/integration/test_vision_first_routing.py::TestVisionFirstRouting::test_good_ocr_does_not_trigger_vision
tests/integration/test_vision_first_routing.py::TestVisionFirstRouting::test_vision_not_called_for_fake_image
tests/integration/test_vision_first_routing.py::TestVisionRecoveryResults::test_yugoslavia_vision_extraction
tests/integration/test_vision_first_routing.py::TestRegressionCases::test_vision_extracts_all_problem_books
```

They live in the vision routing/extraction layer, which this pass was not
allowed to touch; they are reported, not fixed.

---

## 5. What the customer must be told today (draft instructions)

1. Put photos in **one** Google Drive folder shared with the service account — **flat**, no
   sub-folders.
2. **JPEG or PNG only** (disable HEIC on the phone: iOS *Settings → Camera → Formats → Most
   Compatible*).
3. One file per book, photographed **upright**, front cover filling the frame, no flash glare.
4. Do **not** re-upload a corrected photo under a new name — it will be processed as a second book.
   Tell the operator instead.
5. Results appear in the Sheet; `status` = `identified` (confident), `manual_review` (needs a
   human), `failed` (unreadable).
6. Cover images in the Sheet require the workbook to allow external image URLs.

These instructions are only safe once findings 1–3 are fixed or accepted as operator-supervised
limitations.

---

## 6. Deployment readiness

| Target | Verdict |
|---|---|
| Local Windows machine / mini-PC (current design) | READY WITH OPERATOR SUPERVISION |
| Single Linux VM with a persistent disk | READY (needs supervisor + backup of the SQLite file) |
| Docker container with a mounted volume | READY after a Dockerfile is added (none exists today) |
| Vercel serverless | **NOT READY** — no persistent FS, no worker threads, no queue, no frontend |
| Multi-customer SaaS | **NOT READY** — single folder per deployment, no auth/tenancy |

---

## 7. Files changed by this audit

| File | Change |
|---|---|
| `tests/unit/test_customer_readiness_audit.py` | **New.** Offline audit/evidence tests (22). |
| `docs/CUSTOMER_READINESS_AUDIT.md` | **New.** This document. |
| `docs/customer-readiness/evidence/pytest-audit.txt` | **New.** Raw pytest log for the audit file. |
| `docs/customer-readiness/evidence/pytest-existing-pipeline.txt` | **New.** Raw pytest log for the pre-existing pipeline suites. |

That was true of the audit itself. The hardening pass that followed did change
production modules and tests; the current state of every finding is in §9, the
verification in §4.3, and the outstanding real-photo gate in §10.

---

## 8. Remaining work

| Stage | Status |
|---|---|
| 1. Code audit + offline verification | **DONE** |
| 2. Real photographs (blur, glare, angle, sideways without EXIF, near-duplicates) | **BLOCKED — needs images from the user** |
| 3. Controlled live verification (one shared folder, ≤10 photos, live OCR, temp Sheet) | Waiting on stage 2 + user approval |
| 4. Minimal fixes (MIME filter, content hash dedupe, HEIC/extension policy) | **DONE** (see §9) |
| 5. Regression re-run | **DONE** — §4.3 |
| 6. Deployment decision (VM/container vs. architectural change) | Recommendation in §6; decision needed |

---

## 9. Fix status after the customer-ready release

The audit above is the historical record. This section is the current state.
Every "PASS" below is backed by a test in this repository; none of it required
a redesign.

| # | Audit finding | Status now | Proof |
|---|---|---|---|
| 5 | Modified file under the same Drive ID ignored | PARTIAL (unchanged) | Documented pilot limitation; a corrected photo must be uploaded as a new file |
| 6 | Nested sub-folders | NOT IMPLEMENTED (unchanged) | Documented; customer guide says keep the folder flat |
| 7 | Non-image files become failing jobs | **FIXED** | `services/intake_policy.py`; `test_intake_policy.py`, `TestIntakeGateAtSync` |
| 8 | Formats / HEIC | **FIXED (explicit policy)** | HEIC rejected with `REJECTED_HEIC_UNSUPPORTED`; JPEG/PNG/WEBP/TIFF/BMP accepted |
| 9 | Size checked after download | **FIXED** | Size checked from Drive metadata before download; `REJECTED_FILE_TOO_LARGE` recorded |
| 10–11 | EXIF 1–8 | **FIXED** | `test_orientation_normalization.py::TestExifOrientation` (all 8 tags, idempotent, source untouched) |
| 12 | Visual rotation tied to OCR quality | **FIXED** | Landscape geometry gate before OCR (`_apply_geometry_orientation_correction`); `TestGeometryOrientation` |
| 15 | Same Drive ID duplicate | PASS (unchanged) | `test_same_drive_id_never_creates_a_second_job` |
| 16 | Identical bytes, different Drive IDs | **FIXED** | SHA-256 of the normalized image; `test_duplicate_protection.py::TestExactDuplicates` |
| 17 | Near-duplicates (resize/recompress/brightness/crop) | **FIXED (best effort)** | 64-bit dHash + brightness-invariant colour + aspect ratio; `test_content_identity.py`, `TestNearDuplicates`. Offline batch: 2/5, 5/12 and 8/25 downscaled copies were caught by the perceptual gate; the remainder were recorded and flagged by the same-book route instead of being merged |
| 18 | Same book, different photo | **PARTIAL — review only, by design** | Only an identical *checksum-valid* ISBN-13 auto-skips (`certain`). Title/author and fuzzy matches record a second row and flag it (`possible`); different ISBNs are never merged. `TestSameBook`, `test_duplicate_protection.py` (9 scenarios) |
| 22 | 2-hour stuck-job window | **FIXED** | 900 s lease on a per-job `heartbeat_at` (refreshed every 60 s by the owning worker), `claim_job` single-winner transition, retry history preserved, over-limit jobs failed with a reason; `test_job_lease_recovery.py` (14 tests) |
| 24 | Manual review never rejects | PASS (unchanged) | Existing suite |
| 25 | Known-good *Le cœur vert* | PASS | `test_customer_readiness_audit.py::TestKnownGoodBook` |
| 27–28 | Sheets export, ISBN zeros, idempotency | PASS (unchanged) | `tests/integration/test_export_pipeline.py`, `test_google_sheets_service.py` |
| 29 | Per-cycle summary | **FIXED** | `log_sync_summary` logs a discovery stage and a screening stage with arithmetic checks; see *Batch counters* below and `evidence/batch-stats.txt` |
| 30 | Customer UI / API | NOT IMPLEMENTED (unchanged) | Documented as a pilot limitation; Drive folder is the intake surface |
| 31 | Multi-tenant | NOT IMPLEMENTED (unchanged) | One folder per deployment |
| 32 | Vercel hosting | **Documented as not applicable** | `docs/DEPLOYMENT_READINESS_REPORT.md` — worker needs a persistent host |

### Duplicate protection, in one place

1. **Drive file ID** — the sync loop never queues a file already in `app.db`
   (cheap, prevents rescans).
2. **SHA-256 of the normalized image** — identical photographs with different
   Drive IDs are cancelled before OCR; the relationship is stored in
   `duplicate_matches` (`duplicate_of`, hash, kind).
3. **Perceptual hash + colour + aspect ratio** — resized, recompressed,
   brightened or lightly cropped copies are recognised (distance ≤ 3,
   chromaticity ≤ 0.04, aspect within 6%). Distance 4–9 is recorded as
   "possible" and still processed and flagged, never silently dropped. This
   gate is best effort: a heavily downscaled copy of a noisy image can drift
   past it, in which case step 4 still records and flags the copy.
4. **Same-book identity (after OCR)** — review only. A second row is cancelled
   *only* when the normalized ISBN-13 matches **and** the checksum is valid
   (`certain`, `same_isbn`). Everything else is a flag, not a merge:
   equal-but-checksum-invalid ISBN (`possible`, `same_isbn_unverified`),
   exact title + full author (`same_title_full_author`), title + surname
   (`same_title_surname`), near-identical title (`similar_title≥0.92`),
   subtitle variants (`title_suffix_or_subtitle`). Two different valid ISBNs
   are two different books (`different_isbn`) and are processed independently;
   the original row, its metadata and its source-file relationship are never
   modified by a flag.

### Customer-facing behaviour change

Unsupported files no longer become failed jobs: they are recorded in
`intake_rejections` with a stable reason code (`REJECTED_UNSUPPORTED_TYPE`,
`REJECTED_FILE_TOO_LARGE`, `REJECTED_HEIC_UNSUPPORTED`,
`REJECTED_BLOCKED_NAME`, `REJECTED_DIRECTORY`) and never downloaded.

### Orientation: what is guaranteed and what is not

| Case | Behaviour | Evidence |
|---|---|---|
| EXIF tags 1–8 (phone portrait, landscape, upside-down) | **Corrected deterministically**: pixels are transposed for 5–8, output dimensions swapped where required, EXIF stripped, `NORMALIZED_MARKER` written so a second pass is a byte-identical no-op | `test_orientation_normalization.py::TestExifOrientation` (all 8 tags, idempotence, source file untouched, measured output pixels) |
| Sideways photo with no EXIF (landscape aspect) | **Geometry gate**: landscape images are rotated before OCR whenever line detection says the text runs vertically, independent of OCR quality; kept only if the text score improves (≤1 extra OCR) | `TestGeometryOrientation::test_landscape_page_is_rotated_before_trusting_ocr` |
| Any rotation with no EXIF | **OCR-quality fallback**: when the first pass yields <3 words or confidence <0.30, all four rotations (0/90/180/270) are OCR'd and the best `words × confidence` is kept — this is what recovers an upside-down photo | `test_customer_readiness_audit.py::test_upside_down_or_sideways_image_is_reoriented[90/180/270]`, `test_metadata_service.py::TestOrientationCorrection` |
| Upside-down with no EXIF, whose first OCR pass looks plausible | **Not corrected** — the fallback is deliberately lazy (4× OCR cost), so readable-looking garbage at 180° is not re-tried. Such covers usually end in `manual_review` | `test_geometry_gate_does_not_rotate_an_upside_down_photo` |
| Arbitrary tilt / perspective | Not corrected (only 90° multiples); OCR must cope | — |

Originals are never rewritten: normalization reads the downloaded file, writes
into `debug/`/memory, and the source bytes keep their hash.

### Stale-job recovery semantics

- Every worker `claim_job`s a job (`pending → processing`) before doing any
  work; a second worker that loses the race skips the job instead of
  duplicating it. `start_job` remains for callers outside the worker loop.
- The owning worker refreshes `heartbeat_at` every 60 s while the job runs, so
  a slow-but-alive job is never reclaimed.
- `recover_stuck_jobs` (default 900 s, `PROCESSING_TIMEOUT_SECONDS`) requeues a
  job only when `COALESCE(heartbeat_at, started_at)` is older than the lease.
  It never touches `completed`, `failed` or `cancelled` rows, preserves
  `retry_count` and the error history, and marks a job `failed` with a reason
  once it exceeds `MAX_RETRIES` (3).
- Export recovery keeps using `attempted_at` (`recover_stuck_exports`, 7200 s)
  and is unchanged; it is covered by the same test module.

### Batch counters

`log_sync_summary` reports two disjoint stages so no file is counted twice:

- **Files discovered this cycle** — listed on Drive, already known (Drive ID),
  rejected by intake (with the cumulative rejection total), accepted and
  downloaded, then `Check: already + rejected + accepted = listed`.
- **Screening of accepted files** — exact duplicates, near duplicates, same
  book with a valid ISBN (all skipped before OCR), and processed, then
  `Check: skipped + processed = accepted`.
- **Review flags** — *possible near duplicate* and *possible same book* are
  printed under their own heading explicitly labelled as subsets of
  `processed`, never added to the skip totals.
- **Database and provider work** — cumulative identified / manual review /
  failed / pending / retrying, OCR calls, metadata lookups, cover downloads,
  export status counts.

A failed check prints `MISMATCH x/y` instead of `OK`; the arithmetic is
recomputed from database counts, so a mismatch means the cycle genuinely lost
or double-counted files.

Measured offline for 100 / 250 / 500 photographs — raw counts in
`evidence/batch-stats.txt`, the reconciled tables in
`evidence/batch-breakdown.md`:

| Photos | Listed | Rejected | Accepted | Exact skip | Near skip | Same-book skip | Processed | Possible dupes (flagged) | Rows | Distinct books |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 104 | 5 | 99 | 10 | 2 | 0 | 87 | 3 | 84 | 81 |
| 250 | 246 | 5 | 241 | 25 | 5 | 0 | 211 | 7 | 208 | 201 |
| 500 | 484 | 5 | 479 | 50 | 8 | 1 | 420 | 16 | 417 | 401 |

Drive-ID duplicates were 0 in all three runs (the folder contains no repeats),
so that category is proven by `test_same_drive_id_never_creates_a_second_job`
and by the rescan check (0 accepted, 0 new rows), not by these columns.

---

## 10. Real-photo acceptance set (current state)

**7 of 9 cases are covered by genuine photographs** copied byte-for-byte from
the existing Drive cache into `tests/fixtures/real/` (manifest:
`tests/fixtures/real/metadata.json`, which records the source path, Drive file
id, SHA-256, dimensions and `exif_orientation: null` for every file). Nothing
was downloaded, re-encoded, rotated or EXIF-stripped. No fixture is synthetic.

| File | Purpose | Intended check | Status |
|---|---|---|---|
| `upright.jpg` | correctly oriented reference (Jean Vuilleumier, *Le Mal Été*) | identified; no rotation applied | **PASS** — normalization keeps the content upright, `identified` 0.9930, one OCR call |
| `rot90.jpg` | sideways, no EXIF | geometry gate rotates 90° before OCR | **MISSING** — no sideways capture exists in the cache; not fabricated |
| `rot180.jpg` | upside-down, no EXIF (*Vie et mort de la Yougoslavie*) | documented limit: only the poor-OCR fallback may recover it | **PASS** — the line heuristic correctly cannot see 180°; the OCR-quality fallback recovers it (`orientation_angle=180`) |
| `rot270.jpg` | sideways, no EXIF | geometry gate rotates 270° before OCR | **MISSING** — not fabricated |
| `blurry.jpg` | out-of-focus / low light (*L'Invitation*) | no crash; `manual_review` or retry, never silent loss | **PASS** — OCR failure → `retrying`, no row, no export, DB integrity `ok` |
| `glare.jpg` | shrink-wrap glare (*Les Désastres de la guerre*) | no crash; degraded OCR handled, never silent loss | **PASS** — degraded text → review record, at most one export row |
| `angle.jpg` | tilted/perspective (*Le Cœur Vert*) | OCR still attempted; no perspective correction claimed | **PASS** — `identified` 0.9922, title/author/ISBN correct |
| `duplicate-a.jpg` + `duplicate-b.jpg` | the real byte-identical Drive pair (two Drive ids, same SHA-256) | skipped as `exact`, no second OCR call, no second row | **PASS** — second file `cancelled` as `exact_duplicate_of:<first>`, one row, one export row |
| `same-book-2.jpg` | different photo of the same physical book (same printed title/author/publisher, stored upside-down) | flagged `possible_same_book` unless a valid ISBN matches | **PASS** — flagged, two rows kept, original record unchanged |

Notes and remaining limitations:

- `upright-copy.jpg` is realised by the genuine duplicate pair above rather than
  by copying `upright.jpg`: this is real duplicate evidence from production
  data, not a manufactured fixture.
- `rot90`/`rot270` remain unverified. No sideways capture exists in the
  cache, and synthesising one would not be real-photo evidence.
- The `rot180` photograph is also the `same-book-2` photograph: it differs from
  `upright.jpg` in orientation, which is worth knowing when reading a
  same-book result.
- Behavioural results come from `tests/integration/test_real_photo_acceptance.py`
  and `tests/integration/test_pipeline_end_to_end.py`; OCR text is controlled in
  those runs, so recognition quality itself is unverified.
- The 14 photographs from the earlier live run survive under
  `data/batch_downloads/`; 13 of them are byte-identical to files in the current
  cache and were used to build these fixtures. They are not a substitute for
  genuine 90°/270° captures.

### 10.1 Live OCR run (production PaddleOCR-VL, 2026-11-13)

Three approved photographs were run once each through the real
`MetadataService` → `OCRService` → `PaddleOCRVlService` path, with temporary
`jobs.db` / `metadata.db` and no Drive or Sheets writes. Only 3 OCR calls were
consumed.

| Photo | Actual result | Calls | Verdict |
|---|---|---|---|
| `angle.jpg` | `identified` — *Le coeur vert* / Jean-Louis Kuffer, `L'AGE D'HOMME`, 148 pp., confidence 1.0 | 1 | PASS |
| `upright.jpg` | `identified` — *Le mal été* / Jean Vuilleumier, confidence 1.0 | 1 | PASS |
| `rot180.jpg` | `identified` — *Vie et mort de la Yougoslavie* / Paul Garde, 444 pp., confidence 0.8926 | 1 | PASS |

The first live attempt exposed a genuine boundary defect: `LayoutBlock.bbox` is
a flat `[x1, y1, x2, y2]` rectangle, but `OCRService` read it as corner pairs, so
every live response raised and became empty text. Fixed in `_normalise_bbox()`
(Phase 46 of `REFACTOR-LOG.md`); extraction, thresholds, prompts and provider
unchanged. Orientation fallback was not triggered live because the provider
returned readable text on the first pass — the `rot180` row above describes the
controlled harness, not this live run.

Live Sheets rendering is still unverified: no isolated spreadsheet or tab is
configured, and only `GOOGLE_SHEETS_TAB_NAME` inside the production spreadsheet
is supported.
