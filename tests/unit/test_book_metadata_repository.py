"""Unit tests for BookMetadataRepository."""

import os
import tempfile

from models.book_metadata import BookMetadata
from repositories.book_metadata_repository import BookMetadataRepository


def _make_metadata(
    drive_file_id: str = "drive001",
    job_id: str = "job001",
    title: str = "Test Book",
    isbn_13: str = "9780306406157",
) -> BookMetadata:
    """Create a BookMetadata instance for testing."""
    return BookMetadata(
        title=title,
        subtitle="A Test Subtitle",
        authors=["Test Author"],
        isbn_10="0306406152",
        isbn_13=isbn_13,
        publisher="Test Publisher",
        published_date="2024-01-01",
        language="en",
        categories=["Fiction"],
        description="A test book description.",
        thumbnail_url="http://example.com/thumb.jpg",
        cover_url="http://example.com/cover.jpg",
        confidence=0.95,
        source="google_books",
        raw_ocr="Test Book by Test Author",
        drive_file_id=drive_file_id,
        job_id=job_id,
    )


class TestBookMetadataRepository:
    """Tests for BookMetadataRepository CRUD operations."""

    def setup_method(self):
        """Create a temporary database for each test."""
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._repo = BookMetadataRepository(self._db_path)

    def teardown_method(self):
        """Clean up after each test."""
        self._repo.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def test_insert_metadata(self):
        """Test inserting metadata returns a valid ID."""
        metadata = _make_metadata()
        metadata_id = self._repo.insert_metadata(metadata)
        assert metadata_id is not None
        assert metadata_id > 0

    def test_exists_true_after_insert(self):
        """Test exists returns True after insertion."""
        metadata = _make_metadata()
        self._repo.insert_metadata(metadata)
        assert self._repo.exists("drive001") is True

    def test_exists_false_for_unknown(self):
        """Test exists returns False for unknown file ID."""
        assert self._repo.exists("nonexistent") is False

    def test_get_by_drive_file_id(self):
        """Test retrieving metadata by drive file ID."""
        metadata = _make_metadata(drive_file_id="driveABC")
        self._repo.insert_metadata(metadata)
        result = self._repo.get_by_drive_file_id("driveABC")
        assert result is not None
        assert result.title == "Test Book"
        assert result.isbn_13 == "9780306406157"
        assert result.source == "google_books"

    def test_get_by_job_id(self):
        """Test retrieving metadata by job ID."""
        metadata = _make_metadata(job_id="jobXYZ")
        self._repo.insert_metadata(metadata)
        result = self._repo.get_by_job_id("jobXYZ")
        assert result is not None
        assert result.title == "Test Book"

    def test_get_by_drive_file_id_not_found(self):
        """Test retrieving metadata for non-existent file returns None."""
        result = self._repo.get_by_drive_file_id("nonexistent")
        assert result is None

    def test_get_by_job_id_not_found(self):
        """Test retrieving metadata for non-existent job returns None."""
        result = self._repo.get_by_job_id("nonexistent")
        assert result is None

    def test_upsert_inserts_new(self):
        """Upsert on a new drive file ID inserts a new row."""
        metadata = _make_metadata(drive_file_id="up001", job_id="uj001")
        metadata_id = self._repo.upsert_metadata(metadata)
        assert metadata_id > 0
        result = self._repo.get_by_drive_file_id("up001")
        assert result is not None
        assert result.title == "Test Book"

    def test_upsert_updates_existing(self):
        """Upsert on existing drive file ID updates the row, no duplicate."""
        metadata1 = _make_metadata(
            drive_file_id="dup001", job_id="jobA", title="First Book"
        )
        self._repo.upsert_metadata(metadata1)

        metadata2 = _make_metadata(
            drive_file_id="dup001", job_id="jobB", title="Second Book"
        )
        self._repo.upsert_metadata(metadata2)

        results = self._repo.list()
        assert len(results) == 1

        result = self._repo.get_by_drive_file_id("dup001")
        assert result is not None
        assert result.title == "Second Book"
        assert result.job_id == "jobB"

    def test_update_metadata(self):
        """Test updating metadata for an existing drive file ID."""
        metadata = _make_metadata(drive_file_id="update001")
        self._repo.insert_metadata(metadata)

        result = self._repo.get_by_drive_file_id("update001")
        assert result is not None
        assert result.title == "Test Book"

        metadata.title = "Updated Title"
        metadata.confidence = 0.88
        self._repo.update_metadata(metadata)

        result = self._repo.get_by_drive_file_id("update001")
        assert result is not None
        assert result.title == "Updated Title"
        assert result.confidence == 0.88

    def test_update_metadata_nonexistent_returns_false(self):
        """Updating non-existent metadata returns False."""
        metadata = _make_metadata(drive_file_id="new001")
        result = self._repo.update_metadata(metadata)
        assert result is False

    def test_list(self):
        """Test listing all metadata records."""
        metadata1 = _make_metadata(
            drive_file_id="list001", job_id="jobL1", title="Book 1"
        )
        metadata2 = _make_metadata(
            drive_file_id="list002", job_id="jobL2", title="Book 2"
        )
        self._repo.insert_metadata(metadata1)
        self._repo.insert_metadata(metadata2)

        results = self._repo.list()
        assert len(results) == 2

    def test_delete(self):
        """Test deleting metadata by drive file ID."""
        metadata = _make_metadata(drive_file_id="del001")
        self._repo.insert_metadata(metadata)
        assert self._repo.exists("del001") is True

        result = self._repo.delete("del001")
        assert result is True
        assert self._repo.exists("del001") is False

    def test_delete_nonexistent(self):
        """Deleting a non-existent record returns False."""
        result = self._repo.delete("nonexistent")
        assert result is False

    def test_page_count_persisted(self):
        """Page count survives an insert/read round-trip."""
        metadata = _make_metadata(drive_file_id="pages001")
        metadata.page_count = 142
        self._repo.insert_metadata(metadata)

        stored = self._repo.get_by_drive_file_id("pages001")
        assert stored is not None
        assert stored.page_count == 142

    def test_page_count_updated(self):
        """Enrichment can persist a page count on an existing row."""
        metadata = _make_metadata(drive_file_id="pages002")
        self._repo.insert_metadata(metadata)

        metadata.page_count = 304
        self._repo.update_metadata(metadata)

        stored = self._repo.get_by_drive_file_id("pages002")
        assert stored is not None
        assert stored.page_count == 304

    def test_missing_page_count_is_none(self):
        """A book without a page count reads back as None, not 0."""
        metadata = _make_metadata(drive_file_id="pages003")
        self._repo.insert_metadata(metadata)

        stored = self._repo.get_by_drive_file_id("pages003")
        assert stored is not None
        assert stored.page_count is None
