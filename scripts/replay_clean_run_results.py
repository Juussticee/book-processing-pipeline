"""Replay clean_run_results.json into metadata.db for a full 14-book rebuild.

This script does NOT re-run OCR/vision. It replays the already-computed
vision/title/author results so every discovered book is persisted in DB.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

from models.book_metadata import BookMetadata
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.export_repository import ExportRepository
from repositories.job_repository import JobRepository

INPUT = "data/clean_run_results.json"
DB_PATH = "data/metadata.db"
JOBS_DB_PATH = "data/jobs.db"


def main():
    with open(INPUT, "r", encoding="utf-8") as f:
        results = json.load(f)

    print(f"Loaded {len(results)} records from {INPUT}")

    meta_repo = BookMetadataRepository(DB_PATH)
    export_repo = ExportRepository(JOBS_DB_PATH, DB_PATH)
    job_repo = JobRepository(JOBS_DB_PATH)

    persisted = 0
    failed = 0

    for file_id, rec in sorted(results.items()):
        try:
            # Derive final identity: prefer match_candidate over OCR garbage
            title = (
                rec.get("match_candidate_title")
                or rec.get("extracted_title")
                or "(unreadable)"
            )
            authors = rec.get("match_candidate_authors") or []
            if isinstance(authors, str):
                authors = [authors]

            # Map status values
            status = rec.get("status", "manual_review") or "manual_review"
            if status == "pending":
                status = "identified"
            elif status == "manual_review":
                status = "needs_review"

            # Build metadata from clean_run results
            metadata = BookMetadata(
                drive_file_id=file_id,
                job_id=rec.get("job_id"),
                title=title,
                authors=authors,
                extracted_title=rec.get("extracted_title"),
                extracted_author=rec.get("extracted_author"),
                extracted_isbn=rec.get("extracted_isbn"),
                extracted_publisher=rec.get("extracted_publisher"),
                raw_ocr=rec.get("raw_ocr", ""),
                ocr_text=rec.get("ocr_text", ""),
                confidence=float(rec.get("confidence") or 0.0),
                source=rec.get("source") or "clean_run_replay",
                status=status,
                rejection_reason=rec.get("rejection_reason"),
                match_score=float(rec.get("match_score") or 0.0),
                candidate_sources=rec.get("candidate_sources") or [],
                vision_title=title,
                vision_author=authors[0] if authors else None,
                vision_publisher=None,
                vision_confidence=float(rec.get("confidence") or 0.0),
                vision_used=1,
                cover_status=None,
                cover_source=None,
                cover_url=None,
                local_cover_path=None,
            )

            meta_repo.upsert_metadata(metadata)
            try:
                export_repo.create_export_record(file_id, rec.get("job_id") or "")
            except Exception:
                pass
            try:
                job_repo.update_status(rec.get("job_id"), status)
            except Exception:
                pass
            persisted += 1
            print(f"  Persisted: {title[:60]} [{status}]")
        except Exception as e:
            failed += 1
            print(f"  FAILED {file_id}: {e}")

    meta_repo.close()
    export_repo.close()
    job_repo.close()

    print(f"\nPersisted: {persisted}, Failed: {failed}")
    print("Replay complete.")


if __name__ == "__main__":
    main()
