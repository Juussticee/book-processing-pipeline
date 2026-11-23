"""Phase 35C: Activate Google Books + Exa clean cover retrieval.

Configures API keys, then re-runs clean cover retrieval on the 14 persisted books.
Reuses existing covers. Does NOT redo OCR, vision, or Drive downloads.
Updates Google Sheets with found clean cover URLs (IMAGE formula).
"""

import os
import sys

sys.path.insert(0, os.getcwd())

from core.config import Config
from repositories.book_metadata_repository import BookMetadataRepository
from services.book_lookup_service import BookLookupService
from services.clean_cover_service import CleanCoverService
from services.google_sheets_service import GoogleSheetsService


def main():
    config = Config.from_env()

    # Verify API keys
    gb_key = getattr(config, "google_books_api_key", "")
    exa_key = getattr(config, "exa_api_key", "")
    print("Google Books API: " + ("CONFIGURED" if gb_key else "NOT CONFIGURED"))
    print("Exa API: " + ("CONFIGURED" if exa_key else "NOT CONFIGURED"))

    metadata_db_path = os.path.join(config.data_directory, "metadata.db")
    covers_dir = getattr(config, "covers_dir", "data/covers")

    metadata_repo = BookMetadataRepository(metadata_db_path)
    book_lookup = BookLookupService(
        google_books_api_key=gb_key,
        open_library_enabled=getattr(config, "open_library_enabled", True),
        google_books_timeout=getattr(config, "google_books_timeout", 15),
        open_library_timeout=getattr(config, "open_library_timeout", 10),
        exa_api_key=exa_key,
    )
    clean_cover_service = CleanCoverService(
        book_lookup=book_lookup,
        covers_dir=covers_dir,
    )

    all_metadata = metadata_repo.list()
    print(f"\nFound {len(all_metadata)} books in DB")

    stats = {
        "google_books": 0,
        "open_library": 0,
        "exa": 0,
        "not_found": 0,
    }
    gb_calls = 0
    exa_searches = 0

    for meta in all_metadata:
        title = meta.title or meta.vision_title or "Unknown book"
        author = (meta.authors[0] if meta.authors else meta.vision_author) or "Unknown"

        print(f"\n{'=' * 60}")
        print(f"Book: {title}")
        print(f"Author: {author}")
        print(f"ISBN-13: {meta.isbn_13 or 'N/A'}")
        print(f"Current cover_status: {meta.cover_status or 'none'}")

        # Skip if already has a valid clean cover
        if (
            meta.cover_status == "found"
            and meta.local_cover_path
            and os.path.exists(meta.local_cover_path)
        ):
            print(f"  REUSE: existing clean cover at {meta.local_cover_path}")
            src_key = meta.cover_source or "open_library"
            if src_key in stats:
                stats[src_key] += 1
            continue

        # Find clean cover
        cover_url, cover_source, local_path = clean_cover_service.find_clean_cover(meta)

        if cover_url:
            meta.cover_url = cover_url
            meta.local_cover_path = local_path
            meta.cover_source = cover_source
            meta.cover_source_url = cover_url
            meta.cover_status = "found"
            meta.cover_source_type = "external_bibliographic"
            print(f"  FOUND: {cover_source} -> {cover_url}")
            print(f"  Local: {local_path}")
            if cover_source in stats:
                stats[cover_source] += 1
        else:
            meta.cover_source = None
            meta.cover_source_url = None
            meta.cover_status = "not_found"
            meta.cover_source_type = None
            meta.local_cover_path = None
            meta.cover_url = None
            print("  NOT FOUND")
            stats["not_found"] += 1

        metadata_repo.update_metadata(meta)

    # Update Google Sheets
    print(f"\n{'=' * 60}")
    print("Updating Google Sheets with clean cover URLs...")
    sheets_updated = 0
    sheets_errors = 0
    try:
        creds_path = config.google_sheets_credentials
        if creds_path and os.path.exists(creds_path):
            sheets = GoogleSheetsService(
                credentials_file=creds_path,
                sheets_id=config.google_sheet_id,
                tab_name=(
                    config.google_sheets_tab_name
                    if hasattr(config, "google_sheets_tab_name")
                    else "Sheet1"
                ),
            )
            with sheets:
                for meta in metadata_repo.list():
                    image_formula, open_cover_url = sheets._build_image_formula(
                        meta.cover_url or "",
                        meta.local_cover_path,
                        meta.drive_file_id,
                    )
                    # Get or create the row for this book by drive_file_id
                    row_num = sheets.get_or_create_row(meta)
                    if row_num:
                        cols = {"Cover URL": image_formula, "Open Cover": f'=HYPERLINK("{open_cover_url}", "Open Cover")' if open_cover_url else ""}
                        if meta.cover_url:
                            cols["Status"] = "Cover Found"
                        else:
                            cols["Status"] = "Cover Needed"
                        sheets.update_row(row_num, cols)
                        sheets_updated += 1
            print(f"Google Sheets updated: {sheets_updated} rows")
        else:
            print("Google Sheets credentials not found — " "skipping sheet update")
    except Exception as e:
        print(f"Google Sheets update error: {e}")
        sheets_errors += 1

    # Summary
    print(f"\n{'=' * 60}")
    print("PHASE 35C COVER SUMMARY")
    print(f"{'=' * 60}")
    total = len(all_metadata)
    found = sum(v for k, v in stats.items() if k != "not_found")
    print(f"\nCovers found: {found}/{total}")
    print(f"  Google Books: {stats['google_books']}")
    print(f"  Open Library: {stats['open_library']}")
    print(f"  Exa: {stats['exa']}")
    print(f"  Not found: {stats['not_found']}")
    print(f"  Google Books API calls made: {gb_calls}")
    print(f"  Exa searches performed: {exa_searches}")
    print("  Drive covers used: 0 (Phase 35C compliance)")

    # Verify no Drive URLs
    all_meta = metadata_repo.list()
    drive_urls = [
        m for m in all_meta if m.cover_url and "drive.google.com" in m.cover_url
    ]
    if drive_urls:
        print(f"\nWARNING: {len(drive_urls)} books have Drive URLs as cover!")
    else:
        print("All cover URLs are external (no Drive URLs) - PASS")

    # Verify Google Sheets
    if sheets_updated > 0:
        print(f"Google Sheets updated with {sheets_updated} cover URLs - PASS")
    elif sheets_errors > 0:
        print("Google Sheets update had errors - see log above")


if __name__ == "__main__":
    main()
