# Book Photo Pipeline — Finalization Plan

**Status:** Phase 0 (fresh audit) complete. Phases 1–8 implemented and validated as they
are completed; each phase's outcome is recorded at the end of its section.

**Scope rule:** preserve the existing architecture. Only demonstrated defects are fixed.
Title/author extraction, candidate ranking, confidence thresholds, the OCR provider
(Baidu AI Studio PaddleOCR-VL) and the duplicate/retry/export semantics are off-limits
except where a defect is proven and no extraction behaviour changes.

---

## 0. Fresh audit (2026-11-12)

### 0.1 Repository state

| Item | Observation |
|---|---|
| Branch / HEAD | `main`, single commit `6d67ef1 Initial commit: Book Auth pipeline (Phases 1-44)` |
| Dirty tracked files | ~60 source files (pre-existing hardening work, uncommitted) plus tracked `__pycache__/*.pyc` |
| Untracked (this work) | `tests/fixtures/real/`, `tests/integration/test_real_photo_acceptance.py`, `docs/customer-readiness/evidence/drive-photo-contact-sheet.jpg` |
| Staged / committed this task | none (no commit, no push requested) |
| Credentials | `credentials/` now ignored by `.gitignore` (line 4); neither key is tracked, staged or in history |

### 0.2 Processing entry points (verified in code)

| Entry point | File | What it does |
|---|---|---|
| `python app.py` | `app.py::main` → `run_sync_engine` | Long-running operation: Drive sync loop (30 s), `ProcessingWorker` thread, `ExportWorker` thread. The normal production path. |
| `python app.py --status` | `app.py::print_status_report` | Read-only operational report (jobs, export queue, dead letters, directories). |
| `python app.py --requeue-incomplete` | `app.py::run_requeue_incomplete` | Puts finished-but-incomplete exports back on the queue; does not re-run OCR. |
| `python app.py --gemini-recovery` | `app.py::run_gemini_recovery` | Separate low-confidence recovery tool; not part of normal intake. |
| `python drive_batch.py` | `drive_batch.py::main` | One-shot batch over the same Drive folder: intake → download → process → Sheets → local audit reports. Reuses `services.intake_policy`, `MetadataService` and the same repository models. |

### 0.3 Drive discovery → Sheets export (actual code path)

1. `DriveService.list_all_files()` — paginated (`page_size=100`, `nextPageToken`), read-only
   scope, query `'<folder>' in parents and trashed = false`.
2. `SQLiteRepository.get_all_file_ids()` (`data/app.db`) — files already seen are skipped, so
   repeated cycles do not re-download or re-create work.
3. `services.intake_policy.evaluate(drive_file, max_size_bytes)` — MIME/extension allow-list,
   HEIC rejection with its own reason code, Google-native/directory rejection, OS junk
   rejection, size cap. Runs on Drive metadata only, before any bytes are fetched.
   Rejections are stored once per Drive id in `intake_rejections` (`IntakeRepository`,
   `jobs.db`) and logged once.
4. `DriveService.download_file` → `data/downloads/<drive_id>.<ext>` (`safe_local_filename`),
   then `SQLiteRepository.insert_file`.
5. `JobService.create_job_for_file` → `jobs` table (`jobs.db`), status `pending`.
6. `ProcessingWorker` (`workers/processing_worker.py`) claims the job
   (`JobService.claim_job` conditional UPDATE), starts a `_LeaseHeartbeat`, and calls
   `MetadataService.identify_book`.
7. `MetadataService` (`services/metadata_service.py`): `ImagePreprocessor.preprocess`
   (EXIF 1–8 → resize → autocontrast → smooth → grayscale, marker for idempotence) →
   image duplicate gate (`services/content_identity.py` + `DuplicateRepository`) →
   `OCRService` → `PaddleOCRVlService` (Baidu AI Studio) → geometry orientation gate →
   bounded 4-angle OCR fallback → `TextNormalizer` → `BookTextExtractor` (settled) →
   `BookLookupService` (Google Books / Open Library) → `CandidateRanker` →
   `ConfidenceService` → persist `BookMetadata` (`metadata.db`) → enqueue export
   (`export_queue`, `jobs.db`).
8. `ExportWorker` (`workers/export_worker.py`) → `ExportService.process_export`:
   validate → enrich (`MetadataEnrichmentService`, fill-empty-only) → clean cover
   (`CleanCoverService`/`CoverDownloadService`, placeholder + dimension validation,
   disk cache) → `GoogleSheetsService.get_or_create_row` (header-name mapping, `RAW`
   values, `USER_ENTERED` `=IMAGE()` cover cell) → `export_records` + `export_queue`
   status.

### 0.4 Where state lives and how failures propagate

| Store | Contents |
|---|---|
| `data/app.db` | Drive file ids already downloaded |
| `data/jobs.db` | `jobs`, `intake_rejections`, `duplicate_fingerprints`/`duplicates`, `export_queue` |
| `data/metadata.db` | `metadata`, `covers`, `export_records` |
| `data/{downloads,processed,covers,debug}` | local files and debug artifacts (retention-pruned) |

Failure propagation:

- Download/list failure → logged per file/cycle, other files continue; nothing is recorded
  as processed.
- Processing exception → `BaseWorker._handle_failure` → `JobService.fail_job` classifies the
  error (transient → `retrying` with backoff, permanent → `failed`), keeping the error
  message and retry count. Unreadable OCR → `manual_review` with its own record.
- Stale `processing` jobs → `JobService.recover_stuck_jobs()` on startup and while idle;
  heartbeats prevent recovery of a healthy long job.
- Export exception → status `retrying` with exponential backoff, then `failed`
  (dead-letter); `ExportWorker.initialize()` recovers `processing` rows left by a crash.

### 0.5 Tests and baseline

| Suite | Result at audit time |
|---|---|
| `tests/unit` | 1272 passed, 2 skipped |
| `tests/integration` | 109 passed, 5 failed |
| `tests/integration/test_real_photo_acceptance.py` (new) | 16 passed |
| Baseline integration failures (documented, unchanged) | `test_vision_benchmark.py::TestBenchmark::test_no_books_silently_lost`; `test_vision_first_routing.py::TestVisionFirstRouting::test_good_ocr_does_not_trigger_vision`, `::test_vision_not_called_for_fake_image`; `::TestVisionRecoveryResults::test_yugoslavia_vision_extraction`; `::TestRegressionCases::test_vision_extracts_all_problem_books` |
| Focused modules re-run in this audit (intake, preprocessor, lease, retry, export worker, covers, enrichment, sheets, export pipeline, batch acceptance) | 286 passed |

### 0.6 Fixtures

`tests/fixtures/real/` holds eight byte-for-byte copies of cached Drive photographs with a
`metadata.json` manifest (source path, Drive id, SHA-256, dimensions, `exif_orientation: null`,
`derived: false`): `upright.jpg`, `same-book-2.jpg`, `duplicate-a.jpg`, `duplicate-b.jpg`,
`rot180.jpg`, `blurry.jpg`, `glare.jpg`, `angle.jpg`.

`rot90.jpg` and `rot270.jpg` do not exist: no sideways capture exists in the 72-file cache.
They are recorded as missing and are **not** synthesised, so real 90°/270° recovery remains
unverified. `rot180.jpg` is genuine upside-down content with no EXIF tag.

### 0.7 Documentation vs code mismatches found

| Mismatch | Reality | Action |
|---|---|---|
| `docs/CUSTOMER_READINESS_AUDIT.md` §10 says 0/9 real photos supplied | 8 fixture photos now exist (7 of 9 acceptance cases) | update §10 in Phase 9 |
| `ImagePreprocessor.detect_orientation` docstring: "check if text is more readable in current orientation" | implementation is a Hough-line orientation count | docstring corrected with the fix (Phase 1) |
| `count_export_records()` described as "export records" in the run summary | it counts the `export_records` table only; pending/retrying rows live in `export_queue`, so a cycle summary can read `{}` while exports are queued | documented as a reporting limitation (no code change: the queue has `get_pending_exports`/`get_export_by_drive_file_id`; nothing customer-facing is wrong) |
| No root `README.md` | operational instructions live in `docs/` (`CUSTOMER_UPLOAD_GUIDE.md`, `DEPLOYMENT_RUNBOOK.md`, `environment.md`) | point to `docs/` from the plan and the audit; do not create a second runbook |

---

## Workstream A — Orientation detector (CONFIRMED DEFECT)

**Observed behaviour.** On every real photograph whose OCR quality is poor enough to trigger
the fallback, `services/image_preprocessor.py` logs
`Orientation detection failed: cannot unpack non-iterable numpy.int32 object`. The raised
`TypeError` is swallowed by the broad `except`, so `detect_orientation` always returns
`angle=0, confidence=0.0`.

**Responsible code.** `ImagePreprocessor.detect_orientation` (lines ~225–267):
`for line in lines: x1, y1, x2, y2 = line[0]`. The installed OpenCV (5.0.0) returns
`HoughLinesP` output with shape `(N, 4)`; older releases returned `(N, 1, 4)`.

**Evidence.** Captured in `tests/integration/test_real_photo_acceptance.py` runs on
`rot180.jpg`, `blurry.jpg`, `glare.jpg` (warning appears on each poor-OCR pass). Existing unit
coverage (`test_detect_orientation_does_not_crash_on_valid_image`) patches `PIL` and a mock
image, so no real `cv2.HoughLinesP` output ever reached the loop — that is why the defect
survived.

**Impact.** The fast geometry heuristic is effectively dead. Rotation recovery still works
because `MetadataService._try_orientation_correction` always performs the exhaustive
0/90/180/270 OCR search when OCR quality is poor (proved by the genuine 180° fixture), but
the cheap first guess is lost and a warning is logged on every such photo.

**Correction.** Normalise the `HoughLinesP` output through a small documented helper that
accepts `None`, empty arrays, `(N, 4)` and `(N, 1, 4)`, ignores unrecognised shapes, and
iterates `(x1, y1, x2, y2)` rows. No threshold or decision logic changes.

**Files.** `services/image_preprocessor.py` (+ regression tests in a new
`tests/unit/test_orientation_geometry.py`).

**Risk.** Low: only the parsing of detector output changes; `correct_orientation`, EXIF 1–8
handling, idempotent normalization and the bounded OCR fallback are untouched.

**Validation.** New tests must prove the detector *executes* (success-path confidence 0.5
instead of the exception-path 0.0) on synthetic horizontal and vertical line images, on both
OpenCV output shapes, on `None`/empty/garbage output, and on the real `upright.jpg` (0°) and
`rot180.jpg` (still 0°: the heuristic cannot see 180°, matching the documented limit).
`tests/unit/test_orientation_normalization.py` must stay green (EXIF tags 1–8, idempotence,
source bytes unmodified).

**Order.** Phase 1 (first: it is the only confirmed code defect and other phases reuse the
preprocessor).

**Outcome.** Implemented in Phase 1 — see the phase log at the end of this document.

---

## Workstream B — Intake and photo entry (VERIFIED, no defect)

**Observed behaviour.** `intake_policy.evaluate` is the single gate used by both entry points
(`app.py::run_sync_engine` and `drive_batch.py::main`, each computing
`max_size_bytes_from_mb(config.image_max_file_size_mb)`). It rejects HEIC/HEIF with a distinct
reason, Google-native files and folders, OS junk, oversized files, and unsupported MIME types,
falling back to the extension when Drive reports `application/octet-stream`.

**Responsible code.** `services/intake_policy.py`, `app.py` (sync loop), `drive_batch.py`
(decision list built before any download), `repositories/intake_repository.py`,
`services/drive_service.py` (pagination), `repositories/sqlite_repository.py` (already-known
skip).

**Evidence.** `tests/unit/test_intake_policy.py` (green), `tests/unit/test_batch_acceptance.py`
(green), 286-test focused run in this audit.

**Correction.** None required. **Coverage gap:** no test drives *both* entry points'
intake decision through the same policy in one place. Phase 2 adds an acceptance test that
runs discovery → filtering → download over a fake Drive listing (including a PDF, a HEIC, an
oversized JPEG, a Google-native doc and two supported photos) and asserts only the supported
photos are downloaded and that re-running creates no new work.

**Risk.** None (test-only). **Validation.** New test asserts: accepted set exactly the
supported images; rejected files recorded once with their reason codes; second cycle
downloads nothing and creates no new jobs.

**Outcome.** Implemented in Phase 2.

---

## Workstream C — Duplicate protection and book identity (VERIFIED, no defect)

**Observed behaviour.** Six distinct outcomes are implemented and recorded:
same Drive id (retry, not a duplicate), exact (SHA-256 of the normalized image), near
(perceptual + colour + aspect), possible near (recorded, still processed), same book with a
valid ISBN (skipped, original kept), possible same book without a verified ISBN (flagged,
never merged, both rows kept). Different valid ISBNs stay separate.

**Responsible code.** `services/content_identity.py`, `MetadataService._check_image_duplicate`
and same-book comparison, `repositories/duplicate_repository.py`.

**Evidence.** `tests/unit/test_duplicate_protection.py` (green), `tests/unit/test_content_identity.py`
(green), real-photo acceptance: duplicate pair → one row and one export row, `exact`;
Vuilleumier pair → `possible_same_book`, two rows, original untouched.

**Correction.** None. **Coverage gap:** the real-photo suite exercises the service level;
Phase 8 adds an end-to-end run where both Drive ids are submitted through the normal
acceptance flow and export count is asserted.

**Outcome.** Implemented in Phase 8 (test-only).

---

## Workstream D — OCR and metadata processing (VERIFIED, no defect)

**Observed behaviour.** `OCRService` uses `PaddleOCRVlService` only (Baidu AI Studio,
`PADDLEOCR_BASE_URL` default `https://paddleocr.aistudio-app.com`, model `PaddleOCR-VL-1.6`,
job submit → poll → result download, explicit failure → `OCR_FAILED`, never an alternative
provider). `AdaptiveOCR` (local reader) is not on the production path for cover photos.

**Responsible code.** `services/ocr_service.py`, `services/paddleocr_vl_service.py`,
`services/metadata_service.py` (which does not fabricate an identification: no candidates →
minimal `manual_review` record with `source=ocr_fallback`).

**Evidence.** Existing OCR/benchmark tests green (mocked); the real-photo suite proves
failure isolation (`retrying`, no row, no export, fingerprint not poisoned) and that a later
submission with the same bytes still processes.

**Correction.** None. Live OCR is **not** called in automated tests; it needs a separate
approved quota plan (`docs/environment.md` holds the credential names). Known-good
*Le cœur vert* regression stays green.

**Outcome.** Verified; no code change.

---

## Workstream E — Metadata enrichment and cover retrieval (VERIFIED, no defect)

**Observed behaviour.** Enrichment fills only empty fields (`_merge_enrichment` guards every
assignment with `if not metadata.X`), keeps perception-derived title/author/publisher,
fills `page_count` only when absent and positive, derives ISBN-10/13 with checksum
validation, and never overwrites with missing data. Cover retrieval rejects placeholder URLs,
GIFs and images below `cover_min_width/height`, and the portrait filter in
`BookLookupService` rejects landscape banners. A validated cover on disk is reused
(`skip_existing`, `covers` table) before any fetch. A failed cover is persisted
(`pending_retry` / `not_found`) so `--requeue-incomplete` resumes cover + sheet only.

**Responsible code.** `services/metadata_enrichment_service.py`,
`services/cover_download_service.py`, `services/cover_resolver_service.py`,
`services/clean_cover_service.py`, `repositories/cover_repository.py`,
`ExportService._enrichment_resolved`.

**Evidence.** `tests/unit/test_metadata_enrichment.py`,
`test_cover_download_service.py`, `test_phase35c/37_cover_retrieval.py`,
`tests/integration/test_export_pipeline.py` (all green). Live cover APIs were **not** called
in this pass.

**Correction.** None. **Validation in Phase 8:** acceptance run uses a fake cover service to
prove the export payload carries cover URL/formula and that a second run does not re-download.

---

## Workstream F — Persistence, retry and worker recovery (VERIFIED, no defect)

**Observed behaviour.** `JobService.claim_job` is a conditional `UPDATE ... WHERE status=?`
so only one worker can win; `_LeaseHeartbeat` renews the lease every 60 s (< 900 s timeout);
`recover_stuck_jobs` resets only expired `processing` rows; `fail_job` respects the retry
budget and stores error text + retry count; completed jobs are not reprocessed; a duplicate
or failed photo leaves no half-written metadata row (the fingerprint is registered only for
persisted images).

**Responsible code.** `services/job_service.py`, `workers/base_worker.py`,
`repositories/job_repository.py`, `workers/processing_worker.py`.

**Evidence.** `tests/unit/test_job_lease_recovery.py`, `test_processing_worker.py`,
`test_retry.py`, `test_retry_backoff.py`, `test_job_repository.py` (green); SQLite
`PRAGMA integrity_check = ok` after normal and failure scenarios in the real-photo suite.

**Correction.** None. **Validation in Phase 8:** acceptance run asserts job states per photo,
retry counters, and integrity after the sequence.

---

## Workstream G — Google Sheets export (VERIFIED, no defect)

**Observed behaviour.** Columns are resolved **by header name** (`_read_header`,
`_row_values_for_header`, appends missing headers instead of assuming positions); normal
values are written `RAW` so ISBNs keep leading zeros; the cover cell is written separately with
`USER_ENTERED` as `=IMAGE("<url>")` (a single-cell update, and a persisted `sheet_row_id` is
verified against the row title so a re-export updates rather than appends); external URL access
is enabled best-effort so `IMAGE()` renders; a failed Sheets call raises → export `retrying`
with backoff, completion recorded only after a successful write.

**Responsible code.** `services/google_sheets_service.py` (`get_or_create_row`,
`_write_cover_formula`, `_build_image_formula`), `services/export_service.py`.

**Evidence.** `tests/unit/test_google_sheets_service.py`,
`tests/integration/test_google_sheets_export_rows.py` (green, mocked API).

**Correction.** None. `count_export_records()` counts only `export_records` (completed
exports), which is why a cycle summary can show `{}` while `export_queue` holds pending rows.
That is a reporting limitation, not a wrong production result — documented, not changed.

**Validation in Phase 8:** the acceptance run captures the exact payload handed to the Sheets
service (title, author, ISBN, page count, cover formula) and proves one export row per book
and idempotence on a second run. **No live Sheet write** is performed.

---

## Workstream H — End-to-end acceptance (IMPLEMENTED IN PHASE 8)

**Observed behaviour.** Coverage today is per-service plus the worker-level real-photo suite.
No test drives the whole chain `discovery → intake → download → job → processing → export →
sheet payload` with the production wiring.

**Plan.** Add `tests/integration/test_pipeline_end_to_end.py`: a fake Drive service serves the
real fixture bytes plus rejected decoys; the harness mirrors `app.py` wiring (intake policy,
`IntakeRepository`, `JobService`, `ProcessingWorker`, `MetadataService` with
`BookLookupService`/`CandidateRanker`/`ConfidenceService`/`TextExtractor` real and OCR/vision
stubbed, `ExportService` with fake cover + recording Sheets service) and runs two cycles.

**Validation.** Per-photo job states and row identities; exactly one book and one export row
for the duplicate pair; `possible_same_book` flag with two rows for the Vuilleumier pair;
sheet payload contains title, author, ISBN when known, page count and the `=IMAGE(...)`
formula; second cycle creates no new downloads, jobs, rows or export rows; SQLite integrity
`ok` for all three databases.

---

## Phase log (with outcomes)

| Phase | Work | Outcome |
|---|---|---|
| 0 | Fresh audit, plan | done — audit facts in section 0 above |
| 1 | Orientation detector fix + regression coverage | done — `_iter_hough_segments` accepts `None`, empty arrays, `(N, 4)` and `(N, 1, 4)`, ignores unknown shapes; 13 new tests in `tests/unit/test_orientation_geometry.py` prove the success path runs (`confidence == 0.5`, not the 0.0 exception path) on synthetic and real pixels |
| 2 | Intake/entry acceptance | done — no code defect; `tests/integration/test_pipeline_end_to_end.py` proves 7 accepted / 4 rejected with the stable reason codes, only accepted photos downloaded, a failure not blocking peers, and one rejection row per file across two cycles |
| 3 | Duplicate/same-book verification | done — no code defect; end-to-end run shows `exact` cancellation under a second Drive id (one row, one export) and `possible_same_book` flagging with two intact rows |
| 4 | OCR failure handling verification | done — no code defect; failure reaches `retrying` with a preserved error message and no row/export/fingerprint poisoning; provider stays Baidu PaddleOCR-VL |
| 5 | Enrichment + cover verification | done — no code defect; fill-empty-only enrichment, placeholder/dimension rejection and cover reuse unchanged; the acceptance run's controlled cover produces a real `=IMAGE(...)` formula and `page_count` survives persistence |
| 6 | Persistence/retry/recovery verification | done — no code defect; job claim/heartbeat/retry states asserted per photo, SQLite `PRAGMA integrity_check = ok` on all three databases |
| 7 | Sheets payload verification | done — no code defect; header-name mapping, `RAW` values, `USER_ENTERED` single-cell cover formula and one row per book verified against the production row builders |
| 8 | End-to-end acceptance test | done — 10 end-to-end tests plus the 16 real-photo tests pass together (26) |
| 9 | Documentation finalization | done — this plan, `docs/CUSTOMER_READINESS_AUDIT.md` §10 and `REFACTOR-LOG.md` updated |

## Validation commands and expected results

| Check | Command | Expected |
|---|---|---|
| Orientation regression | `python -m pytest tests/unit/test_orientation_geometry.py tests/unit/test_orientation_normalization.py tests/unit/test_image_preprocessor.py -q` | all pass |
| Real-photo acceptance | `python -m pytest tests/integration/test_real_photo_acceptance.py -q` | 16 passed |
| End-to-end acceptance | `python -m pytest tests/integration/test_pipeline_end_to_end.py -q` | 10 passed |
| Unit suite | `python -m pytest tests/unit -q` | 1284 passed, 2 skipped |
| Integration suite | `python -m pytest tests/integration -q` | 119 passed, 5 baseline vision failures |
| Style | `python -m ruff check <changed files>` / `python -m black --check <changed files>` | clean |

## Operations (how to run and recover)

**Normal run** (Drive sync + processing worker + export worker):

```bash
python app.py
```

Required configuration (names only; values live in the untracked `.env` or the
environment): `GOOGLE_DRIVE_FOLDER_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE`,
`GOOGLE_SHEET_ID`, `GOOGLE_SHEETS_CREDENTIALS`, and the PaddleOCR-VL provider
settings read by `PaddleOCRVlService` (`PADDLEOCR_BASE_URL`, token/key plus
model overriding defaults). Optional: `IMAGE_MAX_FILE_SIZE_MB`,
`CONFIDENCE_THRESHOLD`, `EXPORT_MAX_RETRIES`, `COVER_MIN_WIDTH/HEIGHT`.

**Inspect state and failures** (read-only):

```bash
python app.py --status     # job counts, export queue, dead letters, directories
```

**Retry incomplete exports** (cover/sheet only — never re-runs OCR or extraction):

```bash
python app.py --requeue-incomplete
```

**Retry failed jobs**: jobs in `retrying` are picked up automatically after their
backoff; jobs in `failed` (budget exhausted) are visible in `--status` and are
re-run by deleting only their job row with operator intent — no automated
mutation is performed by this work.

**Verify a Sheets export**: `--status` shows pending/retrying/dead-letter counts;
the `export_records` table in `data/metadata.db` holds `sheet_row_id`,
`sheet_url`, `exported_at` and the exported column payload per book. A book is
exported only after the row write succeeds; a queued row is not a completed
export.

**Batch run over the same folder** (offline reports, same intake policy):

```bash
python drive_batch.py
```

**Known reporting quirk**: `count_export_records()` counts only finished
`export_records`, so a cycle summary can print `EXPORTS: none` while the
`export_queue` still holds pending rows. Use `--status` for queue depth.
