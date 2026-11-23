"""Google Drive service for file synchronization."""

import io
import os
import time
from typing import List, Optional, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from core.config import Config
from core.logger import setup_logger
from models.drive_file import DriveFile

logger = setup_logger(__name__)


class DriveService:
    """Service for interacting with Google Drive API."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._service = None

    def authenticate(self) -> bool:
        """Authenticate using the service account credentials."""
        creds_path = self._config.google_service_account_file
        if not creds_path or not os.path.exists(creds_path):
            logger.error("Service account file not found: %s", creds_path)
            return False
        try:
            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            self._service = build("drive", "v3", credentials=credentials)
            logger.info("Authenticated with Google Drive successfully")
            return True
        except Exception as exc:
            logger.error("Authentication failed: %s", exc)
            return False

    def list_files(
        self,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Tuple[List[DriveFile], Optional[str]]:
        """List all files in the configured folder with pagination support."""
        if self._service is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        folder_id = self._config.google_drive_folder_id
        query = f"'{folder_id}' in parents and trashed = false"

        try:
            results = (
                self._service.files()
                .list(
                    q=query,
                    pageSize=page_size,
                    pageToken=page_token,
                    fields=(
                        "nextPageToken,"
                        "files("
                        "id,name,mimeType,modifiedTime,size,md5Checksum,webViewLink"
                        ")"
                    ),
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Failed to list files from Drive: %s", exc)
            raise

        files = [
            DriveFile(
                file_id=f["id"],
                name=f["name"],
                mime_type=f["mimeType"],
                modified_time=f["modifiedTime"],
                size=int(f.get("size")) if f.get("size") else None,
                md5_checksum=f.get("md5Checksum"),
                web_view_link=f.get("webViewLink"),
            )
            for f in results.get("files", [])
        ]

        next_token = results.get("nextPageToken")
        logger.info(
            "Found %d files in Drive (page token: %s)", len(files), bool(next_token)
        )
        return files, next_token

    def list_all_files(self) -> List[DriveFile]:
        """List all files in the configured folder, handling pagination."""
        all_files: List[DriveFile] = []
        page_token: Optional[str] = None

        while True:
            files, next_token = self.list_files(page_token=page_token)
            all_files.extend(files)
            if not next_token:
                break
            page_token = next_token

        return all_files

    def get_file_metadata(self, file_id: str) -> Optional[DriveFile]:
        """Fetch metadata for a single file by ID.

        Uses the Drive API files.get endpoint to retrieve the full
        DriveFile metadata (name, mime_type, modified_time, size, etc.)
        needed for downloading.
        """
        if self._service is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        try:
            result = (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,modifiedTime,size,md5Checksum,webViewLink",
                )
                .execute()
            )
            return DriveFile(
                file_id=result["id"],
                name=result["name"],
                mime_type=result["mimeType"],
                modified_time=result["modifiedTime"],
                size=int(result["size"]) if result.get("size") else None,
                md5_checksum=result.get("md5Checksum"),
                web_view_link=result.get("webViewLink"),
            )
        except HttpError as exc:
            logger.error(
                "Could not fetch metadata for Drive file %s: %s",
                file_id,
                exc,
            )
            return None

    def download_file(
        self,
        drive_file: DriveFile,
        destination_path: str,
        max_retries: int = 3,
    ) -> bool:
        """Download a file from Google Drive with retry logic for transient failures."""
        if self._service is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        for attempt in range(1, max_retries + 1):
            try:
                request = self._service.files().get_media(fileId=drive_file.file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status:
                        logger.debug(
                            "Downloading %s: %d%%",
                            drive_file.name,
                            status.progress() * 100,
                        )

                with open(destination_path, "wb") as f:
                    f.write(fh.getvalue())

                logger.info(
                    "Downloaded: %s (%d bytes)", drive_file.name, drive_file.size or 0
                )
                return True

            except HttpError as exc:
                if (
                    exc.resp.status in (429, 500, 502, 503, 504)
                    and attempt < max_retries
                ):
                    wait_time = 2**attempt
                    logger.warning(
                        "Transient error downloading %s "
                        "(attempt %d/%d), retrying in %ds: %s",
                        drive_file.name,
                        attempt,
                        max_retries,
                        wait_time,
                        exc,
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        "Failed to download %s after %d attempts: %s",
                        drive_file.name,
                        attempt,
                        exc,
                    )
                    raise
            except Exception as exc:
                logger.error(
                    "Unexpected error downloading %s: %s", drive_file.name, exc
                )
                raise

        return False

    def get_file_checksum(self, drive_file: DriveFile) -> Optional[str]:
        """Return the MD5 checksum for a Drive file if available."""
        return drive_file.md5_checksum
