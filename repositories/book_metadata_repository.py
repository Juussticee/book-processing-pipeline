"""Repository for persisting book metadata to SQLite.

Provides CRUD operations for identified book metadata. Uses a dedicated
SQLite database (``metadata.db``) separate from the file-tracking and
job-tracking databases.

Table: book_metadata
  Columns:
    id            INTEGER PRIMARY KEY AUTOINCREMENT
    drive_file_id TEXT    UNIQUE  (one record per Drive file)
    job_id        TEXT    UNIQUE  (nullable FK to processing job)
    title         TEXT
    subtitle      TEXT
    authors       TEXT    (JSON array)
    isbn_10       TEXT
    isbn_13       TEXT
    publisher     TEXT
    published_date TEXT
    language      TEXT
    categories    TEXT    (JSON array)
    description   TEXT
    thumbnail_url TEXT
    cover_url     TEXT
    confidence    REAL    (0.0 - 1.0)
    source        TEXT
    status        TEXT    ('identified', 'manual_review')
    raw_ocr        TEXT
    created_at     TEXT    (ISO 8601)
    updated_at     TEXT    (ISO 8601)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, List, Optional

from core.logger import setup_logger
from models.book_metadata import BookMetadata

logger = setup_logger(__name__)

_STATUS_IDENTIFIED = "identified"
_STATUS_MANUAL_REVIEW = "manual_review"


class BookMetadataRepository:
    """Persists and retrieves book metadata in SQLite.

    Thread-safe: uses ``check_same_thread=False`` plus a ``threading.Lock``.
    All write operations use explicit transactions so a crash mid-write
    cannot leave a partially-inserted row.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_table()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _create_table(self) -> None:
        """Create the book_metadata table if it does not exist."""
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS book_metadata (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    drive_file_id   TEXT    UNIQUE NOT NULL,
                    job_id          TEXT    UNIQUE,
                    title           TEXT,
                    subtitle        TEXT,
                    authors         TEXT,
                    isbn_10         TEXT,
                    isbn_13         TEXT,
                    publisher       TEXT,
                    published_date  TEXT,
                    language        TEXT,
                    page_count      INTEGER,
                    categories      TEXT,
                    description     TEXT,
                    thumbnail_url   TEXT,
                    cover_url       TEXT,
                    confidence      REAL,
                    source          TEXT,
                    status          TEXT,
                    raw_ocr         TEXT,
                    created_at      TEXT,
                    updated_at      TEXT,
                    ocr_text        TEXT,
                    extracted_title TEXT,
                    extracted_author TEXT,
                    extracted_isbn  TEXT,
                    extracted_publisher TEXT,
                    match_score     REAL,
                    candidate_sources TEXT,
                    rejection_reason TEXT,
                    image_links     TEXT,
                    vision_title    TEXT,
                    vision_author   TEXT,
                    vision_publisher TEXT,
                    vision_confidence REAL,
                    vision_used     INTEGER DEFAULT 0,
                    local_cover_path TEXT,
                    cover_source     TEXT,
                    cover_source_url TEXT,
                    cover_status     TEXT,
                    cover_source_type TEXT
                )
                """)
            # Migration: add new columns to existing tables
            self._migrate_schema()
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_book_metadata_job_id "
                "ON book_metadata (job_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_book_metadata_drive_file_id "
                "ON book_metadata (drive_file_id)"
            )

    def _migrate_schema(self) -> None:
        """Add new columns (Milestone 5 migration)."""
        new_columns = {
            "ocr_text": "TEXT",
            "extracted_title": "TEXT",
            "extracted_author": "TEXT",
            "extracted_isbn": "TEXT",
            "extracted_publisher": "TEXT",
            "match_score": "REAL",
            "candidate_sources": "TEXT",
            "rejection_reason": "TEXT",
            "image_links": "TEXT",
            "vision_title": "TEXT",
            "vision_author": "TEXT",
            "vision_publisher": "TEXT",
            "vision_confidence": "REAL",
            "vision_used": "INTEGER DEFAULT 0",
            "local_cover_path": "TEXT",
            "cover_source": "TEXT",
            "cover_source_url": "TEXT",
            "cover_status": "TEXT",
            "cover_source_type": "TEXT",
            "page_count": "INTEGER",
        }
        for col_name, col_type in new_columns.items():
            try:
                self._conn.execute(
                    f"ALTER TABLE book_metadata ADD COLUMN {col_name} {col_type}"
                )
                logger.info("Added column %s to book_metadata", col_name)
            except sqlite3.OperationalError:
                pass  # Column already exists

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _json_list(value: Optional[List[str]]) -> str:
        if value is None:
            return "[]"
        return json.dumps(list(value))

    @staticmethod
    def _parse_json_list(raw: Optional[str]) -> List[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        return []

    @staticmethod
    def _parse_json(raw: Optional[str]) -> dict:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return {}

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _row_to_metadata(self, row: sqlite3.Row) -> BookMetadata:
        return BookMetadata(
            title=row["title"] or "",
            subtitle=row["subtitle"] or "",
            authors=(
                self._parse_json_list(row["authors"]) if "authors" in row.keys() else []
            ),
            isbn_10=row["isbn_10"] or "",
            isbn_13=row["isbn_13"] or "",
            publisher=row["publisher"] or "",
            published_date=row["published_date"] or "",
            language=row["language"] or "",
            page_count=(
                row["page_count"]
                if "page_count" in row.keys() and row["page_count"] is not None
                else None
            ),
            categories=(
                self._parse_json_list(row["categories"])
                if "categories" in row.keys()
                else []
            ),
            description=row["description"] or "",
            thumbnail_url=row["thumbnail_url"] or "",
            cover_url=row["cover_url"] or "",
            confidence=row["confidence"] if "confidence" in row.keys() else 0.0,
            source=row["source"] or "",
            status=(
                row["status"]
                if "status" in row.keys() and row["status"] is not None
                else _STATUS_IDENTIFIED
            ),
            raw_ocr=row["raw_ocr"] or "",
            drive_file_id=row["drive_file_id"] or "",
            job_id=row["job_id"] or "",
            ocr_text=row["ocr_text"] or "" if "ocr_text" in row.keys() else "",
            extracted_title=(
                row["extracted_title"] if "extracted_title" in row.keys() else None
            ),
            extracted_author=(
                row["extracted_author"] if "extracted_author" in row.keys() else None
            ),
            extracted_isbn=(
                row["extracted_isbn"] if "extracted_isbn" in row.keys() else None
            ),
            extracted_publisher=(
                row["extracted_publisher"]
                if "extracted_publisher" in row.keys()
                else None
            ),
            match_score=row["match_score"] if "match_score" in row.keys() else 0.0,
            candidate_sources=(
                self._parse_json_list(row["candidate_sources"])
                if "candidate_sources" in row.keys()
                else []
            ),
            rejection_reason=(
                row["rejection_reason"] if "rejection_reason" in row.keys() else None
            ),
            image_links=self._parse_json(
                row["image_links"] if "image_links" in row.keys() else None
            ),
            vision_title=(
                row["vision_title"] if "vision_title" in row.keys() else None
            ),
            vision_author=(
                row["vision_author"] if "vision_author" in row.keys() else None
            ),
            vision_publisher=(
                row["vision_publisher"] if "vision_publisher" in row.keys() else None
            ),
            vision_confidence=(
                row["vision_confidence"] if "vision_confidence" in row.keys() else 0.0
            ),
            vision_used=bool(row["vision_used"] if "vision_used" in row.keys() else 0),
            local_cover_path=(
                row["local_cover_path"] if "local_cover_path" in row.keys() else None
            ),
            cover_source=(
                row["cover_source"] if "cover_source" in row.keys() else None
            ),
            cover_source_url=(
                row["cover_source_url"] if "cover_source_url" in row.keys() else None
            ),
            cover_status=(
                row["cover_status"] if "cover_status" in row.keys() else None
            ),
            cover_source_type=(
                row["cover_source_type"] if "cover_source_type" in row.keys() else None
            ),
        )

    # ------------------------------------------------------------------
    def insert_metadata(self, metadata: BookMetadata) -> int:
        """Insert a new metadata row.

        Raises ``sqlite3.IntegrityError`` if a row with the same
        ``drive_file_id`` or ``job_id`` already exists.
        """
        now = self._utcnow()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO book_metadata (
                    drive_file_id, job_id, title, subtitle, authors,
                    isbn_10, isbn_13, publisher, published_date,
                    language, page_count, categories, description, thumbnail_url,
                    cover_url, confidence, source, status, raw_ocr,
                    created_at, updated_at, ocr_text, extracted_title,
                    extracted_author, extracted_isbn, extracted_publisher,
                    match_score, candidate_sources, rejection_reason,
                    image_links, vision_title, vision_author,
                    vision_publisher, vision_confidence, vision_used,
                    local_cover_path, cover_source, cover_source_url,
                    cover_status, cover_source_type
                ) VALUES (
                    :drive_file_id, :job_id, :title, :subtitle, :authors,
                    :isbn_10, :isbn_13, :publisher, :published_date,
                    :language, :page_count, :categories, :description, :thumbnail_url,
                    :cover_url, :confidence, :source, :status, :raw_ocr,
                    :created_at, :updated_at, :ocr_text, :extracted_title,
                    :extracted_author, :extracted_isbn, :extracted_publisher,
                    :match_score, :candidate_sources, :rejection_reason,
                    :image_links, :vision_title, :vision_author,
                    :vision_publisher, :vision_confidence, :vision_used,
                    :local_cover_path, :cover_source, :cover_source_url,
                    :cover_status, :cover_source_type
                )
                """,
                {
                    "drive_file_id": metadata.drive_file_id,
                    "job_id": metadata.job_id,
                    "title": metadata.title,
                    "subtitle": metadata.subtitle,
                    "authors": self._json_list(metadata.authors),
                    "isbn_10": metadata.isbn_10,
                    "isbn_13": metadata.isbn_13,
                    "publisher": metadata.publisher,
                    "published_date": metadata.published_date,
                    "language": metadata.language,
                    "page_count": metadata.page_count,
                    "categories": self._json_list(metadata.categories),
                    "description": metadata.description,
                    "thumbnail_url": metadata.thumbnail_url,
                    "cover_url": metadata.cover_url,
                    "confidence": metadata.confidence,
                    "source": metadata.source,
                    "status": metadata.status or _STATUS_IDENTIFIED,
                    "raw_ocr": metadata.raw_ocr,
                    "created_at": now,
                    "updated_at": now,
                    "ocr_text": metadata.ocr_text,
                    "extracted_title": metadata.extracted_title,
                    "extracted_author": metadata.extracted_author,
                    "extracted_isbn": metadata.extracted_isbn,
                    "extracted_publisher": metadata.extracted_publisher,
                    "match_score": metadata.match_score,
                    "candidate_sources": self._json_list(metadata.candidate_sources),
                    "rejection_reason": metadata.rejection_reason,
                    "image_links": (
                        json.dumps(metadata.image_links)
                        if metadata.image_links
                        else None
                    ),
                    "vision_title": metadata.vision_title,
                    "vision_author": metadata.vision_author,
                    "vision_publisher": metadata.vision_publisher,
                    "vision_confidence": metadata.vision_confidence,
                    "vision_used": int(metadata.vision_used),
                    "local_cover_path": metadata.local_cover_path,
                    "cover_source": metadata.cover_source,
                    "cover_source_url": metadata.cover_source_url,
                    "cover_status": metadata.cover_status,
                    "cover_source_type": metadata.cover_source_type,
                },
            )
            self._conn.commit()
            return cursor.lastrowid or 0

    def update_metadata(self, metadata: BookMetadata) -> bool:
        """Update an existing metadata row by ``drive_file_id``.

        Returns ``True`` if a row was updated, ``False`` if no matching
        row existed.
        """
        now = self._utcnow()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE book_metadata SET
                    job_id = :job_id,
                    title = :title,
                    subtitle = :subtitle,
                    authors = :authors,
                    isbn_10 = :isbn_10,
                    isbn_13 = :isbn_13,
                    publisher = :publisher,
                    published_date = :published_date,
                    language = :language,
                    page_count = :page_count,
                    categories = :categories,
                    description = :description,
                    thumbnail_url = :thumbnail_url,
                    cover_url = :cover_url,
                    confidence = :confidence,
                    source = :source,
                    status = :status,
                    raw_ocr = :raw_ocr,
                    ocr_text = :ocr_text,
                    extracted_title = :extracted_title,
                    extracted_author = :extracted_author,
                    extracted_isbn = :extracted_isbn,
                    extracted_publisher = :extracted_publisher,
                    match_score = :match_score,
                    candidate_sources = :candidate_sources,
                    rejection_reason = :rejection_reason,
                    image_links = :image_links,
                    vision_title = :vision_title,
                    vision_author = :vision_author,
                    vision_publisher = :vision_publisher,
                    vision_confidence = :vision_confidence,
                    vision_used = :vision_used,
                    local_cover_path = :local_cover_path,
                    cover_source = :cover_source,
                    cover_source_url = :cover_source_url,
                    cover_status = :cover_status,
                    cover_source_type = :cover_source_type,
                    updated_at = :updated_at
                WHERE drive_file_id = :drive_file_id
                """,
                {
                    "drive_file_id": metadata.drive_file_id,
                    "job_id": metadata.job_id,
                    "title": metadata.title,
                    "subtitle": metadata.subtitle,
                    "authors": self._json_list(metadata.authors),
                    "isbn_10": metadata.isbn_10,
                    "isbn_13": metadata.isbn_13,
                    "publisher": metadata.publisher,
                    "published_date": metadata.published_date,
                    "language": metadata.language,
                    "page_count": metadata.page_count,
                    "categories": self._json_list(metadata.categories),
                    "description": metadata.description,
                    "thumbnail_url": metadata.thumbnail_url,
                    "cover_url": metadata.cover_url,
                    "confidence": metadata.confidence,
                    "source": metadata.source,
                    "status": metadata.status or _STATUS_IDENTIFIED,
                    "raw_ocr": metadata.raw_ocr,
                    "ocr_text": metadata.ocr_text,
                    "extracted_title": metadata.extracted_title,
                    "extracted_author": metadata.extracted_author,
                    "extracted_isbn": metadata.extracted_isbn,
                    "extracted_publisher": metadata.extracted_publisher,
                    "match_score": metadata.match_score,
                    "candidate_sources": self._json_list(metadata.candidate_sources),
                    "rejection_reason": metadata.rejection_reason,
                    "image_links": (
                        json.dumps(metadata.image_links)
                        if metadata.image_links
                        else None
                    ),
                    "vision_title": metadata.vision_title,
                    "vision_author": metadata.vision_author,
                    "vision_publisher": metadata.vision_publisher,
                    "vision_confidence": metadata.vision_confidence,
                    "vision_used": int(metadata.vision_used),
                    "local_cover_path": metadata.local_cover_path,
                    "cover_source": metadata.cover_source,
                    "cover_source_url": metadata.cover_source_url,
                    "cover_status": metadata.cover_status,
                    "cover_source_type": metadata.cover_source_type,
                    "updated_at": now,
                },
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def upsert_metadata(self, metadata: BookMetadata) -> int:
        """Insert or update metadata based on ``drive_file_id``.

        This guarantees one record per Drive file ID — duplicates are
        impossible.  Returns the row ``id``.
        """
        now = self._utcnow()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO book_metadata (
                    drive_file_id, job_id, title, subtitle, authors,
                    isbn_10, isbn_13, publisher, published_date,
                    language, page_count, categories, description, thumbnail_url,
                    cover_url, confidence, source, status, raw_ocr,
                    created_at, updated_at, ocr_text, extracted_title,
                    extracted_author, extracted_isbn, extracted_publisher,
                    match_score, candidate_sources, rejection_reason,
                    image_links, vision_title, vision_author,
                    vision_publisher, vision_confidence, vision_used,
                    local_cover_path, cover_source, cover_source_url,
                    cover_status, cover_source_type
                ) VALUES (
                    :drive_file_id, :job_id, :title, :subtitle, :authors,
                    :isbn_10, :isbn_13, :publisher, :published_date,
                    :language, :page_count, :categories, :description, :thumbnail_url,
                    :cover_url, :confidence, :source, :status, :raw_ocr,
                    :created_at, :updated_at, :ocr_text, :extracted_title,
                    :extracted_author, :extracted_isbn, :extracted_publisher,
                    :match_score, :candidate_sources, :rejection_reason,
                    :image_links, :vision_title, :vision_author,
                    :vision_publisher, :vision_confidence, :vision_used,
                    :local_cover_path, :cover_source, :cover_source_url,
                    :cover_status, :cover_source_type
                )
                ON CONFLICT(drive_file_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    title = excluded.title,
                    subtitle = excluded.subtitle,
                    authors = excluded.authors,
                    isbn_10 = excluded.isbn_10,
                    isbn_13 = excluded.isbn_13,
                    publisher = excluded.publisher,
                    published_date = excluded.published_date,
                    language = excluded.language,
                    page_count = excluded.page_count,
                    categories = excluded.categories,
                    description = excluded.description,
                    thumbnail_url = excluded.thumbnail_url,
                    cover_url = excluded.cover_url,
                    confidence = excluded.confidence,
                    source = excluded.source,
                    status = excluded.status,
                    raw_ocr = excluded.raw_ocr,
                    ocr_text = excluded.ocr_text,
                    extracted_title = excluded.extracted_title,
                    extracted_author = excluded.extracted_author,
                    extracted_isbn = excluded.extracted_isbn,
                    extracted_publisher = excluded.extracted_publisher,
                    match_score = excluded.match_score,
                    candidate_sources = excluded.candidate_sources,
                    rejection_reason = excluded.rejection_reason,
                    image_links = excluded.image_links,
                    vision_title = excluded.vision_title,
                    vision_author = excluded.vision_author,
                    vision_publisher = excluded.vision_publisher,
                    vision_confidence = excluded.vision_confidence,
                    vision_used = excluded.vision_used,
                    updated_at = :updated_at
                """,
                {
                    "drive_file_id": metadata.drive_file_id,
                    "job_id": metadata.job_id,
                    "title": metadata.title,
                    "subtitle": metadata.subtitle,
                    "authors": self._json_list(metadata.authors),
                    "isbn_10": metadata.isbn_10,
                    "isbn_13": metadata.isbn_13,
                    "publisher": metadata.publisher,
                    "published_date": metadata.published_date,
                    "language": metadata.language,
                    "page_count": metadata.page_count,
                    "categories": self._json_list(metadata.categories),
                    "description": metadata.description,
                    "thumbnail_url": metadata.thumbnail_url,
                    "cover_url": metadata.cover_url,
                    "confidence": metadata.confidence,
                    "source": metadata.source,
                    "status": metadata.status or _STATUS_IDENTIFIED,
                    "raw_ocr": metadata.raw_ocr,
                    "ocr_text": metadata.ocr_text,
                    "extracted_title": metadata.extracted_title,
                    "extracted_author": metadata.extracted_author,
                    "extracted_isbn": metadata.extracted_isbn,
                    "extracted_publisher": metadata.extracted_publisher,
                    "match_score": metadata.match_score,
                    "candidate_sources": self._json_list(metadata.candidate_sources),
                    "rejection_reason": metadata.rejection_reason,
                    "image_links": (
                        json.dumps(metadata.image_links)
                        if metadata.image_links
                        else None
                    ),
                    "created_at": now,
                    "updated_at": now,
                    "vision_title": metadata.vision_title,
                    "vision_author": metadata.vision_author,
                    "vision_publisher": metadata.vision_publisher,
                    "vision_confidence": metadata.vision_confidence,
                    "vision_used": int(metadata.vision_used),
                    "local_cover_path": metadata.local_cover_path,
                    "cover_source": metadata.cover_source,
                    "cover_source_url": metadata.cover_source_url,
                    "cover_status": metadata.cover_status,
                    "cover_source_type": metadata.cover_source_type,
                },
            )
            self._conn.commit()
            return cursor.lastrowid or 0

    def get_by_drive_file_id(self, drive_file_id: str) -> Optional[BookMetadata]:
        """Retrieve metadata by Drive file ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM book_metadata WHERE drive_file_id = ?",
                (drive_file_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def get_by_job_id(self, job_id: str) -> Optional[BookMetadata]:
        """Retrieve metadata by job ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM book_metadata WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def list_identity_rows(self) -> List[tuple]:
        """Return ``(drive_file_id, isbn, title, authors)`` for stored books.

        Used by same-book detection: a newly identified book is compared
        against these rows before a second record is created.
        """
        with self._lock:
            rows = self._conn.execute("""
                SELECT drive_file_id, isbn_10, isbn_13, title, authors
                FROM book_metadata
                WHERE drive_file_id IS NOT NULL
                """).fetchall()
        return [
            (
                row["drive_file_id"],
                row["isbn_13"] or row["isbn_10"] or "",
                row["title"] or "",
                self._parse_json_list(row["authors"]),
            )
            for row in rows
        ]

    def exists(self, drive_file_id: str) -> bool:
        """Check whether metadata exists for a given Drive file ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM book_metadata WHERE drive_file_id = ? LIMIT 1",
                (drive_file_id,),
            ).fetchone()
        return row is not None

    def delete(self, drive_file_id: str) -> bool:
        """Delete metadata by Drive file ID. Returns ``True`` if a row was deleted."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM book_metadata WHERE drive_file_id = ?",
                (drive_file_id,),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def clear_all(self) -> int:
        """Delete all metadata rows and reset the id counter.

        Returns the number of rows deleted.
        """
        with self._lock:
            cursor = self._conn.execute("DELETE FROM book_metadata")
            self._conn.commit()
            rowcount = cursor.rowcount
            try:
                self._conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name = 'book_metadata'"
                )
                self._conn.commit()
            except Exception as exc:
                # Cosmetic only (row-id counter); the delete above committed.
                logger.debug("Could not reset book_metadata sequence: %s", exc)
            return rowcount

    def count_metadata(self) -> int:
        """Return the total count of identified books in the metadata database."""
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) as count FROM book_metadata")
            return int(cursor.fetchone()["count"])

    def list(
        self, status: Optional[str] = None, limit: int = 1000
    ) -> List[BookMetadata]:
        """List all metadata records, optionally filtered by status."""
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM book_metadata WHERE status = ? "
                    "ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM book_metadata ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_metadata(row) for row in rows][:limit]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Context-manager support
    def __enter__(self) -> BookMetadataRepository:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
