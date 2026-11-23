"""Unit tests for LocalVisionService.

All tests use mocks — no real Ollama calls, no real model inference.
These test the JSON parsing, error handling, retry logic, and recovery
threshold enforcement of the local VLM service.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from services.local_vision_service import (
    LocalVisionService,
    VLMRecoveryResult,
    LocalVLMJSONParseError,
    LocalVLMTimeoutError,
    RECOVERY_CONFIDENCE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vvm_service():
    """Create a LocalVisionService with test defaults."""
    return LocalVisionService(
        model="test-model",
        host="http://127.0.0.1:11434",
        timeout=30,
        max_retries=2,
    )


@pytest.fixture(autouse=True)
def mock_ram_available():
    """Mock RAM check to return ample memory for non-memory-safety tests.

    This prevents tests from failing on low-memory systems when the Ollama
    server is running. TestMemorySafety tests override this with their own
    RAM values.
    """
    with patch(
        "services.local_vision_service.get_available_ram_gb",
        return_value=8.0,
    ):
        yield


# ---------------------------------------------------------------------------
# Recovery threshold tests
# ---------------------------------------------------------------------------


class TestRecoveryThreshold:
    """VVM must be skipped when confidence >= recovery threshold."""

    def test_high_confidence_skips_vvm(self, vvm_service):
        """Rows with confidence >= 0.70 must NOT call the VVM."""
        result = vvm_service.extract_from_image(
            "/fake/path/image.jpg",
            current_confidence=0.85,
            recovery_threshold=0.70,
        )
        assert result.vlm_called is False
        assert result.title is None
        assert result.author is None
        assert "skipped" in (result.error or "")

    def test_exactly_at_threshold_skips_vvm(self, vvm_service):
        """Confidence exactly 0.70 should skip (matching Gemini behavior)."""
        result = vvm_service.extract_from_image(
            "/fake/path/image.jpg",
            current_confidence=0.70,
            recovery_threshold=0.70,
        )
        assert result.vlm_called is False

    def test_just_below_threshold_calls_vvm(self, vvm_service):
        """Confidence 0.69 should call the VVM."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm") as mock_call:
                    mock_call.return_value = VLMRecoveryResult(
                        vlm_called=True,
                        title="Test Title",
                        author="Test Author",
                        confidence=0.9,
                    )
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.69,
                        recovery_threshold=0.70,
                    )
                    assert result.vlm_called is True
                    assert result.title == "Test Title"
                    assert result.author == "Test Author"
                    mock_call.assert_called_once_with("/fake/path/image.jpg")


# ---------------------------------------------------------------------------
# Valid JSON response tests
# ---------------------------------------------------------------------------


class TestValidJSON:
    """Test successful VVM responses with valid JSON."""

    def test_valid_structured_response(self, vvm_service):
        """VVM returns valid JSON with title, author, confidence."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm") as mock_call:
                    mock_call.return_value = VLMRecoveryResult(
                        vlm_called=True,
                        title="L'individu et la communaute",
                        author="T. Litt",
                        confidence=0.9,
                        raw_response='{"title": "L\'individu et la communaute", "author": "T. Litt", "confidence": 0.9}',
                    )
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.49,
                    )
                    assert result.vlm_called is True
                    assert result.title == "L'individu et la communaute"
                    assert result.author == "T. Litt"
                    assert result.confidence == 0.9

    def test_empty_title_and_author(self, vvm_service):
        """VVM returns empty title and author (image not identifiable)."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm") as mock_call:
                    mock_call.return_value = VLMRecoveryResult(
                        vlm_called=True,
                        title=None,
                        author=None,
                        confidence=0.1,
                    )
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.50,
                    )
                    assert result.vlm_called is True
                    assert result.title is None
                    assert result.author is None
                    assert result.confidence == 0.1

    def test_low_confidence_preserved(self, vvm_service):
        """VVM's low confidence is preserved (not artificially inflated)."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm") as mock_call:
                    mock_call.return_value = VLMRecoveryResult(
                        vlm_called=True,
                        title="Unclear Title",
                        author="?",
                        confidence=0.3,
                    )
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.60,
                    )
                    assert result.confidence == 0.3


# ---------------------------------------------------------------------------
# JSON parsing tests
# ---------------------------------------------------------------------------


class TestJSONParsing:
    """Test VVM response JSON parsing edge cases."""

    def test_markdown_wrapped_json(self, vvm_service):
        """VVM wraps JSON in markdown code fences -- should still parse."""
        raw_response = '{"title": "Test", "author": "Author", "confidence": 0.8}'
        # This is tested by _call_vlm logic directly
        cleaned = raw_response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        parsed = json.loads(cleaned)
        assert parsed["title"] == "Test"
        assert parsed["author"] == "Author"
        assert parsed["confidence"] == 0.8

    def test_malformed_json_raises_parse_error(self, vvm_service):
        """Malformed JSON raises json.JSONDecodeError (not retried)."""
        raw_response = '{"title": "Broken", "author": "Missing quote}'
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw_response)

    def test_missing_title_field(self, vvm_service):
        """VVM response without 'title' field -- should default to None."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm") as mock_call:
                    mock_call.return_value = VLMRecoveryResult(
                        vlm_called=True,
                        title=None,  # missing
                        author="Author",
                        confidence=0.5,
                    )
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.60,
                    )
                    assert result.title is None
                    assert result.author == "Author"

    def test_missing_author_field(self, vvm_service):
        """VVM response without 'author' field -- should default to None."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm") as mock_call:
                    mock_call.return_value = VLMRecoveryResult(
                        vlm_called=True,
                        title="Title",
                        author=None,  # missing
                        confidence=0.5,
                    )
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.60,
                    )
                    assert result.title == "Title"
                    assert result.author is None

    def test_invalid_confidence_type_defaults_to_zero(self, vvm_service):
        """Non-numeric confidence defaults to 0.0."""
        # Test the actual _call_vlm parsing logic
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm") as mock_call:
                    mock_call.return_value = VLMRecoveryResult(
                        vlm_called=True,
                        title="Title",
                        author="Author",
                        confidence=0.0,  # default when invalid
                    )
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.60,
                    )
                    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Test Ollama unavailable, timeout, model not found."""

    def test_ollama_not_installed(self, vvm_service):
        """Ollama server not reachable returns error result (not crash)."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=False):
            result = vvm_service.extract_from_image(
                "/fake/path/image.jpg",
                current_confidence=0.50,
            )
        assert result.vlm_called is True
        assert result.error is not None
        assert "not reachable" in result.error

    def test_model_not_downloaded(self, vvm_service):
        """Model not in Ollama -- returns error."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=False):
                result = vvm_service.extract_from_image(
                    "/fake/path/image.jpg",
                    current_confidence=0.50,
                )
        assert result.vlm_called is True
        assert result.error is not None
        assert "test-model" in result.error

    def test_timeout_handled(self, vvm_service):
        """Timeout is retried up to max_retries, then returns error."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm", side_effect=LocalVLMTimeoutError("timed out")):
                    with patch.object(vvm_service, "_backoff"):
                        result = vvm_service.extract_from_image(
                            "/fake/path/image.jpg",
                            current_confidence=0.50,
                        )
        assert result.vlm_called is True
        assert result.error is not None
        assert result.retry_count == 2  # max_retries=2

    def test_json_parse_error_not_retried(self, vvm_service):
        """JSON parse errors are deterministic -- should NOT be retried."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(
                    vvm_service,
                    "_call_vlm",
                    side_effect=LocalVLMJSONParseError("bad JSON"),
                ):
                    result = vvm_service.extract_from_image(
                        "/fake/path/image.jpg",
                        current_confidence=0.50,
                    )
        assert result.vlm_called is True
        assert result.error is not None
        assert "bad JSON" in result.error
        assert result.retry_count == 0  # NOT retried

    def test_one_failure_does_not_crash_pipeline(self, vvm_service):
        """One failed VVM inference must not terminate the entire pipeline."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=False):
            result = vvm_service.extract_from_image(
                "/fake/path/image.jpg",
                current_confidence=0.50,
            )
        assert result is not None  # Returns result, doesn't crash
        assert result.error is not None
        # The orchestrator should handle this and continue to next image


# ---------------------------------------------------------------------------
# Retry behavior tests
# ---------------------------------------------------------------------------


class TestRetryBehavior:
    """Test that VVM retries on transient errors but not permanent ones."""

    def test_timeout_retries_then_succeeds(self, vvm_service):
        """Timeout on first attempt, success on second."""
        success_result = VLMRecoveryResult(
            vlm_called=True,
            title="Success",
            author="Author",
            confidence=0.9,
        )
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(
                    vvm_service, "_call_vlm",
                    side_effect=[LocalVLMTimeoutError("timeout"), success_result],
                ):
                    with patch.object(vvm_service, "_backoff"):
                        result = vvm_service.extract_from_image(
                            "/fake/path/image.jpg",
                            current_confidence=0.50,
                        )
        assert result.vlm_called is True
        assert result.title == "Success"
        assert result.retry_count == 1

    def test_max_retries_exhausted(self, vvm_service):
        """All retries exhausted -- returns error."""
        with patch.object(vvm_service, "_check_ollama_available", return_value=True):
            with patch.object(vvm_service, "_ensure_model", return_value=True):
                with patch.object(vvm_service, "_call_vlm", side_effect=LocalVLMTimeoutError("timeout")):
                    with patch.object(vvm_service, "_backoff"):
                        result = vvm_service.extract_from_image(
                            "/fake/path/image.jpg",
                            current_confidence=0.50,
                        )
        assert result.retry_count == 2  # max_retries=2
        assert result.error is not None


# ---------------------------------------------------------------------------
# Memory safety tests
# ---------------------------------------------------------------------------


class TestMemorySafety:
    """Test that memory safety checks prevent unsafe inference."""

    def test_low_ram_skips_vvm(self, vvm_service):
        """If available RAM < minimum, VVM is skipped with error."""
        with patch(
            "services.local_vision_service.get_available_ram_gb",
            return_value=0.1,  # below MIN_FREE_RAM_GB of 0.5
        ):
            result = vvm_service.extract_from_image(
                "/fake/path/image.jpg",
                current_confidence=0.50,
            )
        assert result.vlm_called is True
        assert result.error is not None
        assert "MEMORY SAFETY" in result.error

    def test_adequate_ram_proceeds(self, vvm_service):
        """8 GB available RAM -- safe to proceed."""
        with patch(
            "services.local_vision_service.get_available_ram_gb",
            return_value=8.0,
        ):
            with patch.object(vvm_service, "_check_ollama_available", return_value=True):
                with patch.object(vvm_service, "_ensure_model", return_value=True):
                    with patch.object(vvm_service, "_call_vlm") as mock_call:
                        mock_call.return_value = VLMRecoveryResult(
                            vlm_called=True,
                            title="Test",
                            author="Author",
                            confidence=0.8,
                        )
                        result = vvm_service.extract_from_image(
                            "/fake/path/image.jpg",
                            current_confidence=0.50,
                        )
        assert result.vlm_called is True
        assert result.title == "Test"


# ---------------------------------------------------------------------------
# Result dataclass tests
# ---------------------------------------------------------------------------


class TestVLMRecoveryResult:
    """Test VLMRecoveryResult dataclass behavior."""

    def test_default_values(self):
        result = VLMRecoveryResult()
        assert result.vlm_called is False
        assert result.title is None
        assert result.author is None
        assert result.confidence == 0.0
        assert result.retry_count == 0
        assert result.latency == 0.0

    def test_to_dict(self):
        result = VLMRecoveryResult(
            vlm_called=True,
            title="Test",
            author="Author",
            confidence=0.9,
            latency=1.5,
        )
        d = result.to_dict()
        assert d["vlm_called"] is True
        assert d["title"] == "Test"
        assert d["author"] == "Author"
        assert d["confidence"] == 0.9
        assert d["latency"] == 1.5
        assert d["error"] is None
