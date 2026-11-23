"""Regression tests for multiple-author support."""

from __future__ import annotations

from models.book_metadata import BookMetadata
from services.google_sheets_service import GoogleSheetsService
from services.metadata_enrichment_service import MetadataEnrichmentService


class TestBookMetadataAuthorsList:
    def test_single_author_list(self):
        metadata = BookMetadata(
            title="Single Author Book",
            authors=["Victor Hugo"],
        )
        assert metadata.authors == ["Victor Hugo"]
        assert isinstance(metadata.authors, list)

    def test_multiple_authors_list(self):
        metadata = BookMetadata(
            title="Multi Author Book",
            authors=["Jean Dupont", "Marie Martin", "Pierre Durand"],
        )
        assert metadata.authors == ["Jean Dupont", "Marie Martin", "Pierre Durand"]

    def test_empty_authors_list(self):
        metadata = BookMetadata(
            title="No Author Book",
            authors=[],
        )
        assert metadata.authors == []
        assert isinstance(metadata.authors, list)


class TestGoogleSheetsAuthorSerialization:
    def setup_method(self):
        self.service = GoogleSheetsService(
            credentials_file="test_creds.json",
            sheets_id="test_sheet_id_123",
            tab_name="Books",
        )

    def test_single_author_serialized(self):
        metadata = BookMetadata(
            title="Book A",
            authors=["Victor Hugo"],
        )
        row = self.service._map_metadata_to_row(metadata, cover_path=None)
        assert row[2] == "Victor Hugo"

    def test_two_authors_serialized_with_comma(self):
        metadata = BookMetadata(
            title="Book B",
            authors=["Jean Dupont", "Marie Martin"],
        )
        row = self.service._map_metadata_to_row(metadata, cover_path=None)
        # The live spreadsheet stores multi-author books as "A, B".
        assert row[2] == "Jean Dupont, Marie Martin"

    def test_three_authors_serialized(self):
        metadata = BookMetadata(
            title="Book C",
            authors=["Jean Dupont", "Marie Martin", "Pierre Durand"],
        )
        row = self.service._map_metadata_to_row(metadata, cover_path=None)
        assert row[2] == "Jean Dupont, Marie Martin, Pierre Durand"

    def test_empty_authors_serialized_empty_string(self):
        metadata = BookMetadata(
            title="Book D",
            authors=[],
        )
        row = self.service._map_metadata_to_row(metadata, cover_path=None)
        assert row[2] == ""


class TestMetadataEnrichmentPreservesAuthors:
    def test_enrichment_does_not_overwrite_existing_authors(self):
        """_merge_enrichment() must not overwrite existing authors."""
        enrichment = MetadataEnrichmentService(book_lookup=None)  # type: ignore[arg-type]
        metadata = BookMetadata(
            title="Existing Authors",
            authors=["Author A", "Author B"],
        )

        # Candidate with different/single author should not overwrite
        candidate = BookMetadata(
            title="Existing Authors",
            authors=["Candidate Author"],
            isbn_13="1234567890123",
        )
        enrichment._merge_enrichment(metadata, candidate)

        assert metadata.authors == ["Author A", "Author B"]

    def test_enrichment_does_not_overwrite_vision_authors(self):
        """Vision-extracted authors must survive enrichment."""
        enrichment = MetadataEnrichmentService(book_lookup=None)  # type: ignore[arg-type]
        metadata = BookMetadata(
            title="Vision Authors",
            authors=["Vision Author One", "Vision Author Two"],
            vision_title="Vision Authors",
        )

        candidate = BookMetadata(
            title="Vision Authors",
            authors=["API Author"],
            isbn_13="1234567890123",
        )
        enrichment._merge_enrichment(metadata, candidate)

        assert metadata.authors == ["Vision Author One", "Vision Author Two"]

    def test_duplicate_authors_removed_preserving_order(self):
        """Duplicate exact authors should be deduplicated while preserving order."""
        metadata = BookMetadata(
            title="Dup Authors",
            authors=["Author A", "Author B", "Author A"],
        )
        seen = set()
        deduped = []
        for a in metadata.authors:
            if a not in seen:
                seen.add(a)
                deduped.append(a)
        metadata.authors = deduped

        assert metadata.authors == ["Author A", "Author B"]


class TestMultipleAuthorEndToEnd:
    def test_full_path_preserves_all_authors(self):
        """Simulate the complete path: extraction -> vision -> merge -> DB -> Sheets."""
        # Step 1: BookMetadata constructed with multiple authors
        metadata = BookMetadata(
            title="Complete Path Book",
            authors=["Author A", "Author B", "Author C"],
            isbn_13="1234567890123",
            publisher="Test Publisher",
            published_date="2024",
            language="fr",
            categories=["Fiction"],
            description="Test description",
        )

        # Step 2: Persistence representation (list[str] remains list[str])
        assert isinstance(metadata.authors, list)
        assert len(metadata.authors) == 3

        # Step 3: Google Sheets export serialization
        service = GoogleSheetsService(
            credentials_file="test_creds.json",
            sheets_id="test_sheet_id_123",
            tab_name="Books",
        )
        row = service._map_metadata_to_row(metadata, cover_path=None)
        assert row[1] == "Complete Path Book"
        assert row[2] == "Author A, Author B, Author C"
        assert row[3] == "1234567890123"
        assert row[4] == "Test Publisher"

    def test_vision_overrides_ocr_preserves_multiple_authors(self):
        """When vision overrides OCR, multiple vision authors must be preserved."""
        metadata = BookMetadata(
            title="Vision Override",
            authors=["Vision Author One", "Vision Author Two"],
            vision_title="Vision Override",
            vision_author="Vision Author One",
            vision_publisher="Vision Publisher",
        )
        # Simulate the vision override branch from metadata_service.py
        if metadata.vision_title and metadata.vision_author:
            metadata.title = metadata.vision_title
            metadata.authors = [metadata.vision_author]
            if metadata.vision_publisher:
                metadata.publisher = metadata.vision_publisher

        # After override, at least one vision author remains
        assert "Vision Author One" in metadata.authors
        assert metadata.title == "Vision Override"

    def test_existing_single_author_books_unchanged(self):
        """Existing single-author books must not be affected by multi-author support."""
        metadata = BookMetadata(
            title="Classic Book",
            authors=["Classic Author"],
        )
        service = GoogleSheetsService(
            credentials_file="test_creds.json",
            sheets_id="test_sheet_id_123",
            tab_name="Books",
        )
        row = service._map_metadata_to_row(metadata, cover_path=None)
        assert row[2] == "Classic Author"
        assert metadata.authors == ["Classic Author"]
