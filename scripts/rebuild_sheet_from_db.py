import os
import sys

sys.path.insert(0, os.getcwd())

from core.config import Config
from repositories.book_metadata_repository import BookMetadataRepository
from services.google_sheets_service import GoogleSheetsService

config = Config.from_env()
metadata_repo = BookMetadataRepository(
    os.path.join(config.data_directory, "metadata.db")
)
sheets = GoogleSheetsService(
    credentials_file=config.google_sheets_credentials,
    sheets_id=config.google_sheet_id,
    tab_name=getattr(config, "google_sheets_tab_name", "Sheet1"),
)

with sheets:
    sheet = sheets._get_sheet()
    # Clear all existing data rows
    existing = sheet.get_all_values()
    if len(existing) > 1:
        sheet.batch_clear([f"A2:Z{len(existing)}"])
    # Build rows from DB
    rows = [list(sheets.COLUMNS)]
    for meta in metadata_repo.list():
        image_formula, _ = sheets._build_image_formula(
            meta.cover_url or meta.thumbnail_url or "",
            meta.local_cover_path,
            getattr(meta, "drive_file_id", None),
        )
        title = meta.title or meta.vision_title or "(unreadable)"
        author = ", ".join(meta.authors) if meta.authors else (meta.vision_author or "")
        rows.append(
            [
                title,
                author,
                meta.isbn_13 or "",
                meta.publisher or "",
                meta.published_date or "",
                sheets._human_readable_language(meta.language),
                ", ".join(meta.categories) if meta.categories else "",
                meta.description or "",
                image_formula,
            ]
        )
    sheet.update(
        range_name=f"A1:{chr(64 + len(rows[0]))}{len(rows)}",
        values=rows,
        value_input_option="RAW",
        include_values_in_response=False,
    )
    # Metadata is written RAW (ISBNs keep their leading zeros); the cover cells
    # are then typed as live formulas so the images actually render.
    header = rows[0]
    for index, row in enumerate(rows[1:], start=2):
        cover_formula = row[header.index(sheets.COVER_COLUMN)]
        if cover_formula:
            sheets._ensure_cover_formula_written(sheet, header, index, cover_formula)
    final = sheet.get_all_values()
    print("Wrote rows:", len(final))
    for i, row in enumerate(final[:5], start=1):
        print(f"Row {i}: {row[:12]}")
