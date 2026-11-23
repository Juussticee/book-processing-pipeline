"""SQLite-backed result cache for OCR, API lookups, and verification.

Provides a persistent cache keyed by content hashes so that:
- OCR results for identical images are not re-computed
- Book lookup API results are cached (TTL-based)
- Cover verification results are cached

Tables:
    cache_entries: key, value (JSON), created_at, expires_at, category
    cache_hashes: image_hash, file_path, ocr_text_hash

TTL: 7 days for API lookups, 30 days for OCR, 90 days for verification.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Optional

from core.logger import setup_logger

logger = setup_logger(__name__)

# Cache TTLs (in seconds)
TTL_OCR = 30 * 24 * 3600        # 30 days
TTL_API = 7 * 24 * 3600         # 7 days
TTL_VERIFICATION = 90 * 24 * 3600  # 90 days

# Categories
CAT_OCR = "ocr"
CAT_API = "api"
CAT_VERIFICATION = "verification"


class CacheRepository:
    """Thread-safe SQLite cache for pipeline results.

    Args:
        db_path: Path to the cache SQLite database file.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS cache_entries (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    category TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS cache_hashes (
                    image_hash TEXT PRIMARY KEY,
                    file_path TEXT,
                    ocr_text_hash TEXT,
                    created_at REAL NOT NULL
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_category "
                "ON cache_entries (category)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_expires "
                "ON cache_entries (expires_at)"
            )
            self._conn.commit()

    @staticmethod
    def _make_key(*parts: str) -> str:
        """Generate a deterministic cache key from parts."""
        combined = ":".join(str(p) for p in parts)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def _ttl_for_category(self, category: str) -> int:
        if category == CAT_OCR:
            return TTL_OCR
        elif category == CAT_VERIFICATION:
            return TTL_VERIFICATION
        else:
            return TTL_API

    def get(self, key: str) -> Optional[Any]:
        """Retrieve a value from cache by key.

        Returns None if the key doesn't exist or has expired.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM cache_entries WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] < time.time():
                # Expired — delete and return None
                self._conn.execute(
                    "DELETE FROM cache_entries WHERE key = ?", (key,)
                )
                self._conn.commit()
                return None
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return None

    def set(self, key: str, value: Any, category: str = CAT_API) -> None:
        """Store a value in the cache with appropriate TTL."""
        ttl = self._ttl_for_category(category)
        now = time.time()
        try:
            serialized = json.dumps(value)
        except (TypeError, ValueError) as exc:
            logger.debug("Cache set failed (serialization): %s", exc)
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO cache_entries
                    (key, value, category, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, serialized, category, now, now + ttl),
            )
            self._conn.commit()

    def get_or_set(
        self,
        key: str,
        factory: Any,
        category: str = CAT_API,
    ) -> Any:
        """Get value from cache, or compute via factory and cache it.

        Args:
            key: Cache key.
            factory: Callable that produces the value if cache miss.
            category: Cache category for TTL selection.
        """
        cached = self.get(key)
        if cached is not None:
            logger.debug("Cache HIT: %s", key[:16])
            return cached
        logger.debug("Cache MISS: %s", key[:16])
        value = factory()
        if value is not None:
            self.set(key, value, category)
        return value

    def delete(self, key: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM cache_entries WHERE key = ?", (key,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def clear_expired(self) -> int:
        """Delete all expired cache entries. Returns count deleted."""
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM cache_entries WHERE expires_at < ?", (now,)
            )
            self._conn.commit()
            return cursor.rowcount

    def clear_category(self, category: str) -> int:
        """Delete all entries in a category. Returns count deleted."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM cache_entries WHERE category = ?", (category,)
            )
            self._conn.commit()
            return cursor.rowcount

    def get_ocr_cache_key(self, image_hash: str) -> str:
        """Generate a cache key for OCR results of a given image hash."""
        return self._make_key(CAT_OCR, image_hash)

    def get_api_cache_key(self, *query_parts: str) -> str:
        """Generate a cache key for API lookup results."""
        return self._make_key(CAT_API, *query_parts)

    def get_verification_cache_key(
        self, image_hash: str, candidate_id: str
    ) -> str:
        """Generate a cache key for cover verification between image and candidate."""
        return self._make_key(CAT_VERIFICATION, image_hash, candidate_id)

    def store_image_ocr(
        self, image_hash: str, file_path: str, ocr_text: str
    ) -> None:
        """Store an image hash + OCR result mapping for dedup."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO cache_hashes
                    (image_hash, file_path, ocr_text_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    image_hash,
                    file_path,
                    hashlib.sha256(ocr_text.encode("utf-8")).hexdigest(),
                    time.time(),
                ),
            )
            self._conn.commit()

    def get_image_ocr(self, image_hash: str) -> Optional[str]:
        """Check if we've seen this image before and have its OCR text hash."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ocr_text_hash FROM cache_hashes WHERE image_hash = ?",
                (image_hash,),
            ).fetchone()
            return row["ocr_text_hash"] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
