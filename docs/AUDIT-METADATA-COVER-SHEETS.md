# Audit — Metadata Enrichment, Book Covers & Google Sheets Export

Status: Phase 1 (inspection only). No code behaviour was changed to produce this report.

Remediation status (2026-11-03): RC-11 and recommendations 1–4 and 6 are implemented;
recommendation 5 (explicit requeue for incomplete records) is implemented as
`ExportRepository.requeue_incomplete_exports()` + `python app.py --requeue-incomplete`.
See `REFACTOR-LOG.md` phases 45 and 46 for files, tests and remaining limitations.

Verification status (2026-11-03, phase 47): the full metadata → cover → Sheets
workflow was exercised live against the production Drive folder and spreadsheet —
one incomplete book recovered end to end, then a four-record batch, one extra
missing-cover record and a re-run after each fix. Every run completed without
changing the production sheet except where required, the queue stayed at 18
`completed` records, no duplicate rows appeared, and OCR/title/author extraction
was never re-run. Two defects were found and fixed live: Sheets writes now use
`value_input_option=RAW` so cover cells store the same `=IMAGE(...)` text as the
eighteen existing rows instead of a `#N/A` formula, and one failing export no
longer aborts the rest of a worker batch. Full results: `REFACTOR-LOG.md` phase 47,
`data/verification/*.json`.

Cover-render follow-up (2026-11-05, phase 48): the phase-47 RAW protection wrote
cover formulas as literal text, so covers never rendered. Root causes found with
live Sheets API evidence: (1) since 2024 the workbook blocks external URL access
for IMAGE()/IMPORT* by default (`#REF!`, "Please use a desktop web browser…") —
fixed by the documented workbook property `importFunctionsExternalUrlAccessAllowed`,
now enabled via `spreadsheets.batchUpdate`; (2) this workbook rejects the sized
`=IMAGE(url, 1, 120, 80)` form ("Expected 2 arguments, but got 4"), while the
1-argument `=IMAGE(url)` form renders. The service now writes typed mixed rows:
metadata cells RAW (leading-zero ISBNs safe), the cover cell via a dedicated
USER_ENTERED single-cell formula write (normalised to the 1-argument form).
Live-vs-literal-text is distinguished from the cell's `userEnteredValue`
(the values API reports the formula text for both). 12 of 16 production cover
cells were converted to real formulas (https URLs only, `errorValue` absent on
all of them) and a Drive PDF export embedded rendered JPEG cover images.
Details are in `REFACTOR-LOG.md` phase 48.

Note: a "unit" smoke test used to run `scripts/rebuild_sheet_from_db.py`
against the real spreadsheet, and that script's RAW write turned covers back
into literal text. The test now skips unless `RUN_LIVE_SHEETS_TESTS=1`, and the
rebuild script writes metadata RAW then applies covers as live formulas.

Scope inspected: `services/metadata_service.py`, `services/metadata_enrichment_service.py`,
`services/book_lookup_service.py`, `services/clean_cover_service.py`,
`services/cover_resolver_service.py`, `services/cover_download_service.py`,
`services/cover_verifier.py`, `services/metadata_validation_service.py`,
`services/export_service.py`, `services/google_sheets_service.py`,
`workers/processing_worker.py`, `workers/export_worker.py`,
`repositories/book_metadata_repository.py`, `repositories/cover_repository.py`,
`repositories/export_repository.py`, `models/*`, `core/config.py`, `app.py`,
`data/metadata.db`, the live Google Sheet (`Sheet1`), and all related tests.

Title extraction, author extraction, OCR provider, confidence thresholds and
candidate ranking were inspected and treated as read-only contracts.

---

## 1. Current architecture

```
app.py::run_sync_engine
  DriveService.list_all_files -> SQLiteRepository (downloaded file ids)
  -> JobService.create_job_for_file -> ProcessingWorker (thread)

ProcessingWorker.process
  -> MetadataService.identify_book(path, drive_file_id, job_id)
       ImagePreprocessor -> OCR (PaddleOCR-VL / Baidu) -> TextNormalizer
       -> BookTextExtractor (title/author/ISBN/publisher)   [PROTECTED]
       -> BookLookupService (Google Books, Open Library)
       -> CandidateRanker -> ConfidenceService              [PROTECTED]
       -> BookMetadataRepository.upsert_metadata(...)
       -> ExportRepository.create_export_record(...)        [export_queue]

ExportWorker.run (thread, every 10 s)
  -> ExportRepository.get_pending_exports -> ExportService.process_export
       MetadataEnrichmentService.enrich(metadata)      (title+author lookup)
       -> BookMetadataRepository.update_metadata
       CleanCoverService.find_clean_cover(metadata)    (GB -> OL -> Exa)
       -> writes data/covers/<drive_file_id>.jpg
       -> BookMetadataRepository.update_metadata (cover fields)
       GoogleSheetsService.get_or_create_row(metadata, local_cover_path)
       ExportRepository.update_export_record(...)

GoogleSheetsService
  gspread service-account client -> worksheet -> get_all_values / update / append_rows
```

## 2. End-to-end data flow (verified against the live database and sheet)

`data/metadata.db.book_metadata` currently holds 19 records. The live sheet
`Sheet1` has 18 data rows and these 9 headers (A–I):

`Title | Authors | ISBN-13 | Publisher | Published Date | Language | Categories | Description | Open Cover`

Only these 9 columns exist. There is **no** column for ISBN-10, page count,
cover URL, processing status, metadata source, or Drive file ID.

Observed state (examples):

| Drive file | Title | ISBN-13 | Cover in DB | Cover in sheet |
|---|---|---|---|---|
| `1HYtCZ…` | Les désastres de la guerre | 9782825106013 | GB URL, found, local file | yes |
| `14KPSa…` | Le coeur vert | 9782825104699 | OL `-L.jpg`, found, local file | yes |
| `1sgaPX…` | La violence de l'esprit… | *(empty)* | *(empty, cover_status NULL)* | blank |
| `1cyqP8…` | Je le ferai demain | *(empty)* | Amazon “QR store link” PNG, **found** | blank (filtered) |
| `1Bfv9K…` + `14metN9…` | Debout, dans l'ordre… | *(empty)* | GB URL | **two duplicate rows (3 and 5)** |

`export_records.sheet_row_id` is clearly stale: `14KPSa…` (Le coeur vert) stores
`3`, but row 3 is another book; three different books store row `2`.

## 3. Answers to the audit questions

1. **Where does the selected book come from?** `MetadataService.identify_book`
   persists the selected candidate through `BookMetadataRepository.upsert_metadata`
   and creates an `export_queue` row. `ExportService` reloads it by `drive_file_id`.
2. **Fields returned by providers.** Google Books returns title, subtitle, authors,
   ISBN-10/13, publisher, publishedDate, language, **pageCount**, categories,
   description, imageLinks. Open Library search returns title, subtitle, authors,
   ISBNs, first publish year, language, subjects, cover id/URLs. OL work/edition
   details add publisher, publish date, language, description, cover, and
   (available in the editions payload) page counts.
3. **Fields actually persisted.** Everything above **except `page_count`** — the
   `book_metadata` schema has no `page_count` column, so Google Books page counts
   are discarded. Persisted cover fields: `cover_url`, `cover_source`,
   `cover_source_url`, `cover_status`, `cover_source_type`, `local_cover_path`.
4. **Fields lost between services.**
   - `page_count` (model → repository: dropped, no column).
   - `subtitle` is persisted but never merged during enrichment and never exported.
   - `cover_status` on the *failure* branch of `ExportService` is set in memory but
     **never persisted** (`update_metadata` is only called on the success and
     pending-retry branches), so failures are invisible to later retries.
   - ISBN-10, status, confidence, source, drive_file_id are persisted in the DB
     but never reach the sheet (no columns, and the row mapper does not include them).
5. **Missing from the database.** `page_count`.
6. **Missing from Google Sheets.** ISBN-10, page count, cover *URL* (only an
   `IMAGE()` formula exists), identification/export status, metadata source,
   Drive file id. Of these, only the 9 existing headers are "supported" by the sheet.
7. **Does the export map every available field?** No. `_map_metadata_to_row`
   manually packs exactly 9 values, and `get_or_create_row` compares/updates by
   hard-coded column positions (index 0 = title, 1 = author, …).
8. **Does the cover service retrieve a real cover?** It retrieves and downloads an
   image and checks dimensions (>= 200×250), but it does not reject placeholder
   images. `1cyqP8…` (a 261×261 Amazon Kindle **QR code**) passed validation and was
   stored with `cover_status='found'`.
9. **Are cover URLs validated?** Only indirectly, by downloading. `CoverResolverService.validate_cover_url()`
   (HEAD request) is defined but never called. `_validate_url_is_image()` trusts host
   names/extensions, so it returns True for any `covers.openlibrary.org/...jpg`,
   including Open Library’s 1×1 placeholder.
10. **Is the cover associated with the correct book?** Partly. The Exa path and the
    Google Books title-only query are identity-checked with fuzzy title/author
    thresholds, but there is **no ISBN-first cover lookup**, so an edition with a
    better ISBN match can lose to a differently-scored title match.
11. **Are cover downloads cached?** Two ad-hoc caches: an in-process dict in
    `CleanCoverService` and the presence of `data/covers/<id>.jpg`. There is also a
    `cover_downloads` table that `CoverDownloadService` writes, but `ExportService`
    no longer calls it. Enrichment itself is **not** cached, so every retry re-queries
    Google Books/Open Library.
12. **Does it overwrite good data with empty values?** **Yes.** `get_or_create_row`
    writes the whole 9-cell row on every export. If a later run has an empty
    description/category (provider failure, empty enrichment) the previously
    populated cell is cleared.
13. **Can an existing record be enriched later?** Only by manually re-running
    `process_export`; the queue marks the record `completed` and the export worker
    only polls `pending`/`retrying`, so nothing re-exports a book whose cover or
    ISBN was missing at the first export.
14. **DB ↔ sheet inconsistencies.** Stale `sheet_row_id`, duplicate rows for the same
    title/author, cover formula missing although the DB says `found`, and rows whose
    cover cell is blank while `cover_downloads`/`local_cover_path` hold a file.

## 4. Root causes (specific files/functions)

**RC-1 — the sheet only ever receives 9 hard-coded fields.**
`services/google_sheets_service.py::COLUMNS` / `_map_metadata_to_row` /
`get_or_create_row`. Mapping is positional (`chr(65 + len(COLUMNS) - 1)`), so no
other field can be exported and column order is fragile.

**RC-2 — duplicate and wrongly updated rows.**
`ExportService.process_export` never passes the stored `sheet_row_id` to the sheet
service, and `get_or_create_row` deduplicates on the *exact* `Title` + `; `.join(authors)
string. The authors separator is `"; "` in code but the existing rows (and the
`Authors` test contract) use `", "`, so multi-author books never match their own row
and are appended again (`Debout…` rows 3 and 5). Trusting a stored row id without
verifying its identity would also write into the wrong row (Le coeur vert stores 3).

**RC-3 — empty values overwrite populated cells.**
`GoogleSheetsService.get_or_create_row` builds a complete row from the current
metadata and `update()`s all 9 cells; `""` from a failed provider replaces existing
text. No read-merge-write step exists.

**RC-4 — cover placeholders are accepted as real covers.**
`CleanCoverService._validate_image_file` (and `CoverDownloadService._validate_image`)
check only dimensions. A 261×261 QR PNG passes. The URL token filter that already
exists in `GoogleSheetsService._build_image_formula` (`kindle`, `qr`, `app/`,
`store-link`) is *not* applied when persisting `cover_status='found'`.

**RC-5 — cover reuse crashes and re-searches.**
`CleanCoverService._find_existing_cover_url()` calls `self._search_internet_archive`
and `self._search_bnf`, which do not exist on `CleanCoverService`
(they exist on `BookLookupService`). When `local_cover_path` exists but `cover_url`
is empty, `find_clean_cover` raises `AttributeError`; `ExportService` catches it and
marks the export FAILED → retries with backoff → dead letter. The sheet never gets the
metadata even though identification succeeded.

**RC-6 — cover failure state is not persisted.**
`ExportService.process_export`, `else` branch (no cover found) sets
`metadata.cover_status` but never calls `update_metadata`, so the next run redoes the
whole cover search, and the DB keeps `cover_status = NULL`.

**RC-7 — ISBN is not used for edition-specific enrichment or cover lookup.**
`MetadataEnrichmentService.enrich` always searches title+author; `CleanCoverService`
tries Google Books title+author before Open Library ISBN. Requirement priority
(ISBN/edition first) was not implemented. `_merge_enrichment` also ignores
`page_count` and `subtitle`.

**RC-8 — completed exports are never revisited.**
`ExportRepository.get_pending_exports` only returns `pending`/`retrying`;
`ExportService` marks `completed` even when `cover_status='pending_retry'` or
`not_found`. There is no "resume from database" path for a partially enriched record.

**RC-9 — unnecessary repeated external calls.**
Each `process_export` re-runs `MetadataEnrichmentService.enrich` (2–4 HTTP calls) even
when the record is already fully enriched, because no completion signal is checked.

**RC-10 — unvalidated cover URL can still reach the sheet.**
On the `pending_retry` branch the (possibly invalid) `cover_url` is persisted, and
`_map_metadata_to_row` renders it into `=IMAGE(...)` even though the download failed.

**RC-11 — every successful provider response was discarded.**
`BookLookupService._make_request` (`services/book_lookup_service.py`) classifies the
response with `utils.http_errors.classify_http_error`, which returns
`("success", False)` for 2xx/3xx, then does `if not is_transient: raise RuntimeError(
"Permanent HTTP 200")`. Every HTTP 200 was therefore converted into a permanent error
and `_make_request` returned `None`, so `search_by_isbn` / `search_by_title_author`
produced **zero candidates** in production and logged
`HTTP request failed after retries (...): Permanent HTTP 200`.
Live confirmation (this run): `GET https://openlibrary.org/search.json?isbn=9782825104699`
returns `200` with 2 docs, yet the service reported 0 candidates.
Google Books additionally answers `429 Quota exceeded` for the configured key today,
which is an account/quota limit, not a code defect.

Consequence: this is the primary reason metadata columns stayed empty/partial — the
lookup layer never saw any provider data. Fixed by returning the response for the
`"success"` category and only raising for genuinely permanent 4xx errors.

## 5. Recommended minimal changes

1. Persist `page_count` (additive DB migration) — no behaviour change elsewhere.
2. ISBN-first enrichment; merge `page_count`/`subtitle`; never overwrite; skip
   re-running when the record is already resolved.
3. Fix `_find_existing_cover_url`; add ISBN-first cover lookup; reject placeholders
   (URL tokens, GIF, dimensions) before download *and* on validation; only expose a
   validated URL to the sheet; persist cover failure state.
4. Google Sheets: resolve columns by **header name**, keep the existing 9 columns,
   read-merge-write so populated cells are never cleared, verify a stored row id
   before using it, deduplicate with the same author separator the sheet already uses,
   and skip writes when nothing changed. Do **not** add new columns (that would
   redesign the production sheet).
5. Resume support: add an explicit requeue for incomplete records and make retries
   reuse persisted metadata/cover so OCR and extraction are never repeated.
6. HTTP layer: return successful responses instead of classifying them as permanent
   failures (RC-11); add a regression test so a real 200 payload is parsed.

No change to: OCR provider, `BookTextExtractor` title/author logic, prompts,
confidence thresholds, candidate ranking, `MetadataService.identify_book`.
