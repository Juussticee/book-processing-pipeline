"""Force-refresh Cover URL column for all books in Google Sheets.

Matches existing sheet rows by Drive File ID to avoid creating duplicates.
If a row is missing for a book, it appends a new one at the end.
Any extra rows beyond the current DB count are removed.
"""
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
        sheet = sheets._get_sheet()
        all_values = sheet.get_all_values()
        header = all_values[0] if all_values else list(sheets.COLUMNS)
        existing_rows = all_values[1:] if len(all_values) > 1 else []

        # Build map of drive_file_id -> row_number (1-based)
        drive_id_to_row = {}
        for idx, row in enumerate(existing_rows):
            drive_id = row[sheets.DRIVE_FILE_ID_COLUMN_INDEX] if len(row) > sheets.DRIVE_FILE_ID_COLUMN_INDEX else ""
            if drive_id:
                drive_id_to_row[drive_id] = idx + 2  # row 1 is header

        metas = metadata_repo.list()
        updated = 0
        appended = 0

        # First pass: update matching rows
        for meta in metas:
            image_formula, open_cover_url = sheets._build_image_formula(
                meta.cover_url or "",
                meta.local_cover_path,
                meta.drive_file_id,
            )
            if not image_formula:
                print(f"Skipping {meta.title}: no image available")
                continue

            if meta.drive_file_id in drive_id_to_row:
                row_num = drive_id_to_row[meta.drive_file_id]
            else:
                # Append new row at the end
                row_data = sheets._map_metadata_to_row(meta, meta.local_cover_path)
                row_data[-1] = sheets._get_timestamp()
                sheet.append_rows([row_data], insert_data_option="OVERWRITE", value_input_option="USER_ENTERED")
                row_num = len(sheet.get_all_values())
                appended += 1

            cols = {
                "Cover URL": image_formula,
                "Open Cover": f'=HYPERLINK("{open_cover_url}", "Open Cover")' if open_cover_url else "",
            }
            sheets.update_row(row_num, cols)
            print(f"Updated row {row_num} for {meta.title}")
            updated += 1

        # Cleanup: remove extra rows beyond current DB count
        current_row_count = len(sheet.get_all_values())
        expected_rows = 1 + len(metas)  # header + data rows
        if current_row_count > expected_rows:
            for row_num in range(current_row_count, expected_rows, -1):
                sheet.delete_rows(row_num)
                print(f"Deleted extra row {row_num}")

        print(f"Force-refreshed {updated} rows ({appended} appended) in Google Sheets")


if __name__ == "__main__":
    main()
