"""Book lookup service — searches Google Books and Open Library.

Google Books is the primary source (requires API key).
Open Library is the fallback (no API key needed).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from core.logger import setup_logger
from models.book_metadata import BookMetadata
from utils.redaction import redact, redact_url
from utils.retry import retry_with_backoff

# Lazy import guard for requests (created in _get_session)
requests: Optional[object] = None  # type: ignore

logger = setup_logger(__name__)


class BookLookupService:
    """Searches book metadata from Google Books and Open Library.

    Search priority:
    1. ISBN (exact match)
    2. Title + Author
    3. Title only
    4. Author only

    Google Books API is the primary source. Open Library is the fallback.
    All HTTP calls should be mocked in tests.
    """

    GOOGLE_BOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"
    OPEN_LIBRARY_BASE = "https://openlibrary.org/search.json"
    OPEN_LIBRARY_COVERS = "https://covers.openlibrary.org/b"

    INTERNET_ARCHIVE_BASE = "https://archive.org/advancedsearch.php"
    INTERNET_ARCHIVE_COVER = "https://archive.org/services/img/{identifier}"
    BNF_SRU_BASE = "https://catalogue.bnf.fr/api/SRU"

    def __init__(
        self,
        google_books_api_key: Optional[str] = None,
        google_books_timeout: float = 15.0,
        open_library_enabled: bool = True,
        open_library_timeout: float = 15.0,
        exa_api_key: Optional[str] = None,
    ) -> None:
        self._google_books_api_key = google_books_api_key
        self._open_library_enabled = open_library_enabled
        self._google_books_timeout = google_books_timeout
        self._open_library_timeout = open_library_timeout
        self._exa_api_key = exa_api_key or os.getenv("EXA_API_KEY", "")
        self._session = self._get_session()
        self._query_count = 0

    @property
    def query_count(self) -> int:
        """Number of metadata searches issued (for the run summary)."""
        return self._query_count

    def reset_stats(self) -> None:
        self._query_count = 0

    def _get_session(self):
        """Get a requests session (created lazily for testability)."""
        global requests
        if requests is None:
            import requests as _requests  # noqa: WPS433

            requests = _requests
        logger.debug("Creating requests session")
        return requests.Session()

    def search_by_isbn(self, isbn: str) -> List[BookMetadata]:
        """Search for a book by ISBN.

        Args:
            isbn: ISBN-10 or ISBN-13 string.

        Returns:
            List of BookMetadata candidates (may be empty).
        """
        logger.info("Searching by ISBN: %s", isbn)
        self._query_count += 1
        candidates: List[BookMetadata] = []

        # Try Google Books first (if API key available)
        gb_results = self._search_google_books_by_isbn(isbn)
        candidates.extend(gb_results)

        # Fall back to Open Library
        if not candidates and self._open_library_enabled:
            ol_results = self._search_open_library_by_isbn(isbn)
            candidates.extend(ol_results)

        logger.info("ISBN search returned %d candidates", len(candidates))
        return candidates

    def search_by_title_author(
        self, title: str, author: Optional[str] = None
    ) -> List[BookMetadata]:
        """Search for a book by title and optionally author.

        Args:
            title: Book title.
            author: Author name (optional).

        Returns:
            List of BookMetadata candidates (may be empty).
        """
        logger.info("Searching by title='%s', author='%s'", title, author)
        self._query_count += 1
        candidates: List[BookMetadata] = []

        # Google Books first
        gb_results = self._search_google_books_by_title(title, author)
        candidates.extend(gb_results)

        # Open Library fallback if no results
        if not candidates and self._open_library_enabled:
            ol_results = self._search_open_library_by_title(title, author)
            candidates.extend(ol_results)

        logger.info("Title search returned %d candidates", len(candidates))
        return candidates

    def search_by_title(self, title: str) -> List[BookMetadata]:
        """Search for a book by title only.

        Args:
            title: Book title.

        Returns:
            List of BookMetadata candidates.
        """
        return self.search_by_title_author(title, None)

    def search_by_author(self, author: str) -> List[BookMetadata]:
        """Search for books by author name.

        Args:
            author: Author name.

        Returns:
            List of BookMetadata candidates.
        """
        logger.info("Searching by author: %s", author)
        candidates: List[BookMetadata] = []

        # Google Books first
        gb_results = self._search_google_books_by_author(author)
        candidates.extend(gb_results)

        # Open Library fallback if no results
        if not candidates and self._open_library_enabled:
            ol_results = self._search_open_library_by_author(author)
            candidates.extend(ol_results)

        logger.info("Author search returned %d candidates", len(candidates))
        return candidates

    # --- Google Books by author ---
    def _search_google_books_by_author(self, author: str) -> List[BookMetadata]:
        """Search Google Books by author."""
        if not self._google_books_api_key:
            logger.debug("No Google Books API key — skipping author search")
            return []

        query = f"inauthor:{quote_plus(author)}"
        url = f"{self.GOOGLE_BOOKS_BASE}?q={query}&maxResults=10"
        if self._google_books_api_key:
            url += f"&key={self._google_books_api_key}"

        return self._google_books_request(url, title=author)

    # --- Open Library by author ---
    def _search_open_library_by_author(self, author: str) -> List[BookMetadata]:
        """Search Open Library by author."""
        url = f"{self.OPEN_LIBRARY_BASE}?author={quote_plus(author)}"

        try:
            response = self._make_request(url, self._open_library_timeout)
            if response is None:
                return []
            response.raise_for_status()
            data = response.json()

            docs = data.get("docs", [])
            candidates = []
            for doc in docs[:10]:
                metadata = self._parse_open_library_doc(doc)
                if metadata:
                    candidates.append(metadata)
            return candidates

        except Exception as exc:
            logger.warning("Open Library author search failed: %s", exc)
            return []

    # --- Google Books API ---

    def _search_google_books_by_isbn(self, isbn: str) -> List[BookMetadata]:
        """Search Google Books by ISBN."""
        if not self._google_books_api_key:
            logger.debug("No Google Books API key — skipping ISBN search")
            return []

        url = f"{self.GOOGLE_BOOKS_BASE}?q=isbn:{quote_plus(isbn)}"
        if self._google_books_api_key:
            url += f"&key={self._google_books_api_key}"

        return self._google_books_request(url, isbn=isbn)

    def _search_google_books_by_title(
        self, title: str, author: Optional[str] = None
    ) -> List[BookMetadata]:
        """Search Google Books by title (and optionally author)."""
        if not self._google_books_api_key:
            logger.debug("No Google Books API key — skipping title search")
            return []

        # Wrap multi-word titles/authors in quotes to avoid GB 400 errors
        # on phrase queries like 'intitle:LA CONSTRUCTION DES VILLES'
        if " " in title.strip():
            quoted_title = quote_plus('"' + title + '"')
            query = f"intitle:{quoted_title}"
        else:
            query = f"intitle:{quote_plus(title)}"
        if author:
            if " " in author.strip():
                quoted_author = quote_plus('"' + author + '"')
                query += f"+inauthor:{quoted_author}"
            else:
                query += f"+inauthor:{quote_plus(author)}"

        url = f"{self.GOOGLE_BOOKS_BASE}?q={query}&maxResults=10"
        if self._google_books_api_key:
            url += f"&key={self._google_books_api_key}"

        return self._google_books_request(url, title=title)

    def search_google_books_only(
        self, title: str, author: Optional[str] = None
    ) -> List[BookMetadata]:
        """Search ONLY Google Books (bypass Open Library fallback).
        Used by the enrichment service as a secondary source when
        Open Library didn't provide certain fields.
        """
        return self._search_google_books_by_title(title, author)

    # --- HTTP helpers ---

    def _make_request(self, url: str, timeout: float):
        """Make an HTTP request with retry on transient failures.

        Retries on HTTP 429/500/502/503/504, connection errors, and
        timeouts. Permanent HTTP errors (e.g. 400/404) are NOT retried.

        Returns the Response object or ``None`` if all retries failed.
        """
        from utils.http_errors import classify_http_error

        @retry_with_backoff(max_retries=5, initial_delay=2.0, backoff_factor=2.0)
        def _do_request(url: str, timeout: float):
            response = self._session.get(url, timeout=timeout)
            category, is_transient = classify_http_error(response=response)
            if category == "success":
                # 2xx/3xx is a usable response -- return the payload.
                return response
            if not is_transient:
                logger.debug(
                    "HTTP %s for %s classified as permanent -- not retrying",
                    response.status_code,
                    redact_url(url),
                )
                # re-raise as a non-retryable exception so the retry wrapper bails out
                raise RuntimeError(f"Permanent HTTP {response.status_code}")
            return response

        try:
            return _do_request(url, timeout)
        except Exception as exc:
            logger.warning(
                "HTTP request failed after retries (%s): %s",
                redact_url(url),
                redact(exc),
            )
            return None

    def _google_books_request(
        self, url: str, isbn: Optional[str] = None, title: Optional[str] = None
    ) -> List[BookMetadata]:
        """Make a request to the Google Books API and parse results.

        Retries on transient HTTP errors (429, 500, 502, 503, 504,
        connection errors, timeouts) with exponential backoff.
        """
        try:
            response = self._make_request(url, self._google_books_timeout)
            if response is None:
                return []
            if not getattr(response, "ok", False):
                # Guard ``raise_for_status`` against a dummy object returned
                # by the retry wrapper when a permanent HTTP error is seen.
                return []
            try:
                response.raise_for_status()
            except Exception as http_exc:
                logger.warning(
                    "Google Books request failed with HTTP error: %s", http_exc
                )
                return []
            data = response.json()

            items = data.get("items", [])
            candidates = []
            for item in items:
                metadata = self._parse_google_books_item(item, isbn=isbn, title=title)
                if metadata:
                    candidates.append(metadata)
            return candidates

        except Exception as exc:
            logger.warning("Google Books request failed: %s", exc)
            return []

    @staticmethod
    def _parse_google_books_item(
        item: Dict, isbn: Optional[str] = None, title: Optional[str] = None
    ) -> Optional[BookMetadata]:
        """Parse a single Google Books API result into BookMetadata."""
        try:
            volume_info = item.get("volumeInfo", {})

            # Extract ISBNs
            industry_ids = volume_info.get("industryIdentifiers", [])
            isbn_10 = None
            isbn_13 = None
            for id_entry in industry_ids:
                id_type = id_entry.get("type", "")
                id_value = id_entry.get("id", "")
                if id_type == "ISBN_10":
                    isbn_10 = id_value
                elif id_type == "ISBN_13":
                    isbn_13 = id_value

            # Extract authors
            authors = volume_info.get("authors", [])
            if isinstance(authors, str):
                authors = [authors]

            # Extract categories
            categories = volume_info.get("categories", [])

            # Extract image links
            image_links = volume_info.get("imageLinks", {})
            thumbnail = image_links.get("thumbnail")
            cover_url = image_links.get("thumbnail") or image_links.get("cover")
            # Store all image link sizes for fallback resolution
            google_image_links = {}
            for size_key in (
                "smallThumbnail",
                "thumbnail",
                "small",
                "medium",
                "large",
                "extraLarge",
            ):
                url = image_links.get(size_key)
                if url:
                    # Google Books thumbnail URLs often have =sXXX suffix
                    # Replace with =s500 for a larger version if it's a thumbnail
                    clean_url = url.replace("http://", "https://")
                    google_image_links[size_key] = clean_url

            return BookMetadata(
                title=volume_info.get("title", ""),
                subtitle=volume_info.get("subtitle"),
                authors=list(authors) if authors else [],
                isbn_10=isbn_10,
                isbn_13=isbn_13,
                publisher=volume_info.get("publisher"),
                published_date=volume_info.get("publishedDate"),
                language=volume_info.get("language"),
                page_count=volume_info.get("pageCount"),
                categories=list(categories) if categories else [],
                description=volume_info.get("description"),
                thumbnail_url=thumbnail,
                cover_url=cover_url,
                image_links=google_image_links,
                confidence=0.0,  # Will be calculated by ConfidenceService
                source="google_books",
                raw_ocr="",  # Will be filled by MetadataService
            )
        except Exception as exc:
            logger.debug("Failed to parse Google Books item: %s", exc)
            return None

    # --- Open Library API ---

    def _search_open_library_by_isbn(self, isbn: str) -> List[BookMetadata]:
        """Search Open Library by ISBN."""
        url = f"{self.OPEN_LIBRARY_BASE}?isbn={quote_plus(isbn)}"

        try:
            response = self._make_request(url, self._open_library_timeout)
            if response is None:
                return []
            response.raise_for_status()
            data = response.json()

            docs = data.get("docs", [])
            candidates = []
            for doc in docs[:10]:
                metadata = self._parse_open_library_doc(doc)
                if metadata:
                    candidates.append(metadata)
            return candidates

        except Exception as exc:
            logger.warning("Open Library ISBN search failed: %s", exc)
            return []

    def _search_open_library_by_title(
        self, title: str, author: Optional[str] = None
    ) -> List[BookMetadata]:
        """Search Open Library by title (and optionally author)."""
        url = f"{self.OPEN_LIBRARY_BASE}?title={quote_plus(title)}"
        if author:
            url += f"&author={quote_plus(author)}"

        try:
            response = self._make_request(url, self._open_library_timeout)
            if response is None:
                return []
            response.raise_for_status()
            data = response.json()

            docs = data.get("docs", [])
            candidates = []
            for doc in docs[:10]:
                metadata = self._parse_open_library_doc(doc)
                if metadata:
                    candidates.append(metadata)
            return candidates

        except Exception as exc:
            logger.warning("Open Library title search failed: %s", exc)
            return []

    @staticmethod
    def _parse_open_library_doc(doc: Dict) -> Optional[BookMetadata]:
        """Parse a single Open Library search result into BookMetadata."""
        try:
            title = doc.get("title", "")
            if not title:
                return None

            authors = doc.get("author_name", [])
            if isinstance(authors, str):
                authors = [authors]

            # Extract ISBNs
            isbn_list = doc.get("isbn", [])
            isbn_10 = None
            isbn_13 = None
            for isbn in isbn_list:
                if not isbn:
                    continue
                if len(isbn) == 13 and isbn.startswith(("978", "979")):
                    isbn_13 = isbn
                elif len(isbn) == 10:
                    isbn_10 = isbn

            # Construct cover URL
            cover_id = doc.get("cover_i")
            cover_url = None
            ol_image_links = {}
            if cover_id:
                base_url = f"{BookLookupService.OPEN_LIBRARY_COVERS}/b/id/{cover_id}"
                cover_url = f"{base_url}.jpg"  # Default (medium)
                ol_image_links["large"] = f"{base_url}-L.jpg"
                ol_image_links["medium"] = f"{base_url}-M.jpg"
                ol_image_links["small"] = f"{base_url}-S.jpg"

            publisher = doc.get("publisher", [])
            publisher_name = publisher[0] if publisher else None

            first_publish_year = doc.get("first_publish_year")
            published_date = str(first_publish_year) if first_publish_year else None

            return BookMetadata(
                title=title,
                subtitle=doc.get("subtitle"),
                authors=list(authors) if authors else [],
                isbn_10=isbn_10,
                isbn_13=isbn_13,
                publisher=publisher_name,
                published_date=published_date,
                language=(
                    doc.get("language", [None])[0] if doc.get("language") else None
                ),
                page_count=None,
                categories=doc.get("subject", [])[:5] if doc.get("subject") else [],
                description=None,
                thumbnail_url=cover_url,
                cover_url=cover_url,
                image_links=ol_image_links,
                confidence=0.0,
                source="open_library",
                raw_ocr="",
                open_library_work_key=doc.get("key"),
            )
        except Exception as exc:
            logger.debug("Failed to parse Open Library doc: %s", exc)
            return None

    # --- Open Library Work / Edition detail fetch ---

    OPEN_LIBRARY_WORKS = "https://openlibrary.org/works"
    EXA_SEARCH_API = "https://api.exa.ai/search"

    # --- Internet Archive ---

    def search_internet_archive(
        self, title: str, author: Optional[str] = None
    ) -> List[BookMetadata]:
        """Public wrapper for Internet Archive search."""
        return self._search_internet_archive(title, author)

    def _search_internet_archive(
        self, title: str, author: Optional[str]
    ) -> List[BookMetadata]:
        query = f'"{title}"'
        if author:
            query += f' "{author}"'
        url = (
            f"{self.INTERNET_ARCHIVE_BASE}?"
            f"q={quote_plus(query)}&fl%5B%5D=identifier,title,creator,cover"
            f"&output=json&rows=5"
        )
        try:
            data = self._fetch_json(url)
            if not data:
                return []
            docs = data.get("response", {}).get("docs", [])
            candidates: List[BookMetadata] = []
            for doc in docs:
                identifier = doc.get("identifier", "")
                if not identifier:
                    continue
                doc_title = doc.get("title", "") or title
                doc_author = doc.get("creator", "") or (author or "")
                cover_url = self.INTERNET_ARCHIVE_COVER.format(identifier=identifier)
                candidates.append(
                    BookMetadata(
                        title=doc_title,
                        authors=(
                            [doc_author] if doc_author else ([author] if author else [])
                        ),
                        cover_url=cover_url,
                        thumbnail_url=cover_url,
                        source="internet_archive",
                        drive_file_id="",
                        confidence=0.7,
                    )
                )
            logger.info(
                "Internet Archive returned %d candidates for %s",
                len(candidates),
                title[:50],
            )
            return candidates
        except Exception as exc:
            logger.debug("Internet Archive search failed: %s", exc)
            return []

    # --- BnF / Gallica ---

    def search_bnf(
        self, title: str, author: Optional[str] = None
    ) -> List[BookMetadata]:
        """Public wrapper for BnF search."""
        return self._search_bnf(title, author)

    def _search_bnf(self, title: str, author: Optional[str]) -> List[BookMetadata]:
        query = f'title adj "{title}"'
        if author:
            query += f' and author adj "{author}"'
        url = (
            f"{self.BNF_SRU_BASE}?"
            f"version=1.2&operation=searchRetrieve&query={quote_plus(query)}"
            f"&maximumRecords=5&recordSchema=dc"
        )
        try:
            response = self._make_request(url, self._open_library_timeout)
            if response is None:
                return []
            import xml.etree.ElementTree as _ET

            root = _ET.fromstring(response.text)
            ns = {
                "srw": "http://www.loc.gov/zing/srw/",
                "dc": "http://purl.org/dc/elements/1.1/",
            }
            records = root.findall(".//srw:record", ns)
            candidates: List[BookMetadata] = []
            for rec in records[:5]:
                record_data = rec.find(".//srw:recordData", ns)
                if record_data is None or len(record_data) == 0:
                    continue
                inner = record_data[0]
                title_els = inner.findall(".//dc:title", ns)
                creator_els = inner.findall(".//dc:creator", ns)
                identifier_els = inner.findall(".//dc:identifier", ns)
                doc_title = title_els[0].text if title_els else title
                doc_author = creator_els[0].text if creator_els else (author or "")
                arks = [i.text for i in identifier_els if i.text and "ark:/" in i.text]
                cover_url = ""
                if arks:
                    ark_raw = arks[0]
                    ark_id = ark_raw.split("/ark:/")[1].replace("/", "_")
                    cover_url = f"https://gallica.bnf.fr/ark:{ark_id}.full"
                if cover_url:
                    candidates.append(
                        BookMetadata(
                            title=doc_title,
                            authors=(
                                [doc_author]
                                if doc_author
                                else ([author] if author else [])
                            ),
                            cover_url=cover_url,
                            thumbnail_url=cover_url,
                            source="bnf",
                            drive_file_id="",
                            confidence=0.7,
                        )
                    )
            logger.info(
                "BnF returned %d candidates for %s", len(candidates), title[:50]
            )
            return candidates
        except Exception as exc:
            logger.debug("BnF search failed: %s", exc)
            return []

    def fetch_open_library_work_details(self, work_key: str) -> Dict:
        """Fetch detailed work info from Open Library.

        Given a work key like '/works/OL12627957W' or 'OL12627957W',
        fetches the Work JSON and its editions, returning supplementary
        fields not available from the search API: isbn_10, isbn_13,
        publisher, published_date, language, categories (subjects),
        description, cover_url.
        """
        # Normalise work key
        if work_key.startswith("/works/"):
            olid = work_key.replace("/works/", "")
        elif work_key.startswith("OL") and work_key.endswith("W"):
            olid = work_key
        else:
            olid = work_key

        result: Dict = {
            "isbn_10": None,
            "isbn_13": None,
            "publisher": None,
            "published_date": None,
            "language": None,
            "page_count": None,
            "categories": [],
            "description": None,
            "cover_url": None,
            "olid": olid,
        }

        # Fetch the work record (subjects, description, first publish date)
        work_url = f"{self.OPEN_LIBRARY_WORKS}/{olid}.json"
        response = self._make_request(work_url, self._open_library_timeout)
        work_data = response.json() if response else None
        if work_data:
            subjects_raw = work_data.get("subjects", [])
            if subjects_raw:
                result["categories"] = [
                    s["name"] if isinstance(s, dict) else s for s in subjects_raw
                ][:5]
            desc = work_data.get("description")
            if isinstance(desc, dict):
                result["description"] = desc.get("value", "")
            elif isinstance(desc, str):
                result["description"] = desc
            if work_data.get("first_publish_date"):
                result["published_date"] = work_data["first_publish_date"]

        # Fetch editions (ISBNs, publisher, publish date, language, covers)
        editions_url = f"{self.OPEN_LIBRARY_WORKS}/{olid}/editions.json"
        response = self._make_request(editions_url, self._open_library_timeout)
        ed_data = response.json() if response else None
        if ed_data and "entries" in ed_data:
            # Pick the first edition that has ISBNs, preferring ones
            # that also have publisher/date/language information
            for ed in ed_data["entries"]:
                if ed.get("isbn_10") and not result["isbn_10"]:
                    result["isbn_10"] = ed["isbn_10"][0]
                if ed.get("isbn_13") and not result["isbn_13"]:
                    result["isbn_13"] = ed["isbn_13"][0]
                if ed.get("publishers") and not result["publisher"]:
                    result["publisher"] = ed["publishers"][0]
                if ed.get("number_of_pages") and not result["page_count"]:
                    result["page_count"] = ed["number_of_pages"]
                if ed.get("publish_date") and not result["published_date"]:
                    result["published_date"] = ed["publish_date"]
                if ed.get("languages") and not result["language"]:
                    lang = ed["languages"][0]
                    if isinstance(lang, dict):
                        lang = lang.get("key", "")
                    # Normalize Open Library language codes
                    if lang.startswith("/languages/"):
                        lang = lang.split("/languages/", 1)[1]
                    result["language"] = lang
                if ed.get("covers") and not result["cover_url"]:
                    cover_id = ed["covers"][0]
                    result["cover_url"] = (
                        f"{self.OPEN_LIBRARY_COVERS}/b/id/{cover_id}.jpg"
                    )

        return result

    def search_exa_for_cover(
        self,
        title: str,
        author: Optional[str],
        query: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Search Exa for a clean book cover image.

        Searches for: '<title>' '<author>' book cover
        Returns (cover_image_url, source_url) or (None, None).

        Only used when Google Books and Open Library have not yielded a cover.
        Fetches result pages to extract actual image URLs, validates them,
        and downloads to confirm they are real covers (not placeholders).
        """
        if not self._exa_api_key:
            logger.debug("No Exa API key configured — skipping Exa cover search")
            return None, None

        headers = {
            "Authorization": f"Bearer {self._exa_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "numResults": 10,
            "includeImages": True,
            "category": "pdf",
        }

        try:
            response = self._session.post(
                self.EXA_SEARCH_API,
                json=payload,
                headers=headers,
                timeout=self._open_library_timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.debug("Exa search failed: %s", exc)
            return None, None

        results = data.get("results", [])
        for result in results:
            source_url = result.get("url", "")
            if not source_url:
                continue

            # Step 1: Check for direct image URL from Exa result
            image_url = result.get("imageUrl")
            if image_url and self._is_reputable_source(source_url):
                return image_url, source_url

            # Step 2: Check imageLinks in the result
            image_links = result.get("imageLinks", {})
            for url in image_links.values():
                if isinstance(url, str) and self._is_reputable_source(url):
                    return url, source_url

            # Step 3: Fetch the webpage and extract image URLs
            extracted_image = self._extract_image_from_page(source_url)
            if extracted_image:
                logger.info(
                    "Exa image extracted from page: %s -> %s",
                    source_url[:60],
                    extracted_image,
                )
                return extracted_image, source_url

        return None, None

    def _extract_image_from_page(self, page_url: str) -> Optional[str]:
        """Fetch a webpage and extract the most likely cover image URL.

        Parses HTML for <img> tags, filters for image-like URLs
        (contains 'cover', 'book', 'front', or has image extensions),
        and validates via URL pattern matching and image dimensions.
        """
        import re

        if not self._is_reputable_source(page_url):
            return None

        try:
            resp = self._session.get(
                page_url,
                timeout=self._open_library_timeout,
                headers={"User-Agent": "BookProcessor/0.1"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            logger.debug("Failed to fetch page %s: %s", page_url, exc)
            return None

        # Find all image URLs in the HTML
        img_urls = re.findall(
            r'[^"\']*(?:\.jpg|\.jpeg|\.png|\.webp|\.gif)[^"\']*',
            html,
            re.IGNORECASE,
        )

        # Filter for likely cover images, prefer cover-related URLs
        cover_candidates = []
        generic_candidates = []
        for url in img_urls:
            url_lower = url.lower()
            # Skip tiny images, placeholders, icons, sprites
            if any(
                skip in url_lower
                for skip in (
                    "1x1",
                    "spacer",
                    "pixel",
                    "button",
                    "icon",
                    "logo",
                    "avatar",
                    "thumb",
                    "spinner",
                    "sprite",
                    "skin",
                    "nav-",
                    "header",
                    "footer",
                    "banner",
                    "bg-",
                    "background",
                    "flyout",
                    "omaha",
                    "yoda",
                    "global-",
                    "amazonaws",
                    "yourprime",
                    "prime",
                    "piv-",
                    "widget",
                    "deals",
                    "sale",
                    "discount",
                    "carousel",
                    "slider",
                    "hero",
                    "jumbotron",
                    "loadind",
                    "load-ind",
                    "reorg",
                )
            ):
                continue
            # Prefer cover-related URLs
            if any(
                kw in url_lower
                for kw in (
                    "cover",
                    "front",
                    "book",
                    "product",
                    "large",
                    "livre",
                    "couverture",
                )
            ):
                cover_candidates.append(url)
            elif url_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
                generic_candidates.append(url)

        # Try cover-specific candidates first, then generic ones
        all_candidates = cover_candidates + generic_candidates

        for url in all_candidates:
            # Resolve relative URLs
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                from urllib.parse import urljoin

                url = urljoin(page_url, url)
            elif not url.startswith(("http://", "https://")):
                continue

            if self._validate_url_is_image(url):
                resolved = url.replace("http://", "https://")
                # Validate actual image dimensions to reject sprites
                if self._validate_image_dimensions(resolved):
                    return str(resolved)

        return None

    def _validate_image_dimensions(
        self, url: str, min_width: int = 80, min_height: int = 100
    ) -> bool:
        """Download an image and check its dimensions.

        Rejects tiny images (placeholders, sprites, icons) and non-portrait
        images (landscape/square) that are unlikely to be book covers.
        """
        try:
            resp = self._session.get(
                url, timeout=15, headers={"User-Agent": "BookProcessor/0.1"}
            )
            resp.raise_for_status()
            from io import BytesIO

            from PIL import Image

            img = Image.open(BytesIO(resp.content))
            w, h = img.size
            logger.debug("Image %s -> %dx%d", url[:60], w, h)
            # Reject if too small or not portrait-oriented
            if w < min_width or h < min_height:
                return False
            # Book covers are typically portrait (height > width)
            return h >= w
        except Exception as exc:
            logger.debug("Image dimension check failed for %s: %s", url[:60], exc)
            return False

    @staticmethod
    def _validate_url_is_image(url: str) -> bool:
        """Check if a URL looks like an image URL."""
        if not url:
            return False
        url_lower = url.lower()
        return any(
            url_lower.endswith(ext)
            for ext in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
            )
        )

    @staticmethod
    def _is_reputable_source(url: str) -> bool:
        """Check if a URL is from a reputable book/publisher/library source."""
        if not url:
            return False
        reputable_domains = [
            "books.google.com",
            "books.google.fr",
            "covers.openlibrary.org",
            "archive.org",
            "bookshop.org",
            "amazon.com",
            "amazon.fr",
            "amazon.co.uk",
            "barnesandnoble.com",
            "goodreads.com",
            "data.bnf.fr",
            "bnf.fr",
            "worldcat.org",
            "gallica.bnf.fr",
            "publisher.fr",
            "decitre.com",
            "chapitre.com",
            "fnac.com",
        ]
        for domain in reputable_domains:
            if domain in url:
                return True
        return False
