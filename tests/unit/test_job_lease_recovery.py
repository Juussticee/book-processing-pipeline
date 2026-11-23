"""Lease-based stale-work recovery.

A job may only be recovered when its lease has genuinely expired. Recovery
must never steal a job whose worker is still beating, must never touch
finished jobs, must keep retry history, and must stop resurrecting a job
that already used up its retries.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta

import pytest

from models.processing_job import JobStatus
from repositories.export_repository import ExportRepository
from repositories.job_repository import JobRepository
from services.job_service import JobService
from workers.base_worker import BaseWorker

TIMEOUT = 900


def _ago(seconds: float) -> str:
    return (datetime.utcnow() - timedelta(seconds=seconds)).isoformat()


class _Harness:
    def __init__(self, timeout: int = TIMEOUT) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.repo = JobRepository(self.db_path)
        self.service = JobService(self.repo)
        self.timeout = timeout

    def create(self, file_id: str = "drive-1"):
        job = self.repo.create_job(file_id, f"{file_id}.jpg", f"/tmp/{file_id}.jpg")
        assert job is not None
        return job

    def processing_job(
        self,
        file_id: str = "drive-1",
        heartbeat_ago: float | None = None,
        started_ago: float | None = 30,
        retry_count: int = 0,
        status: JobStatus = JobStatus.PROCESSING,
        worker_name: str | None = "WorkerA",
    ):
        job = self.create(file_id)
        with self.repo._connection:  # noqa: SLF001 - simulating a crashed worker
            self.repo._connection.execute(  # noqa: SLF001
                """
                UPDATE jobs
                SET status = ?, started_at = ?, heartbeat_at = ?,
                    retry_count = ?, worker_name = ?
                WHERE job_id = ?
                """,
                (
                    status.value,
                    _ago(started_ago) if started_ago is not None else None,
                    _ago(heartbeat_ago) if heartbeat_ago is not None else None,
                    retry_count,
                    worker_name,
                    job.job_id,
                ),
            )
        return self.repo.get_job_by_id(job.job_id)

    def recover(self) -> int:
        return self.service.recover_stuck_jobs()

    def status_of(self, job_id: str) -> str:
        job = self.repo.get_job_by_id(job_id)
        assert job is not None
        return job.status.value

    def row(self, job_id: str) -> sqlite3.Row:
        cursor = self.repo._connection.execute(  # noqa: SLF001
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        )
        return cursor.fetchone()

    def close(self) -> None:
        self.repo.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)


@pytest.fixture()
def harness():
    h = _Harness()
    yield h
    h.close()


class TestJobLeaseRecovery:
    def test_recent_heartbeat_is_left_alone(self, harness: _Harness) -> None:
        """A long job that keeps beating is still being worked on."""
        job = harness.processing_job(started_ago=5000, heartbeat_ago=5)
        assert harness.recover() == 0
        assert harness.status_of(job.job_id) == "processing"
        assert harness.row(job.job_id)["worker_name"] == "WorkerA"

    def test_expired_heartbeat_is_recovered(self, harness: _Harness) -> None:
        job = harness.processing_job(started_ago=5000, heartbeat_ago=TIMEOUT + 60)
        assert harness.recover() == 1
        assert harness.status_of(job.job_id) == "pending"
        assert harness.row(job.job_id)["heartbeat_at"] is None

    def test_crash_after_claim_without_heartbeat_is_recovered(
        self, harness: _Harness
    ) -> None:
        """Crash right after claiming: no heartbeat row at all."""
        job = harness.processing_job(started_ago=TIMEOUT + 30, heartbeat_ago=None)
        assert harness.recover() == 1
        assert harness.status_of(job.job_id) == "pending"

    def test_recovery_preserves_retry_count(self, harness: _Harness) -> None:
        job = harness.processing_job(
            heartbeat_ago=TIMEOUT + 60, started_ago=TIMEOUT + 90, retry_count=2
        )
        harness.recover()
        row = harness.row(job.job_id)
        assert row["retry_count"] == 2
        assert row["status"] == "pending"
        assert row["completed_at"] is None

    def test_job_at_retry_limit_is_failed_not_requeued(self, harness: _Harness) -> None:
        job = harness.processing_job(
            heartbeat_ago=TIMEOUT + 60, retry_count=JobService.MAX_RETRIES
        )
        assert harness.recover() == 1
        row = harness.row(job.job_id)
        assert row["status"] == "failed"
        assert row["completed_at"] is not None
        assert "retry limit" in (row["error_message"] or "")

    def test_finished_jobs_are_never_touched(self, harness: _Harness) -> None:
        completed = harness.processing_job("drive-ok", status=JobStatus.COMPLETED)
        failed = harness.processing_job(
            "drive-bad", status=JobStatus.FAILED, heartbeat_ago=TIMEOUT * 2
        )
        assert harness.recover() == 0
        assert harness.status_of(completed.job_id) == "completed"
        assert harness.status_of(failed.job_id) == "failed"

    def test_only_one_of_two_workers_can_claim_a_job(self, harness: _Harness) -> None:
        job = harness.create()
        assert harness.service.claim_job(job.job_id, "WorkerA") is True
        assert harness.service.claim_job(job.job_id, "WorkerB") is False
        row = harness.row(job.job_id)
        assert row["status"] == "processing"
        assert row["worker_name"] == "WorkerA"
        assert row["heartbeat_at"] is not None

    def test_second_worker_does_not_process_an_already_claimed_job(
        self, harness: _Harness
    ) -> None:
        """The worker entry point must skip a job another worker owns."""
        job = harness.create()
        assert harness.service.claim_job(job.job_id, "WorkerA") is True

        processed: list[str] = []

        class _Worker(BaseWorker):
            WORKER_NAME = "WorkerB"

            def process(self, job):
                processed.append(job.job_id)
                return True

            def initialize(self):
                pass

            def shutdown(self):
                pass

            def health_check(self):
                return {"status": "ok"}

        _Worker(harness.service)._process_job(job)  # noqa: SLF001

        assert processed == []
        assert harness.status_of(job.job_id) == "processing"

    def test_heartbeat_only_applies_while_processing(self, harness: _Harness) -> None:
        job = harness.processing_job(status=JobStatus.COMPLETED)
        assert harness.service.heartbeat(job.job_id) is False
        assert harness.row(job.job_id)["status"] == "completed"

    def test_completion_during_recovery_window_is_preserved(
        self, harness: _Harness
    ) -> None:
        """Job finishes just before recovery runs: still completed."""
        job = harness.processing_job(heartbeat_ago=TIMEOUT + 60)
        harness.service.complete_job(job.job_id, "WorkerA")
        assert harness.recover() == 0
        assert harness.status_of(job.job_id) == "completed"

    def test_heartbeat_thread_keeps_a_long_job_alive(self, harness: _Harness) -> None:
        """A running worker renews its lease, so recovery leaves it alone."""
        job = harness.create()
        processing = threading.Event()

        class _SlowWorker(BaseWorker):
            WORKER_NAME = "WorkerA"
            HEARTBEAT_INTERVAL = 0.02

            def process(self, job):
                processing.set()
                threading.Event().wait(0.3)
                return True

            def initialize(self):
                pass

            def shutdown(self):
                pass

            def health_check(self):
                return {"status": "ok"}

        worker = _SlowWorker(harness.service)
        thread = threading.Thread(
            target=worker._process_job, args=(job,)
        )  # noqa: SLF001
        thread.start()
        assert processing.wait(5)

        # Lease timeout is far shorter than the job, but heartbeats land every 20 ms.
        harness.service.recover_stuck_jobs()
        assert harness.status_of(job.job_id) == "processing"

        thread.join(timeout=10)
        assert harness.status_of(job.job_id) == "completed"


class TestExportLeaseRecovery:
    def setup_method(self, method) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.repo = ExportRepository(self.db_path)

    def teardown_method(self, method) -> None:
        self.repo.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _processing_export(self, attempted_ago: float | None):
        record = self.repo.create_export_record("drive-x", "job-x")
        assert record is not None
        with self.repo._queue_conn:  # noqa: SLF001 - simulating a crash
            self.repo._queue_conn.execute(  # noqa: SLF001
                "UPDATE export_queue SET status = ?, attempted_at = ? "
                "WHERE drive_file_id = ?",
                (
                    "processing",
                    _ago(attempted_ago) if attempted_ago is not None else None,
                    "drive-x",
                ),
            )
        return record

    def _queue_row(self, drive_file_id: str) -> sqlite3.Row:
        cursor = self.repo._queue_conn.execute(  # noqa: SLF001
            "SELECT status, retry_count, attempted_at FROM export_queue "
            "WHERE drive_file_id = ?",
            (drive_file_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        return row

    def test_fresh_export_attempt_is_left_alone(self) -> None:
        self._processing_export(attempted_ago=30)
        assert self.repo.recover_stuck_exports(7200) == 0
        assert self._queue_row("drive-x")["status"] == "processing"

    def test_expired_export_is_requeued_with_its_retry_count(self) -> None:
        self._processing_export(attempted_ago=7300)
        assert self.repo.recover_stuck_exports(7200) == 1
        row = self._queue_row("drive-x")
        assert row["status"] == "pending"
        assert row["retry_count"] == 0

    def test_completed_export_is_never_requeued(self) -> None:
        record = self.repo.create_export_record("drive-done", "job-done")
        assert record is not None
        with self.repo._queue_conn:  # noqa: SLF001
            self.repo._queue_conn.execute(  # noqa: SLF001
                "UPDATE export_queue SET status = 'completed', attempted_at = ? "
                "WHERE drive_file_id = 'drive-done'",
                (_ago(10000),),
            )
        assert self.repo.recover_stuck_exports(7200) == 0
        assert self._queue_row("drive-done")["status"] == "completed"
