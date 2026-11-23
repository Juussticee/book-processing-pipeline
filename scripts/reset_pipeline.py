"""Reset pipeline state: clear metadata DB rows and Google Sheet data rows."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

PROJECT_ROOT = Path(r"C:\Users\hashe\Documents\Book Auth")
from core.config import Config
from repositories.book_metadata_repository import BookMetadataRepository
from services.google_sheets_service import GoogleSheetsService

def main() -> None:
    config = Config.from_env()
    db_path = str(PROJECT_ROOT / "data" / "metadata.db")
    repo = BookMetadataRepository(db_path)

    cleared_db = repo.clear_all()
    print(f"Cleared DB rows: {cleared_db}")

    # Clear Sheet data rows only; preserve header
    creds_path = getattr(config, "google_sheets_credentials", None) or getattr(config, "google_service_account_file", None) or ""
    if not creds_path or not Path(creds_path).exists():
        creds_path = str(PROJECT_ROOT / "credentials" / "service_account.json")
    sheets = GoogleSheetsService(
        credentials_file=creds_path,
        sheets_id=config.google_sheet_id,
        tab_name=getattr(config, "google_sheets_tab_name", "Sheet1"),
    )
    cleared_sheet = 0
    with sheets:
        try:
            sheet = sheets._get_sheet()
            values = sheet.get_all_values()
            if values:
                sheet.clear()
                sheet.append_row(values[0])
                cleared_sheet = max(0, len(values) - 1)
        except Exception as exc:
            print(f"Sheet clear failed: {exc}")
    print(f"Cleared Sheet data rows: {cleared_sheet}")

if __name__ == "__main__":
    main()
