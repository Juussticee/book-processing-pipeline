"""Unit tests for CoverResolverService.

Tests priority-based fallback and the resolve_cover_urls method
that returns ALL candidate URLs in priority order, including
multi-size image links from Google Books and Open Library
large-format fallback.
"""

from models.book_metadata import BookMetadata
from services.cover_resolver_service import CoverResolverService


class _StubLookupService:
    """Stub BookLookupService returning controlled candidates."""

    def __init__(self, candidates=None):
        self._candidates = candidates or []

    def search_by_isbn(self, isbn):
        if isbn == "999":
            return [
                BookMetadata(
                    title="ISBN Match",
                    authors=["Author"],
                    isbn_13="999",
                    publisher="Pub",
                    published_date="2024-01-01",
                    confidence=0.9,
                    source="google_books",
                    cover_url="https://example.com/isbn-cover.jpg",
                    thumbnail_url="https://example.com/isbn-thumb.jpg",
                    drive_file_id="drive_stub",
                    job_id="job_stub",
                )
            ]
        return []

    def search_by_title_author(self, title, author):
        if title == "Lookup Match":
            return [
                BookMetadata(
                    title="Lookup Match",
                    authors=["Author"],
                    publisher="Pub",
                    published_date="2024-01-01",
                    confidence=0.9,
                    source="google_books",
                    cover_url="https://example.com/title-cover.jpg",
                    drive_file_id="drive_stub",
                    job_id="job_stub",
                )
            ]
        return []


def _make_metadata(**kwargs):
    defaults = {
        "title": "Test Book",
        "authors": ["Test Author"],
        "isbn_10": "0306406152",
        "isbn_13": "9780306406157",
        "publisher": "Test Publisher",
        "published_date": "2024-01-01",
        "language": "en",
        "confidence": 0.95,
        "source": "google_books",
        "drive_file_id": "drive001",
        "job_id": "job001",
    }
    defaults.update(kwargs)
    return BookMetadata(**defaults)


class TestCoverResolverService:
    def setup_method(self):
        self.service = CoverResolverService(book_lookup_service=_StubLookupService())

    def test_priority_cover_url(self):
        metadata = _make_metadata(
            cover_url="https://example.com/best-cover.jpg",
            thumbnail_url="https://example.com/thumb.jpg",
        )
        result = self.service.resolve(metadata)
        assert result == "https://example.com/best-cover.jpg"

    def test_priority_thumbnail_url(self):
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url="https://example.com/thumb.jpg",
        )
        result = self.service.resolve(metadata)
        assert result == "https://example.com/thumb.jpg"

    def test_priority_isbn_lookup(self):
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url=None,
            isbn_13="999",
        )
        result = self.service.resolve(metadata)
        assert result == "https://example.com/isbn-cover.jpg"

    def test_priority_title_author_lookup(self):
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url=None,
            isbn_10=None,
            isbn_13=None,
            title="Lookup Match",
        )
        result = self.service.resolve(metadata)
        assert result == "https://example.com/title-cover.jpg"

    def test_no_cover_found(self):
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url=None,
            isbn_10=None,
            isbn_13=None,
            title="Test Book",
        )
        result = self.service.resolve(metadata)
        assert result is None

    def test_empty_urls_skipped(self):
        metadata = _make_metadata(
            cover_url="",
            thumbnail_url="",
            isbn_10=None,
            isbn_13=None,
            title="Test Book",
        )
        result = self.service.resolve(metadata)
        assert result is None

    # --- New tests for resolve_cover_urls (plural) ---

    def test_resolve_cover_urls_returns_all_candidates(self):
        """resolve_cover_urls should return all candidate URLs in priority order."""
        metadata = _make_metadata(
            cover_url="https://example.com/primary.jpg",
            thumbnail_url="https://example.com/thumb.jpg",
        )
        urls = self.service.resolve_cover_urls(metadata)
        assert "https://example.com/primary.jpg" in urls
        assert "https://example.com/thumb.jpg" in urls

    def test_resolve_cover_urls_no_duplicates(self):
        """resolve_cover_urls should not return duplicate URLs."""
        metadata = _make_metadata(
            cover_url="https://example.com/same.jpg",
            thumbnail_url="https://example.com/same.jpg",
        )
        urls = self.service.resolve_cover_urls(metadata)
        assert urls.count("https://example.com/same.jpg") == 1

    def test_resolve_cover_urls_fallback_to_thumbnail(self):
        """When cover_url is None, should fall back to thumbnail_url."""
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url="https://example.com/thumb.jpg",
            isbn_10=None,
            isbn_13=None,
        )
        urls = self.service.resolve_cover_urls(metadata)
        assert "https://example.com/thumb.jpg" in urls

    def test_resolve_cover_urls_open_library_isbn_fallback(self):
        """Should include Open Library ISBN-based large-size URL as fallback."""
        metadata = _make_metadata(
            cover_url="https://example.com/primary.jpg",
            thumbnail_url="https://example.com/thumb.jpg",
            isbn_13="9780306406157",
        )
        urls = self.service.resolve_cover_urls(metadata)
        # Should include OL large variant first, then medium, then default
        ol_large = "https://covers.openlibrary.org/b/isbn/9780306406157-L.jpg"
        ol_medium = "https://covers.openlibrary.org/b/isbn/9780306406157-M.jpg"
        ol_default = "https://covers.openlibrary.org/b/isbn/9780306406157.jpg"
        assert ol_large in urls
        assert ol_medium in urls
        assert ol_default in urls
        # Large should come before medium and default
        assert urls.index(ol_large) < urls.index(ol_medium)
        assert urls.index(ol_medium) < urls.index(ol_default)

    def test_resolve_cover_urls_no_primary_no_isbn(self):
        """When no primary cover and no ISBN, should return title-based URLs."""
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url=None,
            isbn_10=None,
            isbn_13=None,
            title="Lookup Match",
        )
        urls = self.service.resolve_cover_urls(metadata)
        assert "https://example.com/title-cover.jpg" in urls

    # --- New tests for image_links (Google Books multi-size) ---

    def test_resolve_cover_urls_includes_image_links(self):
        """Should include Google Books image_links URLs (large, medium, etc.)."""
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url=None,
            isbn_10=None,
            isbn_13=None,
            title="Test Book",
            image_links={
                "extraLarge": "https://example.com/xl.jpg",
                "large": "https://example.com/large.jpg",
                "medium": "https://example.com/medium.jpg",
                "thumbnail": "https://example.com/thumb.jpg",
            },
        )
        urls = self.service.resolve_cover_urls(metadata)
        # Large and medium should be included
        assert "https://example.com/large.jpg" in urls
        assert "https://example.com/medium.jpg" in urls
        assert "https://example.com/xl.jpg" in urls
        # Large should come before medium
        assert urls.index("https://example.com/xl.jpg") < urls.index(
            "https://example.com/large.jpg"
        )
        assert urls.index("https://example.com/large.jpg") < urls.index(
            "https://example.com/medium.jpg"
        )

    def test_resolve_cover_urls_image_links_deduped_with_cover_url(self):
        """If image_links thumbnail equals cover_url, should not duplicate."""
        metadata = _make_metadata(
            cover_url="https://example.com/same.jpg",
            image_links={"thumbnail": "https://example.com/same.jpg"},
        )
        urls = self.service.resolve_cover_urls(metadata)
        assert urls.count("https://example.com/same.jpg") == 1

    # --- New tests for OL large-first ordering ---

    def test_open_library_large_before_default(self):
        """OL ISBN URL should list -L.jpg before .jpg."""
        metadata = _make_metadata(
            cover_url=None,
            thumbnail_url=None,
            isbn_10=None,
            title="Test Book",
            isbn_13="9780306406157",
        )
        urls = self.service.resolve_cover_urls(metadata)
        ol_large = "https://covers.openlibrary.org/b/isbn/9780306406157-L.jpg"
        ol_default = "https://covers.openlibrary.org/b/isbn/9780306406157.jpg"
        assert ol_large in urls
        assert ol_default in urls
        assert urls.index(ol_large) < urls.index(ol_default)

    def test_resolve_cover_urls_image_links_normalized(self):
        """Google Books image_links URLs should be normalized to https."""
        metadata = _make_metadata(
            cover_url="https://example.com/http-cover.jpg",
            image_links={"large": "https://example.com/http-large.jpg"},
        )
        urls = self.service.resolve_cover_urls(metadata)
        assert "https://example.com/http-large.jpg" in urls
