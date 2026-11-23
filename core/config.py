"""Configuration management using dataclasses and python-dotenv."""

import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    """Application configuration loaded from environment variables."""

    app_name: str = field(default="Book Processor")
    version: str = field(default="0.1.0")
    environment: str = field(default="Development")
    log_level: str = field(default="INFO")
    data_directory: str = field(default="data")
    log_directory: str = field(default="logs")
    temp_directory: str = field(default="data/temp")
    google_drive_folder_id: str = field(default="")
    google_sheet_id: str = field(default="")
    google_service_account_file: str = field(default="")
    ocr_language: str = field(default="en,fr")
    ocr_use_gpu: bool = field(default=False)
    ocr_confidence_threshold: float = field(default=0.5)
    google_books_api_key: str = field(default="")
    google_books_timeout: int = field(default=15)
    open_library_enabled: bool = field(default=True)
    open_library_timeout: int = field(default=10)
    confidence_threshold: float = field(default=0.85)
    confidence_manual_review_threshold: float = field(default=0.50)
    confidence_rejected_threshold: float = field(default=0.50)
    _required_fields: tuple = field(default=("GOOGLE_DRIVE_FOLDER_ID",), repr=False)

    # Debug artifact retention
    debug_retention_days: int = field(default=3)
    google_sheets_credentials: str = field(default="")
    google_sheets_tab_name: str = field(default="Sheet1")
    covers_dir: str = field(default="data/covers")
    cover_download_timeout: int = field(default=10)
    cover_min_width: int = field(default=200)
    cover_min_height: int = field(default=200)
    export_max_retries: int = field(default=3)
    export_retry_delay: int = field(default=60)
    cache_enabled: bool = field(default=True)
    cache_ttl_days: int = field(default=7)
    exa_api_key: str = field(default="")
    gemini_api_key: str = field(default="")
    gemini_recovery_enabled: bool = field(default=False)
    gemini_daily_max_requests: int = field(default=50)
    gemini_rpm_limit: int = field(default=15)
    # Jobs stuck in PROCESSING longer than this are recovered on restart.
    # Observed jobs finish in <=4 s (fast path) and <=35 s with OCR; a real
    # Baidu OCR + lookup + cover download run stays well under 3 minutes.
    # 15 minutes is >5x the worst observed duration and far below the old
    # 2-hour window that made crash recovery feel broken to operators.
    processing_timeout_seconds: int = field(default=900)
    retry_initial_delay_seconds: float = field(default=5.0)
    retry_max_delay_seconds: float = field(default=300.0)
    retry_backoff_factor: float = field(default=2.0)
    image_max_file_size_mb: float = field(default=20.0)
    image_max_width: int = field(default=4000)
    image_max_height: int = field(default=4000)

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config instance from environment variables."""
        return cls(
            app_name=os.getenv("APP_NAME", "Book Processor"),
            version=os.getenv("VERSION", "0.1.0"),
            environment=os.getenv("ENVIRONMENT", "Development"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            data_directory=os.getenv("DATA_DIRECTORY", "data"),
            log_directory=os.getenv("LOG_DIRECTORY", "logs"),
            temp_directory=os.getenv("TEMP_DIRECTORY", "data/temp"),
            google_drive_folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID", ""),
            google_sheet_id=os.getenv("GOOGLE_SHEET_ID", ""),
            google_service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
            ocr_language=os.getenv("OCR_LANGUAGE", "en,fr,ru"),
            ocr_use_gpu=os.getenv("OCR_USE_GPU", "false").lower() == "true",
            ocr_confidence_threshold=float(
                os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.5")
            ),
            google_books_api_key=os.getenv("GOOGLE_BOOKS_API_KEY", ""),
            exa_api_key=os.getenv("EXA_API_KEY", ""),
            google_books_timeout=int(os.getenv("GOOGLE_BOOKS_TIMEOUT", "15")),
            open_library_enabled=os.getenv("OPEN_LIBRARY_ENABLED", "true").lower()
            == "true",
            open_library_timeout=int(os.getenv("OPEN_LIBRARY_TIMEOUT", "10")),
            confidence_threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.85")),
            confidence_manual_review_threshold=float(
                os.getenv("CONFIDENCE_MANUAL_REVIEW_THRESHOLD", "0.50")
            ),
            confidence_rejected_threshold=float(
                os.getenv("CONFIDENCE_REJECTED_THRESHOLD", "0.50")
            ),
            debug_retention_days=int(os.getenv("DEBUG_RETENTION_DAYS", "3")),
            google_sheets_credentials=os.getenv(
                "GOOGLE_SHEETS_CREDENTIALS", "credentials/service_account.json"
            ),
            google_sheets_tab_name=os.getenv("GOOGLE_SHEETS_TAB_NAME", "Sheet1"),
            covers_dir=os.getenv("COVERS_DIR", "data/covers"),
            cover_download_timeout=int(os.getenv("COVER_DOWNLOAD_TIMEOUT", "10")),
            cover_min_width=int(os.getenv("COVER_MIN_WIDTH", "200")),
            cover_min_height=int(os.getenv("COVER_MIN_HEIGHT", "200")),
            export_max_retries=int(os.getenv("EXPORT_MAX_RETRIES", "3")),
            export_retry_delay=int(os.getenv("EXPORT_RETRY_DELAY", "60")),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_recovery_enabled=os.getenv(
                "GEMINI_RECOVERY_ENABLED", "false"
            ).lower()
            == "true",
            gemini_daily_max_requests=int(os.getenv("GEMINI_DAILY_MAX_REQUESTS", "50")),
            gemini_rpm_limit=int(os.getenv("GEMINI_RPM_LIMIT", "15")),
            processing_timeout_seconds=int(
                os.getenv("PROCESSING_TIMEOUT_SECONDS", "900")
            ),
            retry_initial_delay_seconds=float(
                os.getenv("RETRY_INITIAL_DELAY_SECONDS", "5.0")
            ),
            retry_max_delay_seconds=float(
                os.getenv("RETRY_MAX_DELAY_SECONDS", "300.0")
            ),
            retry_backoff_factor=float(
                os.getenv("RETRY_BACKOFF_FACTOR", "2.0")
            ),
            image_max_file_size_mb=float(
                os.getenv("IMAGE_MAX_FILE_SIZE_MB", "20.0")
            ),
            image_max_width=int(os.getenv("IMAGE_MAX_WIDTH", "4000")),
            image_max_height=int(os.getenv("IMAGE_MAX_HEIGHT", "4000")),
        )

    def validate(self) -> None:
        """Validate that required fields are present.

        Raises ConfigurationError if missing.
        """
        from core.exceptions import ConfigurationError

        missing = []
        for field_name in self._required_fields:
            value = getattr(self, field_name.lower(), "")
            if not value:
                missing.append(field_name)

        if missing:
            raise ConfigurationError(
                "Missing required configuration: "
                f"{', '.join(missing)}. "
                "See docs/environment.md for details."
            )

    def get_status_report(self) -> Dict[str, List[str]]:
        """Return a structured status report of configuration."""
        present = []
        missing = []
        future = []

        field_display = {
            "app_name": "Application Name",
            "version": "Version",
            "environment": "Environment",
            "log_level": "Logging",
            "data_directory": "Data Directory",
            "log_directory": "Log Directory",
            "temp_directory": "Temp Directory",
            "google_drive_folder_id": "Google Drive Folder ID",
            "google_sheet_id": "Google Sheet ID",
            "google_service_account_file": "Google Credentials File",
            "ocr_language": "OCR Language",
            "ocr_use_gpu": "OCR GPU Acceleration",
            "ocr_confidence_threshold": "OCR Confidence Threshold",
            "google_books_api_key": "Google Books API Key",
            "google_books_timeout": "Google Books Timeout",
            "open_library_enabled": "Open Library API",
            "open_library_timeout": "Open Library Timeout",
            "confidence_threshold": "Confidence Threshold",
            "confidence_manual_review_threshold": "Confidence Manual Review Threshold",
            "confidence_rejected_threshold": "Confidence Rejected Threshold",
            "debug_retention_days": "Debug Retention Days",
            "google_sheets_credentials": "Google Sheets Credentials",
            "google_sheets_tab_name": "Google Sheets Tab Name",
            "covers_dir": "Covers Directory",
            "cover_download_timeout": "Cover Download Timeout",
            "cover_min_width": "Cover Min Width",
            "cover_min_height": "Cover Min Height",
            "export_max_retries": "Export Max Retries",
            "export_retry_delay": "Export Retry Delay",
            "cache_enabled": "Result Cache",
            "cache_ttl_days": "Cache TTL (days)",
            "gemini_api_key": "Gemini API Key",
            "gemini_recovery_enabled": "Gemini Recovery",
            "gemini_daily_max_requests": "Gemini Daily Max Requests",
            "gemini_rpm_limit": "Gemini RPM Limit",
            "processing_timeout_seconds": "Processing Timeout Seconds",
            "retry_initial_delay_seconds": "Retry Initial Delay Seconds",
            "retry_max_delay_seconds": "Retry Max Delay Seconds",
            "retry_backoff_factor": "Retry Backoff Factor",
            "image_max_file_size_mb": "Image Max File Size MB",
            "image_max_width": "Image Max Width",
            "image_max_height": "Image Max Height",
        }

        future_fields = {"ocr_language", "google_books_api_key"}

        # Switch settings are always configured once a default exists; a False
        # value means "deliberately off", not "missing". Reporting them under
        # "Missing" told operators an optional feature was misconfigured.
        boolean_fields = {
            "ocr_use_gpu",
            "open_library_enabled",
            "gemini_recovery_enabled",
            "cache_enabled",
        }

        # A credential that only an optional feature needs is not a missing
        # requirement while that feature is switched off.
        optional_with = {"gemini_api_key": "gemini_recovery_enabled"}

        for attr, label in field_display.items():
            value = getattr(self, attr, "")
            if attr in boolean_fields:
                state = "enabled" if value else "disabled"
                present.append(f"  {label} ({state})")
                continue
            if value:
                present.append(f"  {label}")
            elif attr in optional_with and not getattr(
                self, optional_with[attr], False
            ):
                future.append(f"  {label} (optional, feature off)")
            elif attr in future_fields:
                future.append(f"  {label}")
            else:
                missing.append(f"  {label}")

        return {"present": present, "missing": missing, "future": future}
