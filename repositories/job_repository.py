"""Job repository for SQLite-backed processing job management."""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from models.processing_job import ErrorCategory, JobStatus, ProcessingJob


class JobRepository:
    """Manages processing jobs in the SQLite database.

    All database operations are transactional (using ``with self._connection``).
    Jobs are uniquely identified by ``job_id`` (a UUID). The ``file_id`` (Google
    Drive file ID) has a partial unique index so that only one *active* job
    (non-completed, non-cancelled) can exist per Drive file at any time.

    Uses ``check_same_thread=False`` and a threading lock so the repository
    can be safely shared between the main thread (job creation) and the
    worker thread (job polling/processing).
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

    def _create_tables(self) -> None:
        cursor = self._connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                file_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                local_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_message TEXT,
                worker_name TEXT,
                metadata_json TEXT,
                error_category TEXT,
                next_retry_at TEXT,
                last_error_at TEXT,
                heartbeat_at TEXT,
                UNIQUE(job_id)
            )
            """)
        # Migration: add metadata_json column to existing tables
        try:
            cursor.execute("ALTER TABLE jobs ADD COLUMN metadata_json TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        # Migration: add Phase 42 retry/recovery columns
        for column in (
            "error_category TEXT",
            "next_retry_at TEXT",
            "last_error_at TEXT",
            "heartbeat_at TEXT",
        ):
            try:
                cursor.execute(f"ALTER TABLE jobs ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        # One active job per Drive file ID.
        # "active" = not completed, not failed, not cancelled, not manual_review.
        # We use a partial unique index so only one active job can exist.
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_per_file
            ON jobs(file_id)
            WHERE status NOT IN ('completed', 'failed', 'cancelled', 'manual_review')
            """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_file_id ON jobs(file_id)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_next_retry_at ON jobs(next_retry_at)"
        )
        self._connection.commit()

    def close(self) -> None:
        if self._connection:
            self._connection.close()

    def __enter__(self) -> "JobRepository":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def create_job(
        self,
        file_id: str,
        file_name: str,
        local_path: str,
        priority: int = 0,
        worker_name: Optional[str] = None,
    ) -> Optional[ProcessingJob]:
        """Create a new processing job.

        Returns the created job, or ``None`` if a job for this Drive file
        already exists in an active (non-completed, non-cancelled) state.
        """
        if not file_id:
            raise ValueError("file_id cannot be empty")
        if not file_name:
            raise ValueError("file_name cannot be empty")
        if not local_path:
            raise ValueError("local_path cannot be empty")

        job_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        with self._lock:
            try:
                cursor = self._connection.cursor()
                cursor.execute(
                    """
                    INSERT INTO jobs (
                        job_id, file_id, file_name, local_path,
                        status, retry_count, priority,
                        created_at, started_at, completed_at,
                        error_message, worker_name, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        file_id,
                        file_name,
                        local_path,
                        JobStatus.PENDING.value,
                        0,
                        priority,
                        now,
                        None,
                        None,
                        None,
                        worker_name,
                        None,
                    ),
                )
                self._connection.commit()
            except sqlite3.IntegrityError:
                return None

        return ProcessingJob(
            job_id=job_id,
            file_id=file_id,
            file_name=file_name,
            local_path=local_path,
            status=JobStatus.PENDING,
            retry_count=0,
            priority=priority,
            created_at=datetime.fromisoformat(now),
            worker_name=worker_name,
        )

    def get_job_by_id(self, job_id: str) -> Optional[ProcessingJob]:
        """Retrieve a job by its UUID."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
        return self._row_to_job(row) if row else None

    def get_job_by_drive_file_id(self, file_id: str) -> Optional[ProcessingJob]:
        """Retrieve the most recent job for a given Drive file ID.

        Returns the most recent job for the file, regardless of status.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                "SELECT * FROM jobs WHERE file_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (file_id,),
            )
            row = cursor.fetchone()
        return self._row_to_job(row) if row else None

    def get_pending_jobs(self) -> List[ProcessingJob]:
        """Return all PENDING jobs ordered by priority DESC,
        created_at ASC (FIFO within priority).
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                ORDER BY priority DESC, created_at ASC
                """,
                (JobStatus.PENDING.value,),
            )
            rows = cursor.fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_processing_jobs(self) -> List[ProcessingJob]:
        """Return all jobs currently in PROCESSING status."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                "SELECT * FROM jobs WHERE status = ?",
                (JobStatus.PROCESSING.value,),
            )
            rows = cursor.fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_failed_jobs(self) -> List[ProcessingJob]:
        """Return all jobs with status FAILED."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                "SELECT * FROM jobs WHERE status = ?",
                (JobStatus.FAILED.value,),
            )
            rows = cursor.fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_all_jobs(self) -> List[ProcessingJob]:
        """Return all jobs ordered by creation time."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("SELECT * FROM jobs ORDER BY created_at ASC")
            rows = cursor.fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_count_by_status(self) -> dict:
        """Return a count of jobs grouped by status."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("SELECT status, COUNT(*) as count FROM jobs GROUP BY status")
            rows = cursor.fetchall()
        return {row["status"]: row["count"] for row in rows}

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        error_message: Optional[str] = None,
        worker_name: Optional[str] = None,
        extra_updates: Optional[dict] = None,
        started_at: Optional[str] = None,
    ) -> bool:
        """Update the status of a job.

        Sets ``started_at`` when transitioning to PROCESSING.
        Sets ``completed_at`` when transitioning to COMPLETED, FAILED, or CANCELLED.
        Sets ``worker_name`` if provided.
        Accepts optional ``extra_updates`` dict for additional columns.
        Returns True if the job was found and updated.
        """
        now = datetime.utcnow().isoformat()

        with self._lock:
            # Determine effective started_at: extra_updates > explicit param > now
            started_at_from_extra = (
                extra_updates.get("started_at") if extra_updates else None
            )
            if status == JobStatus.PROCESSING:
                effective_started_at = started_at_from_extra or started_at or now
                set_clause = "status = ?, started_at = ?, error_message = ?"
                params = [status.value, effective_started_at, error_message]
            elif status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.MANUAL_REVIEW,
            ):
                set_clause = "status = ?, completed_at = ?, error_message = ?"
                params = [status.value, now, error_message]
            else:
                set_clause = "status = ?, error_message = ?"
                params = [status.value, error_message]

            if worker_name:
                set_clause += ", worker_name = ?"
                params.append(worker_name)

            if extra_updates:
                for key, value in extra_updates.items():
                    # started_at is handled specially in the PROCESSING branch above
                    if key == "started_at":
                        continue
                    # Only allow known column names through; ignore anything
                    # we don't explicitly support here (e.g. dynamic columns).
                    if key in {
                        "worker_name",
                        "error_message",
                        "completed_at",
                        "error_category",
                        "next_retry_at",
                        "last_error_at",
                    }:
                        set_clause += f", {key} = ?"
                        params.append(value)

            params.append(job_id)

            cursor = self._connection.cursor()
            cursor.execute(
                f"UPDATE jobs SET {set_clause} WHERE job_id = ?",
                params,
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def increment_retry(self, job_id: str) -> int:
        """Increment the retry count for a job and return the new value."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                "UPDATE jobs SET retry_count = retry_count + 1 WHERE job_id = ?",
                (job_id,),
            )
            self._connection.commit()
            if cursor.rowcount == 0:
                return -1
            cursor.execute("SELECT retry_count FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
        return row["retry_count"] if row else -1

    def complete_job(self, job_id: str, worker_name: Optional[str] = None) -> bool:
        """Mark a job as COMPLETED."""
        return self.update_status(job_id, JobStatus.COMPLETED, worker_name=worker_name)

    def fail_job(
        self,
        job_id: str,
        error_message: Optional[str] = None,
        worker_name: Optional[str] = None,
        error_category: Optional[str] = None,
    ) -> bool:
        """Mark a job as FAILED with an optional error message and category."""
        extra_updates = {}
        if error_category:
            extra_updates["error_category"] = error_category
        return self.update_status(
            job_id,
            JobStatus.FAILED,
            error_message,
            worker_name,
            extra_updates=extra_updates if extra_updates else None,
        )

    def set_manual_review(
        self,
        job_id: str,
        error_message: Optional[str] = None,
        metadata: Optional[dict] = None,
        worker_name: Optional[str] = None,
    ) -> bool:
        """Mark a job as MANUAL_REVIEW (low confidence, needs human review).

        Stores full review metadata (candidates, scores, confidence, OCR,
        extracted fields, rejection reason) in the metadata_json column
        so it can be inspected without parsing error_message text.
        """
        import json

        def _json_safe(obj):
            """Fallback serializer for non-JSON-serializable objects."""
            return str(obj)

        metadata_json = json.dumps(metadata, default=_json_safe) if metadata else None
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                UPDATE jobs SET status = ?, metadata_json = ?,
                    error_message = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.MANUAL_REVIEW.value,
                    metadata_json,
                    error_message,
                    datetime.utcnow().isoformat(),
                    job_id,
                ),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def get_retryable_jobs(self) -> List[ProcessingJob]:
        """Return RETRYING jobs whose backoff has expired.

        Only jobs with next_retry_at <= now are eligible for retry.
        """
        now = datetime.utcnow().isoformat()
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                SELECT * FROM jobs
                WHERE status = ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?
                ORDER BY priority DESC, created_at ASC
                """,
                (JobStatus.RETRYING.value, now),
            )
            rows = cursor.fetchall()
        return [self._row_to_job(row) for row in rows]

    def reset_retrying_jobs(self) -> int:
        """Reset all RETRYING jobs back to PENDING.

        Called before fetching the pending queue so that jobs with
        remaining retries re-enter the FIFO queue.
        Returns the number of jobs reset.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                UPDATE jobs
                SET status = ?
                WHERE status = ?
                """,
                (JobStatus.PENDING.value, JobStatus.RETRYING.value),
            )
            self._connection.commit()
            return cursor.rowcount

    def requeue_failed_jobs(self, drive_file_id: Optional[str] = None) -> List[str]:
        """Return FAILED jobs to the queue with a fresh retry budget.

        Only jobs in the ``failed`` state are touched -- typically those that
        exhausted ``MAX_RETRIES`` or failed permanently (a corrupt image, a
        rejected provider response). ``pending``, ``processing``, ``retrying``,
        ``completed``, ``manual_review`` and ``cancelled`` rows are left alone, so
        a requeue never reprocesses a finished book and never resurrects a
        duplicate.

        Args:
            drive_file_id: Optional Drive file id to requeue a single photo.

        Returns:
            The job ids that were requeued (empty when nothing matched).
        """
        with self._lock:
            cursor = self._connection.cursor()
            query = "SELECT job_id FROM jobs WHERE status = ?"
            params: List = [JobStatus.FAILED.value]
            if drive_file_id:
                query += " AND file_id = ?"
                params.append(drive_file_id)
            job_ids = [row[0] for row in cursor.execute(query, params).fetchall()]
            if not job_ids:
                return []

            placeholders = ",".join("?" for _ in job_ids)
            cursor.execute(
                f"""
                UPDATE jobs
                SET status = ?, retry_count = 0, started_at = NULL,
                    completed_at = NULL, error_message = NULL,
                    worker_name = NULL, error_category = NULL,
                    next_retry_at = NULL, last_error_at = NULL,
                    heartbeat_at = NULL
                WHERE job_id IN ({placeholders})
                """,
                [JobStatus.PENDING.value, *job_ids],
            )
            self._connection.commit()
            return job_ids

    def claim_job(self, job_id: str, worker_name: Optional[str] = None) -> bool:
        """Atomically move a PENDING job to PROCESSING for this worker.

        The conditional ``WHERE status = 'pending'`` is what stops two workers
        from taking the same job: only one UPDATE can match the row.
        """
        now = datetime.utcnow().isoformat()
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = ?, heartbeat_at = ?, worker_name = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.PROCESSING.value,
                    now,
                    now,
                    worker_name,
                    job_id,
                    JobStatus.PENDING.value,
                ),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def heartbeat(self, job_id: str, worker_name: Optional[str] = None) -> bool:
        """Renew the lease of a job this worker is still processing.

        Only touches rows that are still PROCESSING (and owned by
        ``worker_name`` when given), so a completed job is never resurrected.
        """
        now = datetime.utcnow().isoformat()
        with self._lock:
            cursor = self._connection.cursor()
            if worker_name:
                cursor.execute(
                    """
                    UPDATE jobs SET heartbeat_at = ?
                    WHERE job_id = ? AND status = ? AND worker_name = ?
                    """,
                    (now, job_id, JobStatus.PROCESSING.value, worker_name),
                )
            else:
                cursor.execute(
                    """
                    UPDATE jobs SET heartbeat_at = ?
                    WHERE job_id = ? AND status = ?
                    """,
                    (now, job_id, JobStatus.PROCESSING.value),
                )
            self._connection.commit()
            return cursor.rowcount > 0

    def recover_stuck_jobs(
        self, timeout_seconds: int = 900, max_retries: int = 3
    ) -> int:
        """Recover PROCESSING jobs whose lease has genuinely expired.

        A job is stale only when its most recent activity -- ``heartbeat_at``
        if the worker ever renewed the lease, otherwise ``started_at`` -- is
        older than ``timeout_seconds``. Jobs with a fresh heartbeat are still
        being worked on and are left alone.

        Retry history is preserved: jobs under the retry limit return to
        PENDING without their ``retry_count`` being touched, and jobs that
        already exhausted their retries are failed instead of being
        resurrected forever.
        """
        threshold = (datetime.utcnow() - timedelta(seconds=timeout_seconds)).isoformat()
        now = datetime.utcnow().isoformat()
        expired = "status = ? AND COALESCE(heartbeat_at, started_at, '') <= ?"
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                f"""
                UPDATE jobs
                SET status = ?, started_at = NULL, heartbeat_at = NULL
                WHERE {expired} AND retry_count < ?
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                    threshold,
                    max_retries,
                ),
            )
            recovered = cursor.rowcount
            cursor.execute(
                f"""
                UPDATE jobs
                SET status = ?, completed_at = ?, heartbeat_at = NULL,
                    error_message = COALESCE(error_message, ?)
                WHERE {expired} AND retry_count >= ?
                """,
                (
                    JobStatus.FAILED.value,
                    now,
                    "Lease expired after the retry limit was reached",
                    JobStatus.PROCESSING.value,
                    threshold,
                    max_retries,
                ),
            )
            failed = cursor.rowcount
            self._connection.commit()
        return recovered + failed

    def _row_to_job(self, row: sqlite3.Row) -> ProcessingJob:
        """Convert a database row to a ProcessingJob object."""
        return ProcessingJob(
            job_id=row["job_id"],
            file_id=row["file_id"],
            file_name=row["file_name"],
            local_path=row["local_path"],
            status=JobStatus(row["status"]),
            retry_count=row["retry_count"] or 0,
            priority=row["priority"] or 0,
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
            error_message=row["error_message"],
            worker_name=row["worker_name"],
            error_category=(
                ErrorCategory(row["error_category"]) if row["error_category"] else None
            ),
            next_retry_at=(
                datetime.fromisoformat(row["next_retry_at"])
                if row["next_retry_at"]
                else None
            ),
            last_error_at=(
                datetime.fromisoformat(row["last_error_at"])
                if row["last_error_at"]
                else None
            ),
        )

    def get_manual_review_metadata(self, job_id: str) -> Optional[dict]:
        """Retrieve the full manual review metadata JSON for a job.

        Returns the structured metadata (candidates, OCR text, confidence,
        extracted fields, rejection reason) stored during manual review,
        or None if the job is not in MANUAL_REVIEW or has no metadata.
        """
        import json as _json

        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                "SELECT metadata_json FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            row = cursor.fetchone()
        if row is None or not row["metadata_json"]:
            return None
        try:
            result = _json.loads(row["metadata_json"])
            return result if isinstance(result, dict) else None
        except (_json.JSONDecodeError, TypeError):
            return None
