"""Export status enum and ExportRecord model for Milestone 5."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ExportStatus(str, Enum):
    """Possible states for an export record."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class ExportRecord:
    """Tracks the export status of a single book's metadata to Google Sheets.

    One record per Drive file — duplicates are prevented at the database
    level via UNIQUE constraints on ``drive_file_id`` and ``job_id``.

    Fields:
        drive_file_id: Google Drive file ID (links to files table).
        job_id: Original processing job ID.
        sheet_row_id: Google Sheets row identifier (1-based row number).
        sheet_url: Full URL to the exported row in Google Sheets.
        exported_at: ISO timestamp of when the row was created/updated.
        status: Current export status (ExportStatus enum or string).
        retry_count: Number of retry attempts so far.
        last_error: Error message if the last attempt failed.
        exported_columns: JSON snapshot of exported data for diffing.
        created_at: Record creation timestamp.
        updated_at: Last update timestamp.
    """

    drive_file_id: str = ""
    job_id: str = ""
    sheet_row_id: Optional[str] = None
    sheet_url: Optional[str] = None
    exported_at: Optional[str] = None
    status: ExportStatus = ExportStatus.PENDING
    retry_count: int = 0
    last_error: Optional[str] = None
    exported_columns: Optional[dict] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    id: Optional[int] = field(default=None)

    def __post_init__(self) -> None:
        if not self.drive_file_id:
            raise ValueError("drive_file_id cannot be empty")
        if not self.job_id:
            raise ValueError("job_id cannot be empty")
        # Coerce string status to ExportStatus enum
        if isinstance(self.status, str):
            self.status = ExportStatus(self.status)
