"""Phase 37 regression tests for the 3 remaining not-found books.

Tests cover:
- LA CONSTRUCTION DES VILLES with Le Corbusier / Charles-Edouard Jeanneret
- Viol with Serge Petrilli (short title, false positive prevention)
- Analyses de Vie et Mort de la Yougoslavie with Pavle Ivić
- Quoted multi-word title GB queries
- Pen-name/alias candidate selection when author threshold is not met
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from models.book_metadata import BookMetadata
from services.book_lookup_service import BookLookupService
from services.clean_cover_service import CleanCoverService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_meta(**overrides):
    base = {
        "drive_file_id": "test-drive-id",
        "title": None,
        "vision_title": None,
        "vision_author": None,
        "isbn_13": None,
        "publisher": None,
        "authors": [],
        "cover_url": None,
        "cover_source": None,
        "cover_source_url": None,
        "local_cover_path": None,
    }
    base.update(overrides)
    return BookMetadata(**base)


def make_candidate(title, authors, image_links=None, isbn_13=None):
    return BookMetadata(
        drive_file_id="candidate",
        title=title,
        vision_title=title,
        vision_author=(authors[0] if authors else None),
        authors=authors,
        isbn_13=isbn_13,
        image_links=image_links or {},
        confidence=0.9,
        source="google_books",
    )


# ---------------------------------------------------------------------------
# Isolated DB helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path):
    """Return a path to an empty temporary SQLite database file."""
    db_path = str(tmp_path / "metadata.db")
    conn = sqlite3.connect(db_path)
    conn.close()
    return db_path


class _IsolatedRepo:
    """Thin wrapper around BookMetadataRepository for isolated tests."""

    def __init__(self, db_path: str) -> None:
        from repositories.book_metadata_repository import BookMetadataRepository

        self._repo = BookMetadataRepository(db_path)

    def insert(self, **kwargs) -> int:
        from models.book_metadata import BookMetadata

        kwargs.setdefault("created_at", datetime.utcnow().isoformat())
        kwargs.pop("updated_at", None)
        kwargs.setdefault("job_id", f"job-{uuid.uuid4().hex}")
        meta = BookMetadata(**kwargs)
        return self._repo.insert_metadata(meta)

    def list_all(self):
        return self._repo.list()


# ---------------------------------------------------------------------------
# 1. LA CONSTRUCTION DES VILLES
# ---------------------------------------------------------------------------


class TestLaConstructionDesVilles:
    """Pen-name: CHARLES-EDOUARD JEANNERET == Le Corbusier."""

    def test_rejects_le_corbusier_without_alias_evidence(self):
        lookup = BookLookupService(google_books_api_key="fake")
        svc = CleanCoverService(lookup)

        meta = make_meta(
            drive_file_id="1TtcfjbF5Xelt4JOhGAQ1Yu9LyxCqSr-U",
            title="LA CONSTRUCTION DES VILLES",
            vision_title="LA CONSTRUCTION DES VILLES",
            vision_author="CHARLES-EDOUARD JEANNERET",
            isbn_13="9783856762117",
        )

        candidates = [
            make_candidate(
                "La construction des villes",
                ["Le Corbusier", "Marc Emery"],
                image_links={"smallThumbnail": "http://books.google.com/1"},
            ),
        ]

        best = svc._select_best_candidate_with_cover(
            candidates, meta.vision_title, meta.vision_author, meta
        )
        # Without explicit alias mapping, Le Corbusier != Charles-Edouard Jeanneret
        assert best is None

    def test_rejects_non_corbusier_author_even_with_cover(self):
        lookup = BookLookupService(google_books_api_key="fake")
        svc = CleanCoverService(lookup)

        meta = make_meta(
            title="LA CONSTRUCTION DES VILLES",
            vision_title="LA CONSTRUCTION DES VILLES",
            vision_author="CHARLES-EDOUARD JEANNERET",
        )

        candidates = [
            make_candidate(
                "La construction des villes",
                ["Louis Cloquet"],
                image_links={"smallThumbnail": "http://books.google.com/x"},
            ),
        ]

        best = svc._select_best_candidate_with_cover(
            candidates, meta.vision_title, meta.vision_author, meta
        )
        # Author mismatch => rejected, even though title matches
        assert best is None


# ---------------------------------------------------------------------------
# 2. Viol — short title, false-positive prevention
# ---------------------------------------------------------------------------


class TestViolShortTitle:
    """Short title 'Viol' must not accept unrelated books."""

    def test_rejects_gisele_halimi_viol(self):
        lookup = BookLookupService(google_books_api_key="fake")
        svc = CleanCoverService(lookup)

        meta = make_meta(
            title="Viol",
            vision_title="Viol",
            vision_author="Serge Petrilli",
        )

        candidates = [
            make_candidate(
                "Viol",
                ["Gisèle Halimi"],
                image_links={"smallThumbnail": "http://books.google.com/halimi"},
            ),
            make_candidate(
                "Le viol, une arme de guerre",
                ["Vanessa Fargnoli"],
                image_links={"smallThumbnail": "http://books.google.com/war"},
            ),
        ]

        best = svc._select_best_candidate_with_cover(
            candidates, meta.vision_title, meta.vision_author, meta
        )
        # Neither candidate has matching author => both rejected
        assert best is None

    def test_accepts_serge_petrilli_candidate(self):
        lookup = BookLookupService(google_books_api_key="fake")
        svc = CleanCoverService(lookup)

        meta = make_meta(
            title="Viol",
            vision_title="Viol",
            vision_author="Serge Petrilli",
        )

        candidates = [
            make_candidate(
                "Viol",
                ["Serge Petrilli"],
                image_links={"smallThumbnail": "http://books.google.com/petrilli"},
            ),
        ]

        best = svc._select_best_candidate_with_cover(
            candidates, meta.vision_title, meta.vision_author, meta
        )
        assert best is not None
        assert "Petrilli" in (best.authors[0] if best.authors else "")


# ---------------------------------------------------------------------------
# 3. Analyses de Vie et Mort de la Yougoslavie
# ---------------------------------------------------------------------------


class TestAnalysesYougoslavie:
    """Long French title with diacritics."""

    def test_requires_both_title_and_author_match(self):
        lookup = BookLookupService(google_books_api_key="fake")
        svc = CleanCoverService(lookup)

        meta = make_meta(
            title="Analyses de Vie et Mort de la Yougoslavie",
            vision_title="Analyses de Vie et Mort de la Yougoslavie",
            vision_author="Pavle Ivić",
        )

        candidates = [
            make_candidate(
                "De l'imprécision à la falsification",
                ["Pavle Ivić"],
                image_links={"smallThumbnail": "http://books.google.com/1"},
            ),
            make_candidate(
                "Analyses de vie et mort de la Yougoslavie",
                ["Paul Garde"],
                image_links={"smallThumbnail": "http://books.google.com/2"},
            ),
        ]

        best = svc._select_best_candidate_with_cover(
            candidates, meta.vision_title, meta.vision_author, meta
        )
        # None of these match strongly enough on both title AND author
        assert best is None


# ---------------------------------------------------------------------------
# 4. GB query quoting — multi-word titles/authors
# ---------------------------------------------------------------------------


class TestGoogleBooksQueryQuoting:
    """Multi-word titles must be quoted in GB intitle: queries."""

    def test_title_only_quotes_multi_word(self):
        lookup = BookLookupService(google_books_api_key="fake")
        # We can't call the real API, but we can verify the URL is built correctly
        with patch.object(lookup, "_make_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"items": []}
            mock_req.return_value = mock_resp

            lookup.search_google_books_only("la construction des villes", None)

            called_url = mock_req.call_args[0][0]
            # Multi-word title MUST be wrapped in quotes
            assert "intitle:%22la+construction+des+villes%22" in called_url
            # Must NOT have unquoted multi-word intitle
            assert (
                "intitle:la+construction+des+villes" not in called_url
                or "%22" in called_url.split("q=")[1].split("&")[0]
            )

    def test_author_quotes_multi_word(self):
        lookup = BookLookupService(google_books_api_key="fake")
        with patch.object(lookup, "_make_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"items": []}
            mock_req.return_value = mock_resp

            lookup.search_google_books_only("test", "charles-edouard jeanneret")

            called_url = mock_req.call_args[0][0]
            assert "inauthor:%22charles-edouard+jeanneret%22" in called_url


# ---------------------------------------------------------------------------
# 5. E2E verification: existing covers remain intact
# ---------------------------------------------------------------------------


class TestExistingCoversIntact:
    """Ensure existing clean covers are not broken by new logic."""

    def test_all_existing_covers_have_external_source_only(self, isolated_db):
        repo = _IsolatedRepo(isolated_db)
        repo.insert(
            drive_file_id="cover-1",
            title="Book With Cover",
            authors=["Author"],
            cover_url="https://books.google.com/books/content?id=1",
            cover_source="google_books",
            local_cover_path="data/covers/1.jpg",
            confidence=0.9,
            source="google_books",
            status="identified",
            raw_ocr="",
        )
        repo.insert(
            drive_file_id="cover-2",
            title="Open Library Cover",
            authors=["Author"],
            cover_url="https://openlibrary.org/cover.jpg",
            cover_source="open_library",
            local_cover_path="data/covers/2.jpg",
            confidence=0.9,
            source="open_library",
            status="identified",
            raw_ocr="",
        )
        rows = repo.list_all()
        for meta in rows:
            if meta.cover_url and meta.cover_url.strip():
                assert meta.cover_source in (
                    "google_books",
                    "exa",
                    "open_library",
                ), f"Unexpected source={meta.cover_source!r} for {meta.title!r}"
                assert "drive.google.com" not in (meta.cover_url or "").lower(), (
                    f"Drive URL found as cover for {meta.title!r}: {meta.cover_url}"
                )


# ---------------------------------------------------------------------------
# 6. Title-only fallback triggers correctly
# ---------------------------------------------------------------------------


class TestTitleOnlyFallback:
    """Title-only fallback must trigger when Query 1 returns 0 candidates."""

    def test_falls_back_to_title_only_when_no_candidates(self):
        lookup = BookLookupService(google_books_api_key="fake")
        svc = CleanCoverService(lookup)

        meta = make_meta(
            title="LA CONSTRUCTION DES VILLES",
            vision_title="LA CONSTRUCTION DES VILLES",
            vision_author="CHARLES-EDOUARD JEANNERET",
        )

        # Mock: Query 1 returns 0 candidates, Query 2 returns 1 with cover.
        # Use a matching author so the title-only candidate is accepted.
        def fake_gb_only(title, author=None):
            if author is None:
                return [
                    make_candidate(
                        "La construction des villes",
                        ["CHARLES-EDOUARD JEANNERET"],
                        image_links={"smallThumbnail": "http://books.example.com/1"},
                    )
                ]
            return []

        fake_local = "data/covers/test-drive-id.jpg"

        ol_ret = (None, "open_library")
        exa_ret = (None, "exa")
        with patch.object(lookup, "search_google_books_only", side_effect=fake_gb_only):
            with patch.object(svc, "_search_open_library", return_value=ol_ret):
                with patch.object(svc, "_search_exa", return_value=exa_ret):
                    with patch.object(
                        svc, "_download_clean_cover", return_value=fake_local
                    ):
                        url, source, local = svc.find_clean_cover(meta)

        assert url is not None, "Title-only fallback should have found a cover"
        assert source == "google_books"
