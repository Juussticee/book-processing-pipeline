"""HTTP error classification utilities for the book lookup service.

Provides helpers to classify HTTP responses and exceptions as transient
(retryable) or permanent (should not retry).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# HTTP status codes that are considered transient/retryable
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Error messages/patterns that indicate transient failures
TRANSIENT_PATTERNS = frozenset(
    {
        "timeout",
        "timed out",
        "connection error",
        "connection reset",
        "connection refused",
        "network error",
        "too many requests",
        "rate limit",
        "service unavailable",
        "gateway timeout",
        "temporary",
        "retry",
        "try again",
    }
)


def is_transient_error(exc: Optional[Exception] = None) -> bool:
    """Check if an exception represents a transient (retryable) error.

    Returns True for:
    - Timeout errors
    - Connection errors
    - HTTP 429, 500, 502, 503, 504 responses

    Returns False for:
    - Client errors (4xx except 429)
    - Permanent errors (file not found, auth failures, etc.)
    """
    if exc is None:
        return False

    exc_type = type(exc).__name__.lower()
    exc_msg = str(exc).lower()

    # Check for timeout-related errors
    if "timeout" in exc_type or "timeout" in exc_msg:
        return True

    # Check for connection-related errors
    if any(
        pattern in exc_type or pattern in exc_msg
        for pattern in ("connection", "network", "broken pipe", "reset", "refused")
    ):
        return True

    # Check for rate limiting
    if any(
        pattern in exc_msg for pattern in ("429", "rate limit", "too many requests")
    ):
        return True

    # Check for service unavailability
    if any(pattern in exc_msg for pattern in ("503", "unavailable", "temporarily")):
        return True

    return False


def is_permanent_error(exc: Optional[Exception] = None) -> bool:
    """Check if an exception represents a permanent (non-retryable) error.

    Returns True for:
    - Client errors (4xx except 429)
    - Auth/validation errors
    - File not found
    - Unsupported format/operation

    Heuristic: an exception is considered permanent when its message
    contains an HTTP client-status code (4xx) or an explicit HTTP error
    keyword. Generic non-HTTP exceptions are not treated as permanent.
    """
    if exc is None:
        return False

    exc_msg = str(exc).lower()

    # Only treat as permanent when there is evidence of an HTTP-layer error:
    # either an HTTP status code in the message or a recognised HTTP keyword.
    http_keywords = frozenset(
        {
            "unauthorized",
            "forbidden",
            "not found",
            "conflict",
            "unsupported",
        }
    )

    has_status_code = any(
        f"{code} " in exc_msg or f" {code}" in exc_msg
        for code in (
            "400",
            "401",
            "403",
            "404",
            "405",
            "409",
            "410",
            "413",
            "415",
            "422",
        )
    )

    if has_status_code:
        return True

    if any(keyword in exc_msg for keyword in http_keywords):
        return True

    return False


def classify_error(exc: Optional[Exception] = None) -> str:
    """Classify an error as 'transient', 'permanent', or 'unknown'.

    Returns one of:
    - 'transient': retry with backoff
    - 'permanent': do not retry, fail immediately
    - 'unknown': treat as transient (conservative default)
    """
    if is_permanent_error(exc):
        return "permanent"
    if is_transient_error(exc):
        return "transient"
    # Default to transient for unknown errors - better to retry
    # than to fail permanently on unknown issues
    return "transient"


def classify_http_error(
    exc: Optional[Exception] = None,
    *,
    response: Optional[object] = None,
    status_code: Optional[int] = None,
) -> tuple[str, bool]:
    """Classify an HTTP/network failure as transient or permanent.

    Returns:
        (category, is_transient)

        category is one of:
        - ``"transient"``: retryable (429/5xx, connection/timeout errors)
        - ``"permanent"``: do not retry (4xx except 429)
        - ``"success"``: 2xx/3xx - no error, do not retry
        - ``"unknown"``: classified as transient by default
    """
    # Prefer explicit status code when available
    code: Optional[int] = status_code
    if code is None and response is not None:
        try:
            code = int(response.status_code)  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            code = None

    if code is not None:
        if 200 <= code < 400:
            # Success range - no error, nothing to retry
            return "success", False
        if code in RETRYABLE_STATUS_CODES:
            return "transient", True
        # 4xx except 429 are permanent
        if 400 <= code < 500 and code != 429:
            return "permanent", False
        # 5xx not in retryable set (defensive)
        return "transient", True

    # No HTTP status available; classify the exception directly
    if exc is None:
        return "unknown", True

    return classify_error(exc), is_transient_error(exc)


def is_retryable(
    exc: Optional[Exception] = None,
    *,
    status_code: Optional[int] = None,
    response: Optional[object] = None,
) -> bool:
    """
    Determines if an error should be retried.

    Args:
        exc: The exception to check
        status_code: Optional HTTP status code
        response: Optional response object

    Returns:
        True if the error is transient and should be retried
    """
    if exc is None and status_code is None and response is None:
        return False

    # Check if we have response/status_code info
    if response is not None:
        try:
            code = int(response.status_code)
            return is_transient_error_from_status(code)
        except (AttributeError, TypeError, ValueError):
            pass

    if status_code is not None:
        return is_transient_error_from_status(status_code)

    # No response info, check exception
    if exc is not None:
        return is_transient_error(exc)

    return False


def is_transient_error_from_status(status_code: int) -> bool:
    """Check if a status code indicates a transient (retryable) error.

    Returns True for: 429, 500, 502, 503, 504
    Returns False for: client errors (4xx except 429), success codes (2xx/3xx)
    """
    return status_code in RETRYABLE_STATUS_CODES


class ResponseWrapper:
    """Wrapper to provide status_code attribute for classification."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def is_transient(
    exc: Optional[Exception] = None,
    *,
    response: Optional[object] = None,
    status_code: Optional[int] = None,
) -> bool:
    """Convenience wrapper returning only the transient boolean.

    Returns True only when there is something to retry: a retryable HTTP
    status code or a transient exception. When there is no exception and
    no status code, returns False (nothing to retry).
    """
    # Nothing to classify -> not transient
    if exc is None and response is None and status_code is None:
        return False

    _category, transient = classify_http_error(
        exc, response=response, status_code=status_code
    )
    return transient
