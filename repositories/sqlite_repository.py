"""SQLite repository for tracking downloaded files and processing jobs."""

import os
import sqlite3
from typing import List

from models.drive_file import DriveFile


class SQLiteRepository:
    """Manages SQLite database for file tracking and job processing."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_directory()
        self._connection = sqlite3.connect(db_path)
        self._connection.row_factory = sqlite3.Row
        self._create_tables()

    def _ensure_directory(self) -> None:
        db_dir = os.path.dirname(self._db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def _create_tables(self) -> None:
        cursor = self._connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT UNIQUE NOT NULL,
                file_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                md5_checksum TEXT,
                modified_time TEXT NOT NULL,
                size INTEGER,
                local_filename TEXT NOT NULL,
                download_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'downloaded'
            )
        """)
        self._connection.commit()

    def close(self) -> None:
        if self._connection:
            self._connection.close()

    def __enter__(self) -> "SQLiteRepository":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def insert_file(
        self,
        drive_file: DriveFile,
        local_filename: str,
        download_time: str,
    ) -> bool:
        """Insert a new file record. Returns True if inserted, False if duplicate."""
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO files (
                    file_id, file_name, mime_type, md5_checksum,
                    modified_time, size, local_filename, download_time, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'downloaded')
                """,
                (
                    drive_file.file_id,
                    drive_file.name,
                    drive_file.mime_type,
                    drive_file.md5_checksum,
                    drive_file.modified_time,
                    drive_file.size,
                    local_filename,
                    download_time,
                ),
            )
            self._connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def file_exists(self, file_id: str) -> bool:
        """Check if a file with the given Drive file_id already exists."""
        cursor = self._connection.cursor()
        cursor.execute("SELECT 1 FROM files WHERE file_id = ?", (file_id,))
        return cursor.fetchone() is not None

    def get_all_file_ids(self) -> List[str]:
        """Return all tracked Drive file IDs."""
        cursor = self._connection.cursor()
        cursor.execute("SELECT file_id FROM files")
        return [row["file_id"] for row in cursor.fetchall()]

    def count_files(self) -> int:
        """Return the total count of tracked downloaded files."""
        cursor = self._connection.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM files")
        return int(cursor.fetchone()["count"])

    def get_all_files(self) -> List[DriveFile]:
        """Retrieve all tracked files."""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT file_id, file_name, mime_type, md5_checksum, "
            "modified_time, size FROM files"
        )
        return [
            DriveFile(
                file_id=row["file_id"],
                name=row["file_name"],
                mime_type=row["mime_type"],
                modified_time=row["modified_time"],
                size=row["size"],
                md5_checksum=row["md5_checksum"],
            )
            for row in cursor.fetchall()
        ]

    def update_file_status(self, file_id: str, status: str) -> bool:
        """Update the status of a file record."""
        cursor = self._connection.cursor()
        cursor.execute(
            "UPDATE files SET status = ? WHERE file_id = ?",
            (status, file_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0
