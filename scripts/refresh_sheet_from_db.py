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
    metas = metadata_repo.list()
    rows = [list(sheets.COLUMNS)]
    for meta in metas:
        image_formula, open_cover_url = sheets._build_image_formula(
            meta.cover_url or meta.thumbnail_url or "",
            meta.local_cover_path,
            meta.drive_file_id,
        )
        title = meta.title or meta.vision_title or "(unreadable)"
        author = ", ".join(meta.authors) if meta.authors else (meta.vision_author or "")
        status = "Cover Found" if meta.cover_url else "Cover Needed"
        rows.append(
            [
                title,
                author,
                meta.isbn_10 or "",
                meta.isbn_13 or "",
                meta.publisher or "",
                meta.published_date or "",
                sheets._human_readable_language(meta.language),
                ", ".join(meta.categories) if meta.categories else "",
                meta.description or "",
                meta.cover_url or "",
                image_formula,
                meta.local_cover_path or "",
                f"{(meta.confidence or 0.0):.4f}",
                meta.source or meta.cover_source or "",
                status,
                meta.drive_file_id,
                "",
            ]
        )
    sheet.update(
        range_name=f"A1:{chr(64 + len(rows[0]))}{len(rows)}",
        values=rows,
        include_values_in_response=False,
    )
    vals = sheet.get_all_values()
    print("rows_written", len(vals))
    for r in vals[1:5]:
        print(r)
