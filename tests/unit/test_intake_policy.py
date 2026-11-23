"""Intake policy: files are judged from Drive metadata before download."""

from __future__ import annotations

import pytest

from models.drive_file import DriveFile
from services import intake_policy

MAX_BYTES = 20 * 1024 * 1024


def _file(name: str, mime: str, size=None, file_id: str = "f1") -> DriveFile:
    return DriveFile(
        file_id=file_id,
        name=name,
        mime_type=mime,
        modified_time="2024-01-01T00:00:00.000Z",
        size=size,
    )


class TestAccepted:
    @pytest.mark.parametrize(
        ("name", "mime"),
        [
            ("cover.jpg", "image/jpeg"),
            ("cover.JPG", "image/jpeg"),
            ("cover.jpeg", "image/jpg"),
            ("cover.png", "image/png"),
            ("cover.webp", "image/webp"),
            ("cover.tiff", "image/tiff"),
            ("cover.bmp", "image/bmp"),
        ],
    )
    def test_supported_images_are_accepted(self, name: str, mime: str) -> None:
        decision = intake_policy.evaluate(_file(name, mime, 1024), MAX_BYTES)
        assert decision.accepted
        assert decision.reason is None

    def test_generic_mime_with_image_extension_is_accepted(self) -> None:
        """Drive sometimes reports octet-stream for a real JPEG upload."""
        decision = intake_policy.evaluate(
            _file("cover.jpg", "application/octet-stream", 1024), MAX_BYTES
        )
        assert decision.accepted
        assert "unverified" in decision.detail

    def test_unknown_size_is_accepted(self) -> None:
        decision = intake_policy.evaluate(
            _file("cover.jpg", "image/jpeg", None), MAX_BYTES
        )
        assert decision.accepted


class TestRejected:
    def test_pdf_is_rejected_before_download(self) -> None:
        decision = intake_policy.evaluate(
            _file("scan.pdf", "application/pdf", 1024), MAX_BYTES
        )
        assert decision.rejected
        assert decision.reason == intake_policy.REJECTED_UNSUPPORTED_TYPE

    def test_google_docs_are_rejected(self) -> None:
        decision = intake_policy.evaluate(
            _file("notes", "application/vnd.google-apps.document", 0), MAX_BYTES
        )
        assert decision.reason == intake_policy.REJECTED_UNSUPPORTED_TYPE

    def test_folder_is_rejected(self) -> None:
        decision = intake_policy.evaluate(
            _file("subfolder", "application/vnd.google-apps.folder", 0), MAX_BYTES
        )
        assert decision.reason == intake_policy.REJECTED_DIRECTORY

    @pytest.mark.parametrize("name", ["Thumbs.db", "thumbs.db", "desktop.ini"])
    def test_system_files_are_rejected(self, name: str) -> None:
        decision = intake_policy.evaluate(
            _file(name, "application/octet-stream", 1024), MAX_BYTES
        )
        assert decision.reason == intake_policy.REJECTED_BLOCKED_NAME

    @pytest.mark.parametrize("name", [".DS_Store", "~$cover.docx"])
    def test_hidden_and_temp_files_are_rejected(self, name: str) -> None:
        decision = intake_policy.evaluate(
            _file(name, "application/octet-stream", 1024), MAX_BYTES
        )
        assert decision.reason == intake_policy.REJECTED_BLOCKED_NAME

    def test_unknown_file_is_rejected(self) -> None:
        decision = intake_policy.evaluate(
            _file("archive.zip", "application/zip", 1024), MAX_BYTES
        )
        assert decision.reason == intake_policy.REJECTED_UNSUPPORTED_TYPE

    @pytest.mark.parametrize(
        ("name", "mime"),
        [
            ("IMG_0001.HEIC", "image/heic"),
            ("IMG_0002.heif", "image/heif"),
        ],
    )
    def test_heic_has_an_explicit_policy(self, name: str, mime: str) -> None:
        decision = intake_policy.evaluate(_file(name, mime, 1024), MAX_BYTES)
        assert decision.rejected
        assert decision.reason == intake_policy.REJECTED_HEIC_UNSUPPORTED
        assert "JPEG" in decision.detail

    def test_oversized_file_is_rejected(self) -> None:
        decision = intake_policy.evaluate(
            _file("huge.jpg", "image/jpeg", MAX_BYTES + 1), MAX_BYTES
        )
        assert decision.reason == intake_policy.REJECTED_FILE_TOO_LARGE

    def test_size_exactly_at_limit_is_accepted(self) -> None:
        decision = intake_policy.evaluate(
            _file("ok.jpg", "image/jpeg", MAX_BYTES), MAX_BYTES
        )
        assert decision.accepted


def test_max_size_bytes_uses_megabyte_config() -> None:
    assert intake_policy.max_size_bytes_from_mb(20.0) == 20 * 1024 * 1024
    assert intake_policy.max_size_bytes_from_mb(1.5) == 1572864
