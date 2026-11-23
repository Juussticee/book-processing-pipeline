"""Persistence for intake rejections (files that never become jobs).

Rejections are recorded so an operator can explain to a customer why a
file was not processed, without creating a noisy failed job. The table is
created idempotently and stores one row per Drive file ID, so repeated
sync cycles update nothing and cannot spam the log.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, List, Optional


class IntakeRepository:
    """Stores rejected Drive files and their rejection reason."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS intake_rejections (
                    drive_file_id TEXT PRIMARY KEY,
                    file_name     TEXT NOT NULL,
                    mime_type     TEXT,
                    size_bytes    INTEGER,
                    reason        TEXT NOT NULL,
                    detail        TEXT,
                    detected_at   TEXT NOT NULL
                )
                """)
            self._connection.commit()

    def record(
        self,
        drive_file_id: str,
        file_name: str,
        reason: str,
        mime_type: str = "",
        size_bytes: Optional[int] = None,
        detail: str = "",
    ) -> bool:
        """Insert a rejection. Returns False when it was already recorded."""
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO intake_rejections
                    (drive_file_id, file_name, mime_type, size_bytes,
                     reason, detail, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    drive_file_id,
                    file_name,
                    mime_type,
                    size_bytes,
                    reason,
                    detail,
                    _utcnow(),
                ),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def get(self, drive_file_id: str) -> Optional[Any]:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM intake_rejections WHERE drive_file_id = ?",
                (drive_file_id,),
            ).fetchone()

    def list_all(self) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM intake_rejections ORDER BY detected_at"
                ).fetchall()
            )

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM intake_rejections"
            ).fetchone()
        return int(row["total"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "IntakeRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
