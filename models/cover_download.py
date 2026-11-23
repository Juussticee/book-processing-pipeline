"""Cover download model for Milestone 5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class CoverDownloadStatus(str, Enum):
    """Possible states for a cover download."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CoverDownload:
    """Tracks a single cover image download.

    Fields:
        drive_file_id: Google Drive file ID (links to files table).
        job_id: Original processing job ID.
        cover_url: URL from which the cover was downloaded.
        local_path: Local filesystem path where the cover is saved.
        checksum: SHA256 hash of the downloaded image bytes.
        width: Image width in pixels.
        height: Image height in pixels.
        file_size: File size in bytes.
        resolution: Human-readable dimensions (e.g. "600x800").
        downloaded_at: ISO timestamp of download.
        status: Download status (CoverDownloadStatus enum or string).
        error_message: Error details if the download failed.
        id: Primary key (set by database).
    """

    drive_file_id: str = ""
    job_id: Optional[str] = None
    cover_url: str = ""
    local_path: str = ""
    checksum: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    file_size: Optional[int] = None
    resolution: Optional[str] = None
    downloaded_at: Optional[datetime] = None
    status: CoverDownloadStatus = CoverDownloadStatus.COMPLETED
    error_message: Optional[str] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = CoverDownloadStatus(self.status)
        if self.downloaded_at is None:
            self.downloaded_at = datetime.utcnow()
        if self.resolution is None and self.width and self.height:
            self.resolution = f"{self.width}x{self.height}"
