"""Test application startup."""

import os
import subprocess
import sys


class TestApplicationStarts:
    """Test that the application starts without errors."""

    def test_app_runs_without_errors(self) -> None:
        """Test that running app.py returns exit code 0."""
        result = subprocess.run(
            [sys.executable, "app.py"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            timeout=10,
        )
        assert result.returncode == 0

    def test_app_stderr_has_log_output(self) -> None:
        """Test that stderr contains log output."""
        result = subprocess.run(
            [sys.executable, "app.py"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            timeout=10,
        )
        assert "Application starting" in result.stderr or "INFO" in result.stderr

    def test_app_shows_configuration_report(self) -> None:
        """Test that stdout contains the configuration report."""
        result = subprocess.run(
            [sys.executable, "app.py"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            timeout=10,
        )
        assert "Configuration" in result.stdout
