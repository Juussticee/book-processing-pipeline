"""Patch Google Sheets cover cells for not_found books using Drive fallback."""
import os
import sys

sys.path.insert(0, os.getcwd())

from core.config import Config
from repositories.book_metadata_repository import BookMetadataRepository
from services.google_sheets_service import GoogleSheetsService


def main():
    config = Config.from_env()
    metadata_db_path = os.path.join(config.data_directory, "metadata.db")
    metadata_repo = BookMetadataRepository(metadata_db_path)

    creds_path = getattr(config, "google_sheets_credentials", "")
    if not creds_path or not os.path.exists(creds_path):
        print("Google Sheets credentials not found — skipping")
        return

    sheets = GoogleSheetsService(
        credentials_file=creds_path,
        sheets_id=config.google_sheet_id,
        tab_name=getattr(config, "google_sheets_tab_name", "Sheet1"),
    )
    with sheets:
        updated = 0
        for meta in metadata_repo.list():
            if (meta.cover_status or "").strip().lower() != "not_found":
                continue
            image_formula, open_cover_url = sheets._build_image_formula(
                meta.cover_url or "",
                meta.local_cover_path,
                meta.drive_file_id,
            )
            if not image_formula:
                continue
            row_num = sheets.get_or_create_row(meta)
            if not row_num:
                continue
            cols = {
                "Cover URL": image_formula,
                "Open Cover": f'=HYPERLINK("{open_cover_url}", "Open Cover")' if open_cover_url else "",
            }
            sheets.update_row(row_num, cols)
            print(f"Updated row {row_num} for {meta.title}")
            updated += 1
        print(f"Patched {updated} rows in Google Sheets")


if __name__ == "__main__":
    main()
