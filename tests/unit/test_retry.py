"""Unit tests for retry_with_backoff utility."""

from unittest.mock import patch

import pytest

from utils.retry import RETRYABLE_STATUS_CODES, retry_with_backoff


class TestRetryWithBackoff:
    """Tests for retry_with_backoff decorator."""

    @patch("utils.retry.time.sleep")
    def test_success_on_first_try(self, mock_sleep):
        """Function succeeds on first call — no retries."""
        call_count = [0]

        @retry_with_backoff(max_retries=3, initial_delay=0.01)
        def succeed():
            call_count[0] += 1
            return "ok"

        assert succeed() == "ok"
        assert call_count[0] == 1
        mock_sleep.assert_not_called()

    @patch("utils.retry.time.sleep")
    def test_retries_on_retryable_exception(self, mock_sleep):
        """Function retries on ConnectionError up to max_retries."""
        results = []

        @retry_with_backoff(max_retries=3, initial_delay=0.01, backoff_factor=2.0)
        def flaky():
            results.append(len(results) + 1)
            if len(results) < 3:
                raise ConnectionError("transient")
            return "success"

        assert flaky() == "success"
        assert len(results) == 3
        assert mock_sleep.call_count == 2

    @patch("utils.retry.time.sleep")
    def test_no_retry_on_non_retryable_exception(self, mock_sleep):
        """ValueError is not retried."""

        @retry_with_backoff(max_retries=3, initial_delay=0.01)
        def fail():
            raise ValueError("permanent")

        with pytest.raises(ValueError, match="permanent"):
            fail()
        mock_sleep.assert_not_called()

    @patch("utils.retry.time.sleep")
    def test_retries_on_429_status_code(self, mock_sleep):
        """HTTP 429 triggers retry."""
        from unittest.mock import Mock

        results = []

        @retry_with_backoff(max_retries=3, initial_delay=0.01)
        def api_call():
            results.append(len(results) + 1)
            if len(results) < 2:
                resp = Mock()
                resp.status_code = 429
                return resp
            return "ok"

        assert api_call() == "ok"
        assert len(results) == 2

    @patch("utils.retry.time.sleep")
    def test_no_retry_on_404_status_code(self, mock_sleep):
        """HTTP 404 is not retried — returned immediately."""
        from unittest.mock import Mock

        @retry_with_backoff(max_retries=3, initial_delay=0.01)
        def api_call():
            resp = Mock()
            resp.status_code = 404
            return resp

        resp = api_call()
        assert resp.status_code == 404
        mock_sleep.assert_not_called()

    @patch("utils.retry.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        """Exception re-raised after max retries exhausted."""

        @retry_with_backoff(max_retries=2, initial_delay=0.01)
        def always_fails():
            raise TimeoutError("timeout")

        with pytest.raises(TimeoutError, match="timeout"):
            always_fails()

    @patch("utils.retry.time.sleep")
    def test_exponential_backoff_delays(self, mock_sleep):
        """Backoff delays grow exponentially: 1s, 2s, 4s."""

        @retry_with_backoff(max_retries=4, initial_delay=1.0, backoff_factor=2.0)
        def always_fails():
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            always_fails()

        # Delays should be 1.0, 2.0, 4.0 (3 sleeps for 4 retries)
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0]

    def test_retryable_status_codes(self):
        """Verify the standard set of retryable HTTP status codes."""
        assert 429 in RETRYABLE_STATUS_CODES
        assert 500 in RETRYABLE_STATUS_CODES
        assert 502 in RETRYABLE_STATUS_CODES
        assert 503 in RETRYABLE_STATUS_CODES
        assert 504 in RETRYABLE_STATUS_CODES
        assert 404 not in RETRYABLE_STATUS_CODES
        assert 400 not in RETRYABLE_STATUS_CODES
