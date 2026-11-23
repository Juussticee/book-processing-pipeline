"""Persistence for image fingerprints and duplicate decisions.

Lives in the same SQLite file as the jobs table so a single backup covers
job state and dedupe state. Both tables are created with
``CREATE TABLE IF NOT EXISTS`` and use ``INSERT OR IGNORE``/upserts, so
opening the database repeatedly is safe and the change is backward
compatible with existing databases (no destructive migration).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple


class DuplicateRepository:
    """Stores content fingerprints and records duplicate relationships."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS image_fingerprints (
                    content_hash    TEXT PRIMARY KEY,
                    perceptual_hash TEXT NOT NULL,
                    color_signature TEXT,
                    aspect_ratio    REAL,
                    drive_file_id   TEXT NOT NULL,
                    job_id          TEXT,
                    created_at      TEXT NOT NULL
                )
                """)
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS duplicate_matches (
                    drive_file_id          TEXT PRIMARY KEY,
                    original_drive_file_id TEXT,
                    original_job_id        TEXT,
                    match_kind             TEXT NOT NULL,
                    content_hash           TEXT,
                    perceptual_hash        TEXT,
                    hash_distance          INTEGER,
                    detail                 TEXT,
                    created_at             TEXT NOT NULL
                )
                """)
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fingerprints_drive_file "
                "ON image_fingerprints (drive_file_id)"
            )
            self._connection.commit()

    # -- fingerprints ---------------------------------------------------

    def register_fingerprint(
        self,
        content_hash: str,
        perceptual_hash: str,
        drive_file_id: str,
        job_id: Optional[str],
        color_signature: str = "",
        aspect_ratio: float = 0.0,
    ) -> None:
        """Store a fingerprint; repeats of the same bytes are ignored."""
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO image_fingerprints
                    (content_hash, perceptual_hash, color_signature,
                     aspect_ratio, drive_file_id, job_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_hash,
                    perceptual_hash,
                    color_signature,
                    aspect_ratio,
                    drive_file_id,
                    job_id,
                    _utcnow(),
                ),
            )
            self._connection.commit()

    def find_by_content_hash(self, content_hash: str) -> Optional[Any]:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM image_fingerprints WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()

    def list_fingerprints(self) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM image_fingerprints ORDER BY created_at"
                ).fetchall()
            )

    def list_fingerprints_excluding(self, drive_file_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM image_fingerprints WHERE drive_file_id != ?",
                    (drive_file_id,),
                ).fetchall()
            )

    def has_fingerprint(self, drive_file_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM image_fingerprints WHERE drive_file_id = ? LIMIT 1",
                (drive_file_id,),
            ).fetchone()
        return row is not None

    # -- duplicate decisions -------------------------------------------

    def record_duplicate(
        self,
        drive_file_id: str,
        match_kind: str,
        original_drive_file_id: Optional[str] = None,
        original_job_id: Optional[str] = None,
        content_hash: Optional[str] = None,
        perceptual_hash: Optional[str] = None,
        hash_distance: Optional[int] = None,
        detail: str = "",
    ) -> bool:
        """Record why a file was treated as a duplicate.

        A file can first be flagged as a *possible* near-duplicate and later
        be confirmed as the same book; the stronger, later verdict replaces
        the earlier one instead of being ignored.
        """
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO duplicate_matches
                    (drive_file_id, original_drive_file_id, original_job_id,
                     match_kind, content_hash, perceptual_hash,
                     hash_distance, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(drive_file_id) DO UPDATE SET
                    original_drive_file_id = excluded.original_drive_file_id,
                    original_job_id = excluded.original_job_id,
                    match_kind = excluded.match_kind,
                    content_hash = COALESCE(
                        excluded.content_hash, duplicate_matches.content_hash
                    ),
                    perceptual_hash = COALESCE(
                        excluded.perceptual_hash, duplicate_matches.perceptual_hash
                    ),
                    hash_distance = COALESCE(
                        excluded.hash_distance, duplicate_matches.hash_distance
                    ),
                    detail = excluded.detail
                """,
                (
                    drive_file_id,
                    original_drive_file_id,
                    original_job_id,
                    match_kind,
                    content_hash,
                    perceptual_hash,
                    hash_distance,
                    detail,
                    _utcnow(),
                ),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def get_duplicate(self, drive_file_id: str) -> Optional[Any]:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM duplicate_matches WHERE drive_file_id = ?",
                (drive_file_id,),
            ).fetchone()

    def list_duplicates(self) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM duplicate_matches ORDER BY created_at"
                ).fetchall()
            )

    def count_by_kind_since(self, since_iso: str) -> Sequence[Tuple[str, int]]:
        """Count duplicate decisions per kind created at or after a time."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT match_kind, COUNT(*) AS total
                FROM duplicate_matches
                WHERE created_at >= ?
                GROUP BY match_kind
                """,
                (since_iso,),
            ).fetchall()
        return [(row["match_kind"], row["total"]) for row in rows]

    def count_total(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM duplicate_matches"
            ).fetchone()
        return int(row["total"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "DuplicateRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
