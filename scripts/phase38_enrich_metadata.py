"""Phase 38 metadata enrichment: fill publisher + language for all 14 books.

Uses vision_title + vision_author as primary lookup identity.
Does NOT modify cover pipeline or existing good data.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.getcwd())

from core.config import Config
from repositories.book_metadata_repository import BookMetadataRepository
from services.book_lookup_service import BookLookupService

LANG_NORMALIZE = {
    "fre": "French",
    "fra": "French",
    "fr": "French",
    "eng": "English",
    "en": "English",
    "rus": "Russian",
    "ru": "Russian",
    "spa": "Spanish",
    "es": "Spanish",
    "ger": "German",
    "deu": "German",
    "de": "German",
    "pol": "Polish",
    "pl": "Polish",
    "ita": "Italian",
    "it": "Italian",
}


def normalize_language(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip().lower()
    # Strip Open Library /languages/ prefix
    if raw.startswith("/languages/"):
        raw = raw.split("/languages/", 1)[1]
    return LANG_NORMALIZE.get(raw, raw)


def normalize_publisher(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    # Normalize obvious spacing/case artifacts without inventing
    # Keep original capitalization but strip extra spaces
    return raw


def pick_best(
    candidates: List[Dict[str, Any]],
    title: str,
    author: str,
) -> Optional[Dict[str, Any]]:
    """Pick the best matching candidate from a list of API results."""
    if not candidates:
        return None

    # Score by title/author similarity
    from rapidfuzz import fuzz

    best = None
    best_score = -1.0
    for c in candidates:
        ct = c.get("title", "")
        ca = ", ".join(c.get("authors", [])) if isinstance(c.get("authors"), list) else c.get("authors", "")
        ts = fuzz.ratio(title.lower(), ct.lower()) if ct else 0
        as_ = fuzz.ratio(author.lower(), ca.lower()) if ca else 0
        total = ts * 0.6 + as_ * 0.4
        if total > best_score:
            best_score = total
            best = c
    return best


def enrich_one(
    lookup: BookLookupService,
    row: tuple,
) -> Dict[str, Any]:
    (
        id_,
        drive_file_id,
        job_id,
        title,
        subtitle,
        authors_json,
        isbn_10,
        isbn_13,
        publisher,
        published_date,
        language,
        categories_json,
        description,
        thumbnail_url,
        cover_url,
        confidence,
        source,
        status,
        raw_ocr,
        created_at,
        updated_at,
        ocr_text,
        extracted_title,
        extracted_author,
        extracted_isbn,
        extracted_publisher,
        match_score,
        candidate_sources,
        rejection_reason,
        image_links,
        vision_title,
        vision_author,
        vision_publisher,
        vision_confidence,
        vision_used,
        local_cover_path,
        cover_source,
        cover_source_url,
        cover_status,
        cover_source_type,
    ) = row

    # Use vision title/author as primary identity
    lookup_title = vision_title or title or extracted_title or ""
    lookup_author = vision_author or extracted_author or ""
    if not lookup_title or not lookup_author:
        return {"updated": False, "reason": "missing lookup identity"}

    updated_fields: Dict[str, Any] = {}
    best: Optional[Dict[str, Any]] = None

    # Source 1: Open Library work details if we have an open_library_work_key
    # (We don't store it currently, but we can search by title/author)
    try:
        ol_cands = lookup.search_by_title_author(lookup_title, lookup_author)
        if ol_cands:
            # Convert BookMetadata to dicts
            ol_dicts = []
            for c in ol_cands:
                ol_dicts.append(
                    {
                        "title": c.title,
                        "authors": c.authors,
                        "publisher": c.publisher,
                        "published_date": c.published_date,
                        "language": c.language,
                        "isbn_10": c.isbn_10,
                        "isbn_13": c.isbn_13,
                        "categories": c.categories,
                        "description": c.description,
                        "source": c.source,
                    }
                )
            best = pick_best(ol_dicts, lookup_title, lookup_author)
            if best:
                updated_fields.setdefault("source", "open_library")
    except Exception as exc:
        print(f"  OL search failed: {exc}")

    # Source 2: Google Books
    if not best or not best.get("publisher") or not best.get("language"):
        try:
            gb_cands = lookup.search_google_books_only(lookup_title, lookup_author)
            if gb_cands:
                gb_dicts = []
                for c in gb_cands:
                    gb_dicts.append(
                        {
                            "title": c.title,
                            "authors": c.authors,
                            "publisher": c.publisher,
                            "published_date": c.published_date,
                            "language": c.language,
                            "isbn_10": c.isbn_10,
                            "isbn_13": c.isbn_13,
                            "categories": c.categories,
                            "description": c.description,
                            "source": c.source,
                        }
                    )
                cand = pick_best(gb_dicts, lookup_title, lookup_author)
                if cand:
                    if not best:
                        best = cand
                        updated_fields["source"] = "google_books"
                    else:
                        # Fill gaps
                        if not best.get("publisher") and cand.get("publisher"):
                            best["publisher"] = cand["publisher"]
                        if not best.get("language") and cand.get("language"):
                            best["language"] = cand["language"]
                        if not best.get("isbn_13") and cand.get("isbn_13"):
                            best["isbn_13"] = cand["isbn_13"]
                        if not best.get("isbn_10") and cand.get("isbn_10"):
                            best["isbn_10"] = cand["isbn_10"]
                        if not best.get("published_date") and cand.get("published_date"):
                            best["published_date"] = cand["published_date"]
        except Exception as exc:
            print(f"  GB search failed: {exc}")

    # Source 3: Internet Archive
    if not best or not best.get("publisher"):
        try:
            ia_cands = lookup.search_internet_archive(lookup_title, lookup_author)
            if ia_cands:
                ia_dicts = []
                for c in ia_cands:
                    ia_dicts.append(
                        {
                            "title": c.title,
                            "authors": c.authors,
                            "publisher": c.publisher,
                            "published_date": c.published_date,
                            "language": c.language,
                            "source": c.source,
                        }
                    )
                cand = pick_best(ia_dicts, lookup_title, lookup_author)
                if cand and not best:
                    best = cand
                    updated_fields["source"] = "internet_archive"
        except Exception as exc:
            print(f"  IA search failed: {exc}")

    # Source 4: BnF
    if not best or not best.get("publisher"):
        try:
            bnf_cands = lookup.search_bnf(lookup_title, lookup_author)
            if bnf_cands:
                bnf_dicts = []
                for c in bnf_cands:
                    bnf_dicts.append(
                        {
                            "title": c.title,
                            "authors": c.authors,
                            "publisher": c.publisher,
                            "published_date": c.published_date,
                            "language": c.language,
                            "source": c.source,
                        }
                    )
                cand = pick_best(bnf_dicts, lookup_title, lookup_author)
                if cand and not best:
                    best = cand
                    updated_fields["source"] = "bnf"
        except Exception as exc:
            print(f"  BnF search failed: {exc}")

    # Source 5: Exa web search
    if not best or not best.get("publisher"):
        try:
            exa_query = f'"{lookup_title}" "{lookup_author}" publisher'
            exa_url, exa_src = lookup.search_exa_for_cover(
                lookup_title, lookup_author, exa_query
            )
            if exa_url:
                # Exa doesn't give structured metadata, but we found a cover-related page
                pass
        except Exception as exc:
            print(f"  Exa search failed: {exc}")

    # Apply updates
    if not best:
        return {"updated": False, "reason": "no bibliographic candidate found"}

    pub = best.get("publisher")
    lang = best.get("language")
    isbn10 = best.get("isbn_10")
    isbn13 = best.get("isbn_13")
    pub_date = best.get("published_date")
    cats = best.get("categories")
    desc = best.get("description")

    if publisher and pub:
        pub = None  # Don't overwrite existing
    if language and lang:
        lang = None  # Don't overwrite existing

    if pub:
        updated_fields["publisher"] = normalize_publisher(pub)
    if lang:
        updated_fields["language"] = normalize_language(lang)
    if isbn10 and not isbn_10:
        updated_fields["isbn_10"] = isbn10
    if isbn13 and not isbn_13:
        updated_fields["isbn_13"] = isbn13
    if pub_date and not published_date:
        updated_fields["published_date"] = pub_date
    if cats and not categories_json:
        updated_fields["categories"] = json.dumps(cats)
    if desc and not description:
        updated_fields["description"] = desc

    if not updated_fields:
        return {"updated": False, "reason": "candidate found but no new fields"}

    updated_fields.setdefault("source", best.get("source", "enrichment"))
    return {"updated": True, "fields": updated_fields}


def main() -> int:
    config = Config.from_env()
    repo = BookMetadataRepository(os.path.join(config.data_directory, "metadata.db"))
    lookup = BookLookupService(
        google_books_api_key=getattr(config, "google_books_api_key", None),
        open_library_enabled=getattr(config, "open_library_enabled", True),
        google_books_timeout=getattr(config, "google_books_timeout", 15),
        open_library_timeout=getattr(config, "open_library_timeout", 10),
    )

    rows = repo._conn.execute(
        "SELECT * FROM book_metadata ORDER BY title"
    ).fetchall()
    print(f"Enriching {len(rows)} books...")

    stats = {"updated": 0, "skipped": 0, "failed": 0}
    for row in rows:
        title = row[3]
        vision_title = row[29]
        vision_author = row[30]
        print(f"\n--- {title} ---")
        print(f"    vision_title={vision_title}, vision_author={vision_author}")

        result = enrich_one(lookup, row)
        if result.get("updated"):
            fields = result["fields"]
            print(f"    UPDATE -> {fields}")
            # Build UPDATE dynamically
            sets = []
            vals = []
            for k, v in fields.items():
                sets.append(f"{k}=?")
                vals.append(v)
            vals.append(row[0])  # id
            sql = f"UPDATE book_metadata SET {', '.join(sets)} WHERE id=?"
            repo._conn.execute(sql, vals)
            repo._conn.commit()
            stats["updated"] += 1
        else:
            reason = result.get("reason", "unknown")
            print(f"    SKIP: {reason}")
            stats["skipped"] += 1

    print(f"\nStats: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
