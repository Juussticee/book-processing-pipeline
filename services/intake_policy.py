"""Intake policy: decide whether a Drive file is worth downloading.

This is the first gate of the processing pipeline. It runs on Drive
metadata only (MIME type, name, size) so unsupported or oversized files
are rejected *before* the bytes are downloaded and before a job exists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from models.drive_file import DriveFile

# Google Drive MIME types the image pipeline can safely decode via Pillow.
# HEIC/HEIF is deliberately absent (see HEIC policy below).
SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/tiff",
        "image/bmp",
    }
)

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}
)

# HEIC is the iPhone default. Pillow cannot decode it without the optional
# ``pillow-heif`` plugin, which is not installed in this environment, so HEIC
# is rejected with an explicit reason instead of failing later as a corrupt
# image. The customer guide tells users to switch the camera to JPEG.
HEIC_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/heic",
        "image/heif",
        "image/heic-sequence",
        "image/heif-sequence",
    }
)
HEIC_EXTENSIONS: frozenset[str] = frozenset({".heic", ".heif"})

# Operating-system junk that customers' folders inevitably contain.
BLOCKED_FILE_NAMES: frozenset[str] = frozenset(
    {
        "thumbs.db",
        "desktop.ini",
        ".ds_store",
        "icon\r",
        ".localized",
    }
)

REJECTED_UNSUPPORTED_TYPE = "REJECTED_UNSUPPORTED_TYPE"
REJECTED_FILE_TOO_LARGE = "REJECTED_FILE_TOO_LARGE"
REJECTED_HEIC_UNSUPPORTED = "REJECTED_HEIC_UNSUPPORTED"
REJECTED_DIRECTORY = "REJECTED_DIRECTORY"
REJECTED_BLOCKED_NAME = "REJECTED_BLOCKED_NAME"

GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass(frozen=True)
class IntakeDecision:
    """Result of evaluating one Drive file before download."""

    accepted: bool
    reason: Optional[str] = None
    detail: str = ""
    mime_type: str = ""
    size_bytes: Optional[int] = None

    @property
    def rejected(self) -> bool:
        return not self.accepted


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def evaluate(
    drive_file: DriveFile,
    max_size_bytes: Optional[int] = None,
) -> IntakeDecision:
    """Decide whether ``drive_file`` may enter the image pipeline.

    Args:
        drive_file: Drive metadata (no download performed).
        max_size_bytes: Maximum accepted file size. ``None`` disables the
            check (e.g. when Drive reports no size).

    Returns:
        An :class:`IntakeDecision`; ``accepted`` is False for anything the
        pipeline cannot process safely, with a stable ``reason`` code.
    """
    # Drive metadata objects are duck-typed here so lightweight test doubles
    # and partial listings (no MIME reported) still work.
    name = getattr(drive_file, "name", "") or ""
    mime = (getattr(drive_file, "mime_type", None) or "").strip().lower()
    ext = _extension(name)
    size = getattr(drive_file, "size", None)

    def reject(reason: str, detail: str) -> IntakeDecision:
        return IntakeDecision(
            accepted=False,
            reason=reason,
            detail=detail,
            mime_type=mime,
            size_bytes=size,
        )

    def accept(detail: str = "") -> IntakeDecision:
        return IntakeDecision(
            accepted=True,
            reason=None,
            detail=detail,
            mime_type=mime,
            size_bytes=size,
        )

    # Directories and Google-native documents are never images.
    if mime == GOOGLE_FOLDER_MIME:
        return reject(REJECTED_DIRECTORY, "Google Drive folder")
    if mime.startswith(GOOGLE_APPS_MIME_PREFIX):
        return reject(REJECTED_UNSUPPORTED_TYPE, f"Google-native file ({mime})")

    # OS junk / hidden metadata files.
    if name.strip().lower() in BLOCKED_FILE_NAMES:
        return reject(REJECTED_BLOCKED_NAME, "System metadata file")
    if name.startswith("~$") or name.startswith("."):
        return reject(REJECTED_BLOCKED_NAME, "Temporary or hidden file")

    # HEIC/HEIF: explicit policy, not a generic "unsupported" failure.
    if mime in HEIC_MIME_TYPES or ext in HEIC_EXTENSIONS:
        return reject(
            REJECTED_HEIC_UNSUPPORTED,
            "HEIC/HEIF is not supported; export the photo as JPEG",
        )

    if max_size_bytes is not None and size is not None and size > max_size_bytes:
        return reject(
            REJECTED_FILE_TOO_LARGE,
            f"{size} bytes exceeds the {max_size_bytes} byte limit",
        )

    if mime in SUPPORTED_MIME_TYPES:
        return accept()

    # Drive sometimes reports generic types (application/octet-stream) for
    # valid uploads. Fall back to the extension; the worker still validates
    # the decoded image, so a mislabelled non-image fails as one controlled
    # job rather than being silently accepted.
    if ext in SUPPORTED_EXTENSIONS:
        return accept(f"MIME '{mime or 'unknown'}' unverified; trusting extension")

    return reject(REJECTED_UNSUPPORTED_TYPE, f"MIME '{mime or 'unknown'}'")


def max_size_bytes_from_mb(megabytes: float) -> int:
    """Convert a megabyte limit (as stored in config) into bytes."""
    return int(megabytes * 1024 * 1024)
