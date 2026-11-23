"""Export repository for SQLite-backed export queue and records management.

Manages two tables:
- ``export_queue``: tracks export jobs with retry logic (in jobs.db)
- ``export_records``: tracks final export status per Drive file (in metadata.db)
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import List, Optional

from core.logger import setup_logger
from models.export_record import ExportRecord, ExportStatus

logger = setup_logger(__name__)

_CREATE_QUEUE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS export_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        drive_file_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        last_error TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        attempted_at TEXT,
        next_retry_at TEXT,
        UNIQUE(drive_file_id),
        UNIQUE(job_id)
    )
"""

_CREATE_QUEUE_INDEXES_SQL = [
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_export_queue_drive_file
    ON export_queue(drive_file_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_export_queue_status
    ON export_queue(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_export_queue_retry
    ON export_queue(status, retry_count)
    """,
]

_CREATE_RECORDS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS export_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        drive_file_id TEXT NOT NULL,
        job_id TEXT,
        sheet_row_id TEXT,
        sheet_url TEXT,
        exported_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        retry_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        exported_columns TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(drive_file_id),
        UNIQUE(job_id)
    )
"""

_CREATE_RECORDS_INDEXES_SQL = [
    """
    CREATE INDEX IF NOT EXISTS idx_export_records_drive_file
    ON export_records(drive_file_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_export_records_status
    ON export_records(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_export_records_sheet_row
    ON export_records(sheet_row_id)
    """,
]


class ExportRepository:
    """Manages export queue and records in the SQLite database.

    Uses ``check_same_thread=False`` and a threading lock so the
    repository can be safely shared between the main thread (creating
    export records) and the export worker thread (polling/processing).

    The ``export_queue`` table lives in ``jobs_db_path`` (alongside the
    ``jobs`` table) and the ``export_records`` table lives in
    ``records_db_path`` (alongside ``book_metadata`` and
    ``cover_downloads``).  When ``records_db_path`` is not provided it
    defaults to ``jobs_db_path`` so both tables live in the same file.
    """

    def __init__(
        self, jobs_db_path: str, records_db_path: Optional[str] = None
    ) -> None:
        self._jobs_db_path = jobs_db_path
        self._records_db_path = records_db_path or jobs_db_path
        self._ensure_directory(self._jobs_db_path)
        self._ensure_directory(self._records_db_path)
        self._queue_conn = sqlite3.connect(self._jobs_db_path, check_same_thread=False)
        self._queue_conn.row_factory = sqlite3.Row
        if self._records_db_path != self._jobs_db_path:
            self._records_conn: sqlite3.Connection = sqlite3.connect(
                self._records_db_path, check_same_thread=False
            )
            self._records_conn.row_factory = sqlite3.Row
        else:
            self._records_conn = self._queue_conn
        self._lock = threading.Lock()
        self._create_tables()

    def _ensure_directory(self, db_path: str) -> None:
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def init_db(self) -> None:
        """Initialize database tables. Safe to call — tables auto-created."""
        # Tables are created in __init__ via _create_tables; no-op here
        pass

    def _create_tables(self) -> None:
        """Create export_queue and export_records tables if they don't exist."""
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(_CREATE_QUEUE_TABLE_SQL)
            for idx_sql in _CREATE_QUEUE_INDEXES_SQL:
                cursor.execute(idx_sql)
            self._queue_conn.commit()

            # Migration: add next_retry_at column if it doesn't exist
            # (for databases created before Milestone 8.3)
            cursor.execute("""
                SELECT name FROM pragma_table_info('export_queue')
                WHERE name = 'next_retry_at'
                """)
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE export_queue ADD COLUMN next_retry_at TEXT")
                self._queue_conn.commit()
                logger.info("Added next_retry_at column to export_queue")

            rc = self._records_conn.cursor()
            rc.execute(_CREATE_RECORDS_TABLE_SQL)
            for idx_sql in _CREATE_RECORDS_INDEXES_SQL:
                rc.execute(idx_sql)
            self._records_conn.commit()

    def close(self) -> None:
        """Close the database connection(s)."""
        if self._queue_conn is not None:
            self._queue_conn.close()
        if (
            self._records_conn is not None
            and self._records_conn is not self._queue_conn
        ):
            self._records_conn.close()

    def __enter__(self) -> "ExportRepository":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def create_export_record(
        self, drive_file_id: str, job_id: str, max_retries: int = 3
    ) -> Optional[ExportRecord]:
        """Create a new export queue entry.

        Returns the created ExportRecord, or ``None`` if a record for
        this drive_file_id or job_id already exists (duplicate prevention).
        """
        if not drive_file_id:
            raise ValueError("drive_file_id cannot be empty")
        if not job_id:
            raise ValueError("job_id cannot be empty")

        now = datetime.utcnow().isoformat()
        with self._lock:
            try:
                cursor = self._queue_conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO export_queue (
                        drive_file_id, job_id, status, retry_count,
                        max_retries, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        drive_file_id,
                        job_id,
                        ExportStatus.PENDING.value,
                        0,
                        max_retries,
                        now,
                    ),
                )
                self._queue_conn.commit()
            except sqlite3.IntegrityError:
                logger.info(
                    "Export record already exists for drive_file_id=%s "
                    "(skipping creation)",
                    drive_file_id,
                )
                return None

        logger.info(
            "Export queue entry created for drive_file_id=%s",
            drive_file_id,
        )
        return ExportRecord(
            drive_file_id=drive_file_id,
            job_id=job_id,
            status=ExportStatus.PENDING,
            created_at=_parse_ts(now),
        )

    def get_pending_exports(self, limit: int = 10) -> List[dict]:
        """Return pending and retrying export rows from the queue.

        For RETRYING records, only includes those whose next_retry_at
        has elapsed (or is NULL for backward compatibility). This respects
        the exponential backoff schedule set by set_next_retry().

        Returns list of dicts with keys: drive_file_id, job_id, retry_count,
        max_retries, last_error.
        """
        now = datetime.utcnow().isoformat()
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                SELECT drive_file_id, job_id, retry_count, max_retries,
                       last_error
                FROM export_queue
                WHERE LOWER(status) = ?
                   OR (LOWER(status) = ?
                       AND (next_retry_at IS NULL OR next_retry_at <= ?))
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (
                    ExportStatus.PENDING.value.lower(),
                    ExportStatus.RETRYING.value.lower(),
                    now,
                    limit,
                ),
            )
            rows = cursor.fetchall()

        return [
            {
                "drive_file_id": row["drive_file_id"],
                "job_id": row["job_id"],
                "retry_count": row["retry_count"],
                "max_retries": row["max_retries"],
                "last_error": row["last_error"],
            }
            for row in rows
        ]

    def get_export_by_drive_file_id(self, drive_file_id: str) -> Optional[dict]:
        """Return the export queue row for a given drive_file_id."""
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                SELECT drive_file_id, job_id, status, retry_count,
                       max_retries, last_error
                FROM export_queue
                WHERE drive_file_id = ?
                """,
                (drive_file_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return None
        return {
            "drive_file_id": row["drive_file_id"],
            "job_id": row["job_id"],
            "status": row["status"],
            "retry_count": row["retry_count"],
            "max_retries": row["max_retries"],
            "last_error": row["last_error"],
        }

    def update_export_status(
        self,
        drive_file_id: str,
        status: str,
        error_message: Optional[str] = None,
        sheet_row_id: Optional[str] = None,
    ) -> bool:
        """Update the export queue and export_records tables atomically.

        Updates the export_queue status and, for COMPLETED/EXPORTED status,
        also updates the export_records table.

        Args:
            drive_file_id: The Drive file ID to update.
            status: New status (pending, processing, completed, failed, retrying).
            error_message: Error details if applicable.
            sheet_row_id: Google Sheets row ID (for completed exports).

        Returns:
            True if a row was updated, False otherwise.
        """
        now = datetime.utcnow().isoformat()
        with self._lock:
            cursor = self._queue_conn.cursor()
            increment_retry = status in (
                ExportStatus.FAILED.value,
                ExportStatus.RETRYING.value,
            )
            retry_clause = (
                "retry_count = retry_count + 1"
                if increment_retry
                else "retry_count = retry_count"
            )
            cursor.execute(
                f"""
                UPDATE export_queue
                SET status = ?, {retry_clause},
                    last_error = ?, attempted_at = ?
                WHERE drive_file_id = ?
                """,
                (status, error_message, now, drive_file_id),
            )
            if status == "processing":
                cursor.execute(
                    """
                    UPDATE export_queue
                    SET started_at = ?
                    WHERE drive_file_id = ?
                    """,
                    (now, drive_file_id),
                )
            elif status in ("completed", "failed"):
                cursor.execute(
                    """
                    UPDATE export_queue
                    SET completed_at = ?
                    WHERE drive_file_id = ?
                    """,
                    (now, drive_file_id),
                )
            queue_updated = cursor.rowcount > 0
            self._queue_conn.commit()

            # Read retry_count from the queue table (which lives in jobs.db)
            cursor.execute(
                """
                SELECT retry_count FROM export_queue
                WHERE drive_file_id = ?
                """,
                (drive_file_id,),
            )
            retry_row = cursor.fetchone()
            queue_retry_count = retry_row["retry_count"] if retry_row else 0

            # Sync to export_records table (in metadata.db)
            # First, read the current queue row values from the queue table
            cursor.execute(
                """
                SELECT drive_file_id, job_id, status, retry_count,
                       last_error
                FROM export_queue
                WHERE drive_file_id = ?
                """,
                (drive_file_id,),
            )
            row = cursor.fetchone()

            rc = self._records_conn.cursor()
            if row is not None:
                rc.execute(
                    """
                    INSERT OR IGNORE INTO export_records
                        (drive_file_id, job_id, sheet_row_id, status,
                         retry_count, last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["drive_file_id"],
                        row["job_id"],
                        sheet_row_id,
                        status,
                        queue_retry_count,
                        error_message,
                        now,
                        now,
                    ),
                )
            rc.execute(
                """
                UPDATE export_records
                SET status = ?, sheet_row_id = ?, last_error = ?,
                    retry_count = ?,
                    updated_at = ?
                WHERE drive_file_id = ?
                """,
                (
                    status,
                    sheet_row_id,
                    error_message,
                    queue_retry_count,
                    now,
                    drive_file_id,
                ),
            )
            self._queue_conn.commit()
            self._records_conn.commit()

        return queue_updated

    def increment_retry(self, drive_file_id: str) -> int:
        """Increment the retry count for an export record.

        Returns the new retry count, or -1 if the record was not found.
        """
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                UPDATE export_queue
                SET retry_count = retry_count + 1
                WHERE drive_file_id = ?
                """,
                (drive_file_id,),
            )
            self._queue_conn.commit()
            if cursor.rowcount == 0:
                return -1
            cursor.execute(
                "SELECT retry_count FROM export_queue WHERE drive_file_id = ?",
                (drive_file_id,),
            )
            row = cursor.fetchone()
        return row["retry_count"] if row else -1

    def reset_retrying_exports(self) -> int:
        """Reset RETRYING export entries whose backoff has elapsed back to PENDING.

        Only resets records where ``next_retry_at`` is in the past (or NULL,
        for backwards compatibility with records created before this column
        existed). This implements exponential backoff: failed exports are not
        retried immediately but are scheduled for a later time based on the
        retry count.
        """
        now = datetime.utcnow().isoformat()
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                UPDATE export_queue
                SET status = ?
                WHERE status = ?
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                """,
                (
                    ExportStatus.PENDING.value,
                    ExportStatus.RETRYING.value,
                    now,
                ),
            )
            self._queue_conn.commit()
            return cursor.rowcount

    def recover_stuck_exports(self, timeout_seconds: int = 7200) -> int:
        """Reset exports left in PROCESSING by a crash back to PENDING.

        Called on startup. Only records whose last attempt (``attempted_at``,
        written by every status change) is older than ``timeout_seconds`` are
        recovered, so an export that is genuinely still running is left alone.

        Returns the number of records recovered.
        """
        threshold = (datetime.utcnow() - timedelta(seconds=timeout_seconds)).isoformat()
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                UPDATE export_queue
                SET status = ?
                WHERE status = ?
                  AND (attempted_at IS NULL OR attempted_at <= ?)
                """,
                (
                    ExportStatus.PENDING.value,
                    ExportStatus.PROCESSING.value,
                    threshold,
                ),
            )
            self._queue_conn.commit()
            return cursor.rowcount

    def requeue_export(self, drive_file_id: str, reset_retries: bool = True) -> bool:
        """Put a finished export back on the queue for another pass.

        Unlike :meth:`reset_retrying_exports`, this works on a record in any
        terminal state (``completed`` or ``failed``) and is meant to be called
        explicitly — for example when a cover search failed on the first run.
        Everything needed to resume is already persisted in ``book_metadata``,
        so the worker repeats only the cover lookup and the sheet write.

        Args:
            drive_file_id: Drive file ID of the export to requeue.
            reset_retries: Clear the retry counter and backoff so the record
                gets a fresh retry budget. Defaults to True.

        Returns:
            True if a queue row was requeued, False if no row matched.
        """
        if not drive_file_id:
            return False
        now = datetime.utcnow().isoformat()
        retry_clause = (
            "retry_count = 0" if reset_retries else "retry_count = retry_count"
        )
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                f"""
                UPDATE export_queue
                SET status = ?, {retry_clause}, next_retry_at = NULL,
                    last_error = NULL, started_at = NULL, completed_at = NULL,
                    attempted_at = ?
                WHERE drive_file_id = ?
                """,
                (ExportStatus.PENDING.value, now, drive_file_id),
            )
            updated = cursor.rowcount > 0
            self._queue_conn.commit()
            if updated:
                rc = self._records_conn.cursor()
                rc.execute(
                    f"""
                    UPDATE export_records
                    SET status = ?, {retry_clause}, last_error = NULL,
                        updated_at = ?
                    WHERE drive_file_id = ?
                    """,
                    (ExportStatus.PENDING.value, now, drive_file_id),
                )
                self._records_conn.commit()
        if updated:
            logger.info("Requeued export for drive_file_id=%s", drive_file_id)
        return updated

    def requeue_incomplete_exports(self, limit: int = 1000) -> List[str]:
        """Requeue exports whose book never resolved a cover or an identifier.

        A book counts as incomplete when its persisted ``cover_status`` is not
        ``found``, or when it has neither an ISBN-10 nor an ISBN-13. Those are
        the states a completed export cannot recover from on its own, because
        the worker only polls ``pending``/``retrying`` records (RC-8).

        Only persisted metadata is read — OCR, text extraction and enrichment
        are not repeated, and an export already in flight is never disturbed.

        Returns:
            Drive file IDs that are queued again.
        """
        requeued: List[str] = []
        for row in self._incomplete_metadata_rows(limit):
            drive_file_id = row["drive_file_id"]
            current = self.get_export_by_drive_file_id(drive_file_id)
            if current is None:
                job_id = row["job_id"]
                if not job_id:
                    continue
                if self.create_export_record(drive_file_id, job_id) is not None:
                    requeued.append(drive_file_id)
                continue
            status = str(current.get("status", "")).lower()
            if status in (
                ExportStatus.PENDING.value,
                ExportStatus.PROCESSING.value,
            ):
                # Already queued or being worked on — leave it alone.
                continue
            if self.requeue_export(drive_file_id):
                requeued.append(drive_file_id)
        if requeued:
            logger.info("Requeued %d incomplete export(s)", len(requeued))
        return requeued

    def _incomplete_metadata_rows(self, limit: int) -> List[sqlite3.Row]:
        """Return metadata rows without a validated cover or any ISBN."""
        with self._lock:
            cursor = self._records_conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'book_metadata'"
            )
            if cursor.fetchone() is None:
                # Jobs-only database (first run, tests): nothing to resume.
                return []
            cursor.execute(
                """
                SELECT drive_file_id, job_id, isbn_10, isbn_13, cover_status
                FROM book_metadata
                WHERE COALESCE(TRIM(drive_file_id), '') != ''
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
        return [row for row in rows if self._metadata_row_is_incomplete(row)]

    @staticmethod
    def _metadata_row_is_incomplete(row: sqlite3.Row) -> bool:
        """True when a metadata row still lacks a usable cover or an ISBN."""
        has_cover = (row["cover_status"] or "").strip().lower() == "found"
        has_isbn = bool((row["isbn_10"] or "").strip()) or bool(
            (row["isbn_13"] or "").strip()
        )
        return not has_cover or not has_isbn

    @staticmethod
    def calculate_retry_delay(retry_count: int, base_delay: float = 60.0) -> float:
        """Calculate exponential backoff delay for a given retry attempt.

        Uses the formula: delay = base_delay * (2 ^ retry_count)
        Capped at 1 hour (3600 seconds) to prevent unbounded growth.

        Args:
            retry_count: The current retry count (0 = first failure, 1 = first
                retry). The delay for the NEXT attempt is calculated.
            base_delay: Base delay in seconds. Defaults to 60 seconds.

        Returns:
            Delay in seconds before the next retry attempt.
        """
        delay = base_delay * (2**retry_count)
        # Cap at 1 hour
        return float(min(delay, 3600.0))

    def set_next_retry(
        self, drive_file_id: str, retry_count: int, base_delay: float = 60.0
    ) -> str:
        """Set the next retry time for an export record using exponential backoff.

        Schedules the next retry attempt based on the retry count:
        - Attempt 0: 60s (base_delay)
        - Attempt 1: 120s (2 * base_delay)
        - Attempt 2: 240s (4 * base_delay)
        - etc.

        Args:
            drive_file_id: The Drive file ID to schedule for retry.
            retry_count: The current retry count (used to calculate backoff).
            base_delay: Base delay in seconds. Defaults to 60.

        Returns:
            ISO-formatted timestamp string for the next retry time.
        """
        delay = self.calculate_retry_delay(retry_count, base_delay)
        next_retry = datetime.utcnow() + timedelta(seconds=delay)
        next_retry_str = next_retry.isoformat()
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                UPDATE export_queue
                SET next_retry_at = ?
                WHERE drive_file_id = ?
                """,
                (next_retry_str, drive_file_id),
            )
            self._queue_conn.commit()
        return next_retry_str

    def count_export_records(self) -> dict:
        """Return export-record counts by status (for the run summary)."""
        with self._lock:
            cursor = self._records_conn.cursor()
            cursor.execute(
                "SELECT status, COUNT(*) AS total FROM export_records GROUP BY status"
            )
            return {row["status"]: row["total"] for row in cursor.fetchall()}

    def get_export_record(self, drive_file_id: str) -> Optional[ExportRecord]:
        """Return an ExportRecord from the export_records table."""
        with self._lock:
            cursor = self._records_conn.cursor()
            cursor.execute(
                """
                SELECT id, drive_file_id, job_id, sheet_row_id, sheet_url,
                       exported_at, status, retry_count, last_error,
                       exported_columns, created_at, updated_at
                FROM export_records
                WHERE drive_file_id = ?
                """,
                (drive_file_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return None
        return ExportRecord(
            id=row["id"],
            drive_file_id=row["drive_file_id"],
            job_id=row["job_id"] or "",
            sheet_row_id=row["sheet_row_id"],
            sheet_url=row["sheet_url"],
            exported_at=row["exported_at"],
            status=(
                ExportStatus(row["status"]) if row["status"] else ExportStatus.PENDING
            ),
            retry_count=row["retry_count"] or 0,
            last_error=row["last_error"],
            exported_columns=(
                _parse_json(row["exported_columns"])
                if row["exported_columns"]
                else None
            ),
            created_at=_parse_ts(row["created_at"]),
            updated_at=_parse_ts(row["updated_at"]),
        )

    def update_export_record(
        self,
        drive_file_id: str,
        status: str,
        sheet_row_id: Optional[str] = None,
        sheet_url: Optional[str] = None,
        exported_columns: Optional[dict] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update fields in the export_records table."""
        now = datetime.utcnow().isoformat()
        export_status = ExportStatus(status)
        is_completed = export_status == ExportStatus.COMPLETED

        with self._lock:
            cursor = self._records_conn.cursor()
            cursor.execute(
                """
                UPDATE export_records
                SET status = ?, sheet_row_id = ?, sheet_url = ?,
                    exported_at = ?, exported_columns = ?, last_error = ?,
                    updated_at = ?
                WHERE drive_file_id = ?
                """,
                (
                    status,
                    sheet_row_id,
                    sheet_url,
                    now if is_completed else None,
                    _to_json(exported_columns) if exported_columns else None,
                    error_message,
                    now,
                    drive_file_id,
                ),
            )
            self._records_conn.commit()
            return cursor.rowcount > 0

    def get_dead_letter_exports(self) -> List[dict]:
        """Return all FAILED exports that have exhausted their retry budget.

        These are exports that have reached max_retries without success and
        cannot be retried further. They are effectively dead-lettered.

        Returns:
            List of dicts with keys: drive_file_id, job_id, retry_count,
            max_retries, last_error, attempted_at.
        """
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                SELECT drive_file_id, job_id, retry_count, max_retries,
                       last_error, attempted_at
                FROM export_queue
                WHERE status = ?
                ORDER BY attempted_at ASC
                """,
                (ExportStatus.FAILED.value,),
            )
            rows = cursor.fetchall()

        return [
            {
                "drive_file_id": row["drive_file_id"],
                "job_id": row["job_id"],
                "retry_count": row["retry_count"],
                "max_retries": row["max_retries"],
                "last_error": row["last_error"],
                "attempted_at": row["attempted_at"],
            }
            for row in rows
        ]

    def get_retrying_exports(self) -> List[dict]:
        """Return all RETRYING exports with their scheduled retry time.

        Returns:
            List of dicts with keys: drive_file_id, job_id, retry_count,
            max_retries, last_error, next_retry_at.
        """
        with self._lock:
            cursor = self._queue_conn.cursor()
            cursor.execute(
                """
                SELECT drive_file_id, job_id, retry_count, max_retries,
                       last_error, next_retry_at
                FROM export_queue
                WHERE status = ?
                ORDER BY next_retry_at ASC
                """,
                (ExportStatus.RETRYING.value,),
            )
            rows = cursor.fetchall()

        return [
            {
                "drive_file_id": row["drive_file_id"],
                "job_id": row["job_id"],
                "retry_count": row["retry_count"],
                "max_retries": row["max_retries"],
                "last_error": row["last_error"],
                "next_retry_at": row["next_retry_at"],
            }
            for row in rows
        ]


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _parse_json(s: Optional[str]) -> Optional[dict]:
    if s is None:
        return None
    import json

    try:
        result = json.loads(s)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _to_json(d: Optional[dict]) -> Optional[str]:
    if d is None:
        return None
    import json

    return json.dumps(d)
