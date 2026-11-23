# Book Processing Pipeline

An unattended pipeline that turns photographs of book covers into a reviewable,
customer-facing catalogue.

A photo appears in a Google Drive folder → the pipeline validates and downloads
it, normalises its orientation, refuses duplicates, reads the cover with a hosted
OCR model, resolves the book against public metadata catalogues, ranks the
candidates, stores the record in SQLite, retrieves and verifies a cover image,
and finally writes one row per book to Google Sheets with a status label and a
live cover thumbnail.

It is a single long-running Python process: no web UI, no scheduler, no database
server, no container.

> **Status of this repository.** The application is complete and its offline test
> suite passes. It has **not** been deployed by this repository's maintainers as
> part of the release, and the live Google Sheets export has not been re-verified
> against a fresh spreadsheet since the changes in this release. See
> [Limitations](#limitations) and [Verification status](#verification-status).

---

## Contents

- [Who it is for](#who-it-is-for)
- [Processing workflow](#processing-workflow)
- [Architecture](#architecture)
- [Features](#features)
- [The `Status` label](#the-status-label)
- [Technology](#technology)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [Operator commands](#operator-commands)
- [Testing](#testing)
- [Deployment](#deployment)
- [Security](#security)
- [Limitations](#limitations)
- [Verification status](#verification-status)
- [Documentation map](#documentation-map)

---

## Who it is for

A single operator running one catalogue of one book collection:

- photographs arrive in **one** Drive folder, one book per photo;
- there is no inbound traffic, no public endpoint and no multi-tenant logic;
- everything is sized for one always-on machine, not for horizontal scaling.

## Processing workflow

```
Google Drive folder
      │  (flat listing, one folder, every 30 s)
      ▼
Intake validation ......... format, size, blocked names, folders are refused
      ▼
Image download ............ into data/downloads
      ▼
Image preprocessing ....... EXIF orientation, resize, normalisation
      ▼
Duplicate detection ....... byte identity + content identity, before any OCR
      ▼
PaddleOCR-VL (Baidu AI Studio, hosted)
      ▼
Book identification ....... Google Books + Open Library lookup, ranking,
      │                      verification, confidence scoring
      ▼
Metadata enrichment ....... publisher, dates, categories, ISBN checksums
      ▼
SQLite persistence ........ data/metadata.db (+ jobs.db, app.db)
      ▼
Export queue .............. data/app.db
      ▼
Google Sheets ............. one row per book, Status + metadata + =IMAGE() cover
```

## Architecture

One process (`app.py`), three independent loops, three SQLite files, one cache
directory:

```
app.py
├── drive sync loop ......... list folder, intake gate, download, create job
├── identifier .............. preprocess → duplicate gate → OCR → lookup → rank
└── export loop ............. enrich → cover → sheet row → status label

data/app.db        sync state, export queue
data/jobs.db       job lease, retries, recovery
data/metadata.db   identified books (the system of record)
data/covers/       verified cover images
data/downloads/    original downloaded photos
```

The design keeps **identity** (what the book is, decided once, stored) separate
from **presentation** (how it looks in the sheet, recomputed on demand). A failed
export never changes what the pipeline believes a book is.

## Features

- **Incremental Drive discovery** — a flat folder listing; already-known file ids
  are skipped, so a restart does not re-download the collection.
- **Intake validation** — MIME/extension allow-list, size limit, blocked names,
  Google Docs/PDF/HEIC and folders refused before download.
- **EXIF orientation normalisation** — EXIF is applied and stripped; a geometry
  heuristic plus a bounded rotation fallback handles photos with no EXIF tag.
- **Duplicate protection** — byte-identical files are cancelled before OCR;
  content identity (SHA-256, perceptual hash, colour, aspect) detects the same
  physical book photographed twice.
- **OCR through a hosted model** — Baidu AI Studio **PaddleOCR-VL** is the only
  OCR provider. There is no local OCR engine and no alternative provider.
- **Identification with evidence** — title/author/ISBN candidate search, ranked
  candidates, verification evidence, confidence bands, and an explicit
  `needs_review` outcome instead of a silent guess.
- **Metadata enrichment** — publisher resolution, category/date fill-in, ISBN
  checksum validation, without overwriting richer existing values.
- **Cover retrieval and verification** — candidate cover URLs are downloaded,
  dimension/quality checked, and rejected when they do not match the book.
- **Persistent job queue with recovery** — job leases, retry counts, exponential
  backoff, and recovery of jobs left `processing` by a crash.
- **Google Sheets export** — metadata written as `RAW` values (leading zeros in
  ISBNs preserved), the cover written as a single `USER_ENTERED` `=IMAGE("…")`
  cell, and existing rows updated in place rather than appended twice.
- **Status classification** — `Confirmed` / `Incomplete` / `Needs Review`,
  derived from persisted state only.
- **Operator diagnostics** — `--status`, requeue commands, a read-only workbook
  snapshot script, and structured logs. No diagnostic prints a credential.

## The `Status` label

| Label | Rule |
|---|---|
| `Confirmed` | Identity accepted, publisher present, cover present. ISBN is optional. |
| `Incomplete` | Identity accepted, but the publisher or the cover is missing. |
| `Needs Review` | Identity uncertain or unresolved: persisted status is not `identified`, the title is blank or a placeholder, or the confidence is below the review threshold. Takes precedence over the other two. |

An empty description, date, language or category never changes the label, and a
subsequent export that arrives with poorer provider data cannot downgrade a row
that already holds a publisher or a cover.

## Technology

Python 3.11. The runtime dependencies (`requirements.txt`):

| Package | Used for |
|---|---|
| `python-dotenv` | `.env` loading |
| `Pillow`, `numpy`, `opencv-python-headless` | image decoding, EXIF, orientation, preprocessing |
| `google-auth`, `google-api-python-client` | Drive and Sheets service-account authentication |
| `gspread` | worksheet read/write |
| `requests` | PaddleOCR-VL, Google Books, Open Library, cover downloads |
| `rapidfuzz` | fuzzy title/author matching, same-book detection |

Test/quality tooling: `pytest`, `pytest-cov`, `black`, `ruff`, `mypy`.

External services: Google Drive, Google Sheets, Baidu AI Studio (PaddleOCR-VL),
Google Books, Open Library, cover image hosts.

## Requirements

- Python **3.11** (verified on 3.11.9; 3.12+ is untested).
- An always-on host with a persistent local disk; Windows 10/11 or Windows Server
  2022 is the documented target. Linux works; the service wiring differs.
- Outbound HTTPS to Google, Baidu AI Studio, Open Library and cover hosts. No
  inbound ports are required.
- A Google **service account** JSON key, a Drive folder shared with it as
  **Viewer**, and a spreadsheet shared with it as **Editor**.
- A **Baidu AI Studio** access token for PaddleOCR-VL.

## Installation

```bash
git clone <this-repository-url> book-processing-pipeline
cd book-processing-pipeline
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1     Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env        # Windows: Copy-Item .env.example .env
```

Create the local directories the app expects (all gitignored):

```bash
mkdir -p data/covers data/downloads logs credentials
```

Place your service-account JSON at `credentials/service_account.json`. Never
commit it.

## Configuration

All configuration is environment variables, read from `.env` (see
`.env.example` for the annotated template). The required ones:

| Variable | Meaning |
|---|---|
| `GOOGLE_DRIVE_FOLDER_ID` | the single Drive folder that is scanned |
| `GOOGLE_SHEET_ID` | destination spreadsheet id |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | path to the service-account JSON |
| `GOOGLE_SHEETS_CREDENTIALS` | path to the service-account JSON (same file) |
| `PADDLEOCR_API_TOKEN` | Baidu AI Studio access token |
| `ENVIRONMENT` | `Production` on a server |
| `DATA_DIRECTORY`, `LOG_DIRECTORY`, `TEMP_DIRECTORY` | local writable paths |

Optional providers (Google Books API key, Exa, Gemini recovery) default to off or
unauthenticated and are not needed for the pipeline to run.

Find the service-account address you must share the folder and spreadsheet with —
this prints only the address, never the key:

```bash
python -c "import json; from core.config import Config; print(json.load(open(Config.from_env().google_sheets_credentials))['client_email'])"
```

Validate the whole configuration without revealing secrets:

```bash
python app.py --status
```

## Running

```bash
python app.py                 # run the pipeline (long-running)
python app.py --status        # counters, queue depth, health, exit
```

On first start the app creates the SQLite files and directories, recovers jobs
interrupted by a previous crash, then runs the sync, processing and export loops
until stopped. Do not run two instances against the same `data/` directory.

Photos are added by dropping files into the configured Drive folder — there is no
upload command. See `docs/BOOK_INTAKE_GUIDE.md`.

## Operator commands

```bash
python app.py --status                 # pending / processing / failed / manual review / export queue
python app.py --requeue-failed-jobs    # move jobs out of exhausted retries
python app.py --requeue-incomplete     # retry exports with missing fields/covers
python app.py --refresh-sheet-status   # re-label existing sheet rows (writes Status cells only)
python scripts/read_sheet_state.py     # read-only workbook snapshot; never migrates
```

`--refresh-sheet-status` and the export path **write** to the configured
spreadsheet. `scripts/read_sheet_state.py` is deliberately read-only: it opens the
worksheet without the `Status`-column migration, so inspecting a workbook can
never change it.

## Testing

```bash
python -m pytest tests/unit -q          # fully mocked, no network
python -m pytest tests/integration -q   # controlled fixtures, no network
python -m pytest tests -q               # everything
```

Unit tests use mocks for Google, Baidu and the vision model; no API quota is
consumed. Integration tests exercise the real preprocessing, duplicate detection,
repositories, workers and export formatting with stubbed network clients.

A few suites need private local data — photographs of one collection, or a
benchmark corpus — which is **not** distributed with this repository. They report
`skipped` on a fresh clone instead of failing, and per-collection identifiers live
in local files under `data/` rather than in the code:

| Suite | Needs |
|---|---|
| `tests/integration/test_real_photo_acceptance.py`, photo-driven parts of `tests/integration/test_pipeline_end_to_end.py` | the real photo set; set `REAL_PHOTO_FIXTURES=/path/to/dir` to a directory holding `metadata.json` plus the JPEGs |
| `tests/integration/test_vision_benchmark.py` | `data/downloads/`, `data/vision_model_responses/`, `data/vision_benchmark_books.json`, `data/clean_run_results.json` |
| `tests/integration/test_phase25_paddleocr_benchmark.py` | its OCR cache plus `data/rotated_images.json` |

`_e2e_verify.py` (a development end-to-end check, not part of the application)
reads its target Drive ids from `data/e2e_drive_ids.txt`.

Everything else runs offline in a fresh clone.

## Deployment

A production deployment is a Windows service (or any process supervisor) around
`python app.py`, with the persistent paths on local disk. The step-by-step
procedure — host sizing, NSSM service definition, backups, restore verification,
upgrade and rollback — is in `docs/DEPLOYMENT_READINESS.md` and
`docs/RUNBOOK.md`. Nothing in this repository installs a service or provisions a
host.

## Security

- `.env`, `credentials/`, `data/`, `logs/` and `.venv/` are gitignored. No
  credential, database, downloaded photograph or customer record is tracked.
- No live Drive folder id, spreadsheet id or API key is committed. Examples in the
  docs are placeholders.
- Credentials are supplied by whoever deploys the app: a Google service-account
  JSON in `credentials/` and a Baidu token in `.env`. The service account needs
  Drive **Viewer** on the folder and Sheets **Editor** on the spreadsheet — not
  broader access.
- Logs redact tokens; the credential path is never logged and no service prints a
  key. `tests/unit/test_redaction.py` pins this.
- If a key is ever exposed, rotate it at the provider: delete the old service
  account key and issue a new one, and regenerate the Baidu token.

## Limitations

- **One collection, one folder, one process.** There is no multi-tenant isolation,
  no queue service, no clustering and no rate-limit coordination between hosts.
- **SQLite on local disk.** Backups are file copies; there is no replication.
  Running two instances against one `data/` directory will corrupt state.
- **Hosted OCR is a network dependency.** PaddleOCR-VL latency and availability
  bound throughput. There is no offline OCR fallback by design.
- **OCR accuracy is not guaranteed.** Difficult covers (glare, blur, unusual
  scripts, tiny text) are expected; the pipeline routes them to `Needs Review`
  instead of guessing.
- **Identification is not autonomous.** Public catalogue coverage is incomplete;
  books with no catalogue entry remain in review for a human.
- **Cover images depend on third-party hosts.** A cover URL that stops resolving
  leaves the row `Incomplete`.
- **No measured production throughput figures.** Timing was observed in local runs
  only, and the numbers depend on the OCR service, not on this code.
- **Windows-first documentation.** Service wiring, ACLs and backup commands are
  written for Windows.

## Verification status

What was actually verified for this release:

- The full offline suite runs; see `docs/DEPLOYMENT_READINESS.md` §6 for counts and
  for the tests that require private local data.
- The SQLite schema, backup, restore and integrity checks were exercised on
  copies of real databases, never on the production files.
- PaddleOCR-VL, Drive and Sheets integrations were exercised in earlier live runs
  documented in `REFACTOR-LOG.md`. They were **not** re-run as part of this
  release, and no live API quota was spent producing it.

What was **not** verified for this release:

- A fresh end-to-end run against a newly created spreadsheet (the isolated live
  acceptance requires an operator-owned spreadsheet id; see
  `docs/RUNBOOK.md` §13).
- Rendering of the `=IMAGE()` cover formula in a browser (it depends on the
  workbook's external-URL setting and on the browser session).
- Any production deployment, service installation or hosting setup.

## Documentation map

| File | Contents |
|---|---|
| `docs/DEPLOYMENT_READINESS.md` | architecture, requirements, credential setup, what is verified and how, limitations, hosting and deployment preparation |
| `docs/RUNBOOK.md` | exact commands: start/stop, health checks, status, logs, backup, restore, isolated Sheets acceptance |
| `docs/BOOK_INTAKE_GUIDE.md` | how to submit photos, accepted formats, photo quality, how to read the sheet, recovery |
| `docs/INCIDENT-STATUS-COLUMN.md` | postmortem: a read-only diagnostic once migrated a workbook, and the fix |
| `REFACTOR-LOG.md` | development history, phase by phase, with verification notes |

## License

No license is granted. This project is publicly viewable; it is not open source.
