"""Configuration for Milestone 5 export pipeline.

This dataclass holds configuration only for the publication and
enrichment pipeline. It is loaded from environment variables via
``ExportConfig.from_env()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class ExportConfig:
    """Configuration for the export pipeline (Milestone 5).

    Fields:
        google_sheets_id: Google Sheets document ID (from URL).
        google_sheets_credentials: Path to service account JSON key.
        google_sheets_tab_name: Worksheet tab name within the spreadsheet.
        covers_dir: Directory to store downloaded cover images.
        covers_timeout: HTTP download timeout in seconds.
        cover_min_width: Minimum cover image width in pixels.
        cover_min_height: Minimum cover image height in pixels.
        export_max_retries: Maximum retry attempts for failed exports.
        export_retry_delay: Seconds to wait between retry cycles.
        data_directory: Base data directory (for resolving relative paths).
        google_books_api_key: API key for Google Books lookups.
        open_library_enabled: Whether Open Library lookups are enabled.
    """

    google_sheets_id: str = field(
        default_factory=lambda: os.getenv("GOOGLE_SHEET_ID", "")
    )
    google_sheets_credentials: str = field(
        default_factory=lambda: os.getenv("GOOGLE_SHEETS_CREDENTIALS", "")
    )
    google_sheets_tab_name: str = field(
        default_factory=lambda: os.getenv("GOOGLE_SHEETS_TAB_NAME", "Sheet1")
    )
    covers_dir: str = field(
        default_factory=lambda: os.getenv("COVERS_DIR", "data/covers")
    )
    covers_timeout: int = field(
        default_factory=lambda: int(os.getenv("COVER_DOWNLOAD_TIMEOUT", "10"))
    )
    cover_min_width: int = field(
        default_factory=lambda: int(os.getenv("COVER_MIN_WIDTH", "200"))
    )
    cover_min_height: int = field(
        default_factory=lambda: int(os.getenv("COVER_MIN_HEIGHT", "200"))
    )
    export_max_retries: int = field(
        default_factory=lambda: int(os.getenv("EXPORT_MAX_RETRIES", "3"))
    )
    export_retry_delay: int = field(
        default_factory=lambda: int(os.getenv("EXPORT_RETRY_DELAY", "60"))
    )
    data_directory: str = field(
        default_factory=lambda: os.getenv("DATA_DIRECTORY", "data")
    )
    google_books_api_key: str = field(
        default_factory=lambda: os.getenv("GOOGLE_BOOKS_API_KEY", "")
    )
    open_library_enabled: bool = field(
        default_factory=lambda: os.getenv("OPEN_LIBRARY_ENABLED", "true").lower()
        == "true"
    )

    @classmethod
    def from_env(cls) -> "ExportConfig":
        """Build an ExportConfig from environment variables."""
        return cls(
            google_sheets_id=os.getenv("GOOGLE_SHEET_ID", ""),
            google_sheets_credentials=os.getenv("GOOGLE_SHEETS_CREDENTIALS", ""),
            google_sheets_tab_name=os.getenv("GOOGLE_SHEETS_TAB_NAME", "Sheet1"),
            covers_dir=os.getenv("COVERS_DIR", "data/covers"),
            covers_timeout=int(os.getenv("COVER_DOWNLOAD_TIMEOUT", "10")),
            cover_min_width=int(os.getenv("COVER_MIN_WIDTH", "200")),
            cover_min_height=int(os.getenv("COVER_MIN_HEIGHT", "200")),
            export_max_retries=int(os.getenv("EXPORT_MAX_RETRIES", "3")),
            export_retry_delay=int(os.getenv("EXPORT_RETRY_DELAY", "60")),
            data_directory=os.getenv("DATA_DIRECTORY", "data"),
            google_books_api_key=os.getenv("GOOGLE_BOOKS_API_KEY", ""),
            open_library_enabled=os.getenv("OPEN_LIBRARY_ENABLED", "true").lower()
            == "true",
        )

    def get_status_report(self) -> dict:
        """Return a structured status report of export configuration."""
        present: List[str] = []
        missing: List[str] = []
        future: List[str] = []

        field_display = {
            "google_sheets_id": "Google Sheets ID",
            "google_sheets_credentials": "Google Sheets Credentials",
            "google_sheets_tab_name": "Sheets Tab Name",
            "covers_dir": "Covers Directory",
            "covers_timeout": "Cover Download Timeout",
            "cover_min_width": "Cover Min Width",
            "cover_min_height": "Cover Min Height",
            "export_max_retries": "Export Max Retries",
            "export_retry_delay": "Export Retry Delay",
        }

        for attr, label in field_display.items():
            value = getattr(self, attr, "")
            if value:
                present.append(f"  {label}")
            else:
                missing.append(f"  {label}")

        return {"present": present, "missing": missing, "future": future}
