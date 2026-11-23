"""Cover resolver service for Milestone 6.

Resolves cover URLs for a BookMetadata record using a priority-based
fallback approach. Returns ALL candidate URLs in priority order so the
downloader can try each until one passes image validation.

Priority order:
1. metadata.cover_url (highest quality from API result)
2. metadata.image_links['large'] / 'medium' / 'extraLarge' (Google Books sizes)
3. metadata.thumbnail_url (lower quality)
4. ISBN-based lookup via BookLookupService (returns candidates with image_links)
5. Title + author lookup via BookLookupService (returns candidates with image_links)
6. Open Library cover by ISBN (direct URL construction) - try -L then default
7. Open Library cover by ISBN (direct URL construction) - try -M then default
"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote_plus

from core.logger import setup_logger
from models.book_metadata import BookMetadata
from services.book_lookup_service import BookLookupService

logger = setup_logger(__name__)


class CoverResolverService:
    """Resolves cover image URLs for a BookMetadata record.

    Priority order (returns all candidates in order):
    1. metadata.cover_url
    2. metadata.image_links (large, medium, extraLarge, small, thumbnail)
    3. metadata.thumbnail_url
    4. ISBN-based lookup via BookLookupService
    5. Title + author lookup via BookLookupService
    6. Open Library cover by ISBN (direct URL, large variant)
    7. Open Library cover by ISBN (direct URL, default variant)
    """

    def __init__(
        self,
        book_lookup_service: Optional[BookLookupService] = None,
        google_books_api_key: Optional[str] = None,
        open_library_enabled: bool = True,
    ) -> None:
        self._book_lookup = book_lookup_service or BookLookupService(
            google_books_api_key=google_books_api_key,
            open_library_enabled=open_library_enabled,
        )

    def resolve_cover_urls(self, metadata: BookMetadata) -> List[str]:
        """Return ALL candidate cover URLs in priority order.

        This allows the downloader to try each URL until one passes
        image validation (dimensions, format, etc.).
        """
        urls: List[str] = []

        def _normalize_url(url: Optional[str]) -> Optional[str]:
            """Normalize http:// to https:// for consistent requests."""
            if url and url.startswith("http://"):
                return "https://" + url[7:]
            return url

        def _add(url: Optional[str]) -> None:
            normalized = _normalize_url(url)
            if normalized and normalized not in urls:
                urls.append(normalized)

        # Priority 1: cover_url already set by metadata identification
        _add(metadata.cover_url)

        # Priority 2: image_links from Google Books (larger sizes first)
        if metadata.image_links:
            for size_key in ("extraLarge", "large", "medium", "small"):
                _add(metadata.image_links.get(size_key))
            # Also add explicit thumbnail if not already present
            _add(metadata.image_links.get("thumbnail"))
            _add(metadata.image_links.get("smallThumbnail"))

        # Priority 3: thumbnail_url
        _add(metadata.thumbnail_url)

        # Priority 4: ISBN-based lookup via BookLookupService
        isbn = metadata.isbn_13 or metadata.isbn_10
        if isbn:
            candidates = self._book_lookup.search_by_isbn(isbn)
            for candidate_urls in self._extract_all_covers(candidates):
                for u in candidate_urls:
                    _add(u)

        # Priority 5: title + author lookup via BookLookupService
        if metadata.title:
            author = metadata.authors[0] if metadata.authors else None
            candidates = self._book_lookup.search_by_title_author(
                metadata.title, author
            )
            for candidate_urls in self._extract_all_covers(candidates):
                for u in candidate_urls:
                    _add(u)

        # Priority 6: Open Library cover by ISBN (direct URL)
        # Try large variant first, then default
        ol_urls = self._build_open_library_isbn_urls(isbn)
        for ol_url in ol_urls:
            _add(ol_url)

        # Priority 7: Open Library cover by cover_id (direct URL from candidates)
        # Already handled in priorities 4-5 via search results, but add
        # direct OL title-based URL as ultimate fallback
        ol_title_url = self._build_open_library_title_url(metadata)
        _add(ol_title_url)

        if urls:
            logger.info(
                "Cover resolution found %d candidate URLs for drive_file_id=%s",
                len(urls),
                metadata.drive_file_id,
            )
        else:
            logger.warning(
                "No cover found for drive_file_id=%s",
                metadata.drive_file_id,
            )

        return urls

    def resolve_cover_url(self, metadata: BookMetadata) -> Optional[str]:
        """Return the best cover URL for the given metadata.

        Returns None if no cover is available.
        This is a convenience method that returns only the first URL.
        """
        urls = self.resolve_cover_urls(metadata)
        return urls[0] if urls else None

    # Alias for backwards-compatible calling convention
    def resolve(self, metadata: BookMetadata) -> Optional[str]:
        """Alias for ``resolve_cover_url``."""
        return self.resolve_cover_url(metadata)

    @staticmethod
    def _extract_best_cover(candidates: List[BookMetadata]) -> Optional[str]:
        """Extract the best cover URL from a list of candidates.

        Prefers cover_url, then large/medium image_links, then thumbnail.
        """
        for c in candidates:
            if c.cover_url:
                return c.cover_url
            if c.image_links:
                for size_key in ("extraLarge", "large", "medium"):
                    val = c.image_links.get(size_key)
                    if val:
                        return str(val)
            if c.thumbnail_url:
                return c.thumbnail_url
        return None

    @staticmethod
    def _extract_all_covers(
        candidates: List[BookMetadata],
    ) -> List[List[str]]:
        """Extract ALL cover URLs from each candidate (ordered by quality).

        Returns a list of lists — each inner list contains URLs for one
        candidate, ordered from best to worst quality.
        """
        result: List[List[str]] = []
        for c in candidates:
            candidate_urls: List[str] = []
            if c.cover_url:
                candidate_urls.append(c.cover_url)
            if c.image_links:
                for size_key in ("extraLarge", "large", "medium", "small"):
                    url = c.image_links.get(size_key)
                    if url and url not in candidate_urls:
                        candidate_urls.append(url)
            if c.thumbnail_url and c.thumbnail_url not in candidate_urls:
                candidate_urls.append(c.thumbnail_url)
            if candidate_urls:
                result.append(candidate_urls)
        return result

    @staticmethod
    def _build_open_library_isbn_urls(isbn: Optional[str]) -> List[str]:
        """Build Open Library cover URLs by ISBN.

        Open Library supports both ISBN-based and cover-ID-based URLs.
        For ISBN, the API returns different default sizes:
        - .jpg → medium (M, ~250px)
        - -L.jpg → large
        - -M.jpg → medium
        - -S.jpg → small

        We return large first, then medium, then default, so the
        downloader can try progressively smaller sizes if needed.
        """
        if not isbn:
            return []

        clean_isbn = isbn.replace("-", "").strip()
        base = f"https://covers.openlibrary.org/b/isbn/{quote_plus(clean_isbn)}"
        return [f"{base}-L.jpg", f"{base}-M.jpg", f"{base}.jpg"]

    @staticmethod
    def _build_open_library_title_url(metadata: BookMetadata) -> Optional[str]:
        """Build a direct Open Library cover URL by title+author search.

        This uses the Open Library search API to find the OLID/ID
        and construct a cover URL. Falls back to bid (book ID) search.
        """
        if not metadata.title:
            return None
        # Open Library search API returns docs with 'cover_i'
        # Cover URL format: https://covers.openlibrary.org/b/ISBN.jpg or
        # https://covers.openlibrary.org/b/id/ID.jpg
        # We can't construct these without a search result, so this
        # method returns None (the search-based lookup in priority 4
        # handles this via BookLookupService).
        return None

    def validate_cover_url(self, url: str) -> bool:
        """Check if a cover URL is reachable.

        Uses a HEAD request so we don't download the full image
        just to check availability.
        """
        if not url:
            return False
        try:
            import requests as _requests

            response = _requests.head(url, timeout=5, allow_redirects=True)
            return bool(response.status_code == 200)
        except Exception:
            return False
