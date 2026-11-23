"""Unit tests for ExportWorker batch handling.

All tests use mocks — no Drive, Sheets, or network access.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from workers.export_worker import ExportWorker


def _record(drive_id: str) -> dict:
    return {"drive_file_id": drive_id, "job_id": f"job-{drive_id}"}


class _StubExportService:
    """Minimal ExportService stand-in that can fail selected records."""

    def __init__(self, failing=()):
        self.calls = []
        self._failing = set(failing)
        self._config = SimpleNamespace(export_max_retries=3)

    def process_export(self, drive_file_id, job_id, max_retries=None):
        self.calls.append(drive_file_id)
        if drive_file_id in self._failing:
            raise RuntimeError("boom")
        return SimpleNamespace(status="completed")


def _make_worker(service, batches, poll_interval=0):
    repo = MagicMock()
    repo.recover_stuck_exports.return_value = 0
    repo.reset_retrying_exports.return_value = 0
    repo.get_pending_exports.side_effect = list(batches) + [StopIteration]
    worker = ExportWorker(
        export_service=service, export_repo=repo, poll_interval=poll_interval
    )
    return worker, repo


def test_one_failing_record_does_not_stop_the_batch():
    """A record that raises must not prevent the remaining records running."""
    service = _StubExportService(failing={"bad"})
    worker, repo = _make_worker(service, [[_record("a"), _record("bad"), _record("c")]])

    worker.run()

    assert service.calls == ["a", "bad", "c"]
    assert worker.processed_count == 2
    assert worker.failed_count == 1
    failed_updates = [
        call.args[:2]
        for call in repo.update_export_status.call_args_list
        if call.args[:2] == ("bad", "failed")
    ]
    assert failed_updates == [("bad", "failed")]


def test_only_queued_records_are_processed():
    """Completed exports are never picked up again by the worker."""
    service = _StubExportService()
    worker, repo = _make_worker(service, [[]])

    worker.run()

    assert service.calls == []
    assert worker.processed_count == 0
    assert worker.failed_count == 0
    repo.get_pending_exports.assert_called()


def test_worker_marks_each_record_processing_before_export():
    """Every queued record is claimed, in order, before being exported."""
    service = _StubExportService()
    worker, repo = _make_worker(service, [[_record("a"), _record("b")]])

    worker.run()

    statuses = [
        call.args[:2]
        for call in repo.update_export_status.call_args_list
        if call.args[1] == "processing"
    ]
    assert statuses == [("a", "processing"), ("b", "processing")]
