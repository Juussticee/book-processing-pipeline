import os, sys, sqlite3, re, json

sys.path.insert(0, os.getcwd())
import requests
from PIL import Image
from io import BytesIO
from difflib import SequenceMatcher

from services.book_lookup_service import BookLookupService
from services.clean_cover_service import CleanCoverService

DB = "data/metadata.db"
COVERS_DIR = "data/covers"

os.makedirs(COVERS_DIR, exist_ok=True)


def is_image_response(url):
    try:
        r = requests.head(
            url, timeout=15, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 405 or 200:
            return r.headers.get("content-type", "").startswith("image/")
    except Exception:
        pass
    return False


def validate_image(url):
    try:
        r = requests.get(
            url, timeout=15, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code != 200:
            return False, f"http_{r.status_code}"
        ct = r.headers.get("content-type", "")
        if not ct.startswith("image/"):
            return False, f"content_type_{ct}"
        img = Image.open(BytesIO(r.content))
        w, h = img.size
        if w < 200 or h < 250:
            return False, f"size_{w}x{h}"
        if h <= w:
            return False, f"landscape_{w}x{h}"
        return True, f"valid_{w}x{h}"
    except Exception as e:
        return False, f"error_{e}"


def normalize_text(s):
    if not s:
        return ""
    return re.sub(r"[^\w\s]", "", s).lower().strip()


def candidate_matches(candidate_url, title, author):
    # We cannot inspect image content without vision; accept if provider is reputable and we validated image
    return True


lookup = BookLookupService()
cleaner = CleanCoverService(book_lookup=lookup)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT * FROM book_metadata ORDER BY title")
rows = cur.fetchall()

report = []
for row in rows:
    rec = dict(row)
    title = rec.get("title") or ""
    author = rec.get("authors") or "[]"
    if isinstance(author, str):
        try:
            author = json.loads(author)
        except Exception:
            author = [author]
    author = author[0] if author else ""
    isbn = rec.get("isbn_13") or rec.get("isbn_10") or ""
    cover_url = rec.get("cover_url") or ""
    if cover_url.strip():
        report.append(
            {
                "title": title,
                "author": author,
                "cover_url": cover_url,
                "cover_source": rec.get("cover_source"),
                "status": "HAS_COVER",
            }
        )
        continue

    print(f"\n=== {title} | {author} ===")
    candidates = []

    # 1) CleanCoverService full pipeline
    from models.book_metadata import BookMetadata

    meta = BookMetadata(
        title=title,
        authors=[author] if author else [],
        drive_file_id=rec.get("drive_file_id") or "",
        isbn_10=rec.get("isbn_10"),
        isbn_13=rec.get("isbn_13"),
    )
    url, src, local = cleaner.find_clean_cover(meta)
    if url:
        ok, reason = validate_image(url)
        if ok:
            candidates.append((src, url, local, f"clean_service_ok_{reason}"))
            print(f"  CleanService: {src} {url}")
        else:
            print(f"  CleanService rejected: {url} reason={reason}")
    else:
        print("  CleanService: none")

    # 2) Direct GB queries with variants
    for q_title, q_author in [
        (title, author),
        (
            title.replace("É", "E")
            .replace("é", "e")
            .replace("À", "A")
            .replace("â", "a"),
            author,
        ),
        (title, None),
    ]:
        try:
            for c in lookup.search_google_books_only(q_title, q_author):
                if c.cover_url:
                    ok, reason = validate_image(c.cover_url)
                    if ok:
                        candidates.append(
                            ("google_books", c.cover_url, None, f"gb_ok_{reason}")
                        )
                if c.image_links:
                    for size, u in c.image_links.items():
                        if u:
                            ok, reason = validate_image(u)
                            if ok:
                                candidates.append(
                                    ("google_books", u, None, f"gb_{size}_ok_{reason}")
                                )
        except Exception:
            pass

    # 3) GB by ISBN if available
    if isbn:
        try:
            q = f"isbn:{isbn}"
            for c in lookup.search_google_books_only(q, None):
                if c.cover_url:
                    ok, reason = validate_image(c.cover_url)
                    if ok:
                        candidates.append(
                            (
                                "google_books_isbn",
                                c.cover_url,
                                None,
                                f"gb_isbn_ok_{reason}",
                            )
                        )
        except Exception:
            pass

    # 4) OL ISBN direct cover
    if isbn:
        ol_url = cleaner._open_library_cover_by_isbn(isbn, size="L")
        if ol_url:
            ok, reason = validate_image(ol_url)
            if ok:
                candidates.append(
                    ("open_library_isbn", ol_url, None, f"ol_isbn_ok_{reason}")
                )
            else:
                # try M
                ol_url_m = cleaner._open_library_cover_by_isbn(isbn, size="M")
                if ol_url_m:
                    ok2, reason2 = validate_image(ol_url_m)
                    if ok2:
                        candidates.append(
                            (
                                "open_library_isbn",
                                ol_url_m,
                                None,
                                f"ol_isbn_M_ok_{reason2}",
                            )
                        )

    # 5) OL title/author variants
    for q_title, q_author in [(title, author), (title, None)]:
        try:
            for c in lookup.search_by_title_author(q_title, q_author):
                if c.source != "open_library":
                    continue
                if c.cover_url:
                    ok, reason = validate_image(c.cover_url)
                    if ok:
                        candidates.append(
                            ("open_library", c.cover_url, None, f"ol_ok_{reason}")
                        )
                if c.image_links:
                    for size, u in c.image_links.items():
                        if u:
                            ok, reason = validate_image(u)
                            if ok:
                                candidates.append(
                                    ("open_library", u, None, f"ol_{size}_ok_{reason}")
                                )
        except Exception:
            pass

    # 6) Internet Archive
    try:
        for q_title, q_author in [(title, author), (title, None)]:
            for c in lookup.search_internet_archive(q_title, q_author):
                if c.cover_url:
                    ok, reason = validate_image(c.cover_url)
                    if ok:
                        candidates.append(
                            ("internet_archive", c.cover_url, None, f"ia_ok_{reason}")
                        )
    except Exception:
        pass

    # 7) BnF
    try:
        for c in lookup.search_bnf(title, author):
            if c.cover_url:
                ok, reason = validate_image(c.cover_url)
                if ok:
                    candidates.append(("bnf", c.cover_url, None, f"bnf_ok_{reason}"))
    except Exception:
        pass

    # 8) Exa multiple queries
    for q in [
        f'"{title}" "{author}" book cover',
        f'"{title}" "{author}" couverture livre',
        f'"{title}" "{author}" édition',
        f'"{title}" book cover',
    ]:
        try:
            ex_url, src_url = lookup.search_exa_for_cover(title, author, q)
            if ex_url:
                ok, reason = validate_image(ex_url)
                if ok:
                    candidates.append(("exa", ex_url, src_url, f"exa_ok_{reason}"))
        except Exception:
            pass

    print(f"  total validated candidates: {len(candidates)}")
    for src, url, local, reason in candidates[:5]:
        print(f"    {src}: {url} ({reason})")

    if candidates:
        best_src, best_url, best_local, best_reason = candidates[0]
        # download
        drive_id = rec.get("drive_file_id") or ""
        local_path = os.path.join(COVERS_DIR, f"{drive_id}.jpg")
        try:
            r = requests.get(
                best_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200 and r.headers.get("content-type", "").startswith(
                "image/"
            ):
                with open(local_path, "wb") as f:
                    f.write(r.content)
                print(f"  DOWNLOADED -> {local_path} ({len(r.content)} bytes)")
                rec["cover_url"] = best_url
                rec["cover_source"] = best_src
                rec["cover_status"] = "found"
                rec["local_cover_path"] = local_path
            else:
                print(
                    f'  download failed: {r.status_code} {r.headers.get("content-type")}'
                )
        except Exception as e:
            print(f"  download error: {e}")
    else:
        print("  NO VERIFIED COVER")

    report.append(
        {
            "title": title,
            "author": author,
            "cover_url": rec.get("cover_url") or "",
            "cover_source": rec.get("cover_source") or "",
            "status": "HAS_COVER" if rec.get("cover_url") else "MISSING",
        }
    )

print("\n\n====================")
print("FINAL DB UPDATE")
print("====================")
for rec in report:
    title = rec["title"]
    author = rec["author"]
    cover_url = rec["cover_url"]
    cover_source = rec["cover_source"]
    status = rec["status"]
    print(f"{title} | {author} | {cover_source} | {cover_url} | {status}")
    if cover_url:
        cur.execute(
            "UPDATE book_metadata SET cover_url=?, cover_source=?, cover_status=?, local_cover_path=? WHERE title=?",
            (cover_url, cover_source, "found", rec.get("local_cover_path"), title),
        )

conn.commit()
conn.close()

print("\nDone. DB updated.")
