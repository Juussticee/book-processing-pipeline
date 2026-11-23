"""Test logger initialization."""

import logging
import os
import shutil
import tempfile

from core.logger import setup_logger


class TestLoggerInitializes:
    """Test logger setup."""

    def test_logger_returns_logger(self) -> None:
        """Test that setup_logger returns a logging.Logger instance."""
        logger = setup_logger("test_no_file")
        assert isinstance(logger, logging.Logger)

    def test_logger_has_handlers(self) -> None:
        """Test that the logger has at least one handler."""
        logger = setup_logger("test_no_file")
        assert len(logger.handlers) > 0

    def test_logger_no_duplicate_handlers(self) -> None:
        """Test that calling setup_logger twice does not duplicate handlers."""
        logger1 = setup_logger("test_no_file")
        handler_count = len(logger1.handlers)
        logger2 = setup_logger("test_no_file")
        assert len(logger2.handlers) == handler_count

    def test_logger_file_handler(self) -> None:
        """Test that the logger writes to a file."""
        tmpdir = tempfile.mkdtemp()
        try:
            logger = setup_logger("test_file", log_dir=tmpdir)
            logger.info("test message")
            # Flush, close, and remove all handlers to release file locks on Windows.
            for handler in logger.handlers[:]:
                handler.flush()
                handler.close()
                logger.removeHandler(handler)
            # Ensure all handler streams are released globally.
            logging.shutdown()
            # Verify the log file was written.
            log_files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
            assert len(log_files) > 0
            log_path = os.path.join(tmpdir, log_files[0])
            with open(log_path) as f:
                content = f.read()
            assert "test message" in content
        finally:
            # Clean up temp directory and all remaining handler references.
            logging.shutdown()
            shutil.rmtree(tmpdir, ignore_errors=True)
