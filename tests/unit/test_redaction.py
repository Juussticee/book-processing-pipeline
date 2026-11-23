"""Tests for credential redaction in logs and error text.

These tests guard a real production leak: the Google Books key was written
to ``logs/app.log`` because the request URL (``...&key=AIza...``) was logged
and because ``requests`` embeds the full URL in ``raise_for_status`` errors.
"""

import logging
import os
import shutil
import tempfile

import pytest

from core.logger import setup_logger
from utils.redaction import REDACTED, SecretRedactingFilter, redact, redact_url

GOOGLE_KEY = "AIzaSyD-EXAMPLEKEY1234567890abcdefghij"
BEARER_TOKEN = "paddleocr-token-abcdef1234567890"


class TestRedactText:
    """Unit tests for the redaction helper."""

    def test_google_books_url_key_is_redacted(self) -> None:
        url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:123&key={GOOGLE_KEY}"
        result = redact_url(url)
        assert GOOGLE_KEY not in result
        assert REDACTED in result
        # Non-secret parts stay readable for debugging.
        assert "q=isbn:123" in result
        assert "googleapis.com" in result

    def test_gemini_key_query_param_is_redacted(self) -> None:
        url = f"https://generativelanguage.googleapis.com/v1/models/x:generateContent?key={GOOGLE_KEY}"
        assert GOOGLE_KEY not in redact(url)

    @pytest.mark.parametrize(
        "param",
        ["key", "api_key", "apikey", "access_token", "token", "client_secret"],
    )
    def test_common_query_params_redacted(self, param: str) -> None:
        text = f"GET https://example.com/x?{param}=supersecretvalue&page=2"
        result = redact(text)
        assert "supersecretvalue" not in result
        assert "page=2" in result

    def test_bearer_header_redacted(self) -> None:
        result = redact(f"Authorization: Bearer {BEARER_TOKEN}")
        assert BEARER_TOKEN not in result
        assert "Bearer" in result

    def test_bare_google_key_redacted(self) -> None:
        assert GOOGLE_KEY not in redact(f"using key {GOOGLE_KEY} for search")

    def test_openai_style_key_redacted(self) -> None:
        key = "sk-proj-abcdefghijklmnopqrstuvwx"
        assert key not in redact(f"OPENAI_API_KEY={key}")

    def test_dict_repr_redacted(self) -> None:
        text = "{'key': 'supersecretvalue', 'q': 'dune'}"
        result = redact(text)
        assert "supersecretvalue" not in result
        assert "dune" in result

    def test_plain_text_unchanged(self) -> None:
        text = "Searching by title: Le coeur vert"
        assert redact(text) == text

    def test_none_and_non_string(self) -> None:
        assert redact(None) == ""
        assert redact(ValueError("boom")) == "boom"

    def test_exception_message_from_requests_is_redacted(self) -> None:
        """raise_for_status embeds the URL; that text must not leak."""

        class FakeHTTPError(Exception):
            pass

        exc = FakeHTTPError(
            f"429 Client Error: Too Many Requests for url: "
            f"https://www.googleapis.com/books/v1/volumes?q=dune&key={GOOGLE_KEY}"
        )
        result = redact(exc)
        assert GOOGLE_KEY not in result
        assert "429" in result


class TestSecretRedactingFilter:
    """The filter must protect every handler, not just curated call sites."""

    def _make_record(
        self, logger: logging.Logger, message: str, *args
    ) -> logging.LogRecord:
        return logger.makeRecord(
            logger.name, logging.INFO, __file__, 1, message, args, None
        )

    def test_filter_redacts_interpolated_args(self) -> None:
        logger = logging.getLogger("test_redaction_filter_args")
        record = self._make_record(
            logger,
            "HTTP failed (%s): %s",
            f"https://x.test/?key={GOOGLE_KEY}",
            f"error at https://x.test/?key={GOOGLE_KEY}",
        )
        SecretRedactingFilter().filter(record)
        rendered = record.getMessage()
        assert GOOGLE_KEY not in rendered
        assert "HTTP failed" in rendered

    def test_filter_redacts_message_without_args(self) -> None:
        logger = logging.getLogger("test_redaction_filter_msg")
        record = self._make_record(logger, f"key is {GOOGLE_KEY}")
        SecretRedactingFilter().filter(record)
        assert GOOGLE_KEY not in record.getMessage()

    def test_filter_never_drops_records(self) -> None:
        logger = logging.getLogger("test_redaction_filter_keep")
        record = self._make_record(logger, "nothing secret here")
        assert SecretRedactingFilter().filter(record) is True


class TestLoggerWritesRedactedSecrets:
    """End-to-end: a modular logger must not persist a key to app.log."""

    def test_file_log_contains_no_api_key(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="redaction-log-")
        try:
            logger = setup_logger("test_redaction_file", log_dir=tmpdir)
            url = f"https://www.googleapis.com/books/v1/volumes?q=dune&key={GOOGLE_KEY}"
            logger.warning("HTTP request failed after retries (%s): %s", url, "boom")
            for handler in list(logger.handlers):
                handler.flush()

            log_path = os.path.join(tmpdir, "app.log")
            assert os.path.exists(log_path)
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            assert "HTTP request failed after retries" in content
            assert GOOGLE_KEY not in content
            assert REDACTED in content
        finally:
            logger = logging.getLogger("test_redaction_file")
            for handler in list(logger.handlers):
                handler.flush()
                handler.close()
                logger.removeHandler(handler)
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestSharedLogHandlers:
    """Many modules call setup_logger(); they must not each own a rotator."""

    def test_same_log_dir_shares_one_file_handler(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="shared-handler-")
        try:
            first = setup_logger("test_shared_a", log_dir=tmpdir)
            second = setup_logger("test_shared_b", log_dir=tmpdir)

            def file_handlers(logger: logging.Logger):
                return [
                    h
                    for h in logger.handlers
                    if getattr(h, "baseFilename", "").startswith(
                        os.path.abspath(tmpdir)
                    )
                ]

            first_handlers = file_handlers(first)
            second_handlers = file_handlers(second)
            assert first_handlers and second_handlers
            assert first_handlers[0] is second_handlers[0]
        finally:
            for name in ("test_shared_a", "test_shared_b"):
                logger = logging.getLogger(name)
                for handler in list(logger.handlers):
                    handler.flush()
                    logger.removeHandler(handler)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_repeated_setup_does_not_duplicate_handlers(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="shared-handler-dup-")
        try:
            logger = setup_logger("test_shared_dup", log_dir=tmpdir)
            count = len(logger.handlers)
            setup_logger("test_shared_dup", log_dir=tmpdir)
            assert len(logger.handlers) == count
        finally:
            logger = logging.getLogger("test_shared_dup")
            for handler in list(logger.handlers):
                handler.flush()
                logger.removeHandler(handler)
            shutil.rmtree(tmpdir, ignore_errors=True)
