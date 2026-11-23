> **Note (2026-11-13):** see `docs/BOOK_INTAKE_GUIDE.md` for the current, code-verified
> upload and output guide. Kept for history.

# How to submit your books

This guide is for the person photographing the books. You do not need to
rename files, type ISBNs, or run anything. You only need a Google Drive
folder and a phone.

---

## 1. Create one folder in Google Drive

1. Open Google Drive and create a folder, for example **"Books to process"**.
2. Keep it **flat**: put photographs directly in this folder, not in
   sub-folders. Sub-folders are not scanned.
3. Share the folder with the integration account address you were given
   (the address that ends in `@...iam.gserviceaccount.com`), with the
   **Viewer** role. Viewer is enough — the system never edits your files.

Send the folder link to whoever runs the system. They will connect it once.

---

## 2. Photograph the books

- Photograph the **front cover**, filling most of the frame.
- Hold the phone **upright** (portrait), roughly parallel to the cover.
- Use **daylight or a well-lit room**; avoid direct flash on glossy covers.
- Keep the book **flat and still** — motion blur is the most common reason a
  cover cannot be read.
- One photograph per book. If a cover is unreadable, take a second, sharper
  photograph and upload that one instead of both.

The system corrects rotations recorded by the phone (including upside-down and
sideways shots taken with the camera turned). If that rotation information was
stripped — for example after editing and re-saving the file — an upside-down
cover is only re-tried when the text cannot be read at all, so check the
preview before uploading.

---

## 3. Supported formats

| Format | Supported |
|---|---|
| JPEG / JPG | Yes |
| PNG | Yes |
| WEBP, TIFF, BMP | Yes |
| **HEIC / HEIF** (iPhone default) | **No** |
| PDF, ZIP, Word, Google Docs | No |

**iPhone users:** set *Settings → Camera → Formats → Most Compatible* so new
photos are saved as JPEG. Photos already taken as HEIC must be exported or
converted to JPEG before upload. HEIC files are rejected with the reason
`REJECTED_HEIC_UNSUPPORTED` and are not processed.

**File size:** each photograph must be **20 MB or smaller**. Larger files are
rejected before download (`REJECTED_FILE_TOO_LARGE`).

---

## 4. What happens after you upload

1. The system scans the folder every 30 seconds.
2. Unsupported files (PDFs, `Thumbs.db`, HEIC, oversized images) are skipped
   and recorded with a reason — they do not become failed jobs.
3. Each new photograph is downloaded, straightened, and read.
4. The book is looked up in library catalogues and matched.
5. Results are written to the Google Sheet you were given.

You do not need to wait, refresh, or re-upload. New files are picked up
automatically.

---

## 5. Duplicates

The system protects you from duplicates in three ways:

- **Same file scanned twice** — nothing happens; it is processed once.
- **The same photograph uploaded twice** (a copy, a re-upload, a re-saved
  version) — the second copy is normally recognised by its content and
  skipped, so it costs no extra OCR call and creates no second row.
- **A different photograph of a book that is already in the sheet** — a second
  row is skipped automatically **only when the ISBN reads the same in both
  photographs**. Otherwise the photograph is still recorded, with a duplicate
  flag in the sheet for a person to check, so nothing is lost and nothing is
  silently merged.

Two photographs that print **different ISBNs** are always treated as two
different books (two editions, for example) and are both processed.

If you deliberately want a second row for the same book, make sure its ISBN is
visible and differs from any existing row; otherwise expect the new photograph
to appear as a flagged duplicate until someone confirms it.

---

## 6. Books that need a human

Some covers cannot be read confidently: heavy blur, glare, a damaged cover,
or a book that is not in any catalogue. These are **not** thrown away. They
are marked `manual_review` (or `needs_review`) in the sheet with the text
that was read, so a person can confirm or correct the entry.

---

## 7. What not to upload

- Screenshots, photos of screens, or photos of several books at once.
- Photos of the back cover, spine, or interior pages (front cover only).
- PDFs, scans, ZIP archives, or documents.
- HEIC files (convert to JPEG first).
- Files larger than 20 MB.
- Personal photos or anything you do not want processed.

---

## 8. If something looks wrong

- **A book is missing from the sheet** — check the sheet's `status` column
  first; it may be `manual_review`. If it is absent entirely, the file was
  probably rejected (unsupported type, HEIC, or too large).
- **A book appears twice** — send the two file names to the operator; the
  duplicate record shows which file was treated as the original.
- **A cover is rotated or unreadable** — re-photograph it upright and upload
  the new photo. Do not upload a corrected copy under a new name if the
  original was already processed; tell the operator instead.
- **A book is flagged as a possible duplicate** — the row is real, not an
  error. Confirm it in the sheet; the duplicate record keeps the original
  photograph's identity so the two can be compared.

---

## 9. ZIP files

ZIP upload is **not supported**. Upload individual photographs to the
configured Google Drive folder.
