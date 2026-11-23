"""Cover download service for Milestone 5.

Downloads cover images, validates them, and tracks downloads
in the cover_downloads table. Tries multiple URLs until one
passes validation.
"""

from __future__ import annotations

import hashlib
import os
from typing import List, Optional, Tuple

from core.logger import setup_logger
from models.book_metadata import BookMetadata
from models.cover_download import CoverDownload, CoverDownloadStatus
from repositories.cover_repository import CoverRepository

logger = setup_logger(__name__)


class CoverDownloadService:
    """Downloads, validates, and persists cover images.

    Responsibilities:
    - Try multiple cover URLs until one passes validation.
    - Download cover image from a URL with timeout and retry.
    - Validate image format and dimensions.
    - Compute SHA256 checksum for deduplication.
    - Store cover in data/covers/ with safe filename.
    - Track download in CoverRepository.

    Attributes:
        _cover_repo: Repository for persist cover download records.
        _covers_dir: Directory to store downloaded cover images.
        _timeout: HTTP download timeout in seconds.
        _min_width: Minimum acceptable image width in pixels.
        _min_height: Minimum acceptable image height in pixels.
        _max_file_size: Maximum acceptable file size in bytes.
    """

    # URL fragments that identify placeholder / promotional artwork rather
    # than a real book cover.
    PLACEHOLDER_URL_TOKENS = (
        "kindle",
        "store-link",
        "store_link",
        "qr-code",
        "qr_code",
        "qr-store",
        "placeholder",
        "no-cover",
        "no_cover",
        "nocover",
        "missing-cover",
        "spacer",
        "transparent",
        "1x1",
        "pixel.gif",
    )

    def __init__(
        self,
        cover_repo: CoverRepository,
        covers_dir: str = "data/covers",
        timeout: int = 10,
        min_width: int = 200,
        min_height: int = 200,
        max_file_size: int = 50 * 1024 * 1024,  # 50 MB
    ) -> None:
        self._cover_repo = cover_repo
        self._download_count = 0
        self._covers_dir = covers_dir
        self._timeout = timeout
        self._min_width = min_width
        self._min_height = min_height
        self._max_file_size = max_file_size
        os.makedirs(covers_dir, exist_ok=True)

    @property
    def download_count(self) -> int:
        """Cover download attempts (for the run summary)."""
        return self._download_count

    def reset_stats(self) -> None:
        """Reset the per-cycle download counter."""
        self._download_count = 0

    def download_cover(
        self,
        drive_file_id: str,
        job_id: Optional[str],
        cover_url: str,
        metadata: Optional[BookMetadata] = None,
    ) -> CoverDownload:
        """Download and validate a cover image.

        Args:
            drive_file_id: Drive file ID for naming and tracking.
            job_id: Original processing job ID.
            cover_url: URL to download the cover from.
            metadata: Optional BookMetadata for logging context.

        Returns:
            CoverDownload record (COMPLETED on success, FAILED on error).
        """
        self._download_count += 1
        # Check if cover already exists for this drive_file_id
        existing = self._cover_repo.get_cover_by_drive_file_id(drive_file_id)
        if existing and existing.status == CoverDownloadStatus.COMPLETED:
            logger.info(
                "Cover already downloaded for drive_file_id=%s -- skipping",
                drive_file_id,
            )
            return existing

        # Try the primary URL first, then fall back to thumbnail_url
        # and title+author derived URLs from metadata.
        urls_to_try = [cover_url] if cover_url else []

        if metadata:
            if metadata.thumbnail_url and metadata.thumbnail_url not in urls_to_try:
                urls_to_try.append(metadata.thumbnail_url)

        # Try each URL until one passes validation
        for i, url in enumerate(urls_to_try):
            if not url:
                continue

            if i > 0:
                logger.info(
                    "Trying fallback cover URL (%d/%d) for " "drive_file_id=%s",
                    i + 1,
                    len(urls_to_try),
                    drive_file_id,
                )
            else:
                logger.info(
                    "Downloading cover for drive_file_id=%s",
                    drive_file_id,
                )

            try:
                raw_bytes = self._fetch_image(url, self._timeout)
            except Exception as exc:
                logger.error(
                    "Cover download failed for drive_file_id=%s: %s",
                    drive_file_id,
                    type(exc).__name__,
                )
                if i == len(urls_to_try) - 1:
                    # Last URL failed -- record failure
                    self._record_failure(
                        drive_file_id,
                        job_id,
                        cover_url,
                        type(exc).__name__,
                    )
                    return CoverDownload(
                        drive_file_id=drive_file_id,
                        job_id=job_id,
                        cover_url=cover_url,
                        local_path="",
                        status=CoverDownloadStatus.FAILED,
                        error_message=type(exc).__name__,
                    )
                continue

            # Validate image
            img_info = self._validate_image(raw_bytes, url)
            if img_info is None:
                logger.warning(
                    "Cover validation failed for URL %d for " "drive_file_id=%s",
                    i + 1,
                    drive_file_id,
                )
                if i == len(urls_to_try) - 1:
                    self._record_failure(
                        drive_file_id,
                        job_id,
                        cover_url,
                        "Invalid image format or dimensions below threshold",
                    )
                    return CoverDownload(
                        drive_file_id=drive_file_id,
                        job_id=job_id,
                        cover_url=cover_url,
                        local_path="",
                        status=CoverDownloadStatus.FAILED,
                        error_message=(
                            "Invalid image format or " "dimensions below threshold"
                        ),
                    )
                continue

            width, height, file_size, checksum = img_info

            # Generate safe filename
            ext = self._extract_extension(url, raw_bytes)
            filename = f"cover_{drive_file_id}.{ext}"
            local_path = os.path.join(self._covers_dir, filename)

            # Check checksum for deduplication
            existing_by_checksum = self._cover_repo.get_by_checksum(checksum)
            if existing_by_checksum and existing_by_checksum.local_path:
                logger.info(
                    "Cover already exists with same checksum for " "drive_file_id=%s",
                    drive_file_id,
                )
                local_path = existing_by_checksum.local_path
            else:
                with open(local_path, "wb") as f:
                    f.write(raw_bytes)
                logger.info(
                    "Cover saved to %s (%dx%d, %d bytes)",
                    "data/covers/",
                    width,
                    height,
                    file_size,
                )

            # Persist record
            self._cover_repo.insert_cover(
                drive_file_id=drive_file_id,
                job_id=job_id,
                cover_url=url,
                local_path=local_path,
                checksum=checksum,
                width=width,
                height=height,
                file_size=file_size,
                status=CoverDownloadStatus.COMPLETED.value,
            )

            logger.info(
                "Cover download completed for drive_file_id=%s " "(%dx%d, %d bytes)",
                drive_file_id,
                width,
                height,
                file_size,
            )

            return CoverDownload(
                drive_file_id=drive_file_id,
                job_id=job_id,
                cover_url=url,
                local_path=local_path,
                checksum=checksum,
                width=width,
                height=height,
                file_size=file_size,
                resolution=f"{width}x{height}",
                downloaded_at=None,
                status=CoverDownloadStatus.COMPLETED,
            )

        # All URLs exhausted
        error_msg = "All cover URLs exhausted without valid image"
        self._record_failure(drive_file_id, job_id, cover_url, error_msg)
        return CoverDownload(
            drive_file_id=drive_file_id,
            job_id=job_id,
            cover_url=cover_url,
            local_path="",
            status=CoverDownloadStatus.FAILED,
            error_message=error_msg,
        )

    def download_covers(
        self,
        drive_file_id: str,
        job_id: Optional[str],
        cover_urls: List[str],
        metadata: Optional[BookMetadata] = None,
    ) -> CoverDownload:
        """Try multiple cover URLs until one passes validation.

        Args:
            drive_file_id: Drive file ID for naming and tracking.
            job_id: Original processing job ID.
            cover_urls: List of URLs to try in priority order.
            metadata: Optional BookMetadata for logging context.

        Returns:
            CoverDownload record (COMPLETED on success, FAILED if all fail).
        """
        if not cover_urls:
            return CoverDownload(
                drive_file_id=drive_file_id,
                job_id=job_id,
                cover_url="",
                local_path="",
                status=CoverDownloadStatus.FAILED,
                error_message="No cover URLs provided",
            )

        primary_url = cover_urls[0]

        # Check if cover already exists for this drive_file_id
        existing = self._cover_repo.get_cover_by_drive_file_id(drive_file_id)
        if existing and existing.status == CoverDownloadStatus.COMPLETED:
            logger.info(
                "Cover already downloaded for drive_file_id=%s -- " "skipping",
                drive_file_id,
            )
            return existing

        for i, url in enumerate(cover_urls):
            if not url:
                continue

            if i > 0:
                logger.info(
                    "Trying fallback cover URL (%d/%d) for " "drive_file_id=%s",
                    i + 1,
                    len(cover_urls),
                    drive_file_id,
                )
            else:
                logger.info(
                    "Downloading cover for drive_file_id=%s",
                    drive_file_id,
                )

            try:
                raw_bytes = self._fetch_image(url, self._timeout)
            except Exception as exc:
                logger.error(
                    "Cover download failed for drive_file_id=%s: %s",
                    drive_file_id,
                    type(exc).__name__,
                )
                continue

            # Validate image
            img_info = self._validate_image(raw_bytes, url)
            if img_info is None:
                logger.warning(
                    "Cover validation failed for URL %d for " "drive_file_id=%s",
                    i + 1,
                    drive_file_id,
                )
                continue

            width, height, file_size, checksum = img_info

            # Generate safe filename
            ext = self._extract_extension(url, raw_bytes)
            filename = f"cover_{drive_file_id}.{ext}"
            local_path = os.path.join(self._covers_dir, filename)

            # Check checksum for deduplication
            existing_by_checksum = self._cover_repo.get_by_checksum(checksum)
            if existing_by_checksum and existing_by_checksum.local_path:
                logger.info(
                    "Cover already exists with same checksum for " "drive_file_id=%s",
                    drive_file_id,
                )
                local_path = existing_by_checksum.local_path
            else:
                with open(local_path, "wb") as f:
                    f.write(raw_bytes)
                logger.info(
                    "Cover saved to %s (%dx%d, %d bytes)",
                    "data/covers/",
                    width,
                    height,
                    file_size,
                )

            # Persist record
            self._cover_repo.insert_cover(
                drive_file_id=drive_file_id,
                job_id=job_id,
                cover_url=url,
                local_path=local_path,
                checksum=checksum,
                width=width,
                height=height,
                file_size=file_size,
                status=CoverDownloadStatus.COMPLETED.value,
            )

            logger.info(
                "Cover download completed for drive_file_id=%s " "(%dx%d, %d bytes)",
                drive_file_id,
                width,
                height,
                file_size,
            )

            return CoverDownload(
                drive_file_id=drive_file_id,
                job_id=job_id,
                cover_url=url,
                local_path=local_path,
                checksum=checksum,
                width=width,
                height=height,
                file_size=file_size,
                resolution=f"{width}x{height}",
                downloaded_at=None,
                status=CoverDownloadStatus.COMPLETED,
            )

        # All URLs exhausted
        error_msg = "All cover URLs exhausted without valid image"
        self._record_failure(drive_file_id, job_id, primary_url, error_msg)
        return CoverDownload(
            drive_file_id=drive_file_id,
            job_id=job_id,
            cover_url=primary_url,
            local_path="",
            status=CoverDownloadStatus.FAILED,
            error_message=error_msg,
        )

    def _record_failure(
        self,
        drive_file_id: str,
        job_id: Optional[str],
        cover_url: str,
        error_message: str,
    ) -> None:
        """Record a cover download failure in the repository."""
        try:
            self._cover_repo.insert_cover(
                drive_file_id=drive_file_id,
                job_id=job_id,
                cover_url=cover_url,
                local_path="",
                checksum="",
                width=0,
                height=0,
                file_size=0,
                status=CoverDownloadStatus.FAILED.value,
                error_message=error_message,
            )
        except Exception as exc:
            logger.debug("Failed to record cover failure: %s", type(exc).__name__)

    def skip_existing(self, drive_file_id: str, checksum: str) -> bool:
        """Check if a cover with the same checksum already exists."""
        existing = self._cover_repo.get_by_checksum(checksum)
        return existing is not None

    def close(self) -> None:
        """Close the cover repository."""
        if self._cover_repo:
            self._cover_repo.close()

    def _fetch_image(self, url: str, timeout: int) -> bytes:
        """Download raw image bytes from a URL.

        Rejects placeholder URLs and non-image content types before the
        payload is treated as a cover.
        """
        import requests as _requests

        if self._is_placeholder_url(url):
            raise ValueError("placeholder cover URL")

        response = _requests.get(url, timeout=timeout)
        response.raise_for_status()

        content_type = (response.headers.get("content-type") or "").lower()
        url_lower = url.lower()
        has_image_extension = any(
            url_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")
        )
        if content_type and not content_type.startswith("image/"):
            if not has_image_extension:
                raise ValueError(f"non-image content-type: {content_type}")
        if content_type.startswith("image/gif"):
            raise ValueError("GIF covers are placeholders")
        return bytes(response.content)

    @classmethod
    def _is_placeholder_url(cls, url: str) -> bool:
        """True when a URL clearly identifies placeholder artwork."""
        lowered = (url or "").lower()
        return any(token in lowered for token in cls.PLACEHOLDER_URL_TOKENS)

    def _validate_image(
        self, raw_bytes: bytes, url: str
    ) -> Optional[Tuple[int, int, int, str]]:
        """Validate image format and dimensions.

        Returns (width, height, file_size, checksum) tuple on success,
        or None if validation fails.
        """
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(raw_bytes))
            if (img.format or "").upper() == "GIF":
                logger.warning("Cover URL returned a GIF placeholder")
                return None
            img = img.convert("RGB")  # type: ignore[assignment]
            width, height = img.size
            file_size = len(raw_bytes)
            checksum = hashlib.sha256(raw_bytes).hexdigest()

            if file_size > self._max_file_size:
                logger.warning(
                    "Cover file size %d exceeds max %d for URL",
                    file_size,
                    self._max_file_size,
                )
                return None

            if width < self._min_width or height < self._min_height:
                logger.warning(
                    "Cover dimensions %dx%d below minimum %dx%d for URL",
                    width,
                    height,
                    self._min_width,
                    self._min_height,
                )
                return None

            return (width, height, file_size, checksum)
        except Exception as exc:
            logger.warning(
                "Image validation failed: %s",
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _extract_extension(url: str, raw_bytes: bytes) -> str:
        """Extract image file extension from URL or content."""
        url_lower = url.lower()
        if ".png" in url_lower:
            return "png"
        if ".webp" in url_lower:
            return "webp"
        if ".gif" in url_lower:
            return "gif"
        return "jpg"
