"""Test configuration loading."""

import os
import tempfile

import pytest

from core.config import Config
from core.exceptions import ConfigurationError


class TestConfigLoadsCorrectly:
    """Test that configuration loads correctly from environment variables."""

    def test_default_config(self) -> None:
        """Test that Config loads with default values when no env vars are set."""
        with tempfile.TemporaryDirectory():
            env = {
                "APP_NAME": "Book Processor",
                "VERSION": "0.1.0",
                "ENVIRONMENT": "Development",
                "LOG_LEVEL": "INFO",
                "DATA_DIRECTORY": "data",
                "LOG_DIRECTORY": "logs",
                "TEMP_DIRECTORY": "data/temp",
                "GOOGLE_DRIVE_FOLDER_ID": "test_folder_123",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "GOOGLE_SHEET_ID": "",
                "OCR_LANGUAGE": "en",
                "GOOGLE_BOOKS_API_KEY": "",
            }
            old_env = {k: os.environ.pop(k, None) for k in env}
            try:
                os.environ.update(env)
                config = Config.from_env()
                assert config.app_name == "Book Processor"
                assert config.version == "0.1.0"
                assert config.environment == "Development"
                assert config.log_level == "INFO"
            finally:
                for k in env:
                    os.environ.pop(k, None)
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v

    def test_validate_passes_with_all_defaults_set(self) -> None:
        """Test that validation passes when all fields have their default values."""
        with tempfile.TemporaryDirectory():
            env = {
                "APP_NAME": "Book Processor",
                "VERSION": "0.1.0",
                "ENVIRONMENT": "Development",
                "LOG_LEVEL": "INFO",
                "DATA_DIRECTORY": "data",
                "LOG_DIRECTORY": "logs",
                "TEMP_DIRECTORY": "data/temp",
                "GOOGLE_DRIVE_FOLDER_ID": "test_folder_123",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "GOOGLE_SHEET_ID": "",
                "OCR_LANGUAGE": "en",
                "GOOGLE_BOOKS_API_KEY": "",
            }
            old_env = {k: os.environ.pop(k, None) for k in env}
            try:
                os.environ.update(env)
                config = Config.from_env()
                # All fields have defaults so validation should pass.
                config.validate()
            finally:
                for k in env:
                    os.environ.pop(k, None)
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v

    def test_validate_fails_when_required_fields_missing(self) -> None:
        """Test validation raises ConfigurationError when required fields missing."""
        with tempfile.TemporaryDirectory():
            env = {
                "APP_NAME": "Book Processor",
                "VERSION": "0.1.0",
                "ENVIRONMENT": "Development",
                "LOG_LEVEL": "INFO",
                "DATA_DIRECTORY": "data",
                "LOG_DIRECTORY": "logs",
                "TEMP_DIRECTORY": "data/temp",
                "GOOGLE_DRIVE_FOLDER_ID": "",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "GOOGLE_SHEET_ID": "",
                "OCR_LANGUAGE": "en",
                "GOOGLE_BOOKS_API_KEY": "",
            }
            old_env = {k: os.environ.pop(k, None) for k in env}
            try:
                os.environ.update(env)
                config = Config.from_env()
                with pytest.raises(ConfigurationError):
                    config.validate()
            finally:
                for k in env:
                    os.environ.pop(k, None)
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v

    def test_get_status_report(self) -> None:
        """Test that get_status_report returns a structured dict."""
        with tempfile.TemporaryDirectory():
            env = {
                "APP_NAME": "Book Processor",
                "VERSION": "0.1.0",
                "ENVIRONMENT": "Development",
                "LOG_LEVEL": "INFO",
                "DATA_DIRECTORY": "data",
                "LOG_DIRECTORY": "logs",
                "TEMP_DIRECTORY": "data/temp",
                "GOOGLE_DRIVE_FOLDER_ID": "test_folder_123",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "GOOGLE_SHEET_ID": "",
                "OCR_LANGUAGE": "en",
                "GOOGLE_BOOKS_API_KEY": "",
            }
            old_env = {k: os.environ.pop(k, None) for k in env}
            try:
                os.environ.update(env)
                config = Config.from_env()
                report = config.get_status_report()
                assert "present" in report
                assert "missing" in report
                assert "future" in report
            finally:
                for k in env:
                    os.environ.pop(k, None)
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v


class TestEnvironmentOverridesDefaults:
    """Test that environment variables override default values."""

    def test_env_overrides(self) -> None:
        """Test that environment variables override default configuration values."""
        with tempfile.TemporaryDirectory():
            env = {
                "APP_NAME": "Custom Processor",
                "VERSION": "0.2.0",
                "ENVIRONMENT": "Staging",
                "LOG_LEVEL": "DEBUG",
                "DATA_DIRECTORY": "mydata",
                "LOG_DIRECTORY": "mylogs",
                "TEMP_DIRECTORY": "mytemp",
                "GOOGLE_DRIVE_FOLDER_ID": "test_folder_123",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "GOOGLE_SHEET_ID": "",
                "OCR_LANGUAGE": "fr",
                "GOOGLE_BOOKS_API_KEY": "",
            }
            old_env = {k: os.environ.pop(k, None) for k in env}
            try:
                os.environ.update(env)
                config = Config.from_env()
                assert config.app_name == "Custom Processor"
                assert config.version == "0.2.0"
                assert config.environment == "Staging"
                assert config.log_level == "DEBUG"
                assert config.data_directory == "mydata"
                assert config.log_directory == "mylogs"
                assert config.temp_directory == "mytemp"
                assert config.ocr_language == "fr"
            finally:
                for k in env:
                    os.environ.pop(k, None)
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v

    def test_confidence_thresholds_from_env(self) -> None:
        """Test that confidence thresholds are loaded from env vars."""
        with tempfile.TemporaryDirectory():
            env = {
                "APP_NAME": "Book Processor",
                "VERSION": "0.1.0",
                "ENVIRONMENT": "Development",
                "LOG_LEVEL": "INFO",
                "DATA_DIRECTORY": "data",
                "LOG_DIRECTORY": "logs",
                "TEMP_DIRECTORY": "data/temp",
                "GOOGLE_DRIVE_FOLDER_ID": "test_folder_123",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "GOOGLE_SHEET_ID": "",
                "OCR_LANGUAGE": "en",
                "GOOGLE_BOOKS_API_KEY": "",
                "CONFIDENCE_THRESHOLD": "0.90",
                "CONFIDENCE_MANUAL_REVIEW_THRESHOLD": "0.60",
                "CONFIDENCE_REJECTED_THRESHOLD": "0.40",
            }
            old_env = {k: os.environ.pop(k, None) for k in env}
            try:
                os.environ.update(env)
                config = Config.from_env()
                assert config.confidence_threshold == 0.90
                assert config.confidence_manual_review_threshold == 0.60
                assert config.confidence_rejected_threshold == 0.40
            finally:
                for k in env:
                    os.environ.pop(k, None)
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v

    def test_confidence_thresholds_default(self) -> None:
        """Test that confidence thresholds default correctly."""
        with tempfile.TemporaryDirectory():
            env = {
                "APP_NAME": "Book Processor",
                "VERSION": "0.1.0",
                "ENVIRONMENT": "Development",
                "LOG_LEVEL": "INFO",
                "DATA_DIRECTORY": "data",
                "LOG_DIRECTORY": "logs",
                "TEMP_DIRECTORY": "data/temp",
                "GOOGLE_DRIVE_FOLDER_ID": "test_folder_123",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "GOOGLE_SHEET_ID": "",
                "OCR_LANGUAGE": "en",
                "GOOGLE_BOOKS_API_KEY": "",
            }
            old_env = {k: os.environ.pop(k, None) for k in env}
            try:
                os.environ.update(env)
                config = Config.from_env()
                assert config.confidence_threshold == 0.85
                assert config.confidence_manual_review_threshold == 0.50
                assert config.confidence_rejected_threshold == 0.50
            finally:
                for k in env:
                    os.environ.pop(k, None)
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v
