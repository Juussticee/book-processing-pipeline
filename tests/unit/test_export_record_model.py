"""Unit tests for ExportRecord model."""

from models.export_record import ExportRecord, ExportStatus


class TestExportRecord:
    def test_create_pending(self):
        record = ExportRecord(drive_file_id="drive001", job_id="job001")
        assert record.drive_file_id == "drive001"
        assert record.job_id == "job001"
        assert record.status == ExportStatus.PENDING
        assert record.retry_count == 0
        assert record.sheet_row_id is None
        assert record.last_error is None

    def test_create_completed(self):
        record = ExportRecord(
            drive_file_id="drive002",
            job_id="job002",
            status=ExportStatus.COMPLETED,
            sheet_row_id="42",
        )
        assert record.status == ExportStatus.COMPLETED
        assert record.sheet_row_id == "42"

    def test_coerce_string_status(self):
        record = ExportRecord(
            drive_file_id="drive003", job_id="job003", status="failed"
        )
        assert record.status == ExportStatus.FAILED

    def test_empty_drive_file_id_raises(self):
        try:
            ExportRecord(drive_file_id="", job_id="job001")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_empty_job_id_raises(self):
        try:
            ExportRecord(drive_file_id="drive001", job_id="")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_exported_columns_stored(self):
        record = ExportRecord(
            drive_file_id="drive004",
            job_id="job004",
            exported_columns={"title": "Test"},
        )
        assert record.exported_columns == {"title": "Test"}

    def test_repr_no_credentials(self):
        record = ExportRecord(drive_file_id="drive005", job_id="job005")
        repr_str = repr(record)
        assert "credentials" not in repr_str.lower()
        assert "service_account" not in repr_str.lower()
