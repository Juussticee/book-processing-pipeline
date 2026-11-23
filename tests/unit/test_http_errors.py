"""Tests for HTTP error classification utilities."""

from __future__ import annotations

from utils.http_errors import (
    RETRYABLE_STATUS_CODES,
    classify_error,
    classify_http_error,
    is_permanent_error,
    is_retryable,
    is_transient,
    is_transient_error,
)


class TestRetryableStatusCodes:
    """Verify the retryable status code set."""

    def test_retryable_set_contains_expected_codes(self) -> None:
        assert 429 in RETRYABLE_STATUS_CODES
        assert 500 in RETRYABLE_STATUS_CODES
        assert 502 in RETRYABLE_STATUS_CODES
        assert 503 in RETRYABLE_STATUS_CODES
        assert 504 in RETRYABLE_STATUS_CODES

    def test_retryable_set_excludes_client_errors(self) -> None:
        assert 400 not in RETRYABLE_STATUS_CODES
        assert 404 not in RETRYABLE_STATUS_CODES
        assert 403 not in RETRYABLE_STATUS_CODES


class TestIsTransientError:
    """Verify transient error detection."""

    def test_none_is_not_transient(self) -> None:
        assert not is_transient_error(None)

    def test_timeout_error_is_transient(self) -> None:
        exc = TimeoutError("Connection timed out")
        assert is_transient_error(exc)

    def test_connection_error_is_transient(self) -> None:
        exc = ConnectionError("Connection reset by peer")
        assert is_transient_error(exc)

    def test_network_error_message_is_transient(self) -> None:
        exc = OSError("Network is unreachable")
        assert is_transient_error(exc)

    def test_rate_limit_message_is_transient(self) -> None:
        exc = Exception("429 Too Many Requests")
        assert is_transient_error(exc)

    def test_service_unavailable_is_transient(self) -> None:
        exc = Exception("503 Service Unavailable")
        assert is_transient_error(exc)

    def test_permanent_error_is_not_transient(self) -> None:
        exc = Exception("404 Not Found")
        assert not is_transient_error(exc)

    def test_value_error_is_not_transient(self) -> None:
        exc = ValueError("Invalid value")
        assert not is_transient_error(exc)


class TestIsPermanentError:
    """Verify permanent error detection."""

    def test_none_is_not_permanent(self) -> None:
        assert not is_permanent_error(None)

    def test_client_error_400_is_permanent(self) -> None:
        exc = Exception("400 Bad Request")
        assert is_permanent_error(exc)

    def test_auth_error_401_is_permanent(self) -> None:
        exc = Exception("401 Unauthorized")
        assert is_permanent_error(exc)

    def test_forbidden_403_is_permanent(self) -> None:
        exc = Exception("403 Forbidden")
        assert is_permanent_error(exc)

    def test_not_found_404_is_permanent(self) -> None:
        exc = Exception("404 Not Found")
        assert is_permanent_error(exc)

    def test_validation_error_is_not_permanent(self) -> None:
        # ValueErrors are not HTTP errors; they are "unknown" by default
        # and get classified as transient for retry purposes, not permanent.
        exc = ValueError("Invalid input")
        assert not is_permanent_error(exc)

    def test_timeout_is_not_permanent(self) -> None:
        exc = TimeoutError("Timed out")
        assert not is_permanent_error(exc)


class TestClassifyError:
    """Verify error classification."""

    def test_transient_classification(self) -> None:
        exc = TimeoutError("Connection timed out")
        assert classify_error(exc) == "transient"

    def test_permanent_classification(self) -> None:
        exc = Exception("404 Not Found")
        assert classify_error(exc) == "permanent"

    def test_unknown_defaults_to_transient(self) -> None:
        # Unknown errors default to transient (conservative)
        exc = RuntimeError("Something unexpected")
        assert classify_error(exc) == "transient"

    def test_none_classifies_as_transient(self) -> None:
        assert classify_error(None) == "transient"


class TestIsRetryable:
    """Verify retry decision logic."""

    def test_none_exception_not_retryable(self) -> None:
        assert not is_retryable(None)

    def test_permanent_error_not_retryable(self) -> None:
        exc = Exception("404 Not Found")
        assert not is_retryable(exc)

    def test_transient_error_retryable(self) -> None:
        exc = TimeoutError("Connection timed out")
        assert is_retryable(exc)

    def test_transient_status_code_retryable(self) -> None:
        assert is_retryable(status_code=500)
        assert is_retryable(status_code=503)
        assert is_retryable(status_code=429)

    def test_permanent_status_code_not_retryable(self) -> None:
        assert not is_retryable(status_code=404)
        assert not is_retryable(status_code=400)

    def test_success_status_code_not_retryable(self) -> None:
        assert not is_retryable(status_code=200)
        assert not is_retryable(status_code=201)
        assert not is_retryable(status_code=301)

    def test_no_args_not_retryable(self) -> None:
        assert not is_retryable()
        assert not is_retryable(status_code=None)

    def test_response_wrapper_retryable(self) -> None:
        from requests import Response

        response = Response()
        response.status_code = 500
        assert is_retryable(response=response)

        response.status_code = 404
        assert not is_retryable(response=response)

        response.status_code = 200
        assert not is_retryable(response=response)


class TestClassifyHttpError:
    """Verify HTTP error classification with response objects."""

    def test_transient_status_500(self) -> None:
        from requests import Response

        response = Response()
        response.status_code = 500
        category, is_transient_flag = classify_http_error(response=response)
        assert category == "transient"
        assert is_transient_flag is True

    def test_transient_status_429(self) -> None:
        from requests import Response

        response = Response()
        response.status_code = 429
        category, is_transient_flag = classify_http_error(response=response)
        assert category == "transient"
        assert is_transient_flag is True

    def test_permanent_status_404(self) -> None:
        from requests import Response

        response = Response()
        response.status_code = 404
        category, is_transient_flag = classify_http_error(response=response)
        assert category == "permanent"
        assert is_transient_flag is False

    def test_permanent_status_400(self) -> None:
        from requests import Response

        response = Response()
        response.status_code = 400
        category, is_transient_flag = classify_http_error(response=response)
        assert category == "permanent"
        assert is_transient_flag is False

    def test_none_response_unknown_transient(self) -> None:
        category, is_transient_flag = classify_http_error()
        assert category == "unknown"
        assert is_transient_flag is True

    def test_exception_without_response_is_transient(self) -> None:
        exc = TimeoutError("timed out")
        category, is_transient_flag = classify_http_error(exc=exc)
        assert category == "transient"
        assert is_transient_flag is True

    def test_is_transient_convenience(self) -> None:
        from requests import Response

        response = Response()
        response.status_code = 500
        assert is_transient(response=response) is True

        response = Response()
        response.status_code = 503
        assert is_transient(response=response) is True

        response = Response()
        response.status_code = 404
        assert is_transient(response=response) is False

        response = Response()
        response.status_code = 429
        assert is_transient(response=response) is True

        response = Response()
        response.status_code = 200
        assert is_transient(response=response) is False  # 200 is not retryable

        # Test with various timeout/connection errors
        assert is_transient(exc=TimeoutError("Connection timed out")) is True
        assert is_transient(exc=ConnectionError("Connection reset")) is True
        assert is_transient(exc=OSError("Network is unreachable")) is True
        assert is_transient(exc=ValueError("Invalid value")) is False  # Not transient

        # Test with status codes directly
        assert is_transient(status_code=500) is True
        assert is_transient(status_code=503) is True
        assert is_transient(status_code=429) is True
        assert is_transient(status_code=404) is False
        assert is_transient(status_code=400) is False
        assert is_transient(status_code=None) is False  # No status code = not retryable

        # Test with both exc and status_code (status_code takes precedence)
        assert is_transient(exc=ValueError("Invalid"), status_code=500) is True
        assert is_transient(exc=TimeoutError("timeout"), status_code=404) is False
