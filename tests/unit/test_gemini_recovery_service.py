"""Unit tests for GeminiRecoveryService.

Tests MUST use mocks — no real Gemini API key or network calls.

Covers:
  1.  confidence >= 0.70  -> Gemini is NOT called
  2.  confidence < 0.70   -> Gemini is called
  3.  Gemini returns valid structured title/author
  4.  Gemini returns malformed output
  5.  Gemini returns no title
  6.  Gemini returns no author
  7.  Gemini timeout
  8.  Gemini HTTP 429 (retryable)
  9.  quota exceeded (daily)
  10. Retry-After handling
  11. exponential backoff
  12. maximum retry limit
  13. daily quota stop
  14. process stops cleanly after quota exhaustion
  15. existing Sheet row is updated, not duplicated
  16. high-confidence pipeline results remain unchanged
  17. Gemini failure preserves existing result
  18. no Gemini request for normal/high-confidence books
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from core.config import Config
from services.gemini_recovery_service import (
    GeminiRecoveryResult,
    GeminiRecoveryService,
    GeminiRecoveryStats,
    GeminiQuotaExhausted,
    GeminiRetryAfter,
    MAX_RETRIES,
    RECOVERY_CONFIDENCE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config():
    """Config with Gemini recovery enabled."""
    return Config(
        gemini_api_key="test-key",
        gemini_recovery_enabled=True,
        gemini_daily_max_requests=50,
        gemini_rpm_limit=15,
    )


@pytest.fixture
def mock_config_disabled():
    """Config with Gemini recovery disabled."""
    return Config(
        gemini_api_key="",
        gemini_recovery_enabled=False,
    )


@pytest.fixture
def gemini_service(mock_config):
    return GeminiRecoveryService(
        api_key=mock_config.gemini_api_key,
        daily_max_requests=mock_config.gemini_daily_max_requests,
        rpm_limit=mock_config.gemini_rpm_limit,
    )


@pytest.fixture(autouse=True)
def mock_open_file():
    """Mock builtins.open for image file reads in _call_gemini_api.

    Tests that mock _call_gemini_api directly don't need this (they bypass
    the file read). Tests that mock requests need this because
    _call_gemini_api opens the image file.
    """
    mock_file = MagicMock()
    mock_file.read.return_value = b"fake-image-bytes"
    with patch("builtins.open", MagicMock()) as mock_open:
        mock_open.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_open


def _mock_response(text: str, status_code: int = 200, headers=None):
    """Build a mock requests.Response-like object."""
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    resp.json = Mock(return_value=json.loads(text) if text else {})
    resp.raise_for_status = Mock()
    return resp


def _gemini_api_response(json_str: str, status_code: int = 200):
    """Build a mock response in the Gemini API format.

    Gemini returns: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
    """
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json_str}]
                }
            }
        ]
    }
    return _mock_response(json.dumps(payload), status_code=status_code)


def _valid_gemini_response():
    """A valid Gemini structured response (in API format)."""
    return _gemini_api_response(json.dumps({
        "is_book_cover": True,
        "title": "L'individu et la communauté",
        "author": "Théodore Litt",
        "script": "Latin",
        "confidence": 0.92,
    }))


def _malformed_gemini_response(text: str):
    """A Gemini response with malformed JSON in the text field."""
    return _gemini_api_response(text)


# ---------------------------------------------------------------------------
# 1 & 18. High confidence — Gemini NOT called
# ---------------------------------------------------------------------------

class TestHighConfidenceNotCalled:
    """Tests 1, 16, 18 — high-confidence books skip Gemini entirely."""

    def test_confidence_above_070_skips_gemini(self, gemini_service):
        """Test 1: confidence >= 0.70 -> Gemini is NOT called."""
        result = gemini_service.extract_from_image(
            image_path="/fake/path.jpg",
            current_confidence=0.85,
        )
        assert result.gemini_called is False
        assert result.title is None
        assert result.author is None
        assert result.reason == "skipped_high_confidence"

    def test_confidence_exactly_070_skips_gemini(self, gemini_service):
        """confidence == 0.70 (boundary) -> NOT called (>= threshold)."""
        result = gemini_service.extract_from_image(
            image_path="/fake/path.jpg",
            current_confidence=0.70,
        )
        assert result.gemini_called is False

    def test_confidence_069_calls_gemini(self, gemini_service):
        """confidence == 0.69 (just below) -> IS called."""
        with patch.object(gemini_service, "_call_gemini_api") as mock_api:
            mock_api.return_value = GeminiRecoveryResult(
                gemini_called=True,
                title="Test",
                author="Author",
            )
            result = gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.69,
            )
        assert result.gemini_called is True

    def test_no_gemini_request_for_high_confidence(self, gemini_service):
        """Test 18: explicitly verify _call_gemini_api is never invoked."""
        with patch.object(gemini_service, "_call_gemini_api") as mock_api:
            gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.90,
            )
        mock_api.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Low confidence — Gemini called
# ---------------------------------------------------------------------------

class TestLowConfidenceCallsGemini:
    """Test 2."""

    def test_low_confidence_calls_gemini(self, gemini_service):
        with patch.object(gemini_service, "_call_gemini_api") as mock_api:
            mock_api.return_value = GeminiRecoveryResult(
                gemini_called=True,
                title="Recovered",
                author="Writer",
            )
            result = gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.30,
            )
        assert result.gemini_called is True
        assert result.title == "Recovered"
        assert result.author == "Writer"


# ---------------------------------------------------------------------------
# 3. Valid structured output
# ---------------------------------------------------------------------------

class TestValidStructuredOutput:
    """Test 3."""

    @patch("services.gemini_recovery_service.requests")
    def test_valid_structured_response(self, mock_requests, gemini_service):
        mock_requests.post.return_value = _valid_gemini_response()

        with patch.object(gemini_service, "_rate_limit_sleep"):
            result = gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.49,
            )
        assert result.gemini_called is True
        assert result.title == "L'individu et la communauté"
        assert result.author == "Théodore Litt"
        assert result.script == "Latin"
        assert result.is_book_cover is True
        assert result.confidence == 0.92


# ---------------------------------------------------------------------------
# 4. Malformed output
# ---------------------------------------------------------------------------

class TestMalformedOutput:
    """Test 4."""

    @patch("services.gemini_recovery_service.requests")
    def test_malformed_json_response(self, mock_requests, gemini_service):
        mock_requests.post.return_value = _malformed_gemini_response(
            "not valid json at all"
        )
        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch.object(gemini_service, "_backoff_sleep"):
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        assert result.gemini_called is True
        assert result.parsing_error is not None
        assert result.title is None

    @patch("services.gemini_recovery_service.requests")
    def test_markdown_wrapped_json_response(self, mock_requests, gemini_service):
        """Gemini often wraps JSON in ```json code blocks — should parse correctly."""
        markdown_json = (
            "```json\n"
            "{\n"
            '  "is_book_cover": true,\n'
            '  "title": "Test Book",\n'
            '  "author": "Test Author",\n'
            '  "script": "Latin",\n'
            '  "confidence": 0.85\n'
            "}\n"
            "```"
        )
        mock_requests.post.return_value = _gemini_api_response(markdown_json)
        with patch.object(gemini_service, "_rate_limit_sleep"):
            result = gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.40,
            )
        assert result.gemini_called is True
        assert result.parsing_error is None
        assert result.title == "Test Book"
        assert result.author == "Test Author"
        assert result.confidence == 0.85

    @patch("services.gemini_recovery_service.requests")
    def test_parse_error_not_retried(self, mock_requests, gemini_service):
        """GeminiParseError should not trigger retries — return immediately."""
        mock_requests.post.return_value = _malformed_gemini_response("not json at all")
        call_count = 0
        original_post = mock_requests.post

        def counting_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original_post.return_value

        mock_requests.post.side_effect = counting_post
        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch.object(gemini_service, "_backoff_sleep") as mock_backoff:
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        assert call_count == 1, f"Expected 1 API call, got {call_count}"
        assert mock_backoff.call_count == 0, "Parse errors should not trigger backoff/retry"
        assert result.parsing_error is not None
        assert result.title is None


# ---------------------------------------------------------------------------
# 5. No title
# ---------------------------------------------------------------------------

class TestNoTitle:
    """Test 5."""

    @patch("services.gemini_recovery_service.requests")
    def test_no_title_returned(self, mock_requests, gemini_service):
        mock_requests.post.return_value = _gemini_api_response(json.dumps({
            "is_book_cover": True,
            "title": "",
            "author": "Théodore Litt",
            "script": "Latin",
            "confidence": 0.8,
        }))

        with patch.object(gemini_service, "_rate_limit_sleep"):
            result = gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.40,
            )
        assert result.gemini_called is True
        assert result.title is None
        assert result.author == "Théodore Litt"


# ---------------------------------------------------------------------------
# 6. No author
# ---------------------------------------------------------------------------

class TestNoAuthor:
    """Test 6."""

    @patch("services.gemini_recovery_service.requests")
    def test_no_author_returned(self, mock_requests, gemini_service):
        mock_requests.post.return_value = _gemini_api_response(json.dumps({
            "is_book_cover": True,
            "title": "Some Book",
            "author": "",
            "script": "Latin",
            "confidence": 0.8,
        }))

        with patch.object(gemini_service, "_rate_limit_sleep"):
            result = gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.40,
            )
        assert result.gemini_called is True
        assert result.title == "Some Book"
        assert result.author is None


# ---------------------------------------------------------------------------
# 7. Timeout
# ---------------------------------------------------------------------------

class TestTimeout:
    """Test 7."""

    @patch("services.gemini_recovery_service.requests")
    def test_timeout_handled(self, mock_requests, gemini_service):
        mock_requests.exceptions = Mock()
        mock_requests.exceptions.Timeout = type("Timeout", (Exception,), {})
        mock_requests.exceptions.ConnectionError = type("ConnErr", (Exception,), {})
        mock_requests.post.side_effect = mock_requests.exceptions.Timeout(
            "timeout"
        )

        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch.object(gemini_service, "_backoff_sleep"):
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        assert result.gemini_called is True
        assert result.parsing_error is not None


# ---------------------------------------------------------------------------
# 8. HTTP 429 retryable
# ---------------------------------------------------------------------------

class TestHTTP429:
    """Test 8 — HTTP 429 is retryable."""

    @patch("services.gemini_recovery_service.requests")
    def test_429_then_success(self, mock_requests, gemini_service):
        """429 first (with Retry-After via GeminiRetryAfter), then success."""
        fail_response = _mock_response("", status_code=429, headers={})
        success_response = _valid_gemini_response()
        mock_requests.post.side_effect = side_effects_with_retry_after(
            fail_response, success_response
        )

        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch.object(gemini_service, "_backoff_sleep") as mock_sleep:
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        assert result.gemini_called is True
        assert result.title == "L'individu et la communauté"
        assert mock_requests.post.call_count == 2  # retried once
        mock_sleep.assert_called_once()

    @patch("services.gemini_recovery_service.requests")
    def test_429_then_connection_error_then_success(
        self, mock_requests, gemini_service
    ):
        """429, then ConnectionError, then success."""

        class FakeConnErr(Exception):
            pass

        class FakeTimeout(Exception):
            pass

        mock_requests.exceptions = Mock()
        mock_requests.exceptions.Timeout = FakeTimeout
        mock_requests.exceptions.ConnectionError = FakeConnErr

        fail_429 = _mock_response("", status_code=429, headers={})
        success = _valid_gemini_response()
        mock_requests.post.side_effect = side_effects_with_retry_after(
            fail_429, FakeConnErr("connection error"), success
        )

        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch.object(gemini_service, "_backoff_sleep"):
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        assert result.gemini_called is True
        assert result.title == "L'individu et la communauté"


def side_effects_with_retry_after(*responses):
    """Make mock requests.post return the given responses side by side.

    For 429 responses, the service will raise GeminiRetryAfter (which
    carries the delay), and the retry loop will call _backoff_sleep.

    For exceptions, they'll be raised directly.
    """
    side_effects = []
    for r in responses:
        if isinstance(r, Exception):
            side_effects.append(r)
        else:
            side_effects.append(r)
    return side_effects


# ---------------------------------------------------------------------------
# 9. Quota exceeded (daily)
# ---------------------------------------------------------------------------

class TestQuotaExceeded:
    """Test 9 — daily quota exhaustion."""

    def test_daily_quota_stop_message(self, gemini_service):
        """After daily max requests, quota exhausted."""
        gemini_service._daily_count = gemini_service._daily_max_requests
        result = gemini_service.extract_from_image(
            image_path="/fake/path.jpg",
            current_confidence=0.40,
        )
        assert result.gemini_called is False
        assert result.reason == "daily_quota_exhausted"

    def test_daily_quota_raises_exception(self, gemini_service):
        """When daily quota is exhausted and called from a batch, raises."""
        gemini_service._daily_count = gemini_service._daily_max_requests
        with pytest.raises(GeminiQuotaExhausted):
            gemini_service._check_quota_or_raise()

    def test_daily_quota_logged_as_error(self, gemini_service):
        """The 'GEMINI DAILY QUOTA...' message is printed to logs."""
        gemini_service._daily_count = gemini_service._daily_max_requests
        with patch("services.gemini_recovery_service.logger") as mock_logger:
            gemini_service.extract_from_image(
                image_path="/fake/path.jpg",
                current_confidence=0.40,
            )
        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args[0]
        message = " ".join(str(a) for a in call_args)
        assert "GEMINI DAILY QUOTA" in message
        assert "PROCESS STOPPED FOR TODAY" in message


# ---------------------------------------------------------------------------
# 10. Retry-After handling
# ---------------------------------------------------------------------------

class TestRetryAfter:
    """Test 10."""

    @patch("services.gemini_recovery_service.requests")
    def test_retry_after_respected(self, mock_requests, gemini_service):
        """When API returns Retry-After header, the delay is used."""
        retry_response = _mock_response("", status_code=429)
        retry_response.headers = {"Retry-After": "7"}
        success_response = _valid_gemini_response()

        # The service raises GeminiRetryAfter(7.0) for Retry-After
        def side_effect(*args, **kwargs):
            resp = retry_response if not hasattr(side_effect, "_called") else success_response
            side_effect._called = True
            return resp

        mock_requests.post.side_effect = side_effect

        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch("services.gemini_recovery_service.time.sleep") as mock_sleep:
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        assert result.gemini_called is True
        assert result.title == "L'individu et la communauté"
        sleep_calls = [call.args[0] for call in mock_sleep.call_args_list]
        # Should have slept for >= 7 (Retry-After) at some point
        assert any(d >= 7 for d in sleep_calls), f"Expected Retry-After>=7, got {sleep_calls}"


# ---------------------------------------------------------------------------
# 11. Exponential backoff
# ---------------------------------------------------------------------------

class TestExponentialBackoff:
    """Test 11."""

    @patch("services.gemini_recovery_service.requests")
    def test_exponential_backoff_delays(self, mock_requests, gemini_service):
        """Retry delays increase exponentially."""
        fail_429 = _mock_response("", status_code=429, headers={})
        success = _valid_gemini_response()

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                return fail_429
            return success

        mock_requests.post.side_effect = side_effect

        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch("services.gemini_recovery_service.time.sleep") as mock_sleep:
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        assert result.gemini_called is True
        assert result.title == "L'individu et la communauté"
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # Should have at least 2 backoff sleeps (after 1st and 2nd failures)
        assert len(delays) >= 2
        # Delays should be increasing (exponential backoff)
        assert delays[-1] > delays[0]


# ---------------------------------------------------------------------------
# 12. Maximum retry limit
# ---------------------------------------------------------------------------

class TestMaxRetryLimit:
    """Test 12."""

    @patch("services.gemini_recovery_service.requests")
    def test_max_retries_exhausted(self, mock_requests, gemini_service):
        """After MAX_RETRIES failed attempts, gives up gracefully."""
        fail_response = _mock_response("", status_code=429, headers={})

        def side_effect(*args, **kwargs):
            return fail_response

        mock_requests.post.side_effect = side_effect

        with patch.object(gemini_service, "_rate_limit_sleep"):
            with patch("services.gemini_recovery_service.time.sleep"):
                result = gemini_service.extract_from_image(
                    image_path="/fake/path.jpg",
                    current_confidence=0.40,
                )
        # Should have tried MAX_RETRIES times
        assert mock_requests.post.call_count == MAX_RETRIES
        assert result.gemini_called is True
        assert result.parsing_error is not None


# ---------------------------------------------------------------------------
# 13 & 14. Daily quota stop / clean stop
# ---------------------------------------------------------------------------

class TestDailyQuotaStop:
    """Test 13."""

    def test_quota_exhaustion_stops_processing(self, gemini_service):
        """After quota exhausted, subsequent calls are skipped."""
        gemini_service._daily_count = gemini_service._daily_max_requests
        result = gemini_service.extract_from_image(
            image_path="/fake/path.jpg",
            current_confidence=0.30,
        )
        assert result.gemini_called is False
        assert result.reason == "daily_quota_exhausted"


class TestCleanStop:
    """Test 14 — no crash after quota exhaustion."""

    def test_no_crash_on_quota_exhaustion(self, gemini_service):
        """The service does not raise; returns a result with reason."""
        gemini_service._daily_count = gemini_service._daily_max_requests
        result = gemini_service.extract_from_image(
            image_path="/fake/path.jpg",
            current_confidence=0.30,
        )
        assert result is not None
        assert result.gemini_called is False
        assert result.reason == "daily_quota_exhausted"


# ---------------------------------------------------------------------------
# 15. Sheet row updated, not duplicated
# ---------------------------------------------------------------------------

class TestSheetRowUpdate:
    """Test 15 — orchestrator updates existing row, not duplicate."""

    def test_sheet_row_updated_not_duplicated(self, mock_config):
        from services.gemini_recovery_orchestrator import (
            GeminiRecoveryOrchestrator,
        )
        from services.gemini_recovery_service import GeminiRecoveryService

        gemini_service = GeminiRecoveryService(
            api_key="test-key",
            daily_max_requests=50,
            rpm_limit=15,
        )

        rows = [
            {
                "_row_num": 2,
                "Title": "Wrong Title",
                "Authors": "Wrong Author",
                "Confidence": "0.45",
                "Drive File ID": "file123",
                "Status": "Needs Review",
            },
        ]

        mock_sheets = MagicMock()
        mock_drive = MagicMock()
        mock_drive.get_file_metadata.return_value = MagicMock()
        mock_drive.download_file.return_value = True

        mock_metadata = MagicMock()
        mock_bm = MagicMock()
        mock_bm.confidence = 0.80
        mock_bm.title = "Recovered Title"
        mock_bm.status = "identified"
        mock_metadata.identify_book.return_value = (mock_bm, [])

        with patch.object(gemini_service, "extract_from_image") as mock_extract:
            mock_extract.return_value = GeminiRecoveryResult(
                gemini_called=True,
                title="Recovered Title",
                author="Correct Author",
                script="Latin",
                is_book_cover=True,
                confidence=0.9,
            )
            orchestrator = GeminiRecoveryOrchestrator(
                gemini_service=gemini_service,
                metadata_service=mock_metadata,
                sheets_service=mock_sheets,
                drive_service=mock_drive,
            )
            orchestrator.process_rows(rows)

        # Row should be updated, not created as new
        mock_sheets.get_or_create_row.assert_called()
        assert mock_metadata.identify_book.called


# ---------------------------------------------------------------------------
# 16. High-confidence pipeline results unchanged
# ---------------------------------------------------------------------------

class TestHighConfidenceUnchanged:
    """Test 16 — high-confidence books are skipped entirely."""

    def test_high_confidence_row_not_processed(self, mock_config):
        """Orchestrator should not call Gemini for confidence >= 0.70."""
        from services.gemini_recovery_orchestrator import (
            GeminiRecoveryOrchestrator,
        )
        from services.gemini_recovery_service import GeminiRecoveryService

        gemini_service = GeminiRecoveryService(
            api_key="test-key",
            daily_max_requests=50,
            rpm_limit=15,
        )

        rows = [
            {
                "_row_num": 2,
                "Title": "Correct Book",
                "Authors": "Correct Author",
                "Confidence": "0.85",
                "Drive File ID": "file123",
                "Status": "Verified",
            },
        ]

        mock_sheets = MagicMock()
        mock_drive = MagicMock()
        mock_metadata = MagicMock()

        with patch.object(gemini_service, "extract_from_image") as mock_extract:
            orchestrator = GeminiRecoveryOrchestrator(
                gemini_service=gemini_service,
                metadata_service=mock_metadata,
                sheets_service=mock_sheets,
                drive_service=mock_drive,
            )
            orchestrator.process_rows(rows)

        # Gemini should NOT have been called
        mock_extract.assert_not_called()
        # MetadataService should NOT have been called for re-identification
        mock_metadata.identify_book.assert_not_called()


# ---------------------------------------------------------------------------
# 17. Gemini failure preserves existing result
# ---------------------------------------------------------------------------

class TestGeminiFailurePreservesResult:
    """Test 17 — on Gemini failure, existing result is kept."""

    def test_orchestrator_preserves_on_gemini_failure(self, mock_config):
        """When Gemini returns no title/author, existing result is preserved."""
        from services.gemini_recovery_orchestrator import GeminiRecoveryOrchestrator
        from services.gemini_recovery_service import GeminiRecoveryService

        gemini_service = GeminiRecoveryService(
            api_key="test-key",
            daily_max_requests=50,
            rpm_limit=15,
        )

        rows = [
            {
                "_row_num": 2,
                "Title": "Original Title",
                "Authors": "Original Author",
                "Confidence": "0.45",
                "Drive File ID": "file123",
                "Status": "Needs Review",
            },
        ]

        mock_sheets = MagicMock()
        mock_drive = MagicMock()
        mock_drive.get_file_metadata.return_value = MagicMock()
        mock_drive.download_file.return_value = True
        mock_metadata = MagicMock()

        with patch.object(gemini_service, "extract_from_image") as mock_extract:
            # Gemini returns failure (no title/author)
            mock_extract.return_value = GeminiRecoveryResult(
                gemini_called=True,
                title=None,
                author=None,
                parsing_error="all retries exhausted",
            )
            orchestrator = GeminiRecoveryOrchestrator(
                gemini_service=gemini_service,
                metadata_service=mock_metadata,
                sheets_service=mock_sheets,
                drive_service=mock_drive,
            )
            orchestrator.process_rows(rows)

        # MetadataService should NOT be called (Gemini failed)
        mock_metadata.identify_book.assert_not_called()
        # Sheet should NOT be updated
        mock_sheets.get_or_create_row.assert_not_called()
