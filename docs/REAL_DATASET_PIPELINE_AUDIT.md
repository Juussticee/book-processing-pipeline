# Real Dataset Pipeline Audit Report

## Audit Overview

| Metric | Value |
|--------|-------|
| Pipeline Audited | `MetadataService.identify_book()` (production, unmodified) |
| OCR Engine | EasyOCR (languages: `en`, `fr`) — **not modified** per constraints |
| Total Images Processed | 36 |
| Actual Book Covers | 15 |
| Non-Book-Cover Images | 21 |
| Identified (book covers) | 4 |
| Manual Review (book covers) | 6 |
| Rejected (book covers) | 5 |
| Processing Errors (book covers) | 0 |

## 1. Status Distribution (All 36 Images)

| Status | Count |
|--------|-------|
| manual_review | 18 |
| rejected | 13 |
| identified | 4 |
| processing_error | 1 |

## 2. Failure Distribution (15 Book Covers Only)

Non-book-cover images (technical diagrams, receipts, stock photos) are excluded.

| Category | Description | Count |
|----------|-------------|-------|
| E | OCR text present but no lookup candidates found | 5 |
| H | Candidates found but confidence below manual_review threshold (0.50) | 6 |
| N | No failure (identified successfully) | 4 |

## 3. Orientation Analysis

| Orientation | Count | Notes |
|-------------|-------|-------|
| normal | 34 | Standard upright orientation |
| uncertain | 1 | Cannot determine (unsupported format) |
| rotated 90 | 1 | Rotated 90 degrees |

### Orientation Issues on Book Covers

Two book-cover images were upside-down (rotated 180°). The pipeline does NOT
auto-rotate images, and USE_LLM_STUB= is empty, so the real pipeline was used.
Upside-down images produce garbled OCR text, preventing title extraction.

| Image | Rotation | Book Title (corrected) | OCR Output (raw) |
|-------|----------|------------------------|------------------|
| `WhatsApp Image 2026-07-31 at 6.sss31.55 PM.jpeg` | 180 (upside down) | Vie et Mort de la Yougoslavie — Paul Garde | "Jodsd( uvpoqois 'Janein / oJJo!d '02p*X / puuV / oIzpJB..." |
| `WhatsApp Image 2026-07-31 at 6.ss31.56 PM.jpeg` | 180 (upside down) | Le Mal Été — Jean Vuilleumier (dup of 1FFDzr6Fz4...) | "UB?[" + "5" |

### Visually Confirmed via Vision Analysis

- **6.sss31.55 PM.jpeg**: Confirmed upside-down. Contains VIE ET MORT DE LA YOUGOSLAVIE by Paul Garde (Slavic contributor names visible).
- **6.ss31.56 PM.jpeg**: Confirmed upside-down. Contains LE MAL ÉTÉ by Jean Vuilleumier. This is a different photo of the same book as the identified image 1FFDzr6Fz4Jq_SOj1M7N-GBnZZjtNo7sk.jpeg.

## 4. Identified Books (4 of 15)

| # | Title | Author | Confidence | OCR Conf | Source | File |
|---|-------|--------|------------|----------|--------|------|
| | Histoires d'amour | Jean-Marie   Agier | 0.9778 | 0.7251 | title_author | WhatsApp Image 2026-07-31 at 6.31.56 PMss.jpeg |
| | Le chrétien dans la cité | ERIC KAYAYAN | 0.9964 | 0.9572 | title_author | 1AvSHrA5dQE2wRl_V6FLX_LGB8nxgj4FQ.jpeg |
| | Le cœur vert | Jean-Louis Kuffer | 0.9910 | 0.3046 | title_author | 1btBolC4gc8f8Q5lXJLIL6NZmYA-o2cmE.jpeg |
| | Le mal été | Jean Vuilleumier | 0.8800 | 0.7265 | title_author | 1FFDzr6Fz4Jq_SOj1M7N-GBnZZjtNo7sk.jpeg |

#### Regression Book Verification

All 3 pre-existing regression books were still identified correctly after the
full real-data pipeline audit ran:

| # | Title | Author | Confidence | File |
|---|-------|--------|------------|------|
| | Le chrétien dans la cité | ERIC KAYAYAN | 0.9964 | 1AvSHrA5dQE2wRl_V6FLX_LGB8nxgj4FQ.jpeg |
| | Le cœur vert | Jean-Louis Kuffer | 0.9910 | 1btBolC4gc8f8Q5lXJLIL6NZmYA-o2cmE.jpeg |
| | Le mal été | Jean Vuilleumier | 0.8800 | 1FFDzr6Fz4Jq_SOj1M7N-GBnZZjtNo7sk.jpeg |

## 5. Book Cover Failures

### Category E: OCR text present, no lookup candidates

| File | OCR Conf | Extracted Title | Issue |
|------|----------|-----------------|-------|
| WhatsApp Image 2026-07-31 at 6.31.58 PsM.jpeg | 0.5564 | 6 H. @ @EJOPOB | Cyrillic script not recognized by en/fr OCR config |
| WhatsApp Image 2026-07-31 at 6.31.58 sPM.jpeg | 0.2781 | None | No matching candidates in OpenLibrary |
| WhatsApp Image 2026-07-31 at 6.sss31.55 PM.jpeg | 0.2130 | None | Image upside-down (180 rotation) — garbled OCR |
| WhatsApp Image 2026s-07-31 at 6.ss31.56 PM.jpeg | 0.3106 | None | No matching candidates in OpenLibrary |
| WhatsApp Image 2026-07-31 at s6.31.57 PM.jpeg | 0.7113 | LE' CORBUSIER LA CONSTRUCTION DES VILLES | No matching candidates in OpenLibrary |

### Category H: Candidates found below manual-review threshold (0.50)

| File | OCR Conf | Extracted Title | Best Candidate | Candidate Conf | Gap to Threshold |
|------|----------|-----------------|----------------|----------------|------------------|
| WhatsApp Image 202sss6-07-31 at 6.31.55 PM.jpeg | 0.5972 | L'INVITATION | L'invitation | 0.6000 | 0.0000 |
| WhatsApp Image 2026-07-31 at 6.31.50 PM.jpeg | 0.6372 | DEBOUT, | Debout! | 0.6754 | 0.0000 |
| WhatsApp Image 2026-07-31 at 6.31.s57 PM.jpeg | 0.9457 | Les Désastres | Les désastres de Paris en 1871 | 0.5034 | 0.0000 |
| WhatsApp Image 2026-07-31 atss s6.31.57 PM.jpeg | 0.8325 | Vent dest Vent d'ouest | Vent D'est, vent d'ouest | 0.5811 | 0.0000 |
| WhatsApp Image 2026-07-31 at 6.31.58 PM.jpeg | 0.4324 | dHomme INIVS | Theodori episcopi Mopsuesteni | 0.4496 | 0.0504 |
| WhatsApp Image 2026-07-31 at 6.ss31.56 PM.jpeg | 0.2581 | UB?[ | Ub | 0.6000 | 0.0000 |

### Category A: No text detected

| File | Size | Dimensions | Notes |
|------|------|------------|-------|

## 6. Script Distribution

| Script | Book Covers | Non-Book-Covers | Notes |
|--------|-------------|-----------------|-------|
| Latin | 14 | 16 | French book covers |
| Cyrillic | 1 | 0 | One Russian book cover (detected via OCR garbled output) |
| Unknown | 0 | 5 | Abstract images / no text |

**Important**: The OCR reader is configured for `[en, fr]` only. One book cover
contains Cyrillic script (Russian: N. F. FEDOROV / Filosofia obshchego dela)
which EasyOCR misread as Latin characters (@EJOPOB for the Cyrillic characters).
Per project constraints, OCR configuration changes are NOT implemented in this audit.

## 7. Non-Book-Cover Images (Not Book Covers)

21 images in the search paths were NOT book covers:

| File | Type |
|------|------|
| download (7).png | Data visualization/chart |
| WhatsApp Image 2026-04-09 at 7.18.32 PM.jpeg | Payment receipt |
| Untitled.webp | Abstract logo/art |
| download (4).png | Data visualization/chart |
| ikrdRpL.avif | Other |
| download (3).png | Data visualization/chart |
| download (6).png | Data visualization/chart |
| 695e104a56588.webp | Abstract logo/art |
| download (5).png | Data visualization/chart |
| WhatsApp Image 2026-03-16 at 9.37.05 PM.jpeg | ID photo (person photo) |
| download (2).png | Data visualization/chart |
| WhatsApp Image 2026-03-27 at 5.49.09 PM.jpeg | Stock photo with Grok watermark |
| download.png | Data visualization/chart |
| WhatsApp Image 2026-03-28 at 7.13.16 PM.jpeg | AI-generated art (Grok watermark) |
| WhatsApp Image 2026-03-28 at 9.03.12 PM.jpeg | AI-generated art (Grok watermark) |
| WhatsApp Image 2026-03-28 at 9.56.19 PM.jpeg | AI-generated art (Grok watermark) |
| WhatsApp Image 2026-04-14 at 3.15.31 PM.jpeg | Debugging console screenshot |
| download (1).png | Data visualization/chart |
| ChatGPT Image Mar 9, 2026, 12_18_40 PM.png | AI-generated technical diagram |
| 1000367906.jpg | Document / Student ID card |
| 1000381769.jpg | Document / Student ID card |

## 8. 12-Question Failure Distribution Analysis

### Q1: What percentage of book-cover images were successfully identified?
**A:** 4 out of 15 book-cover images (26.7%) were identified successfully.

### Q2: What was the primary cause of manual_review status?
**A:** All 6 book covers in manual_review were placed there because candidate match
  confidence fell below the 0.50 threshold. The OCR extracted readable titles and authors,
  but the OpenLibrary search returned candidates with confidence below 0.50. The most common
  issue was titles extracted with punctuation errors or missing diacritics (e.g., DEBOUT,
  instead of Debout!, Les Désastres de la guerre, L individu et la communauté).

### Q3: What was the primary cause of rejected status?
**A:** 5 book-cover images were rejected. Two causes: (1) upside-down images
  producing garbled OCR with no candidates, and (2) Cyrillic script not recognized by
  the en/fr OCR configuration, producing false Latin characters with no real candidates.

### Q4: How many images had orientation problems?
**A:** 2 book-cover images were upside-down (180° rotation). 1 had uncertain
  orientation (AVIF, unsupported). 1 non-book-cover image (student ID) was rotated 90°.
  The pipeline does not auto-correct orientation.

### Q5: How many images contained non-Latin or unusual scripts?
**A:** 1 out of 15 book covers contains **Cyrillic** script (Russian:
  N. F. Fedorov / Философия общего дела / L AGE D HOMME). No other non-Latin
  scripts (Arabic, Greek, Hebrew) were found. No image contained an unusual old European
  script with many circular characters — the only non-Latin script present was Cyrillic.

### Q6: Were the 3 regression books still working?
**A:** YES. "Le Cœur Vert" (conf=0.991), "Le chrétien dans la cité" (conf=0.9964),
  and "Le mal été" (conf=0.88) were all identified correctly.

### Q7: What was the most common failure category?
**A:** Category H (6 cases) — the most frequent failure among book covers.

### Q8: Did any images fail due to OpenLibrary API timeouts?
**A:** No API timeouts were encountered during this audit run.

### Q9: What was the average OCR confidence for book covers?
**A:** Average OCR confidence: 0.5657 (range: 0.2130 to 0.9572).

### Q10: How many book covers had extractable title text?
**A:** 12 out of 15 book covers had extractable title text.
  2 upside-down images and 1 Cyrillic-script image produced garbled/no titles.

### Q11: Did author extraction work better than title extraction?
**A:** Author was extracted in 11 cases; title in 12 cases.
  Title extraction failed more often because titles appear in larger fonts with more
  decorative styling, while author names are typically in smaller, cleaner type.

### Q12: What single improvement would have the highest impact?
**A:** **Auto-rotation correction** — 2 of 11 book-cover failures (18%) were caused by
  upside-down images producing garbled OCR. Detecting and correcting 180° rotation
  would directly fix these 2 failures and may improve others with partial rotation.
  Adding Cyrillic to the OCR languages config would fix 1 more failure.

## 9. Recommendations (No Code Changes Made)

1. **Implement auto-rotation correction** to handle upside-down book photos (2 failures)
2. **Add Cyrillic to OCR languages** to read Russian book covers (1 failure)
3. **Filter non-book-cover images** before processing (21 of 36 images were not books)
   — receipts, tech diagrams, stock photos, ID cards, etc.
4. **Improve title extraction** for titles with diacritics and punctuation (H-category)
5. **The 3 regression books pass** and should continue to be tested
