"""Tests for Milestone 8.5: Debug artifact retention policy.

Tests verify:
  - cleanup_old_debug_artifacts removes old directories
  - retention_days=0 disables cleanup
  - Recent artifacts are preserved
  - Non-existent debug directory returns 0
"""

import os
import shutil
import tempfile
import time

import pytest

from services.metadata_service import MetadataService


class TestDebugArtifactRetention:
    """Tests for the debug artifact retention cleanup policy."""

    @pytest.fixture
    def temp_data_dir(self):
        """Create a temporary data directory for debug artifacts."""
        temp_dir = tempfile.mkdtemp(prefix="hermes-verify-")
        debug_dir = os.path.join(temp_dir, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        yield temp_dir, debug_dir
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def metadata_service(self, temp_data_dir):
        """Create a MetadataService with a temp data directory."""
        temp_dir, _ = temp_data_dir
        return MetadataService(data_directory=temp_dir)

    def _create_debug_artifact(self, debug_dir: str, job_id: str, age_days: int = 0):
        """Create a debug artifact directory, optionally backdated."""
        job_dir = os.path.join(debug_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "final_decision.json"), "w") as f:
            f.write('{"test": true}')
        # Backdate the directory mtime
        if age_days > 0:
            old_time = time.time() - (age_days * 86400)
            for fname in os.listdir(job_dir):
                os.utime(os.path.join(job_dir, fname), (old_time, old_time))
            os.utime(job_dir, (old_time, old_time))
        return job_dir

    def test_retention_disables_with_zero_days(self, temp_data_dir, metadata_service):
        """retention_days=0 should disable cleanup entirely."""
        _, debug_dir = temp_data_dir
        self._create_debug_artifact(debug_dir, "old_job_1", age_days=10)
        result = metadata_service.cleanup_old_debug_artifacts(retention_days=0)
        assert result == 0
        assert os.path.isdir(os.path.join(debug_dir, "old_job_1"))

    def test_removes_old_artifacts(self, temp_data_dir, metadata_service):
        """Artifacts older than retention period should be removed."""
        _, debug_dir = temp_data_dir
        self._create_debug_artifact(debug_dir, "old_job_1", age_days=5)
        self._create_debug_artifact(debug_dir, "old_job_2", age_days=3)
        result = metadata_service.cleanup_old_debug_artifacts(retention_days=2)
        assert result == 2
        assert not os.path.isdir(os.path.join(debug_dir, "old_job_1"))
        assert not os.path.isdir(os.path.join(debug_dir, "old_job_2"))

    def test_preserves_recent_artifacts(self, temp_data_dir, metadata_service):
        """Artifacts within retention period should be preserved."""
        _, debug_dir = temp_data_dir
        self._create_debug_artifact(debug_dir, "recent_job", age_days=1)
        result = metadata_service.cleanup_old_debug_artifacts(retention_days=3)
        assert result == 0
        assert os.path.isdir(os.path.join(debug_dir, "recent_job"))

    def test_mixed_old_and_new(self, temp_data_dir, metadata_service):
        """Old artifacts removed, recent ones preserved."""
        _, debug_dir = temp_data_dir
        self._create_debug_artifact(debug_dir, "old_job", age_days=5)
        self._create_debug_artifact(debug_dir, "recent_job", age_days=1)
        result = metadata_service.cleanup_old_debug_artifacts(retention_days=3)
        assert result == 1
        assert not os.path.isdir(os.path.join(debug_dir, "old_job"))
        assert os.path.isdir(os.path.join(debug_dir, "recent_job"))

    def test_nonexistent_debug_dir(self, metadata_service):
        """Cleanup on non-existent debug directory returns 0."""
        result = metadata_service.cleanup_old_debug_artifacts(retention_days=3)
        assert result == 0

    def test_never_deletes_outside_debug_dir(
        self, temp_data_dir, metadata_service, monkeypatch
    ):
        """A traversal-style entry must not reach a sibling directory."""
        temp_dir, debug_dir = temp_data_dir
        covers_dir = os.path.join(temp_dir, "covers")
        os.makedirs(covers_dir, exist_ok=True)
        cover = os.path.join(covers_dir, "cover.jpg")
        with open(cover, "w") as f:
            f.write("img")
        old_time = time.time() - (30 * 86400)
        os.utime(covers_dir, (old_time, old_time))

        monkeypatch.setattr(os, "listdir", lambda _p: ["../covers"])
        removed = metadata_service.cleanup_old_debug_artifacts(retention_days=1)

        assert removed == 0
        assert os.path.isdir(covers_dir)
        assert os.path.isfile(cover)

    def test_failed_removal_is_not_counted(
        self, temp_data_dir, metadata_service, monkeypatch
    ):
        """A removal that raises must not be reported as success."""
        _, debug_dir = temp_data_dir
        self._create_debug_artifact(debug_dir, "old_job", age_days=10)

        def boom(*_args, **_kwargs):
            raise OSError("locked")

        monkeypatch.setattr(
            "services.metadata_service.shutil.rmtree", boom, raising=True
        )
        removed = metadata_service.cleanup_old_debug_artifacts(retention_days=1)

        assert removed == 0
        assert os.path.isdir(os.path.join(debug_dir, "old_job"))

    def test_default_retention_days(self, temp_data_dir, metadata_service):
        """Default retention_days=3 is used when parameter is omitted."""
        _, debug_dir = temp_data_dir
        # With default retention (3 days), a 5-day old artifact should be removed
        self._create_debug_artifact(debug_dir, "old_job", age_days=5)
        result = metadata_service.cleanup_old_debug_artifacts()
        assert result == 1
