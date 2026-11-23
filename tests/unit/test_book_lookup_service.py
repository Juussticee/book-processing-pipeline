"""Unit tests for BookLookupService."""

from unittest.mock import MagicMock, patch

from services.book_lookup_service import BookLookupService


class TestBookLookupService:
    """Test the book lookup service."""

    def setup_method(self) -> None:
        self._service = BookLookupService(
            google_books_api_key="test_key",
            open_library_enabled=True,
        )

    @patch("services.book_lookup_service.requests")
    def test_google_books_request_success(self, mock_requests_module) -> None:
        """Test successful Google Books API response parsing."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "items": [
                {
                    "volumeInfo": {
                        "title": "Test Book",
                        "authors": ["Test Author"],
                        "publisher": "Test Pub",
                        "publishedDate": "2024-01-01",
                        "description": "A test book",
                        "imageLinks": {"thumbnail": "http://example.com/cover.jpg"},
                        "industryIdentifiers": [
                            {"type": "ISBN_10", "id": "0306406152"},
                            {"type": "ISBN_13", "id": "9780306406157"},
                        ],
                        "pageCount": 200,
                        "categories": ["Fiction"],
                        "language": "en",
                    }
                }
            ]
        }
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_requests_module.Session.return_value = mock_session

        service = BookLookupService(google_books_api_key="test_key")
        results = service._search_google_books_by_isbn("9780306406157")

        assert len(results) == 1
        assert results[0].title == "Test Book"
        assert "Test Author" in results[0].authors
        assert results[0].isbn_13 == "9780306406157"
        assert results[0].source == "google_books"

    def test_google_books_request_no_api_key(self) -> None:
        """Without API key, Google Books search returns empty."""
        service = BookLookupService(
            google_books_api_key=None,
            open_library_enabled=False,
        )
        results = service._search_google_books_by_isbn("9780306406157")
        assert results == []

    @patch("services.book_lookup_service.requests")
    def test_open_library_request_success(self, mock_requests_module) -> None:
        """Test successful Open Library API response parsing."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "docs": [
                {
                    "title": "OL Book",
                    "author_name": ["OL Author"],
                    "isbn": ["9780306406157"],
                    "publisher": ["OL Publisher"],
                    "first_publish_year": 2020,
                    "language": ["eng"],
                    "subject": ["Fiction", "Drama"],
                    "cover_i": 12345,
                }
            ]
        }
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_requests_module.Session.return_value = mock_session

        service = BookLookupService(open_library_enabled=True)
        results = service._search_open_library_by_title("OL Book")

        assert len(results) == 1
        assert results[0].title == "OL Book"
        assert "OL Author" in results[0].authors
        assert results[0].source == "open_library"

    def test_search_by_isbn_no_results(self) -> None:
        """ISBN search with no API key and disabled Open Library returns empty."""
        service = BookLookupService(
            google_books_api_key=None,
            open_library_enabled=False,
        )
        results = service.search_by_isbn("9780306406157")
        assert results == []

    @patch("services.book_lookup_service.requests")
    def test_search_by_title_and_author(self, mock_requests_module) -> None:
        """Test title+author search."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"items": []}
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_requests_module.Session.return_value = mock_session

        service = BookLookupService(google_books_api_key="key")
        results = service.search_by_title_author("Test", "Author")
        assert results == []  # Google Books returned no items, OL fallback also empty

    @patch("services.book_lookup_service.requests")
    def test_search_by_title(self, mock_requests_module) -> None:
        """Test title-only search delegates to title+author with None author."""
        mock_session = MagicMock()
        mock_requests_module.Session.return_value = mock_session
        service = BookLookupService(google_books_api_key="key")
        with patch.object(service, "search_by_title_author") as mock_search:
            service.search_by_title("Test Title")
            mock_search.assert_called_once_with("Test Title", None)

    @patch("services.book_lookup_service.requests")
    def test_google_books_request_handles_http_error(
        self, mock_requests_module
    ) -> None:
        """HTTP errors should return empty list, not crash."""
        mock_session = MagicMock()
        mock_session.get.side_effect = Exception("Connection error")
        mock_requests_module.Session.return_value = mock_session

        service = BookLookupService(google_books_api_key="key")
        results = service._google_books_request(
            "https://www.googleapis.com/books/v1/volumes?q=test"
        )
        assert results == []

    @patch("services.book_lookup_service.requests")
    def test_search_by_isbn_calls_google_then_open_library(
        self, mock_requests_module
    ) -> None:
        """ISBN search tries Google Books first, falls back to Open Library."""
        # Google Books returns empty, Open Library returns a result
        ol_response = MagicMock()
        ol_response.raise_for_status.return_value = None
        ol_response.json.return_value = {
            "docs": [
                {
                    "title": "OL Book",
                    "author_name": ["Author"],
                    "isbn": ["9780306406157"],
                }
            ]
        }
        gb_response = MagicMock()
        gb_response.raise_for_status.return_value = None
        gb_response.json.return_value = {"items": []}

        mock_session = MagicMock()
        mock_session.get.side_effect = [gb_response, ol_response]
        mock_requests_module.Session.return_value = mock_session

        service = BookLookupService(
            google_books_api_key="key",
            open_library_enabled=True,
        )
        results = service.search_by_isbn("9780306406157")

        assert len(results) == 1
        assert results[0].title == "OL Book"
        assert results[0].source == "open_library"


class TestHttpResponseHandling:
    """A successful HTTP response must not be mistaken for a failure.

    Regression: every 2xx response was classified as "success" and then
    discarded by the permanent-error branch, so live provider lookups always
    returned zero candidates even though the APIs answered correctly.
    """

    def _service(self, response) -> BookLookupService:
        service = BookLookupService(
            google_books_api_key="test_key",
            open_library_enabled=True,
            google_books_timeout=1,
            open_library_timeout=1,
        )
        session = MagicMock()
        session.get.return_value = response
        service._session = session
        return service

    def test_http_200_response_is_returned(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.ok = True
        service = self._service(response)

        assert service._make_request("https://example.com/ok", 1) is response

    def test_http_404_returns_none(self) -> None:
        response = MagicMock()
        response.status_code = 404
        response.ok = False
        service = self._service(response)

        assert service._make_request("https://example.com/missing", 1) is None

    def test_successful_open_library_payload_is_parsed(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.ok = True
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "docs": [
                {
                    "title": "Le coeur vert",
                    "author_name": ["Jean-Louis Kuffer"],
                    "first_publish_year": 1993,
                    "publisher": ["L'Age d'Homme"],
                    "language": ["fre"],
                    "number_of_pages_median": 142,
                    "isbn": ["9782825104699"],
                }
            ]
        }
        service = self._service(response)

        results = service.search_by_isbn("9782825104699")

        assert len(results) == 1
        assert results[0].title == "Le coeur vert"
        assert results[0].source == "open_library"
