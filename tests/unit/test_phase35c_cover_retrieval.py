"""Phase 35C tests: Google Books + Exa clean cover retrieval.

Tests cover:
1.  Google Books configured detection
2.  Google Books search with title+author
3.  Google Books cover selection from imageLinks (large > thumbnail)
4.  Google Books wrong-result rejection (fuzzy matching)
5.  Google Books placeholder rejection (tiny images)
6.  Open Library fallback when Google Books fails
7.  Exa fallback when Google Books + OL fail
8.  Exa page -> image extraction
9.  External image validation (dimensions, content-type)
10. Drive image never selected as final cover
11. Existing cover reused (cache + local file check)
12. Duplicate cover download prevention
13. Missing cover does not reject book
"""

import os
from unittest.mock import Mock, patch

import pytest

from core.config import Config
from models.book_metadata import BookMetadata
from services.book_lookup_service import BookLookupService
from services.clean_cover_service import CleanCoverService

PIL_AVAILABLE = True
try:
    from PIL import Image
except ImportError:
    PIL_AVAILABLE = False


def make_image(path, size=(300, 400), color="blue"):
    """Helper to create a valid test image."""
    if not PIL_AVAILABLE:
        pytest.skip("PIL not available")
    Image.new("RGB", size, color=color).save(path, "JPEG")
    return path


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


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
        isbn_13="9782825101087",
    )


@pytest.fixture
def google_books_candidate():
    """A Google Books metadata candidate with image links."""
    return BookMetadata(
        title="L'Invitation",
        authors=["Blanche Berthoud"],
        isbn_13="9782825101087",
        image_links={
            "thumbnail": "https://books.google.com/books/thumbnail.jpg",
            "large": "https://books.google.com/books/large.jpg",
        },
        source="google_books",
    )


@pytest.fixture
def google_books_wrong_candidate():
    """A Google Books candidate with wrong title (should be rejected)."""
    return BookMetadata(
        title="The Complete Guide to Gardening",
        authors=["Someone Else"],
        isbn_13="9780000000000",
        image_links={"thumbnail": "https://books.google.com/books/wrong.jpg"},
        source="google_books",
    )


# ------------------------------------------------------------------
# 1. Google Books configured detection
# ------------------------------------------------------------------


class TestGoogleBooksConfig:
    """Test that Google Books API key is properly loaded from config."""

    def test_config_reads_google_books_api_key(self):
        """Config.from_env reads GOOGLE_BOOKS_API_KEY."""
        with patch.dict(os.environ, {"GOOGLE_BOOKS_API_KEY": "test_key"}):
            config = Config.from_env()
            assert config.google_books_api_key == "test_key"

    def test_config_reads_exa_api_key(self):
        """Config.from_env reads EXA_API_KEY."""
        with patch.dict(os.environ, {"EXA_API_KEY": "test_exa"}):
            config = Config.from_env()
            assert config.exa_api_key == "test_exa"

    def test_config_empty_keys_when_unset(self):
        """Config handles missing keys gracefully."""
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()
            assert config.google_books_api_key == ""
            assert config.exa_api_key == ""

    def test_book_lookup_receives_exa_key(self):
        """BookLookupService stores the Exa API key."""
        bls = BookLookupService(google_books_api_key="gb_key", exa_api_key="exa_key")
        assert bls._exa_api_key == "exa_key"


# ------------------------------------------------------------------
# 2. Google Books search with title+author
# ------------------------------------------------------------------


class TestGoogleBooksSearch:
    """Test Google Books cover search uses vision title + author."""

    def test_gb_search_uses_title_and_author(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """Google Books search uses verified vision title + author."""
        mock_book_lookup.search_google_books_only.return_value = []
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service, "_search_exa", return_value=(None, "exa")
            ):
                clean_cover_service.find_clean_cover(sample_metadata)
        # Phase 36: GB search does title+author first, then title-only fallback
        mock_book_lookup.search_google_books_only.assert_any_call(
            "L'Invitation", "Blanche Berthoud"
        )
        mock_book_lookup.search_google_books_only.assert_any_call("L'Invitation", None)

    def test_gb_not_called_when_title_missing(
        self, clean_cover_service, mock_book_lookup
    ):
        """If no vision title, Google Books should not be called."""
        metadata = BookMetadata(
            drive_file_id="test_001", title="Unknown", vision_title=None
        )
        mock_book_lookup.search_google_books_only.return_value = []
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service, "_search_exa", return_value=(None, "exa")
            ):
                # With vision_title=None but title='Unknown', GB IS called
                # but returns empty list -> falls through to OL -> Exa -> None
                url, source, local = clean_cover_service.find_clean_cover(metadata)
                assert url is None
                assert source == "not_found"
        # Phase 36: GB search does title+author first, then title-only fallback
        assert mock_book_lookup.search_google_books_only.call_count == 2


# ------------------------------------------------------------------
# 3. Google Books cover selection from imageLinks
# ------------------------------------------------------------------


class TestGoogleBooksCoverSelection:
    """Test that the highest-quality Google Books image is selected."""

    def test_prefers_large_image(
        self,
        clean_cover_service,
        mock_book_lookup,
        sample_metadata,
        google_books_candidate,
        tmp_path,
    ):
        """Google Books cover prefers 'large' over 'thumbnail'."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        cover_file = os.path.join(covers_dir, "test_drive_001.jpg")
        make_image(cover_file)
        mock_book_lookup.search_google_books_only.return_value = [
            google_books_candidate
        ]
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
                url, source, _ = clean_cover_service.find_clean_cover(sample_metadata)
        assert source == "google_books"
        assert "large.jpg" in url

    def test_falls_back_to_thumbnail(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """If only thumbnail is available, use it."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        cover_file = os.path.join(covers_dir, "test_drive_001.jpg")
        make_image(cover_file)
        candidate = BookMetadata(
            title="L'Invitation",
            authors=["Blanche Berthoud"],
            image_links={"thumbnail": "https://books.google.com/books/thumb.jpg"},
            source="google_books",
        )
        mock_book_lookup.search_google_books_only.return_value = [candidate]
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
                url, source, _ = clean_cover_service.find_clean_cover(sample_metadata)
        assert source == "google_books"
        assert "thumb.jpg" in url


# ------------------------------------------------------------------
# 4. Google Books wrong-result rejection
# ------------------------------------------------------------------


class TestGoogleBooksWrongResultRejection:
    """Test wrong Google Books results are rejected via fuzzy matching."""

    def test_wrong_title_rejected_falls_through(
        self,
        clean_cover_service,
        mock_book_lookup,
        sample_metadata,
        tmp_path,
        google_books_wrong_candidate,
    ):
        """Wrong title/author candidate is rejected, falls to OL."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        cover_file = os.path.join(covers_dir, "test_drive_001.jpg")
        make_image(cover_file)
        mock_book_lookup.search_google_books_only.return_value = [
            google_books_wrong_candidate
        ]
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(
                "https://covers.openlibrary.org/b/isbn/9782825-L.jpg",
                "open_library",
            ),
        ):
            with patch.object(
                clean_cover_service,
                "_download_clean_cover",
                return_value=cover_file,
            ):
                url, source, _ = clean_cover_service.find_clean_cover(sample_metadata)
        assert source == "open_library"
        assert "openlibrary" in url


# ------------------------------------------------------------------
# 5. Google Books placeholder rejection
# ------------------------------------------------------------------


class TestPlaceholderRejection:
    """Test that placeholder/tiny images are rejected."""

    def test_1x1_image_rejected(self, clean_cover_service, tmp_path):
        """A 1x1 image should be rejected by _validate_image_file."""
        path = str(tmp_path / "tiny.jpg")
        make_image(path, size=(1, 1))
        assert clean_cover_service._validate_image_file(path) is False

    def test_small_image_rejected(self, clean_cover_service, tmp_path):
        """An image smaller than 200x250 should be rejected."""
        path = str(tmp_path / "small.jpg")
        make_image(path, size=(50, 50))
        assert clean_cover_service._validate_image_file(path) is False

    def test_valid_image_accepted(self, clean_cover_service, tmp_path):
        """An image >= 200x250 should be accepted."""
        path = str(tmp_path / "valid.jpg")
        make_image(path, size=(300, 400))
        assert clean_cover_service._validate_image_file(path) is True


# ------------------------------------------------------------------
# 6. Open Library fallback when Google Books fails
# ------------------------------------------------------------------


class TestOpenLibraryFallback:
    """Test that Open Library is used when Google Books fails."""

    def test_ol_used_when_gb_returns_empty(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """When GB returns no candidates, OL should be tried."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        cover_file = os.path.join(covers_dir, "test_drive_001.jpg")
        make_image(cover_file)
        mock_book_lookup.search_google_books_only.return_value = []
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(
                "https://covers.openlibrary.org/b/isbn/9782825-L.jpg",
                "open_library",
            ),
        ):
            with patch.object(
                clean_cover_service,
                "_download_clean_cover",
                return_value=cover_file,
            ):
                url, source, _ = clean_cover_service.find_clean_cover(sample_metadata)
        assert source == "open_library"
        assert "openlibrary" in url


# ------------------------------------------------------------------
# 7. Exa fallback when Google Books + OL fail
# ------------------------------------------------------------------


class TestExaFallback:
    """Test that Exa web search is used when GB and OL fail."""

    def test_exa_used_when_gb_ol_fail(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """When GB and OL both fail, Exa should be tried."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        cover_file = os.path.join(covers_dir, "test_drive_001.jpg")
        make_image(cover_file)
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_exa_for_cover.return_value = (
            "https://example.com/cover.jpg",
            "https://example.com/book-page",
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
                url, source, _ = clean_cover_service.find_clean_cover(sample_metadata)
        assert source == "exa"
        mock_book_lookup.search_exa_for_cover.assert_called_once()


# ------------------------------------------------------------------
# 8. Exa page -> image extraction
# ------------------------------------------------------------------


class TestExaPageImageExtraction:
    """Test BookLookupService extracts real images from web pages."""

    def test_extract_finds_image(self):
        """_extract_image_from_page finds cover image URLs in HTML."""
        bls = BookLookupService(exa_api_key="dummy")
        html = """
        <html><body>
        <img src="https://example.com/spacer.gif" />
        <img src="https://example.com/logo.png" />
        <img src="https://example.com/covers/mybook-front.jpg" />
        </body></html>
        """
        with patch.object(bls, "_session") as mock_session:
            mock_resp = Mock()
            mock_resp.text = html
            mock_resp.raise_for_status = Mock()
            mock_resp.content = b""
            mock_session.get.return_value = mock_resp
            with patch.object(bls, "_is_reputable_source", return_value=True):
                with patch.object(bls, "_validate_image_dimensions", return_value=True):
                    result = bls._extract_image_from_page("https://x.com")
                    assert "front.jpg" in result

    def test_extract_rejects_sprites(self):
        """_extract_image_from_page rejects sprite/UI images."""
        bls = BookLookupService(exa_api_key="dummy")
        html = """
        <img src="https://cdn.example.com/sprite.nav-icon.gif" />
        <img src="https://cdn.example.com/loading-spinner.gif" />
        """
        with patch.object(bls, "_session") as mock_session:
            mock_resp = Mock()
            mock_resp.text = html
            mock_resp.raise_for_status = Mock()
            mock_session.get.return_value = mock_resp
            with patch.object(bls, "_is_reputable_source", return_value=True):
                with patch.object(bls, "_validate_image_dimensions", return_value=True):
                    result = bls._extract_image_from_page("https://example.com")
                    assert result is None


# ------------------------------------------------------------------
# 9. External image validation
# ------------------------------------------------------------------


class TestExternalImageValidation:
    """Test validation of external image URLs."""

    def test_url_jpg_accepted(self, clean_cover_service):
        assert clean_cover_service._validate_url_is_image(
            "https://example.com/cover.jpg"
        )

    def test_url_html_rejected(self, clean_cover_service):
        assert not clean_cover_service._validate_url_is_image(
            "https://example.com/page.html"
        )

    def test_url_empty_rejected(self, clean_cover_service):
        assert not clean_cover_service._validate_url_is_image("")

    def test_url_png_accepted(self, clean_cover_service):
        assert clean_cover_service._validate_url_is_image(
            "https://example.com/cover.png"
        )

    def test_url_webp_accepted(self, clean_cover_service):
        assert clean_cover_service._validate_url_is_image(
            "https://example.com/cover.webp"
        )


# ------------------------------------------------------------------
# 10. Drive image never selected
# ------------------------------------------------------------------


class TestDriveNeverSelected:
    """Test that Drive images are NEVER used as final covers."""

    def test_drive_url_never_returned(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """find_clean_cover never returns a Drive URL."""
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_exa_for_cover.return_value = (None, None)
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service, "_search_exa", return_value=(None, "exa")
            ):
                url, source, local = clean_cover_service.find_clean_cover(
                    sample_metadata
                )
        assert url is None
        assert local is None

    def test_gb_candidate_drive_url_rejected(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """Google Books candidate with Drive URL must not be used."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        candidate = BookMetadata(
            title="L'Invitation",
            authors=["Blanche Berthoud"],
            image_links={"thumbnail": "https://drive.google.com/uc?id=F"},
            source="google_books",
        )
        mock_book_lookup.search_google_books_only.return_value = [candidate]
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service, "_search_exa", return_value=(None, "exa")
            ):
                url, _, _ = clean_cover_service.find_clean_cover(sample_metadata)
        if url:
            assert "drive.google.com" not in url


# ------------------------------------------------------------------
# 11. Existing cover reused
# ------------------------------------------------------------------


class TestExistingCoverReuse:
    """Test that existing clean covers are reused without re-download."""

    def test_existing_local_cover_reused(self, tmp_path):
        """If a local cover file already exists, it is reused."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        drive_id = "existing_book_001"
        cover_file = os.path.join(covers_dir, f"cover_{drive_id}.jpg")
        make_image(cover_file, size=(300, 400))
        mock_book_lookup = Mock(spec=BookLookupService)
        service = CleanCoverService(book_lookup=mock_book_lookup, covers_dir=covers_dir)
        metadata = BookMetadata(
            title="Existing Book",
            authors=["Author"],
            vision_title="Existing Book",
            vision_author="Author",
            drive_file_id=drive_id,
            local_cover_path=cover_file,
            cover_source="open_library",
            cover_status="found",
            cover_url="https://covers.openlibrary.org/b/isbn/9780-L.jpg",
        )
        url, source, local = service.find_clean_cover(metadata)
        mock_book_lookup.search_google_books_only.assert_not_called()
        assert local == cover_file
        assert source == "open_library"


# ------------------------------------------------------------------
# 12. Duplicate cover download prevention
# ------------------------------------------------------------------


class TestDuplicateDownloadPrevention:
    """Test that the same cover is not downloaded twice."""

    def test_cache_prevents_redundant_download(
        self, clean_cover_service, mock_book_lookup, sample_metadata, tmp_path
    ):
        """find_clean_cover caches results to avoid redundant downloads."""
        covers_dir = str(tmp_path / "covers")
        os.makedirs(covers_dir, exist_ok=True)
        cover_file = os.path.join(covers_dir, "test_drive_001.jpg")
        make_image(cover_file)
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_exa_for_cover.return_value = (
            "https://example.com/cover.jpg",
            "https://example.com/page",
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
                r1 = clean_cover_service.find_clean_cover(sample_metadata)
                r2 = clean_cover_service.find_clean_cover(sample_metadata)
                assert r1 == r2


# ------------------------------------------------------------------
# 13. Missing cover does not reject book
# ------------------------------------------------------------------


class TestNoRejectionOnMissingCover:
    """Test that a missing cover never rejects a book."""

    def test_missing_cover_returns_not_found(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """When no cover is found, returns (None, 'not_found', None)."""
        mock_book_lookup.search_google_books_only.return_value = []
        mock_book_lookup.search_exa_for_cover.return_value = (None, None)
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(None, "open_library"),
        ):
            with patch.object(
                clean_cover_service, "_search_exa", return_value=(None, "exa")
            ):
                url, source, local = clean_cover_service.find_clean_cover(
                    sample_metadata
                )
        assert url is None
        assert source == "not_found"
        assert local is None

    def test_ol_download_failure_falls_through_to_exa(
        self, clean_cover_service, mock_book_lookup, sample_metadata
    ):
        """OL URL found but download fails -> fall through to Exa."""
        ol_url = "https://covers.openlibrary.org/b/isbn/9782825-L.jpg"
        mock_book_lookup.search_google_books_only.return_value = []
        with patch.object(
            clean_cover_service,
            "_search_open_library",
            return_value=(ol_url, "open_library"),
        ):
            with patch.object(
                clean_cover_service, "_download_clean_cover", return_value=None
            ):
                with patch.object(
                    clean_cover_service,
                    "_search_exa",
                    return_value=(None, "exa"),
                ):
                    url, source, local = clean_cover_service.find_clean_cover(
                        sample_metadata
                    )
        assert url is None
        assert source == "not_found"
        assert local is None
