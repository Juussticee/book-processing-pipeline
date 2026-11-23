"""Export book covers to a local Excel workbook with embedded images."""
import os
from pathlib import Path

import sqlite3
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from PIL import Image
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


PROJECT_ROOT = Path(r"C:\Users\hashe\Documents\Book Auth")
DB_PATH = PROJECT_ROOT / "data" / "metadata.db"
COVERS_DIR = PROJECT_ROOT / "data" / "covers"
DRIVE_DIR = PROJECT_ROOT / "data" / "drive_inputs"
OUTPUT_PATH = PROJECT_ROOT / "data" / "book_covers.xlsx"


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"metadata.db not found at {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT title, vision_author, cover_status, cover_source, local_cover_path, drive_file_id "
        "FROM book_metadata ORDER BY title"
    )
    rows = c.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Book Covers"

    # Header
    headers = [
        "Title",
        "Author",
        "Status",
        "Source",
        "Local Cover Path",
        "Drive File ID",
        "Cover Image",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Column widths
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 42
    ws.column_dimensions["F"].width = 36
    ws.column_dimensions["G"].width = 32
    ws.row_dimensions[1].height = 24

    tmp_files = []
    for row in rows:
        title = row["title"] or ""
        author = row["vision_author"] or ""
        status = row["cover_status"] or ""
        source = row["cover_source"] or ""
        local_path = row["local_cover_path"] or ""
        drive_id = row["drive_file_id"] or ""

        # Choose image path: prefer local cover, fallback to downloaded drive image
        image_path = None
        if local_path:
            candidate = Path(local_path)
            if not candidate.is_absolute():
                candidate = PROJECT_ROOT / candidate
            if candidate.exists():
                image_path = candidate

        if image_path is None and drive_id:
            drive_img = DRIVE_DIR / f"{drive_id}.jpg"
            if drive_img.exists():
                image_path = drive_img

        ws.append([title, author, status, source, str(local_path), str(drive_id), ""])
        row_idx = ws.max_row

        if image_path and image_path.exists():
            try:
                # Pillow resize preserves aspect within display box
                with Image.open(image_path) as source_img:
                    max_w, max_h = 150, 200
                    w, h = source_img.size
                    if w > max_w or h > max_h:
                        scale = min(max_w / w, max_h / h)
                        resized = source_img.resize(
                            (int(w * scale), int(h * scale)), Image.LANCZOS
                        )
                    else:
                        resized = source_img
                    tmp = image_path.with_suffix(".tmp_xl.jpg")
                    if resized.mode != "RGB":
                        resized = resized.convert("RGB")
                    resized.save(tmp, quality=90)
                    tmp_files.append(tmp)
                xl_img = XLImage(str(tmp))
                ws.add_image(xl_img, f"G{row_idx}")
            except Exception as exc:
                print(f"Failed to embed image for {title[:40]}: {exc}")

    # Adjust rows
    for row_idx in range(2, ws.max_row + 1):
        ws.row_dimensions[row_idx].height = 110

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(OUTPUT_PATH))

    # Cleanup temp files after save
    for tmp in tmp_files:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    print(f"Exported Excel workbook to: {OUTPUT_PATH}")
    print(f"Books exported: {len(rows)}")


if __name__ == "__main__":
    main()
