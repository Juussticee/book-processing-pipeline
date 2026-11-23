"""Isolated Google Sheets acceptance check.

Writes a small number of rows into an **isolated** spreadsheet that you name
explicitly, then verifies the real customer-facing behaviour:

- the first column is ``Status`` and carries the three labels
  (``Confirmed`` / ``Incomplete`` / ``Needs Review``)
- metadata cells are written as values (``RAW``), not formulas
- the ISBN cell keeps leading zeros verbatim
- the cover cell carries a ``=IMAGE("…")`` formula (``USER_ENTERED``)
- no cell reports a formula error
- a record that moves from ``Needs Review`` to ``Confirmed`` updates its row
  instead of appending a second one
- a repeated export does not duplicate a row
- optionally exports the spreadsheet as PDF and counts embedded images, which
  is the closest programmatic proxy for "the cover actually renders"

It never touches the production spreadsheet: the value passed to
``--spreadsheet-id`` is compared against ``GOOGLE_SHEET_ID`` and the run is
refused when they match. The credentials file is never printed.

Usage (PowerShell, from the project root):

    .venv\\Scripts\\python.exe scripts\\sheets_acceptance_check.py `
        --spreadsheet-id <ISOLATED_SPREADSHEET_ID> --tab Acceptance `
        --pdf-out data\\sheets_acceptance.pdf

The spreadsheet must first be shared with the service-account email (Editor).
Print that address with:

    .venv\\Scripts\\python.exe -c "import json; from core.config import Config;
    print(json.load(open(Config.from_env().google_sheets_credentials))['client_email'])"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from models.book_metadata import BookMetadata  # noqa: E402
from services.google_sheets_service import GoogleSheetsService  # noqa: E402

# A real book identified live on 2026-11-13 (Le cœur vert / Jean-Louis Kuffer).
LIVE_BOOK = {
    "title": "Le coeur vert",
    "authors": ["Jean-Louis Kuffer"],
    "publisher": "L'AGE D'HOMME",
    "page_count": 148,
    "cover_url": (
        "https://books.google.com/books/content?id=-EhnVGylV3EC"
        "&printsec=frontcover&img=1&zoom=1&edge=curl&source=gbs_api"
    ),
    "source": "google_books",
    "confidence": 1.0,
}

LEADING_ZERO_ISBN = "0012345678"


def _metadata(**overrides) -> BookMetadata:
    data = dict(LIVE_BOOK)
    data.update(overrides)
    return BookMetadata(**data)


def check(service: GoogleSheetsService, args) -> int:
    worksheet = service._get_sheet()  # noqa: SLF001 - creates the tab + header
    failures: list[str] = []

    def record(ok: bool, message: str) -> None:
        print(("PASS  " if ok else "FAIL  ") + message)
        if not ok:
            failures.append(message)

    header = service._read_header(worksheet.get_all_values())  # noqa: SLF001
    print("Header:", header)
    record(header[0] == "Status", "Status is the first column")

    title_idx = header.index("Title")
    isbn_idx = header.index("ISBN-13")
    cover_idx = header.index("Open Cover")
    status_idx = header.index("Status")

    def rows() -> list[list[str]]:
        return worksheet.get_all_values()

    # ---- Row 1: a fully confirmed record ---------------------------------
    confirmed = _metadata(isbn_13="9782825104699")
    confirmed_row = service.get_or_create_row(confirmed, None)
    values = rows()
    record(
        values[confirmed_row - 1][status_idx] == "Confirmed",
        f"complete record is Confirmed (row {confirmed_row})",
    )
    record(
        values[confirmed_row - 1][title_idx] == LIVE_BOOK["title"],
        "title cell is a value, not a formula",
    )
    record(
        values[confirmed_row - 1][cover_idx].startswith('=IMAGE("'),
        "cover cell is an =IMAGE(...) formula",
    )

    # ---- Row 2: no ISBN at all must stay Confirmed -------------------------
    no_isbn = _metadata(
        title="Acceptance no-ISBN confirmed",
        isbn_13=None,
        isbn_10=None,
    )
    no_isbn_row = service.get_or_create_row(no_isbn, None)
    no_isbn_values = rows()[no_isbn_row - 1]
    record(
        no_isbn_values[status_idx] == "Confirmed",
        f"missing ISBN alone does not downgrade the record (row {no_isbn_row})",
    )
    record(
        no_isbn_values[isbn_idx] == "", f"the ISBN cell is empty (row {no_isbn_row})"
    )

    # ---- Row 3: identity confirmed, expected metadata missing --------------
    incomplete = _metadata(
        title="Acceptance incomplete - no publisher",
        publisher=None,
    )
    incomplete_row = service.get_or_create_row(incomplete, None)
    record(
        rows()[incomplete_row - 1][status_idx] == "Incomplete",
        f"missing publisher yields Incomplete (row {incomplete_row})",
    )

    # ---- Row 4: needs review, then promoted in place ----------------------
    review = _metadata(
        title="Acceptance review - to be resolved",
        status="needs_review",
        confidence=0.42,
    )
    review_row = service.get_or_create_row(review, None)
    record(
        rows()[review_row - 1][status_idx] == "Needs Review",
        f"review record is marked Needs Review (row {review_row})",
    )

    before = len(rows())
    resolved = _metadata(
        title="Acceptance review - to be resolved",
        isbn_13="9780306406157",
        status="identified",
        confidence=0.97,
    )
    promoted = service.get_or_create_row(resolved, None, row_id=review_row)
    after_rows = rows()
    record(
        promoted == review_row and len(after_rows) == before,
        f"resolving the review updates row {review_row} in place",
    )
    record(
        after_rows[review_row - 1][status_idx] == "Confirmed",
        "the same row is now labelled Confirmed",
    )

    # ---- Row 5: leading-zero ISBN, still a value -------------------------
    zero = _metadata(
        title="Acceptance leading-zero ISBN",
        isbn_13=LEADING_ZERO_ISBN,
    )
    zero_row = service.get_or_create_row(zero, None)
    record(
        rows()[zero_row - 1][isbn_idx] == LEADING_ZERO_ISBN,
        f"ISBN leading zeros preserved verbatim (row {zero_row})",
    )

    # ---- Idempotence ------------------------------------------------------
    before = len(rows())
    again = service.get_or_create_row(confirmed, None)
    record(
        again == confirmed_row and len(rows()) == before,
        "re-export does not append a duplicate row",
    )

    # ---- Re-read as formulas / errors -------------------------------------
    formula_values = worksheet.get(
        f"A{confirmed_row}:{chr(64 + len(header))}{confirmed_row}",
        value_render_option="FORMULA",
    )
    record(
        formula_values[0][title_idx] == LIVE_BOOK["title"],
        "metadata cell re-read as FORMULA is still plain text",
    )
    record(
        formula_values[0][cover_idx].startswith('=IMAGE("'),
        "cover cell re-read as FORMULA is still the IMAGE formula",
    )

    errors = _cell_errors(service, args, 1, max(len(rows()), confirmed_row))
    record(errors == [], f"no formula error rendered in the written rows ({errors})")

    # ---- Rendering proxy: embedded images in the PDF export --------------
    if args.pdf_out:
        pdf_bytes = _export_pdf(service, args.spreadsheet_id)
        if pdf_bytes is None:
            print("SKIP  PDF export unavailable with the current credentials/scopes")
        else:
            out = Path(args.pdf_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(pdf_bytes)
            images = pdf_bytes.count(b"/Subtype /Image")
            record(
                pdf_bytes.startswith(b"%PDF") and images > 0,
                f"rendered PDF has {images} embedded image(s) -> {out}",
            )

    print()
    if failures:
        print(f"RESULT: FAILED ({len(failures)} check(s) failed)")
        return 1
    print("RESULT: LIVE ISOLATED SHEETS ACCEPTANCE PASSED")
    return 0


def _credentials(service: GoogleSheetsService):
    """Reuse the already-authorised credentials from the Sheets client."""
    return service._get_client().auth  # noqa: SLF001


def _cell_errors(service: GoogleSheetsService, args, first_row: int, last_row: int):
    """Return any ``errorValue`` rendered in the written rows (e.g. #ERROR!)."""
    from googleapiclient.discovery import build

    api = build(
        "sheets", "v4", credentials=_credentials(service), cache_discovery=False
    )
    response = (
        api.spreadsheets()
        .get(
            spreadsheetId=args.spreadsheet_id,
            includeGridData=True,
            ranges=[f"{args.tab}!A{first_row}:Z{last_row}"],
        )
        .execute()
    )
    found: list[str] = []
    for sheet in response.get("sheets", []):
        for data in sheet.get("data", []):
            for row_data in data.get("rowData", []):
                for cell in row_data.get("values", []):
                    error = (cell.get("effectiveValue") or {}).get("errorValue")
                    if error:
                        found.append(str(error))
    return found


def _export_pdf(service: GoogleSheetsService, spreadsheet_id: str):
    """Export the spreadsheet as PDF through the Drive API (drive.readonly)."""
    from googleapiclient.discovery import build

    try:
        drive = build(
            "drive", "v3", credentials=_credentials(service), cache_discovery=False
        )
        return (
            drive.files()
            .export(fileId=spreadsheet_id, mimeType="application/pdf")
            .execute()
        )
    except Exception as exc:  # scope/ownership problem -> report, do not crash
        print(f"PDF export failed: {type(exc).__name__}: {str(exc)[:160]}")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Isolated Google Sheets acceptance check."
    )
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--tab", default="Acceptance")
    parser.add_argument("--pdf-out", default="")
    args = parser.parse_args(argv)

    config = Config.from_env()
    production_id = (getattr(config, "google_sheet_id", "") or "").strip()
    if production_id and args.spreadsheet_id.strip() == production_id:
        print("REFUSED: that is the production spreadsheet id (GOOGLE_SHEET_ID).")
        print("Create a separate spreadsheet, share it with the service account,")
        print("then retry with the new id.")
        return 2

    credentials_file = getattr(config, "google_sheets_credentials", "")
    if not credentials_file or not os.path.exists(credentials_file):
        print("Blocker: GOOGLE_SHEETS_CREDENTIALS is missing or does not exist.")
        return 2

    service = GoogleSheetsService(
        credentials_file=credentials_file,
        sheets_id=args.spreadsheet_id,
        tab_name=args.tab,
    )
    try:
        return check(service, args)
    except Exception as exc:  # wrong id / not shared / API disabled -> blocker
        print(f"BLOCKED: {type(exc).__name__}: {str(exc)[:200]}")
        print("Check that the spreadsheet id is correct and shared with the")
        print("service-account email as Editor, and that the Sheets + Drive APIs")
        print("are enabled for the project.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
