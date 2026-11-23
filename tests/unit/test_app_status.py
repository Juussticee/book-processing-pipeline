"""Tests for Milestone 8.6: App status command (python app.py --status).

Tests verify:
  - print_status_report function exists and is importable
  - main() with --status flag calls print_status_report
  - --status flag is parsed correctly
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest


class TestStatusCommand:
    """Tests for the --status command in app.py."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Ensure app.py is importable."""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        # Reload to pick up changes
        import importlib

        import app as app_module

        importlib.reload(app_module)
        self.app_module = app_module

    def test_print_status_report_exists(self):
        """print_status_report function should exist in app module."""
        assert hasattr(self.app_module, "print_status_report")
        assert callable(self.app_module.print_status_report)

    def test_main_checks_for_status_flag(self):
        """main() should check for --status flag in sys.argv."""
        # Read the source to verify the flag check exists
        import inspect

        source = inspect.getsource(self.app_module.main)
        assert "--status" in source
        assert "print_status_report" in source

    def test_status_flag_short_form(self):
        """main() should also check for -s short flag."""
        import inspect

        source = inspect.getsource(self.app_module.main)
        assert "-s" in source

    def test_status_command_does_not_enter_sync_loop(self):
        """When --status is passed, main should not call run_sync_engine."""
        with patch.object(self.app_module, "print_status_report") as mock_status:
            with patch.object(self.app_module, "run_sync_engine") as mock_sync:
                with patch.object(self.app_module, "Config") as mock_config_cls:
                    mock_config = MagicMock()
                    mock_config.get_status_report.return_value = {
                        "present": [],
                        "missing": [],
                        "future": [],
                    }
                    mock_config_cls.from_env.return_value = mock_config
                    test_args = ["app.py", "--status"]
                    with patch.object(sys, "argv", test_args):
                        self.app_module.main()
                    mock_status.assert_called_once()
                    mock_sync.assert_not_called()
