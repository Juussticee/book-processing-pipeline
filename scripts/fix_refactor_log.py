"""Append Phase 35C entry to REFACTOR-LOG.md"""
import os

refactor_log_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "REFACTOR-LOG.md"
)
content = open(refactor_log_path, "r", encoding="utf-8").read()

new_entry = """### Phase 35C: Activate Google Books + Exa Clean Cover Retrieval (2026-08-22)

**Problem:** Phase 35B left 12/14 books without clean covers -- Google Books API was not configured, and Exa cover search was not activated. Only Open Library provided 2 covers.

**Fix:**

1. API Key Configuration: Added valid Exa API key (EXA_API_KEY) to .env -- verified HTTP 200. Added Google Books API key (GOOGLE_BOOKS_API_KEY=AIzaSy...k-VU) to .env -- verified HTTP 200. Added exa_api_key field to core/config.py Config dataclass. Updated app.py to pass config.exa_api_key to BookLookupService.

2. services/book_lookup_service.py -- Enhanced Exa Cover Search: Replaced search_exa_for_cover with enhanced version adding Authorization Bearer header, _extract_image_from_page (HTML img tag parsing), _is_reputable_source (15+ domains: BnF, Worldcat, publisher.fr, isbn.org, books.google.com), _validate_url_is_image, _validate_image_dimensions (min 200x250), and aggressive URL filtering with 20+ skip patterns (sprites, loading spinners, Amazon ad banners, navigation elements).

3. services/clean_cover_service.py -- Fixed Fall-Through Logic: Changed fall-through from 'if not local_path and not cover_url' to 'if not local_path' so download failures trigger next source attempt. Added existing local cover check from metadata.local_cover_path. Added metadata.cover_url check in _find_existing_cover_url. Raised _validate_image_file minimum dimensions to 200x250. Added None guard for metadata.isbn_13 in _select_best_candidate.

4. scripts/phase35b_clean_covers.py -- Added Google Sheets Update: Added GoogleSheetsService integration to push clean cover URLs to Sheets using _build_image_formula for IMAGE() rendering. Updates Cover URL and Status columns for all 14 rows.

5. tests/unit/test_phase35c_cover_retrieval.py -- 27 New Tests: Google Books config/key loading, GB search with title+author, cover selection priority, wrong-result rejection, placeholder rejection, OL fallback, Exa fallback, page to image extraction, URL validation, Drive never selected, existing cover reuse, duplicate prevention, no rejection on missing cover.

**Final Results (14 books, verified via DB + Google Sheets):**
- Source | Before (35B) | After (35C)
- Google Books | 0 | 5
- Open Library | 2 | 2
- Exa | 3 | 2
- Not found | 9 | 5
- Total covers | 5/14 | 9/14
- Drive covers used | 0 | 0
- Sheets updated | N/A | 14 rows

**Test count:** 154/154 passing (127 existing + 27 new Phase 35C)
**Quality gates:** ruff clean, black clean, mypy clean (0 errors)
"""

# Insert after the first line (header)
lines = content.split("\n")
# Find the end of the first line
insert_pos = 1  # After "# REFACTOR-LOG.md"
new_lines = ["# REFACTOR-LOG.md", ""]
new_lines.extend(new_entry.split("\n"))
new_lines.extend(lines[1:])  # Skip the original first two lines ("# REFACTOR-LOG.md" + blank)
# But the original content after header is the rest
content_new = "\n".join(new_lines)
content_new = "# REFACTOR-LOG.md\n\n" + new_entry + "\n" + "\n".join(lines[1:])

with open(refactor_log_path, "w", encoding="utf-8") as f:
    f.write(content_new)
print("REFACTOR-LOG.md updated with Phase 35C entry")
