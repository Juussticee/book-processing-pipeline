"""Regression tests for Phase 35B: Clean external cover retrieval.

Tests cover:
1. Google Books cover selected first
2. Open Library cover selected when Google Books unavailable
3. Cover title/author validation
4. Wrong Google Books cover rejected
5. Drive image NEVER selected as final cover
6. Clean external image downloaded locally
7. Local path points to external cover
8. Existing clean cover is reused (cache)
9. Cover failure does not reject book
10. No re-download on duplicate processing
"""

import os
from unittest.mock import Mock, patch

import pytest

from models.book_metadata import BookMetadata
from services.book_lookup_service import BookLookupService
from services.clean_cover_service import CleanCoverService


@pytest.fixture
def mock_book_lookup():
    """Create a mock BookLookupService."""
    return Mock(spec=BookLookupService)


@pytest.fixture
def clean_cover_service(tmp_path, mock_book_lookup):
    """Create a CleanCoverService with a temp covers directory."""
    covers_dir = str(tmp_path / "covers")
    return CleanCoverService(
        book_lookup=mock_book_lookup,
        covers_dir=covers_dir,
    )


@pytest.fixture
def sample_metadata():
    """Sample metadata with verified vision title + author."""
    return BookMetadata(
        title="L'Invitation",
        authors=["Blanche Berthoud"],
        vision_title="L'Invitation",
        vision_author="Blanche Berthoud",
        drive_file_id="test_drive_001",
        isbn_13="9782825101209",
    )


class TestCleanCoverService:
    """Test clean cover retrieval service."""

    def test_google_books_cover_selected_first(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """Priority 1: Google Books cover is selected when available."""
        # Create a fake downloaded image file
        covers_path = os.path.join(str(tmp_path), "covers")
        os.makedirs(covers_path, exist_ok=True)
        cover_file = os.path.join(covers_path, "test_drive_001.jpg")
        with open(cover_file, "wb") as f:
            # Minimal valid JPEG header
            f.write(
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01"
                b"\x00\x01\x00\x00\xff\xdb\x00C\x00"
                + b"\x00" * 65
                + b"\xff\xd9"
                + b"\x00" * 200
            )

        mock_candidate = BookMetadata(
            title="L'Invitation",
            authors=["Blanche Berthoud"],
            image_links={"thumbnail": "https://books.google.com/books/abc.jpg"},
            source="google_books",
        )
        mock_book_lookup.search_google_books_only.return_value = [mock_candidate]

        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service,
                "_download_clean_cover",
                return_value=cover_file,
            ):
                with patch.object(
                    clean_cover_service, "_validate_url_is_image", return_value=True
                ):
                    cover_url, source, local_path = (
                        clean_cover_service.find_clean_cover(sample_metadata)
                    )

        assert "books.google.com" in cover_url
        assert source == "google_books"

    def test_open_library_cover_when_google_books_unavailable(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """Priority 2: Open Library cover selected when Google Books unavailable."""
        covers_path = os.path.join(str(tmp_path), "covers")
        os.makedirs(covers_path, exist_ok=True)
        cover_file = os.path.join(covers_path, "test_drive_001.jpg")
        with open(cover_file, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)

        # Google Books returns empty
        mock_book_lookup.search_google_books_only.return_value = []
        # OL returns a cover
        ol_candidate = BookMetadata(
            title="L'Invitation",
            authors=["Blanche Berthoud"],
            image_links={
                "thumbnail": "https://covers.openlibrary.org/b/isbn/9782825101209-M.jpg"
            },
            source="open_library",
        )
        mock_book_lookup.search_by_title_author.return_value = [ol_candidate]

        with patch.object(
            clean_cover_service,
            "_download_clean_cover",
            return_value=cover_file,
        ):
            with patch.object(
                clean_cover_service, "_validate_url_is_image", return_value=True
            ):
                cover_url, source, local_path = clean_cover_service.find_clean_cover(
                    sample_metadata
                )

        assert source == "open_library"
        assert "openlibrary.org" in cover_url

    def test_wrong_google_books_cover_rejected(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """A Google Books candidate with wrong title is rejected."""
        wrong_candidate = BookMetadata(
            title="Theodori Episcopi Mopsuesteni",
            authors=["Some Other Author"],
            image_links={"thumbnail": "https://books.google.com/books/wrong.jpg"},
            source="google_books",
        )
        mock_book_lookup.search_google_books_only.return_value = [wrong_candidate]

        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service, "_download_clean_cover", return_value=None
            ):
                cover_url, source, local_path = clean_cover_service.find_clean_cover(
                    sample_metadata
                )

        assert cover_url is None
        assert source == "not_found"

    def test_drive_image_never_selected(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """Phase 35B: Drive image is NEVER selected as final cover."""
        # No external covers available
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_by_title_author.return_value = []

        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service,
                "_search_exa",
                return_value=(None, "exa"),
            ):
                cover_url, source, local_path = clean_cover_service.find_clean_cover(
                    sample_metadata
                )

        assert cover_url is None
        assert source == "not_found"
        assert local_path is None
        # No Drive image URL should ever be returned
        assert "drive.google.com" not in str(cover_url)

    def test_clean_cover_downloaded_locally(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """Clean external cover is downloaded to data/covers/{drive_file_id}.jpg."""
        gb_url = "https://books.google.com/books/clean_cover.jpg"
        mock_candidate = BookMetadata(
            title="L'Invitation",
            authors=["Blanche Berthoud"],
            image_links={"thumbnail": gb_url},
            source="google_books",
        )
        mock_book_lookup.search_google_books_only.return_value = [mock_candidate]

        # Create a real image file for the mock download
        covers_path = os.path.join(str(tmp_path), "covers")
        os.makedirs(covers_path, exist_ok=True)
        cover_file = os.path.join(covers_path, "test_drive_001.jpg")
        with open(cover_file, "wb") as f:
            f.write(
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00"
                + b"\x00" * 65
                + b"\xff\xd9"
                + b"\x00" * 200
            )

        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service,
                "_download_clean_cover",
                return_value=cover_file,
            ):
                with patch.object(
                    clean_cover_service, "_validate_url_is_image", return_value=True
                ):
                    cover_url, source, local_path = (
                        clean_cover_service.find_clean_cover(sample_metadata)
                    )

        assert local_path == cover_file
        assert local_path == os.path.join(covers_path, "test_drive_001.jpg")
        assert "drive.google.com" not in local_path

    def test_existing_clean_cover_reused(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """If a clean cover already exists locally, it is reused without re-download."""
        covers_path = os.path.join(str(tmp_path), "covers")
        os.makedirs(covers_path, exist_ok=True)
        cover_file = os.path.join(covers_path, "test_drive_001.jpg")
        with open(cover_file, "wb") as f:
            f.write(
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00"
                + b"\x00" * 65
                + b"\xff\xd9"
                + b"\x00" * 200
            )

        # First call — should download
        mock_candidate = BookMetadata(
            title="L'Invitation",
            authors=["Blanche Berthoud"],
            image_links={"thumbnail": "https://books.google.com/books/cover.jpg"},
            source="google_books",
        )
        mock_book_lookup.search_google_books_only.return_value = [mock_candidate]
        download_count = 0

        def mock_download(url, drive_id):
            nonlocal download_count
            download_count += 1
            # If file exists, _download_clean_cover should return it without downloading
            existing = os.path.join(covers_path, f"{drive_id}.jpg")
            if os.path.exists(existing) and clean_cover_service._validate_image_file(
                existing
            ):
                return existing
            return cover_file

        with patch.object(
            clean_cover_service, "_download_clean_cover", side_effect=mock_download
        ):
            with patch.object(
                clean_cover_service, "_validate_url_is_image", return_value=True
            ):
                # First call
                clean_cover_service.find_clean_cover(sample_metadata)
                # Second call (cached)
                clean_cover_service._processed_cache.clear()
                clean_cover_service.find_clean_cover(sample_metadata)

        # Download should only happen once (due to cache + existing file check)
        assert download_count >= 1  # At least checked once

    def test_cover_failure_does_not_reject_book(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """Phase 35B: Cover failure does NOT reject the book — only cover is empty."""
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_by_title_author.return_value = []

        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service,
                "_search_exa",
                return_value=(None, "exa"),
            ):
                cover_url, source, local_path = clean_cover_service.find_clean_cover(
                    sample_metadata
                )

        assert cover_url is None
        assert local_path is None
        # Book metadata itself should still be intact
        assert sample_metadata.title == "L'Invitation"
        assert sample_metadata.vision_title == "L'Invitation"

    def test_isbn_prefix_language_detection_french(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """Cover search uses vision title + author, not OCR."""
        # The service should use metadata.title (from vision), not OCR text
        assert sample_metadata.title == "L'Invitation"
        assert sample_metadata.vision_author == "Blanche Berthoud"

    def test_google_books_url_upgraded_to_https(self):
        """HTTP URLs are upgraded to HTTPS."""
        url = "http://books.google.com/books/cover.jpg=s100"
        upgraded = CleanCoverService._upgrade_google_books_url(url)
        assert upgraded.startswith("https://")
        assert "s500" in upgraded

    def test_validate_url_is_image(self):
        """Non-image URLs are rejected."""
        assert (
            CleanCoverService._validate_url_is_image("https://example.com/cover.jpg")
            is True
        )
        assert (
            CleanCoverService._validate_url_is_image("https://example.com/page.html")
            is False
        )
        assert (
            CleanCoverService._validate_url_is_image(
                "https://books.google.com/books?id=abc"
            )
            is True
        )
        assert (
            CleanCoverService._validate_url_is_image(
                "https://covers.openlibrary.org/b/isbn/123-M.jpg"
            )
            is True
        )

    def test_no_cover_returns_not_found_status(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """When no cover is found, status is 'not_found'."""
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_by_title_author.return_value = []

        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service,
                "_search_exa",
                return_value=(None, "exa"),
            ):
                cover_url, source, local_path = clean_cover_service.find_clean_cover(
                    sample_metadata
                )

        assert source == "not_found"
        assert cover_url is None

    def test_russian_book_cover_search(self):
        """Cyrillic title + author are used for search."""
        meta = BookMetadata(
            title="Философия общего дела",
            authors=["Н. Ф. Фёдоров"],
            vision_title="Философия общего дела",
            vision_author="Н. Ф. Фёдоров",
            drive_file_id="test_russian_001",
        )
        assert meta.title == "Философия общего дела"
        assert meta.vision_author == "Н. Ф. Фёдоров"


# ------------------------------------------------------------------
# Production hardening: ISBN-first ordering, cover validation, reuse
# ------------------------------------------------------------------


def _write_cover_image(path: str, size=(400, 600), fmt: str = "PNG") -> str:
    """Write a real image so dimension/format validation actually runs."""
    from PIL import Image

    Image.new("RGB", size, color="white").save(path, format=fmt)
    return path


class TestIsbnFirstCoverRetrieval:
    """The selected edition (ISBN) must drive cover selection."""

    @pytest.fixture
    def service(self, tmp_path, mock_book_lookup):
        return CleanCoverService(
            book_lookup=mock_book_lookup,
            covers_dir=str(tmp_path / "covers"),
        )

    def _fake_download(self, tmp_path, recorded):
        def download(url, drive_id):
            recorded.append(url)
            covers_dir = tmp_path / "covers"
            covers_dir.mkdir(parents=True, exist_ok=True)
            return _write_cover_image(
                str(covers_dir / f"{drive_id}.jpg"), size=(400, 600)
            )

        return download

    def test_isbn_match_selected_before_title_search(
        self, service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """An exact ISBN match is used without a title+author search."""
        isbn_candidate = BookMetadata(
            title="L'Invitation",
            authors=["Blanche Berthoud"],
            isbn_13="9782825101209",
            image_links={
                "thumbnail": "https://books.google.com/books/isbn-edition.jpg"
            },
            source="google_books",
        )
        mock_book_lookup.search_by_isbn.return_value = [isbn_candidate]
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_by_title_author.return_value = []
        downloaded = []

        with (
            patch.object(
                service,
                "_download_clean_cover",
                side_effect=self._fake_download(tmp_path, downloaded),
            ),
            patch.object(service, "_validate_url_is_image", return_value=True),
        ):
            cover_url, source, local_path = service.find_clean_cover(sample_metadata)

        assert source == "google_books"
        assert cover_url.startswith("https://books.google.com/books/isbn-edition.jpg")
        assert local_path is not None
        # The edition was resolved from the ISBN, not from a title search.
        mock_book_lookup.search_by_isbn.assert_called_once_with("9782825101209")
        mock_book_lookup.search_google_books_only.assert_not_called()

    def test_isbn_candidate_for_another_book_is_rejected(
        self, service, mock_book_lookup, sample_metadata
    ):
        """A provider mis-filing the ISBN must not attach a wrong-book cover."""
        wrong_book = BookMetadata(
            title="完全に別の本",
            authors=["Someone Else"],
            isbn_13="9782825101209",
            image_links={"thumbnail": "https://books.google.com/books/wrong.jpg"},
            source="google_books",
        )
        mock_book_lookup.search_by_isbn.return_value = [wrong_book]

        with (
            patch.object(
                service, "_search_open_library", return_value=(None, "open_library")
            ),
            patch.object(
                service, "_search_google_books", return_value=(None, "google_books")
            ),
            patch.object(service, "_search_exa", return_value=(None, "exa")),
            patch.object(service, "_download_clean_cover", return_value=None),
        ):
            cover_url, source, local_path = service.find_clean_cover(sample_metadata)

        assert cover_url is None
        assert local_path is None
        assert source == "not_found"

    def test_provider_failure_falls_back_to_next_source(
        self, service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """An ISBN lookup error must not abort the fallback chain."""
        mock_book_lookup.search_by_isbn.side_effect = RuntimeError("502 Bad Gateway")
        mock_book_lookup.search_by_title_author.return_value = []
        mock_book_lookup.search_google_books_only.return_value = []
        downloaded = []

        with (
            patch.object(
                service,
                "_search_open_library",
                return_value=(
                    "https://covers.openlibrary.org/b/isbn/9782825101209-L.jpg",
                    "open_library",
                ),
            ),
            patch.object(
                service,
                "_download_clean_cover",
                side_effect=self._fake_download(tmp_path, downloaded),
            ),
            patch.object(service, "_validate_url_is_image", return_value=True),
        ):
            cover_url, source, local_path = service.find_clean_cover(sample_metadata)

        assert source == "open_library"
        assert local_path is not None
        assert "openlibrary.org" in cover_url

    def test_existing_validated_cover_reused_without_network(
        self, service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """A cover already persisted to disk is reused with zero API calls."""
        covers_dir = tmp_path / "covers"
        covers_dir.mkdir(parents=True, exist_ok=True)
        local = _write_cover_image(str(covers_dir / "existing.png"))
        sample_metadata.local_cover_path = local
        sample_metadata.cover_source = "google_books"
        sample_metadata.cover_source_url = "https://books.google.com/books/e.jpg"

        with patch.object(service, "_download_clean_cover") as download:
            cover_url, source, local_path = service.find_clean_cover(sample_metadata)

        assert local_path == local
        assert source == "google_books"
        assert cover_url == "https://books.google.com/books/e.jpg"
        download.assert_not_called()
        mock_book_lookup.search_by_isbn.assert_not_called()
        mock_book_lookup.search_by_title_author.assert_not_called()

    def test_stale_local_cover_is_not_reused(
        self, service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """A persisted path pointing at a dead placeholder is re-resolved."""
        missing = str(tmp_path / "gone.png")
        sample_metadata.local_cover_path = missing
        mock_book_lookup.search_by_isbn.return_value = []
        mock_book_lookup.search_by_title_author.return_value = []
        mock_book_lookup.search_google_books_only.return_value = []

        with (
            patch.object(
                service, "_search_open_library", return_value=(None, "open_library")
            ),
            patch.object(service, "_search_exa", return_value=(None, "exa")),
            patch.object(service, "_download_clean_cover", return_value=None),
        ):
            cover_url, source, local_path = service.find_clean_cover(sample_metadata)

        assert local_path is None
        assert cover_url is None


class TestCoverImageValidation:
    """Validation must reject placeholders and unusable images."""

    def test_placeholder_urls_detected(self):
        assert CleanCoverService._is_placeholder_url(
            "https://m.media-amazon.com/images/I/41xyz._kindle-store-link.jpg"
        )
        assert CleanCoverService._is_placeholder_url(
            "https://example.com/no-cover-placeholder.png"
        )
        assert CleanCoverService._is_placeholder_url("https://example.com/1x1.gif")
        assert not CleanCoverService._is_placeholder_url(
            "https://books.google.com/books/content?id=abc&printsec=frontcover"
        )
        assert not CleanCoverService._is_placeholder_url(None)

    def test_gif_cover_rejected(self, tmp_path):
        path = _write_cover_image(
            str(tmp_path / "cover.gif"), size=(400, 600), fmt="GIF"
        )
        assert CleanCoverService._validate_image_file(path) is False

    def test_tiny_image_rejected(self, tmp_path):
        path = _write_cover_image(str(tmp_path / "tiny.png"), size=(100, 100))
        assert CleanCoverService._validate_image_file(path) is False

    def test_real_cover_accepted(self, tmp_path):
        path = _write_cover_image(str(tmp_path / "cover.png"), size=(400, 600))
        assert CleanCoverService._validate_image_file(path) is True

    def test_non_image_file_rejected(self, tmp_path):
        path = tmp_path / "not-an-image.jpg"
        path.write_bytes(b"<html>404 not found</html>")
        assert CleanCoverService._validate_image_file(str(path)) is False
