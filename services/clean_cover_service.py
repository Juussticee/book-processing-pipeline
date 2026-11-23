"""Clean cover retrieval service (Phase 35B).

Retrieves CLEAN DIGITAL book covers from external bibliographic sources
using the VERIFIED vision title + author as the search identity.

Cover source priority:
1. Google Books (imageLinks — large, medium, thumbnail)
2. Open Library (covers.openlibrary.org by cover ID or ISBN)
3. Exa web search (publisher/library/bookstore sites)

The original Google Drive images are NEVER used as final covers.
They remain available only for OCR, vision analysis, debugging, and audit.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from core.logger import setup_logger
from models.book_metadata import BookMetadata
from services.book_lookup_service import BookLookupService

logger = setup_logger(__name__)


@dataclass
class CoverCandidate:
    """A candidate cover image from an external source."""

    url: str
    source: str  # 'google_books', 'open_library', 'exa'
    title: Optional[str] = None
    author: Optional[str] = None
    isbn: Optional[str] = None
    publisher: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    edition_info: Optional[str] = None


class CleanCoverService:
    """Retrieves clean external book covers for identified books.

    Uses vision-verified title + author to search external sources.
    Google Books is prioritized. Open Library and Exa web search
    serve as fallbacks.

    Drive images are NEVER used as final covers.
    """

    GOOGLE_BOOKS_COVERS_BASE = "https://books.google.com/books"
    OPEN_LIBRARY_COVERS_BASE = "https://covers.openlibrary.org/b"

    # URL fragments that identify placeholder / promotional artwork rather
    # than a real book cover (e.g. Amazon's Kindle QR-code tile, or the
    # 1x1 spacer image some providers return when they have no cover).
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
    # Minimum cover dimensions.  A 261x261 QR tile can pass a pure
    # width/height check, so the token list above is used as well.
    MIN_COVER_WIDTH = 200
    MIN_COVER_HEIGHT = 250

    def __init__(
        self,
        book_lookup: BookLookupService,
        covers_dir: str = "data/covers",
    ) -> None:
        self._book_lookup = book_lookup
        self._covers_dir = covers_dir
        self._processed_cache: dict[
            str, Tuple[Optional[str], Optional[str], Optional[str]]
        ] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_clean_cover(
        self, metadata: BookMetadata
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Find a clean external cover for the given book metadata.

        Uses the VERIFIED vision title + author as search identity.

        Returns:
            (cover_url, cover_source, local_cover_path) tuple.
            cover_url is None if no clean cover was found.
            NEVER returns a Drive image URL.
        """
        vision_title = metadata.title or metadata.vision_title or ""
        vision_author = (
            metadata.authors[0] if metadata.authors else metadata.vision_author or None
        )

        if not vision_title:
            logger.warning(
                "Cannot find clean cover — no title (drive=%s)",
                metadata.drive_file_id,
            )
            return None, "none", None

        # Durability check: a validated cover already on disk is reused with
        # no network access at all (idempotent retries).
        reusable = self._reusable_cover(metadata)
        if reusable is not None:
            logger.info(
                "Reusing existing clean cover for '%s' (drive=%s)",
                vision_title[:50],
                metadata.drive_file_id,
            )
            return reusable

        cache_key = metadata.drive_file_id
        if cache_key in self._processed_cache:
            cached_url, cached_source, cached_local = self._processed_cache[cache_key]
            if cached_local and os.path.exists(cached_local):
                logger.info(
                    "Reusing cached clean cover for '%s' (drive=%s)",
                    vision_title[:50],
                    metadata.drive_file_id,
                )
                return cached_url, cached_source, cached_local

        cover_url: Optional[str] = None
        cover_source = "not_found"
        local_path: Optional[str] = None

        # Priority 1: ISBN-specific Google Books cover (edition match)
        isbn = metadata.isbn_13 or metadata.isbn_10
        if isbn:
            cover_url, cover_source = self._search_google_books_by_isbn(isbn, metadata)
            local_path = self._try_cover(cover_url, cover_source, metadata)

        # Priority 2: Open Library (ISBN first, then title search)
        if not local_path:
            cover_url, cover_source = self._search_open_library(
                vision_title, vision_author, metadata
            )
            local_path = self._try_cover(cover_url, cover_source, metadata)

        # Priority 3: Google Books strong title + author match
        if not local_path:
            cover_url, cover_source = self._search_google_books(
                vision_title, vision_author, metadata
            )
            local_path = self._try_cover(cover_url, cover_source, metadata)

        # Priority 4: Exa web search fallback (existing approved source)
        if not local_path:
            cover_url, cover_source = self._search_exa(
                vision_title, vision_author, metadata
            )
            local_path = self._try_cover(cover_url, cover_source, metadata)

        if local_path:
            # Cache for future lookups (idempotency)
            self._processed_cache[cache_key] = (cover_url, cover_source, local_path)

            logger.info(
                "Clean cover found for '%s': source=%s, url=%s, local=%s",
                vision_title[:50],
                cover_source,
                cover_url,
                local_path,
            )
            return cover_url, cover_source, local_path

        # If we found a cover URL but download/validation failed,
        # still return the URL so the caller can persist it and retry later.
        if cover_url:
            logger.info(
                "Clean cover URL found but download failed for '%s' "
                "(source=%s, url=%s) — returning URL for retry",
                vision_title[:50],
                cover_source,
                cover_url,
            )
            return cover_url, cover_source, None

        logger.info(
            "No clean external cover found for '%s' (drive=%s) — "
            "cover will be left empty, book still exported",
            vision_title[:50],
            metadata.drive_file_id,
        )
        return None, "not_found", None

    # ------------------------------------------------------------------
    # Reuse of an already validated cover
    # ------------------------------------------------------------------

    def _reusable_cover(
        self, metadata: BookMetadata
    ) -> Optional[Tuple[Optional[str], str, Optional[str]]]:
        """Return (url, source, local_path) when a validated cover is on disk.

        This is the durable cache: the database already records the validated
        cover, so no provider is contacted and nothing is re-downloaded.
        """
        path = metadata.local_cover_path
        if not path or not os.path.exists(path):
            return None
        if not self._validate_image_file(path):
            return None
        if self._is_placeholder_url(path):
            return None
        return (
            self._find_existing_cover_url(metadata),
            metadata.cover_source or "cached",
            path,
        )

    # ------------------------------------------------------------------
    # Google Books cover search — ISBN / edition match (PRIORITY 1)
    # ------------------------------------------------------------------

    def _search_google_books_by_isbn(
        self, isbn: str, metadata: BookMetadata
    ) -> Tuple[Optional[str], str]:
        """Find a cover for the exact ISBN, then verify the book identity.

        An exact ISBN match identifies the edition; the title check below is a
        safety net so a provider mis-filing the ISBN cannot attach an unrelated
        cover to an identified book.
        """
        try:
            candidates = self._book_lookup.search_by_isbn(isbn)
        except Exception as exc:
            logger.debug("Google Books ISBN cover search failed: %s", exc)
            return None, "google_books"

        if not isinstance(candidates, list):
            return None, "google_books"

        normalised = isbn.replace("-", "").replace(" ", "").strip()
        for candidate in candidates:
            if candidate.source != "google_books":
                continue
            candidate_isbns = {
                (candidate.isbn_13 or "").replace("-", "").strip(),
                (candidate.isbn_10 or "").replace("-", "").strip(),
            }
            if normalised not in candidate_isbns:
                continue
            if not self._title_compatible(candidate, metadata):
                continue
            cover_url = self._extract_best_cover_url(candidate)
            if cover_url:
                logger.info(
                    "Google Books ISBN cover found (isbn=%s)",
                    normalised,
                )
                return cover_url, "google_books"

        return None, "google_books"

    @staticmethod
    def _title_compatible(candidate: BookMetadata, metadata: BookMetadata) -> bool:
        """Lower-bound title check for an ISBN-matched candidate."""
        from difflib import SequenceMatcher

        if not metadata.title or not candidate.title:
            return True
        similarity = SequenceMatcher(
            None, metadata.title.lower().strip(), candidate.title.lower().strip()
        ).ratio()
        return similarity >= 0.50

    # ------------------------------------------------------------------
    # Google Books cover search by title + author (PRIORITY 3)
    # ------------------------------------------------------------------

    def _try_cover(
        self,
        cover_url: Optional[str],
        cover_source: str,
        metadata: BookMetadata,
    ) -> Optional[str]:
        """Attempt to download a cover URL. Returns local path on success."""
        if not cover_url:
            return None

        local_path = self._download_clean_cover(cover_url, metadata.drive_file_id)
        if not local_path:
            logger.warning(
                "Cover URL from %s failed validation/download for '%s' — "
                "falling through to next source",
                cover_source,
                metadata.title or metadata.vision_title or "Unknown book"[:50],
            )
            return None

        return local_path

    def _search_google_books(
        self,
        title: str,
        author: Optional[str],
        metadata: BookMetadata,
    ) -> Tuple[Optional[str], str]:
        """Search Google Books for a clean cover image.

        Uses vision title + author as search identity. Extracts imageLinks
        from the best Google Books candidate and upgrades HTTP to HTTPS.

        Performs two search queries:
        1. title + author (strictest identity match)
        2. title only (fallback when author name format differs)

        Only returns covers that match the book identity (title/author
        verified via fuzzy matching with normalized author comparison).
        """
        # Query 1: title + author
        try:
            candidates = self._book_lookup.search_google_books_only(title, author)
        except Exception as exc:
            logger.debug("Google Books title+author search failed: %s", exc)
            candidates = []

        best = None
        if candidates:
            best = self._select_best_candidate_with_cover(
                candidates, title, author, metadata
            )
            if not best:
                # Fallback: best by title/author match even without cover
                best = self._select_best_candidate(candidates, title, author, metadata)
                if best:
                    logger.info(
                        "GB Query 1 fallback selected candidate without cover: %s",
                        (best.title or "")[:60],
                    )
        else:
            logger.info("GB Query 1 returned 0 candidates for '%s'", title[:50])

        # Query 2: title only (fallback for author name format mismatches,
        # pen name variants, or GB transient 503 on Query 1)
        if not best or not (best.image_links or best.cover_url):
            logger.info(
                "GB Query 2 (title-only) triggered for '%s', best=%s",
                title[:50],
                "None" if not best else best.title[:50],
            )
            try:
                title_only_candidates = self._book_lookup.search_google_books_only(
                    title, None
                )
            except Exception as exc:
                logger.debug("Google Books title-only search failed: %s", exc)
                title_only_candidates = []
            logger.info(
                "GB Query 2 returned %d candidates for '%s'",
                len(title_only_candidates),
                title[:50],
            )
            if title_only_candidates:
                best_title_only = self._select_best_candidate_with_cover(
                    title_only_candidates, title, author, metadata
                )
                if best_title_only and (
                    best_title_only.image_links or best_title_only.cover_url
                ):
                    best = best_title_only
                    logger.info(
                        "GB Query 2 selected candidate: %s",
                        best.title[:60],
                    )

        if not best:
            logger.info(
                "Google Books cover rejected — no identity match for '%s'",
                title[:50],
            )
            return None, "google_books"

        # Extract best cover URL from image_links (prefer larger sizes)
        cover_url = self._extract_best_cover_url(best)
        if not cover_url:
            return None, "google_books"

        # Upgrade HTTP to HTTPS and maximize image size (zoom/s parameter)
        cover_url = self._upgrade_google_books_url(cover_url)

        logger.info(
            "Google Books cover found for '%s' (isbn13=%s)",
            title[:50],
            metadata.isbn_13 or "N/A",
        )
        return cover_url, "google_books"

    @staticmethod
    def _author_similarity(a1: str, a2: str) -> float:
        """Compute author similarity using both word-set and ordered comparison.

        Handles reversed name order (e.g. "Marcel Poncet" vs "PONCET Marcel")
        by computing Jaccard similarity on word sets, which is order-independent.
        Also computes SequenceMatcher similarity on sorted words to catch
        partial matches. Returns the higher of the two scores.
        """
        from difflib import SequenceMatcher

        words1 = set(a1.lower().strip().split())
        words2 = set(a2.lower().strip().split())
        if not words1 or not words2:
            return 0.0
        intersection = len(words1 & words2)
        union = len(words1 | words2)
        jaccard = intersection / union if union > 0 else 0.0
        seq_sim = SequenceMatcher(
            None, " ".join(sorted(words1)), " ".join(sorted(words2))
        ).ratio()
        return max(jaccard, seq_sim)

    def _select_best_candidate_with_cover(
        self,
        candidates: List[BookMetadata],
        title: str,
        author: Optional[str],
        metadata: Optional[BookMetadata] = None,
    ) -> Optional[BookMetadata]:
        """Select the best candidate that actually has a cover image.

        Like _select_best_candidate, but iterates through ALL candidates
        (not just the top-scoring one) and returns the first one that:
        1. Passes the title similarity threshold
        2. Passes the author similarity threshold (when author provided)
        3. Has image_links or cover_url

        This ensures we find a cover from a matching book even when the
        top-scoring match has no cover image.
        """
        from difflib import SequenceMatcher

        title_threshold = 0.60
        author_threshold = 0.60

        # Sort candidates by title similarity (descending)
        scored: List[Tuple[float, BookMetadata]] = []
        for cand in candidates:
            cand_title = cand.title or ""
            title_sim = SequenceMatcher(
                None, title.lower().strip(), cand_title.lower().strip()
            ).ratio()
            if title_sim < title_threshold:
                continue

            author_score = 0.0
            if author and cand.authors:
                author_sim = self._author_similarity(author, cand.authors[0])
                if author_sim < author_threshold:
                    continue
                author_score = author_sim
            elif not author:
                author_score = 0.5

            score = title_sim * 0.7 + author_score * 0.3
            if metadata and cand.isbn_13 and cand.isbn_13 == metadata.isbn_13:
                score += 0.2

            scored.append((score, cand))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Return first candidate that has a cover
        for score, cand in scored:
            if cand.image_links or cand.cover_url:
                return cand

        return None

    def _extract_best_cover_url(self, candidate: BookMetadata) -> Optional[str]:
        """Extract the highest-quality cover URL from a Google Books candidate.

        Priority: extraLarge > large > medium > thumbnail > smallThumbnail.
        Upgrades Google Books thumbnail URLs to larger sizes when possible
        using documented URL parameters.
        """
        if candidate.image_links:
            # Try each size in priority order
            for size_key in ("extraLarge", "large", "medium", "small"):
                url = candidate.image_links.get(size_key)
                if url:
                    return self._upgrade_google_books_url(url)
            # Fall back to thumbnail/smallThumbnail
            for size_key in ("thumbnail", "smallThumbnail"):
                url = candidate.image_links.get(size_key)
                if url:
                    return self._upgrade_google_books_url(url)

        # Final fallback: cover_url or thumbnail_url
        if candidate.cover_url:
            return self._upgrade_google_books_url(candidate.cover_url)
        if candidate.thumbnail_url:
            return self._upgrade_google_books_url(candidate.thumbnail_url)
        return None

    @staticmethod
    def _upgrade_google_books_url(url: str) -> str:
        """Upgrade Google Books image URL to HTTPS and larger size.

        Google Books thumbnail URLs end with =sXXX (e.g. =s500).
        Replace with =s500 for a good quality image (500px).
        If the URL has no size parameter, append =s500.

        Also upgrades HTTP to HTTPS for Google Books image URLs.
        Only applies documented size parameters — no invented transformations.
        """
        # Upgrade HTTP to HTTPS
        if url and url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        if "books.google.com" in url or "books.googleusercontent.com" in url:
            import re

            # Google Books content URLs use &zoom=1 for small images.
            # Upgrade to zoom=3 for higher resolution (documented API param).
            if re.search(r"[?&]zoom=\d+", url):
                url = re.sub(r"zoom=\d+", "zoom=3", url)
            elif re.search(r"&zoom=", url):
                url = re.sub(r"&zoom=", "&zoom=3", url)
            # Google Books thumbnail URLs may also use =sXXX suffix
            elif re.search(r"=s\d+", url):
                url = re.sub(r"=s\d+", "=s500", url)
            elif re.search(r"=\d+", url):
                url = re.sub(r"=\d+", "=s500", url)
            else:
                url = url + "=s500"
        return url

    def _select_best_candidate(
        self,
        candidates: List[BookMetadata],
        title: str,
        author: Optional[str],
        metadata: Optional[BookMetadata] = None,
    ) -> Optional[BookMetadata]:
        """Select the best Google Books candidate by title/author similarity.

        Requires minimum similarity threshold to avoid wrong-book covers.
        """
        from difflib import SequenceMatcher

        best = None
        best_score = 0.0
        title_threshold = 0.60
        author_threshold = 0.60

        for cand in candidates:
            cand_title = cand.title or ""
            title_sim = SequenceMatcher(
                None, title.lower().strip(), cand_title.lower().strip()
            ).ratio()

            if title_sim < title_threshold:
                continue

            author_score = 0.0
            if author and cand.authors:
                author_sim = self._author_similarity(author, cand.authors[0])
                if author_sim < author_threshold:
                    continue
                author_score = author_sim
            elif not author:
                author_score = 0.5  # No author to match, give partial credit

            # Composite score: title 70%, author 30%
            score = title_sim * 0.7 + author_score * 0.3

            # Bonus for ISBN match
            if metadata and cand.isbn_13 and cand.isbn_13 == metadata.isbn_13:
                score += 0.2

            if score > best_score:
                best_score = score
                best = cand

        return best if best_score >= title_threshold else None

    # ------------------------------------------------------------------
    # Open Library cover search (PRIORITY 2)
    # ------------------------------------------------------------------

    def _search_open_library(
        self,
        title: str,
        author: Optional[str],
        metadata: BookMetadata,
    ) -> Tuple[Optional[str], str]:
        """Search Open Library for a clean cover image.

        Tries ISBN-based lookup first, then title+author search.
        Uses OL cover ID endpoints for large-size images.
        """
        # Try ISBN-based OL cover first
        isbn = metadata.isbn_13 or metadata.isbn_10
        if isbn:
            ol_url = self._open_library_cover_by_isbn(isbn, size="L")
            if ol_url and self._validate_url_is_image(ol_url):
                logger.info("Open Library cover found via ISBN for '%s'", title[:50])
                return ol_url, "open_library"

            # Try medium if large didn't work
            ol_url = self._open_library_cover_by_isbn(isbn, size="M")
            if ol_url and self._validate_url_is_image(ol_url):
                return ol_url, "open_library"

        # Try title+author search via the lookup service
        try:
            candidates = self._book_lookup.search_by_title_author(title, author)
        except Exception as exc:
            logger.debug("Open Library cover search failed: %s", exc)
            return None, "open_library"

        if not candidates:
            return None, "open_library"

        # Get all cover URLs from candidates (best first)
        for cand in candidates:
            if cand.source != "open_library":
                continue
            # Check image_links for OL cover sizes
            if cand.image_links:
                for size_key in ("large", "medium", "small"):
                    url = cand.image_links.get(size_key)
                    if url:
                        url = self._ensure_https(url)
                        if self._validate_url_is_image(url):
                            logger.info(
                                "Open Library cover found via title search for '%s'",
                                title[:50],
                            )
                            return url, "open_library"
            # Check direct cover_url
            if cand.cover_url:
                url = self._ensure_https(cand.cover_url)
                if self._validate_url_is_image(url):
                    return url, "open_library"

        return None, "open_library"

    def _open_library_cover_by_isbn(self, isbn: str, size: str = "L") -> Optional[str]:
        """Build an Open Library cover URL by ISBN.

        Open Library covers API: https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg
        Sizes: -L (large), -M (medium), -S (small), .jpg (default medium)
        """
        clean_isbn = isbn.replace("-", "").strip()
        suffix = f"-{size}.jpg" if size and size != "default" else ".jpg"
        return f"{self.OPEN_LIBRARY_COVERS_BASE}/isbn/{clean_isbn}{suffix}"

    # ------------------------------------------------------------------
    # Exa web search fallback (PRIORITY 3)
    # ------------------------------------------------------------------

    def _search_exa(
        self,
        title: str,
        author: Optional[str],
        metadata: BookMetadata,
    ) -> Tuple[Optional[str], str]:
        """Use Exa web search to find a clean cover from reputable sources.

        Searches for: "<title>" "<author>" book cover
        Prefers publisher, library, catalog, or bookseller sites.
        """
        query = f'"{title}"'
        if author:
            query += f' "{author}"'
        query += " book cover"

        try:
            # Use book_lookup service's Exa integration if available
            cover_url, source_url = self._book_lookup.search_exa_for_cover(
                title, author, query
            )
            if cover_url:
                cover_url = self._ensure_https(cover_url)
                if self._validate_url_is_image(cover_url):
                    logger.info(
                        "Exa cover found for '%s' from %s",
                        title[:50],
                        source_url,
                    )
                    return cover_url, "exa"
        except Exception as exc:
            logger.debug("Exa cover search failed: %s", exc)

        return None, "exa"

    # ------------------------------------------------------------------
    # Cover download
    # ------------------------------------------------------------------

    def _download_clean_cover(
        self, cover_url: str, drive_file_id: str
    ) -> Optional[str]:
        """Download a clean external cover to data/covers/{drive_file_id}.jpg.

        Returns the local file path, or None if download/validation fails.
        Uses a temporary file during download and only moves it to the final
        path if it passes image validation.
        """
        os.makedirs(self._covers_dir, exist_ok=True)
        local_path = os.path.join(self._covers_dir, f"{drive_file_id}.jpg")

        # Don't re-download if we already have a valid clean cover
        if os.path.exists(local_path) and self._validate_image_file(local_path):
            logger.info("Reusing existing clean cover at %s", local_path)
            return local_path

        # Download to a temporary file first
        temp_path = os.path.join(self._covers_dir, f"{drive_file_id}.tmp")

        try:
            import requests as _requests

            response = _requests.get(cover_url, timeout=15, stream=True)
            response.raise_for_status()

            if self._is_placeholder_url(cover_url):
                logger.warning("Refusing placeholder cover URL: %s", cover_url)
                response.close()
                return None

            # Check content type — if URL has an image extension, accept
            # even when content-type header is missing or non-standard (e.g. OL)
            content_type = response.headers.get("content-type", "")
            url_lower = cover_url.lower()
            has_image_ext = any(
                url_lower.endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")
            )
            if not content_type.startswith("image/") and not has_image_ext:
                logger.warning(
                    "Cover URL returned non-image content-type: %s",
                    content_type,
                )
                response.close()
                return None

            # Write to temp file
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            response.close()

            # Validate the downloaded file
            if not self._validate_image_file(temp_path):
                logger.warning("Downloaded cover at %s failed validation", temp_path)
                os.remove(temp_path)
                return None

            # Move temp file to final path
            shutil.move(temp_path, local_path)
            logger.info(
                "Downloaded clean cover: %s -> %s (%d bytes)",
                cover_url,
                local_path,
                os.path.getsize(local_path),
            )
            return local_path

        except Exception as exc:
            logger.warning("Failed to download cover from %s: %s", cover_url, exc)
            # Clean up temp file if it exists
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None

    @classmethod
    def _validate_image_file(cls, path: str) -> bool:
        """Validate that a local file is a real cover image.

        Rejects: unreadable files, animated GIF placeholders (the format Open
        Library serves for missing covers), images smaller than a usable cover,
        and images with a placeholder file name.
        """
        if not os.path.exists(path):
            return False
        try:
            from PIL import Image

            with Image.open(path) as img:
                if (img.format or "").upper() == "GIF":
                    return False
                width, height = img.size
                # Reject images that are too small (placeholders, sprites, icons)
                if width < cls.MIN_COVER_WIDTH or height < cls.MIN_COVER_HEIGHT:
                    return False
                return True
        except Exception:
            return False

    @classmethod
    def _is_placeholder_url(cls, url: Optional[str]) -> bool:
        """True when a URL/path clearly identifies a placeholder image."""
        if not url:
            return False
        lowered = url.lower()
        return any(token in lowered for token in cls.PLACEHOLDER_URL_TOKENS)

    @staticmethod
    def _ensure_https(url: str) -> str:
        """Upgrade http:// to https://."""
        if url and url.startswith("http://"):
            return "https://" + url[len("http://") :]
        return url

    @classmethod
    def _validate_url_is_image(cls, url: str) -> bool:
        """Check if a URL looks like a real cover image.

        Placeholder URLs are rejected even when they end in an image
        extension.  A positive result here is only a cheap pre-filter; every
        candidate is still downloaded and validated before it is stored.
        """
        if not url:
            return False
        if cls._is_placeholder_url(url):
            return False
        parsed = urlparse(url)
        path = parsed.path.lower()
        # Check file extension
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            if path.endswith(ext):
                return True
        # Google Books URLs don't have extensions but are known to return images
        if "books.google.com" in url or "books.googleusercontent.com" in url:
            return True
        # Open Library URLs
        if "covers.openlibrary.org" in url:
            return True
        return False

    def _find_existing_cover_url(self, metadata: BookMetadata) -> Optional[str]:
        """Return the already-persisted cover URL for a cached cover.

        Deliberately does NOT search providers: this is the offline path used
        when a validated cover is already on disk.  (The previous implementation
        called ``_search_internet_archive``/``_search_bnf`` on this class, which
        do not exist here — it existed only on ``BookLookupService`` — so it
        raised ``AttributeError`` and failed the whole export.)
        """
        for url in (metadata.cover_source_url, metadata.cover_url):
            if url and not self._is_placeholder_url(url):
                return self._ensure_https(url)
        return None
