"""Read-only snapshot of the configured Google Sheet.

Prints the tab header and the first rows exactly as stored. It never writes:
the worksheet is opened with ``ensure_layout=False`` so a legacy workbook is
*not* migrated by looking at it.
"""

import os
import sys

sys.path.insert(0, os.getcwd())

from core.config import Config
from services.google_sheets_service import GoogleSheetsService

config = Config.from_env()
svc = GoogleSheetsService(
    credentials_file=config.google_sheets_credentials,
    sheets_id=config.google_sheet_id,
    tab_name=getattr(config, "google_sheets_tab_name", "Sheet1"),
)
with svc:
    sheet = svc._get_sheet(ensure_layout=False)  # noqa: SLF001 - read-only open
    all_values = sheet.get_all_values(value_render_option="FORMULA")
    if not all_values:
        print("rows=0 (empty tab)")
        raise SystemExit(0)
    header = list(all_values[0])
    while header and not header[-1]:
        header.pop()
    print(f"rows={len(all_values) - 1} (excluding header)")
    print(f"columns={len(header)}")
    for idx, name in enumerate(header):
        print(f"  col{idx}: {name}")
    for row_idx, row in enumerate(all_values[1:6], start=2):
        preview = [str(cell)[:40] for cell in row[: len(header)]]
        print(f"row {row_idx}: {preview}")
