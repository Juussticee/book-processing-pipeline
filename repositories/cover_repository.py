"""Cover download repository for SQLite-backed cover image tracking.

Manages the ``cover_downloads`` table in ``metadata.db``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from typing import List, Optional

from core.logger import setup_logger
from models.cover_download import CoverDownload, CoverDownloadStatus

logger = setup_logger(__name__)


class CoverRepository:
    """Manages cover download records in the SQLite database.

    Uses ``check_same_thread=False`` and a threading lock for
    thread-safe access from the export worker.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_directory()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._create_tables()

    def _ensure_directory(self) -> None:
        db_dir = os.path.dirname(self._db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def init_db(self) -> None:
        """Initialize database tables. Safe to call — tables auto-created."""
        pass

    def _create_tables(self) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cover_downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drive_file_id TEXT NOT NULL,
                    job_id TEXT,
                    cover_url TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    checksum TEXT,
                    width INTEGER,
                    height INTEGER,
                    file_size INTEGER,
                    resolution TEXT,
                    downloaded_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    error_message TEXT,
                    UNIQUE(drive_file_id),
                    UNIQUE(job_id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cover_downloads_drive_file
                ON cover_downloads(drive_file_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cover_downloads_job_id
                ON cover_downloads(job_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cover_downloads_checksum
                ON cover_downloads(checksum)
            """)
            self._connection.commit()

    def close(self) -> None:
        if self._connection:
            self._connection.close()

    def __enter__(self) -> "CoverRepository":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def insert_cover(
        self,
        drive_file_id: str,
        job_id: Optional[str],
        cover_url: str,
        local_path: str,
        checksum: str,
        width: int,
        height: int,
        file_size: int,
        status: str = CoverDownloadStatus.COMPLETED.value,
        error_message: Optional[str] = None,
    ) -> None:
        """Insert a cover download record.

        Uses INSERT OR REPLACE to handle the case where a record already
        exists for the same drive_file_id (e.g. retry after failure).
        """
        now = datetime.utcnow().isoformat()
        resolution = f"{width}x{height}" if width and height else None

        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO cover_downloads (
                    drive_file_id, job_id, cover_url, local_path,
                    checksum, width, height, file_size, resolution,
                    downloaded_at, status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    drive_file_id,
                    job_id,
                    cover_url,
                    local_path,
                    checksum,
                    width,
                    height,
                    file_size,
                    resolution,
                    now,
                    status,
                    error_message,
                ),
            )
            self._connection.commit()

    def get_cover_by_drive_file_id(self, drive_file_id: str) -> Optional[CoverDownload]:
        """Return the cover download for a given drive_file_id, or None."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                SELECT id, drive_file_id, job_id, cover_url, local_path,
                       checksum, width, height, file_size, resolution,
                       downloaded_at, status, error_message
                FROM cover_downloads
                WHERE drive_file_id = ?
                """,
                (drive_file_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return None
        return CoverDownload(
            id=row["id"],
            drive_file_id=row["drive_file_id"],
            job_id=row["job_id"],
            cover_url=row["cover_url"],
            local_path=row["local_path"],
            checksum=row["checksum"],
            width=row["width"],
            height=row["height"],
            file_size=row["file_size"],
            resolution=row["resolution"],
            downloaded_at=_parse_ts(row["downloaded_at"]),
            status=(
                CoverDownloadStatus(row["status"])
                if row["status"]
                else CoverDownloadStatus.COMPLETED
            ),
            error_message=row["error_message"],
        )

    def update_cover_status(
        self, drive_file_id: str, status: str, error_message: Optional[str] = None
    ) -> bool:
        """Update the status of a cover download record."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                UPDATE cover_downloads
                SET status = ?, error_message = ?
                WHERE drive_file_id = ?
                """,
                (status, error_message, drive_file_id),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def get_covers_for_export(self) -> List[CoverDownload]:
        """Return all completed covers that haven't been exported yet."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                SELECT cd.id, cd.drive_file_id, cd.job_id, cd.cover_url,
                       cd.local_path, cd.checksum, cd.width, cd.height,
                       cd.file_size, cd.resolution, cd.downloaded_at,
                       cd.status, cd.error_message
                FROM cover_downloads cd
                INNER JOIN export_records er ON cd.drive_file_id = er.drive_file_id
                WHERE cd.status = ?
                  AND er.status != 'completed'
                """,
                (CoverDownloadStatus.COMPLETED.value,),
            )
            rows = cursor.fetchall()

        return [
            CoverDownload(
                id=row["id"],
                drive_file_id=row["drive_file_id"],
                job_id=row["job_id"],
                cover_url=row["cover_url"],
                local_path=row["local_path"],
                checksum=row["checksum"],
                width=row["width"],
                height=row["height"],
                file_size=row["file_size"],
                resolution=row["resolution"],
                downloaded_at=_parse_ts(row["downloaded_at"]),
                status=(
                    CoverDownloadStatus(row["status"])
                    if row["status"]
                    else CoverDownloadStatus.COMPLETED
                ),
                error_message=row["error_message"],
            )
            for row in rows
        ]

    def get_by_checksum(self, checksum: str) -> Optional[CoverDownload]:
        """Return a cover download by checksum (for deduplication)."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                SELECT id, drive_file_id, job_id, cover_url, local_path,
                       checksum, width, height, file_size, resolution,
                       downloaded_at, status, error_message
                FROM cover_downloads
                WHERE checksum = ?
                """,
                (checksum,),
            )
            row = cursor.fetchone()

        if row is None:
            return None
        return CoverDownload(
            id=row["id"],
            drive_file_id=row["drive_file_id"],
            job_id=row["job_id"],
            cover_url=row["cover_url"],
            local_path=row["local_path"],
            checksum=row["checksum"],
            width=row["width"],
            height=row["height"],
            file_size=row["file_size"],
            resolution=row["resolution"],
            downloaded_at=_parse_ts(row["downloaded_at"]),
            status=(
                CoverDownloadStatus(row["status"])
                if row["status"]
                else CoverDownloadStatus.COMPLETED
            ),
            error_message=row["error_message"],
        )


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
