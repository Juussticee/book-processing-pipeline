"""Gemini recovery service for low-confidence book identification.

This service provides a SECONDARY recovery mechanism: when the existing
pipeline produces a confidence < 0.70, Gemini is invoked as a visual
extraction layer to recover better title/author information.

Key design principles:
  - Gemini is NOT part of the normal/fast path. It only runs for books
    with existing confidence < 0.70.
  - Gemini is an extraction layer, NOT the final authority. Its output
    is fed back through the existing MetadataService.identify_book()
    pipeline, which remains the decision-maker.
  - Confidence thresholds are NOT lowered. The existing thresholds
    (0.85 auto-accept, 0.50 manual_review, 0.50 rejected) are unchanged.
  - Rate limiting and daily quota protection prevent exhausting API quota.
  - Transient errors (429, 5xx, timeouts) are retried with exponential
    backoff + jitter. Permanent errors are not retried.

Configuration (environment variables):
  GEMINI_API_KEY          - API key (never hard-coded)
  GEMINI_RECOVERY_ENABLED - "true"/"false" (default false)
  GEMINI_DAILY_MAX_REQUESTS - safety limit (default 50)
  GEMINI_RPM_LIMIT        - local rate limit (default 15)
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import base64
import requests

from core.logger import setup_logger

logger = setup_logger(__name__)

# Recovery triggers only for books with confidence below this threshold
RECOVERY_CONFIDENCE_THRESHOLD: float = 0.70

# Gemini API configuration
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-lite:generateContent"
)


# ---------------------------------------------------------------------------
# Retry policy constants
# ---------------------------------------------------------------------------

#: Maximum retry attempts within a 5-minute window
MAX_RETRIES: int = 10

#: Base delay (seconds) for exponential backoff
RETRY_INITIAL_DELAY: float = 1.0

#: Backoff multiplier
RETRY_BACKOFF_FACTOR: float = 2.0

#: Maximum delay between retries (seconds)
RETRY_MAX_DELAY: float = 60.0

#: HTTP status codes that should trigger a retry
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class GeminiQuotaExhausted(Exception):
    """Raised when daily quota / retry limit is exhausted.

    Indicates a permanent stop — do not retry.
    """


class GeminiParseError(Exception):
    """Raised when Gemini returns a successfully transmitted response that
    cannot be parsed (e.g. malformed JSON).

    This is NOT a transient/network error — retrying will return the same
    unparseable response.  The retry loop should return immediately
    instead of burning retries.
    """


class GeminiRetryAfter(Exception):
    """Raised when the API returns a Retry-After header.

    Carries the delay value so the retry loop can sleep for the
    specified duration instead of using exponential backoff.
    """

    def __init__(self, delay: float):
        self.delay = delay
        super().__init__(f"Retry-After: {delay}s")


@dataclass
class GeminiRecoveryResult:
    """Result of a Gemini extraction attempt.

    Attributes:
        gemini_called: Whether Gemini was actually invoked (False if skipped
            because confidence >= threshold or quota exhausted).
        title: Extracted book title, or None if not extracted.
        author: Extracted author name, or None if not extracted.
        script: Detected script/language (e.g. "Latin", "Arabic").
        is_book_cover: Whether the image appears to be a book cover.
        confidence: Gemini's own confidence estimate (0.0-1.0).
        parsing_error: Error message if the response was malformed.
        reason: Skip reason (e.g. "skipped_high_confidence",
            "daily_quota_exhausted").
        retry_count: Number of retries consumed.
    """

    gemini_called: bool = False
    title: Optional[str] = None
    author: Optional[str] = None
    script: Optional[str] = None
    is_book_cover: Optional[bool] = None
    confidence: float = 0.0
    parsing_error: Optional[str] = None
    reason: Optional[str] = None
    retry_count: int = 0
    raw: Optional[Dict] = None


@dataclass
class GeminiRecoveryStats:
    """Aggregate statistics for a recovery session."""

    total_rows: int = 0
    processed: int = 0
    low_confidence_rows: int = 0
    gemini_calls: int = 0
    gemini_successes: int = 0
    re_identified: int = 0
    unresolved: int = 0
    retries: int = 0
    api_errors: list = field(default_factory=list)


class GeminiRecoveryService:
    """Lightweight Gemini extraction service with retry/quota protection.

    Responsibilities:
      1. Skip Gemini for confidence >= 0.70 (high-confidence fast path).
      2. Encode the book-cover image and send to Gemini 2.5 Flash-Lite.
      3. Parse structured JSON response.
      4. Retry transient errors with exponential backoff + jitter.
      5. Enforce daily request limit and per-minute rate limit.
      6. Never crash the application on API errors.
    """

    def __init__(
        self,
        api_key: str,
        daily_max_requests: int = 50,
        rpm_limit: int = 15,
    ) -> None:
        self._api_key = api_key
        self._daily_max_requests = daily_max_requests
        self._rpm_limit = rpm_limit
        self._daily_count = 0
        self._last_request_time: float = 0.0
        self._window_start: float = time.time()

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def extract_from_image(
        self,
        image_path: str,
        current_confidence: float,
    ) -> GeminiRecoveryResult:
        """Attempt Gemini visual extraction for a low-confidence book.

        Args:
            image_path: Path to the downloaded book cover image.
            current_confidence: Existing pipeline confidence (0.0-1.0).

        Returns:
            GeminiRecoveryResult. If current_confidence >= 0.70,
            Gemini is NOT called and gemini_called=False.
        """
        # --- Fast path: skip Gemini for high-confidence books ---
        if current_confidence >= RECOVERY_CONFIDENCE_THRESHOLD:
            return GeminiRecoveryResult(
                gemini_called=False,
                reason="skipped_high_confidence",
            )

        # --- Daily quota check ---
        try:
            self._check_quota_or_raise()
        except GeminiQuotaExhausted:
            return GeminiRecoveryResult(
                gemini_called=False,
                reason="daily_quota_exhausted",
            )

        # --- Rate limiting ---
        self._rate_limit_sleep()

        # --- Build and send request with retry ---
        retry_count = 0
        last_error: Optional[str] = None
        retry_after_delay: Optional[float] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = self._call_gemini_api(image_path)
                # Success — got a parseable response
                self._daily_count += 1
                self._last_request_time = time.time()
                result.retry_count = retry_count
                return result

            except GeminiQuotaExhausted:
                # Daily quota exhausted mid-retries — stop
                logger.error(
                    "GEMINI DAILY QUOTA / RETRY LIMIT REACHED\n"
                    "PROCESS STOPPED FOR TODAY\n"
                    "Daily count: %d, max: %d",
                    self._daily_count,
                    self._daily_max_requests,
                )
                return GeminiRecoveryResult(
                    gemini_called=True,
                    reason="daily_quota_exhausted",
                    parsing_error="daily_quota_exhausted",
                )

            except GeminiRetryAfter as exc:
                # API returned Retry-After — respect it
                retry_after_delay = exc.delay
                retry_count += 1
                if attempt < MAX_RETRIES:
                    self._backoff_sleep(attempt, "retry-after", retry_after_delay)
                    continue
                break

            except Exception as exc:
                err_type = type(exc).__name__
                last_error = f"{err_type}: {exc}"

                # Permanent errors — don't retry
                if err_type in ("QuotaExceededError", "DailyQuotaExceeded"):
                    return GeminiRecoveryResult(
                        gemini_called=True,
                        reason="daily_quota_exhausted",
                        parsing_error=last_error,
                    )

                # Parse errors are deterministic — retrying returns the same
                # unparseable response.  Return immediately without retrying.
                if isinstance(exc, GeminiParseError):
                    return GeminiRecoveryResult(
                        gemini_called=True,
                        parsing_error=last_error,
                        retry_count=retry_count,
                    )

                retry_count += 1
                if attempt < MAX_RETRIES:
                    self._backoff_sleep(attempt, last_error)
                    continue
                break

        # All retries exhausted
        return GeminiRecoveryResult(
            gemini_called=True,
            parsing_error=last_error or "all retries exhausted",
            retry_count=retry_count,
        )

    def get_daily_count(self) -> int:
        """Return the number of Gemini requests used today."""
        return self._daily_count

    def reset_daily_window(self) -> None:
        """Reset the daily counter (call at midnight or on new day)."""
        self._daily_count = 0
        self._window_start = time.time()

    def is_daily_quota_exhausted(self) -> bool:
        """Check if the daily quota limit has been reached."""
        return self._daily_count >= self._daily_max_requests

    # -------------------------------------------------------------------
    # Internal: quota / rate limiting
    # -------------------------------------------------------------------

    def _check_quota_or_raise(self) -> None:
        """Raise GeminiQuotaExhausted if daily limit reached."""
        # Reset window if more than 1 day has passed
        if time.time() - self._window_start > 86400:
            self.reset_daily_window()
        if self._daily_count >= self._daily_max_requests:
            logger.error(
                "GEMINI DAILY QUOTA / RETRY LIMIT REACHED\n"
                "PROCESS STOPPED FOR TODAY\n"
                "Daily count: %d, max: %d",
                self._daily_count,
                self._daily_max_requests,
            )
            raise GeminiQuotaExhausted(
                f"Daily quota exhausted: {self._daily_count}/"
                f"{self._daily_max_requests}"
            )

    def _rate_limit_sleep(self) -> None:
        """Enforce local RPM limit via sleep."""
        now = time.time()
        if self._last_request_time > 0:
            elapsed = now - self._last_request_time
            min_interval = 60.0 / self._rpm_limit
            if elapsed < min_interval:
                sleep_time = min_interval - elapsed
                logger.debug("Rate limiting: sleeping %.2fs", sleep_time)
                time.sleep(sleep_time)

    def _backoff_sleep(
        self, attempt: int, error_detail: str, override_delay: Optional[float] = None
    ) -> None:
        """Exponential backoff with jitter for retryable errors.

        If override_delay is provided (from Retry-After), use it instead
        of exponential backoff.
        """
        if override_delay is not None:
            delay = override_delay
        else:
            delay = min(
                RETRY_INITIAL_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1)),
                RETRY_MAX_DELAY,
            )
            # Add jitter: ±25%
            jitter = random.uniform(-0.25, 0.25) * delay
            delay = max(0.1, delay + jitter)

        logger.warning(
            "Gemini retry %d/%d after %.1fs: %s",
            attempt,
            MAX_RETRIES,
            delay,
            error_detail,
        )
        time.sleep(delay)

    # -------------------------------------------------------------------
    # Internal: API call
    # -------------------------------------------------------------------

    def _call_gemini_api(self, image_path: str) -> GeminiRecoveryResult:
        """Send image to Gemini and parse the structured response.

        This method handles a single API call. Retry logic is handled
        by the caller (extract_from_image).

        Raises:
            GeminiQuotaExhausted: If the API returns a daily quota error.
            GeminiRetryAfter: If the API returns Retry-After.
            Exception: On HTTP errors or network failures.
        """
        # Read and encode image
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        # Build Gemini prompt — strict structured JSON
        prompt = (
            "You are a book cover analysis expert. Analyze this book cover "
            "image and extract ONLY the following information as a strict "
            "JSON object with these exact keys:\n"
            "- is_book_cover (boolean): whether this appears to be a book cover\n"
            "- title (string): the most likely book title, or empty string\n"
            "- author (string): the most likely author name, or empty string\n"
            "- script (string): the visible language/script (e.g. Latin,"
            " Arabic, Cyrillic, Chinese)\n"
            "- confidence (float 0.0-1.0): how confident are you in the"
            " title/author extraction\n"
            "\n"
            "Return ONLY the JSON object, nothing else. If you cannot read"
            " any title or author, return empty strings for those fields.\n"
        )

        url = f"{GEMINI_API_URL}?key={self._api_key}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": image_data,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 512,
            },
        }

        response = requests.post(url, json=payload, timeout=30)

        if response.status_code in RETRYABLE_STATUS_CODES:
            # Check for Retry-After header
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                    raise GeminiRetryAfter(delay)
                except (ValueError, TypeError):
                    pass  # Fall through to raise_for_status

            # Check for quota exhaustion markers
            resp_text = response.text
            if "quota" in resp_text.lower() or "daily" in resp_text.lower():
                raise GeminiQuotaExhausted(
                    f"API indicated quota exhaustion: "
                    f"status={response.status_code}"
                )
            # Retryable status without quota markers — raise for retry
            raise Exception(
                f"Gemini API returned retryable status "
                f"{response.status_code}: {resp_text[:200]}"
            )

        response.raise_for_status()

        # Parse the response — Gemini returns JSON
        data = response.json()

        # Extract the text from the response
        candidates = data.get("candidates", [])
        if not candidates:
            raise Exception("No candidates in Gemini response")

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            raise Exception("No content parts in Gemini response")

        text_response = parts[0].get("text", "")

        # Parse JSON from the response text
        # Gemini may wrap JSON in markdown code blocks
        cleaned = text_response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            raise GeminiParseError(f"Malformed JSON from Gemini: {text_response[:200]}")

        return GeminiRecoveryResult(
            gemini_called=True,
            title=parsed.get("title", "").strip() or None,
            author=parsed.get("author", "").strip() or None,
            script=parsed.get("script", "").strip() or None,
            is_book_cover=parsed.get("is_book_cover"),
            confidence=float(parsed.get("confidence", 0.0) or 0.0),
            raw=parsed,
        )
