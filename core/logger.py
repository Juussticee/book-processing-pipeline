"""Logger setup using Python's standard logging module."""

import logging
import logging.handlers
import os
import threading
from pathlib import Path
from typing import Dict, Optional

from core.constants import LOG_DATEFMT, LOG_FORMAT
from utils.redaction import SecretRedactingFilter


class WindowsCompatibleRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that gracefully handles Windows file locking.

    On Windows, RotatingFileHandler can fail when another process has the
    log file open (e.g., IDE, log viewer, another Python instance). This
    subclass catches rotation errors and continues logging to the current
    file instead of crashing.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            # Rotation failed (file locked by another process).
            # Continue logging to the current file.
            pass


# One handler per physical log target, shared by every logger in the
# process. Previously each module's ``setup_logger`` call created its own
# ``RotatingFileHandler`` on the same ``logs/app.log``; several handlers
# rotating one file race with each other, corrupt rotation bookkeeping and
# silently drop or duplicate records.
_SHARED_HANDLERS: Dict[str, logging.Handler] = {}
_HANDLER_LOCK = threading.Lock()


def _handler_is_usable(handler: Optional[logging.Handler]) -> bool:
    """Return True when ``handler`` is open and still attached to a stream."""
    if handler is None:
        return False
    if not isinstance(handler, logging.Handler):
        return False
    # A closed StreamHandler has its stream set to None.
    if isinstance(handler, logging.StreamHandler):
        return getattr(handler, "stream", None) is not None
    return True


def _shared_console_handler(level: int, formatter: logging.Formatter):
    """Return the process-wide stderr handler, creating it once."""
    key = "console"
    with _HANDLER_LOCK:
        handler = _SHARED_HANDLERS.get(key)
        if not _handler_is_usable(handler):
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            handler.addFilter(SecretRedactingFilter())
            _SHARED_HANDLERS[key] = handler
        assert handler is not None
        _tighten(handler, level)
        return handler


def _tighten(handler: logging.Handler, level: int) -> None:
    """Lower a shared handler's level without ever raising it again."""
    if handler.level == logging.NOTSET or level < handler.level:
        handler.setLevel(level)


def _shared_file_handler(
    log_dir: str, level: int, formatter: logging.Formatter
) -> logging.Handler:
    """Return the process-wide rotating file handler for ``log_dir``."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    target = os.path.abspath(str(log_path / "app.log"))
    with _HANDLER_LOCK:
        handler = _SHARED_HANDLERS.get(target)
        if not _handler_is_usable(handler):
            handler = WindowsCompatibleRotatingFileHandler(
                target,
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            handler.addFilter(SecretRedactingFilter())
            _SHARED_HANDLERS[target] = handler
        assert handler is not None
        _tighten(handler, level)
        return handler


def setup_logger(
    name: str = "book_processor",
    level: str = "INFO",
    log_dir: str = "logs",
    console: bool = True,
    file: bool = True,
) -> logging.Logger:
    """Set up and return a logger with console and rotating file handlers.

    Handlers are shared per physical target (stderr and each log file) and
    carry a :class:`~utils.redaction.SecretRedactingFilter`, so API keys
    passed through URLs by third-party libraries never reach the log file.

    Args:
        name: Logger name.
        level: Log level string (e.g. "DEBUG").
        log_dir: Directory for the log file.
        console: Whether to attach a console handler.
        file: Whether to attach a rotating file handler.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid adding duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    numeric_level = logger.level

    if console:
        logger.addHandler(_shared_console_handler(numeric_level, formatter))

    if file:
        logger.addHandler(_shared_file_handler(log_dir, numeric_level, formatter))

    return logger
