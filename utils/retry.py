"""Retry utility with exponential backoff.

Retries a function on transient HTTP errors: 429, 500, 502, 503, 504,
connection errors, and timeouts. Does NOT retry permanent failures
(4xx except 429, 404, etc.).

Usage (decorator factory with config)::

    @retry_with_backoff(max_retries=3, initial_delay=1.0)
    def my_api_call():
        ...

Usage (decorator without args)::

    @retry_with_backoff
    def my_api_call():
        ...
"""

from __future__ import annotations

import functools
import time
from typing import Callable, Optional, Tuple, Type

from core.logger import setup_logger

logger = setup_logger(__name__)

# HTTP status codes that should trigger a retry
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Exception types that indicate transient network failures
RETRYABLE_EXCEPTION_TYPES: Tuple[Type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)

# Also retry requests.exceptions errors if requests is available
try:
    import requests as _requests_module  # noqa: F401

    RETRYABLE_EXCEPTION_TYPES = (  # type: ignore[assignment]
        ConnectionError,
        TimeoutError,
        OSError,
        _requests_module.exceptions.ConnectionError,
        _requests_module.exceptions.Timeout,
    )
except ImportError:
    pass


def retry_with_backoff(
    func: Optional[Callable] = None,
    *,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: Tuple[Type[BaseException], ...] = RETRYABLE_EXCEPTION_TYPES,
    retryable_status_codes: frozenset = RETRYABLE_STATUS_CODES,
) -> Callable:
    """Wrap *func* with retry logic using exponential backoff.

    Can be used as ``@retry_with_backoff`` (no args) or
    ``@retry_with_backoff(max_retries=3, ...)`` (with args).

    The wrapper inspects the return value: if it is a ``requests.Response``
    whose status code is in *retryable_status_codes*, the call is retried.
    Non-Response return values are returned immediately.

    Args:
        func: The function to wrap (for bare-decorator usage).
        max_retries: Maximum number of retry attempts (default 3).
        initial_delay: Seconds to wait before first retry (default 1.0).
        max_delay: Maximum delay between retries (default 30.0).
        backoff_factor: Multiplier for exponential backoff (default 2.0).
        retryable_exceptions: Exception types that trigger retry.
        retryable_status_codes: HTTP status codes that trigger retry.

    Returns:
        A wrapped function with retry behaviour.
    """

    def decorator(original_func: Callable) -> Callable:
        @functools.wraps(original_func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception: Optional[BaseException] = None
            for attempt in range(1, max_retries + 1):
                try:
                    result = original_func(*args, **kwargs)
                    # If result is a Response with a retryable status, retry
                    response = getattr(result, "status_code", None)
                    if (
                        response is not None
                        and isinstance(response, int)
                        and response in retryable_status_codes
                    ):
                        if attempt < max_retries:
                            logger.warning(
                                "HTTP %d from %s - retry %d/%d after %.1fs",
                                response,
                                getattr(original_func, "__name__", "func"),
                                attempt,
                                max_retries,
                                delay,
                            )
                            time.sleep(delay)
                            delay = min(delay * backoff_factor, max_delay)
                            continue
                        logger.error(
                            "HTTP %d after %d retries - giving up",
                            response,
                            max_retries,
                        )
                        return result
                    return result
                except retryable_exceptions as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        logger.warning(
                            "Transient error in %s: %s - retry %d/%d after %.1fs",
                            getattr(original_func, "__name__", "func"),
                            exc,
                            attempt,
                            max_retries,
                            delay,
                        )
                        time.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                    else:
                        logger.error(
                            "Transient error after %d retries - giving up: %s",
                            max_retries,
                            exc,
                        )
                except Exception:
                    # Non-retryable exception - propagate immediately
                    raise
            # If we exhausted all retries on exceptions, re-raise the last one
            if last_exception is not None:
                raise last_exception
            # Fallback - should not reach here
            return original_func(*args, **kwargs)

        return wrapper

    # Support both @retry_with_backoff and @retry_with_backoff(...)
    if func is not None:
        return decorator(func)
    return decorator
