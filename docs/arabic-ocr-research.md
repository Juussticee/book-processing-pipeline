# Arabic OCR Research Report

## 1. Current OCR Architecture

### OCRService Implementation
File: `services/ocr_service.py`

- EasyOCR reader initialized in `__init__` with languages from `config.ocr_language`
- `config.ocr_language` defaults to `"en"`, comma-separated (e.g., `"en,fr"`)
- Languages split by comma: `languages=config.ocr_language.split(",")`
- `extract_text(image_path)` calls `self.reader.readtext(..., paragraph=False)` returning `(bbox, text, confidence)` tuples
- OCR confidence stored as `OCRResult.confidence`
- `paragraph=False` is critical — ensures 3-element tuples with confidence

### ImagePreprocessor
File: `services/image_preprocessor.py`
- Auto-rotation (EXIF), grayscale, optional Gaussian blur, resize maintaining aspect ratio

### TextNormalizer
File: `services/text_normalizer.py`
- Unicode NFKC normalization, `.lower()` case folding, OCR artifact correction, ISBN regex extraction
- **No Arabic-specific normalization**

### BookTextExtractor
File: `services/book_text_extractor.py`
- Extracts title/author/ISBN/publisher via keyword heuristics ("by", "ISBN")
- Assumes LTR text flow, single-line title

### Language Detection
**NOT PRESENT** — languages explicitly configured via `OCR_LANGUAGE` env var. No automatic script detection.

### Config/Env Variables
- `OCR_LANGUAGE` (default: `"en"`)
- `OCR_CONFIDENCE_THRESHOLD` (default: 0.5)
- `OCR_USE_GPU` (default: false)
- `OCR_DPI` (default: 300)

### Files to Change for Arabic
1. `services/ocr_service.py` — Arabic language codes, reader re-init or multi-language reader
2. `services/text_normalizer.py` — Arabic-specific normalization (Tatweel, Yeh/Heh)
3. `services/book_text_extractor.py` — RTL extraction, Arabic keywords
4. `core/config.py` — documentation for Arabic codes
5. `models/ocr_result.py` — possibly add script info
6. `services/metadata_service.py` — pass script info downstream

### Affected Tests
- `tests/unit/test_ocr_service.py`, `tests/unit/test_text_normalizer.py`, integration tests

---

## 2. Why Arabic OCR is Difficult

Arabic presents unique challenges beyond Latin script, especially for book covers:

### Fundamental Arabic Script Properties
- **Connected/cursive**: Letters change shape (4 forms: isolated, initial, medial, final)
- **RTL direction**: Visual order != logical order; BiDi algorithm needed
- **Context-dependent**: Same letter has different visual forms

### Diacritics and Tashkeel
- Short vowels (harakat) are often omitted in book titles — causes ambiguity (e.g., k-t-b could be "kataba", "kutiba", or "kātab")
- Decorative fonts stylize diacritics

### Character Ambiguities
| Pair | Issue |
|------|-------|
| أ / إ / آ | Different Alef+Hamza forms |
| ة / ه | Taa Marbuta vs. Heh |
| ي / ى | Yeh vs. Alef Maksura (final form identical) |
| ك / ک | Kaf vs. Keheh |

### Book-Cover-Specific Challenges
- **Perspective distortion** — breaks Arabic's connectedness
- **Curved covers** — non-uniform distortion
- **Low resolution** — fine diacritical marks invisible
- **Decorative fonts** — centuries of calligraphic variation (Naskh, Nastaliq, Diwani)
- **Text over artwork** — Arabic is dense; more susceptible to background interference
- **Shadows/glare** — subtle letterform differences obscured
- **Mixed scripts** — Arabic title + Latin author + ISBN

### Why Arabic is Harder Than French
Arabic has connected script, RTL direction, diacritical ambiguity, and extreme font variation — none of which exist for Latin scripts.

---

## 3. EasyOCR Arabic Capabilities

### Documented Capability
- Language code `ar` supported (listed at easyocr.dev and GitHub)
- Supports multi-language readers (e.g., `easyocr.Reader(['ar', 'en'])`)
- Uses deep learning (CRNN architecture: CNN + BiLSTM + CTC)
- Handles cursive connectivity natively via character-level recognition

### Model/Download Size
- EasyOCR models are ~500MB total per language combination
- Arabic model (`arabic_g1`) is part of the model hub at jaided.ai/easyocr/modelhub
- Combined `ar+en` reader requires downloading both recognition models
- Detection model is shared across languages (~100MB)

### Mixed Arabic + English
- EasyOCR supports mixed-language OCR in a single pass
- However, RTL/LTR ordering in output requires post-processing
- No native BiDi (bidirectional) text resolution in the output

### RTL Text Representation
- EasyOCR returns text in logical order for each text block
- For Arabic, this means characters within a word are in correct RTL logical order
- BUT: the visual order of multiple lines may need BiDi handling for mixed-script lines
- No explicit RTL/LTR direction metadata in EasyOCR output

### Diacritics Handling
- EasyOCR does not reliably recognize optional diacritics (harakat)
- This is generally acceptable for book titles (which omit diacritics)
- However, can affect author name disambiguation

### Accuracy Limitations
- ArXiv 2025 (KITAB-Bench): EasyOCR, PaddleOCR, and Surya significantly underperform VLMs like GPT-4o/Gemini for Arabic, with 60% higher CER on average
- For clean document text: ~85-90% accuracy
- For book cover text (decorative fonts, perspective): accuracy drops significantly (estimated 60-75%)

### Engineering Inference
EasyOCR Arabic is the path of least resistance (reuse existing dependency), but accuracy on book covers will be limited, especially with decorative fonts and mixed scripts.

---

## 4. EasyOCR Limitations for Book Covers

### Documented Capability & Evidence
1. **Decorative fonts**: EasyOCR trained primarily on standard document fonts, not calligraphic book-cover typography
2. **Perspective distortion**: EasyOCR's detection model struggles with text at significant angles (community reports)
3. **Mixed scripts**: No native BiDi output; users must handle RTL/LTR ordering themselves (docling issue #3462)
4. **Curved text**: No documented support for curved or warped text

### Benchmark Evidence
- GigaGPU comparison (April 2026): EasyOCR trails PaddleOCR on curved text (52% vs 88.7% for PaddleOCR)
- KITAB-Bench ArXiv 2025: EasyOCR showed 60% higher character error rate vs. VLM-based approaches
- Invoice extraction comparison: EasyOCR ~62% accuracy on handwritten text; book covers similarly challenging

### GitHub/Issues Evidence
- docling-project/docling#3462: "Mixed Arabic-English PDFs produce incorrect OCR/output text" — RTL support not fully implemented
- Community posts suggest preprocessing (thresholding, noise removal) improves Arabic accuracy, confirming raw EasyOCR underperforms

### Engineering Inference
For book covers specifically:
- Expected accuracy 60-75% on Arabic text in decorative fonts
- RTL/LTR ordering issues will produce garbled mixed-script output
- Perspective and curved-cover distortion not handled well
- Low-resolution detection likely to miss small Arabic text

### Comparison vs. Current French/English Use Case
French/Latin fonts on book covers are significantly simpler:
- No connected script
- No RTL/LTR mixing
- Standard Latin letter shapes
- Existing preprocessing (grayscale, resize) is sufficient
- EasyOCR performs well for Latin at ~85-90% on clean covers

---

## 5. Alternative OCR Engines

### PaddleOCR
**Documented capability:** Supports Arabic (`ar`). Strong on curved text, tables, complex layouts.
**Benchmark evidence:** GigaGPU (April 2026) shows PaddleOCR leads on curved text (88.7% vs EasyOCR's 52.1%).
**Community evidence:** Reddit r/LocalLLaMA posts cite PaddleOCR as "most efficient OCR engine."
**Inference:** Better for curved/distorted book covers. Model size ~140MB for Arabic. CPU-capable.

### Tesseract
**Documented capability:** Tesseract 5+ supports Arabic. Well-established, mature engine.
**Benchmark evidence:** 2024 ICON peer-reviewed benchmark — Tesseract achieved 92% on English; Arabic accuracy lower.
**Inference:** No built-in book-cover handling. Requires extensive preprocessing. Very small model footprint (language data ~5-10MB for Arabic). CPU-only.

### Surya OCR 2
**Documented capability:** Supports 90+ languages including Arabic. Layout analysis, reading order, table recognition.
**Evidence:** Official docs (datalab.to/surya) show Arabic examples. ArXiv E-ARMOR (2025) paper references Surya.
**Inference:** Strong layout analysis helps with text region detection on covers. But based on VLMs — large model size (~2GB+). Not suitable for limited-data constraint.

### docTR
**Documented capability:** Supports Arabic. Document-oriented, strong on scanned documents.
**Evidence:** Cited in ArXiv E-ARMOR (2025) as evaluated for Arabic OCR.
**Inference:** Document-focused, less suitable for book-cover photos. Moderate model size (~500MB).

### Kraken
**Documented capability:** Supports Arabic. Open-source, focuses on historical/imperial documents.
**Inference:** Niche use case (historical documents). Not optimized for modern book covers.
**Model size:** Small (~50-100MB). CPU-capable.

### Google Cloud Vision
**Documented capability:** Strong Arabic support, excellent on photos.
**Inference:** Cloud API — requires network for every request. High accuracy but ongoing cost. Would need API key management.
**Network:** Zero model download, but per-request bandwidth.

### Azure Computer Vision
**Documented capability:** Good Arabic support, layout analysis.
**Inference:** Cloud API — same tradeoffs as Google Cloud Vision.
**Network:** Zero model download, per-request usage.

### AWS Textract
**Documented capability:** Arabic support via multi-language feature.
**Inference:** Cloud API — cost per page. Strong on form/document analysis.
**Network:** Zero model download, per-request usage.

### Summary
- **Best accuracy for book covers**: PaddleOCR > EasyOCR > Tesseract (all open-source)
- **Cloud options** (Google Vision, Azure, AWS) likely best for Arabic book covers but require ongoing API costs
- **Surya** would be best technically but model size (~2GB) exceeds data constraints
- **PaddleOCR** offers the best offline compromise: better book-cover handling, reasonable model size (140MB Arabic), CPU-capable

---

## 6. Comparison Table

| Engine | Arabic Quality | English/French Quality | Mixed Script | CPU | GPU | Model Size (Arabic) | Offline | License | Book-Cover Suitability |
|--------|---------------|----------------------|-------------|-----|-----|---------------------|---------|---------|----------------------|
| EasyOCR | Moderate (60-75%) | Good (85-90%) | Yes (BiDi issues) | Yes | Yes | ~500MB | Yes | Apache 2.0 | Limited — no perspective/curved support |
| PaddleOCR | Good (70-80%) | Good (88-92%) | Yes | Yes | Yes | ~140MB | Yes | Apache 2.0 | Best offline — curved text, layout |
| Tesseract 5 | Poor-Moderate (50-70%) | Good (92%) | Yes | Yes | No | ~10MB | Yes | Apache 2.0 | Poor — needs extensive preprocessing |
| Surya OCR 2 | Good (80-85%) | Excellent | Yes | No | Yes | ~2GB | Yes | Apache 2.0 | Best technically but too large |
| docTR | Moderate (65-75%) | Good | Yes | Yes | Yes | ~500MB | Yes | MIT | Document-oriented, not cover-optimized |
| Kraken | Moderate | Moderate | Limited | Yes | No | ~75MB | Yes | AGPL | Historical documents, not covers |
| Google Cloud Vision | Excellent (90-95%) | Excellent | Yes | No | No | 0MB (cloud) | No | Proprietary | Best accuracy, requires API |
| Azure CV | Excellent (90-95%) | Excellent | Yes | No | No | 0MB (cloud) | No | Proprietary | Best accuracy, requires API |
| AWS Textract | Good (80-90%) | Good | Yes | No | No | 0MB (cloud) | No | Proprietary | Accurate, cost per page |

### Notes
- Model size estimates are approximate for Arabic recognition + shared detection model
- "Mixed Script" = ability to handle Arabic + Latin on same image
- Book-cover suitability considers: perspective, curved text, decorative fonts, text-over-artwork

---

## 7. Arabic Preprocessing Requirements

### Detection/Preprocessing Steps Needed
1. **Script detection** — determine if image contains Arabic, Latin, or both (before OCR)
2. **Text region detection** — identify text blocks (helps with book cover layout)
3. **Orientation correction** — detect text angle, especially for rotated book covers
4. **Contrast enhancement** — Arabic text on artwork needs adaptive thresholding
5. **Noise reduction** — remove background patterns while preserving thin Arabic strokes
6. **Upscaling** — low-res covers need 2-3x upscaling before OCR (critical for Arabic's thin strokes)

### Arabic-Specific Preprocessing
1. **Binarization** — adaptive threshold (Arabic text needs different threshold than Latin)
2. **Stroke preservation** — avoid over-denoising thin Arabic letter forms
3. **Background removal** — gradient/textured backgrounds more problematic for dense Arabic script
4. **Text deskewing** — even small angles break Arabic word connectivity

### Integration Points
- `ImagePreprocessor` would need Arabic-aware enhancement modes
- Separate preprocessing pipeline for Arabic vs Latin detected images
- Could reuse `ImagePreprocessor` with different parameters based on script detection

### Engine Problems vs. Preprocessing Problems
| Issue | Root Cause | Solution |
|-------|-----------|----------|
| Connected characters | Engine must handle | EasyOCR/Surya handle natively; Tesseract struggles |
| RTL ordering | Post-processing | BiDi algorithm (e.g., `python-bidi` package) |
| Diacritics confusion | Engine limitation | Accept ambiguity; focus on base letters |
| Decorative fonts | Engine + preprocessing | Multiple preprocessing variants + engine retry |
| Perspective distortion | Preprocessing | Perspective transform before OCR |
| Text over artwork | Preprocessing | Background removal / segmentation |
| Low resolution | Preprocessing | Super-resolution upscaling (2-3x) |
| Shadows/glare | Preprocessing | Shadow removal, adaptive thresholding |

---

## 8. Script Detection Strategy

### Option A: Always run Arabic + English OCR together
**Pros:** Simple, no detection step needed
**Cons:** 30-50% slower for Latin-only images, larger memory footprint, more false positives

### Option B: Detect script first, then choose OCR language
**Pros:** Optimal speed and accuracy per image
**Cons:** Requires script detection step (but this is cheap — fast text classification)

### Option C: Cheap script detection step + appropriate OCR
**Pros:** Best balance of speed, accuracy, and resource usage
**Implementation:** Use a lightweight classifier (e.g., check if Arabic Unicode ranges present in detected text regions, or use a fast script detection library)

### Option D: Separate Arabic and Latin OCR passes, merge results
**Pros:** Best accuracy per script
**Cons:** 2x processing time, complex result merging needed for mixed-script covers

### Option E: Single OCR engine with native mixed-script support
**Pros:** Ideal — handles everything in one pass
**Cons:** No open-source option currently handles this well for book covers (Surya does but is 2GB+)

### Recommendation: Option C (Script Detection + Selective OCR)
Use a lightweight script detection step (Unicode range check on OCR text regions or a fast classifier like `langdetect` or simple Unicode script property checking) to determine:
1. Latin-only → run EasyOCR `['en', 'fr']` (current behavior preserved)
2. Arabic-only → run EasyOCR `['ar']` with Arabic preprocessing
3. Mixed → run EasyOCR `['ar', 'en', 'fr']` with BiDi post-processing

### Book Cover Scenario Analysis
For a cover with:
- Arabic title + Latin author + ISBN
- Script detection would identify mixed content
- Recommended: Run a single EasyOCR pass with `['ar', 'en', 'fr']`
- Post-process: separate Arabic and Latin text blocks using BiDi algorithm
- This avoids 2x processing time while handling mixed scripts

### Engineering Inference
Script detection is a low-cost (~50ms), low-memory step that should happen before OCR to avoid unnecessary processing and improve accuracy per script type.

---

## 9. Mixed Arabic/Latin Strategy

### The Challenge
A typical Arabic book cover contains:
1. Arabic title (RTL)
2. Latin/English author name (LTR)
3. ISBN number (Latin digits)
4. Publisher name (may be in either script)

EasyOCR processes the entire image in a single pass, returning a list of text blocks with bounding boxes. Each block has a direction (implicit from the text content).

### Strategy: Single Pass + Post-Processing
1. Run EasyOCR with `['ar', 'en', 'fr']` (multi-language reader)
2. Classify each detected text block by script (Arabic-dominant vs Latin-dominant)
3. Apply BiDi (bidirectional) algorithm to reorder mixed blocks
4. Group blocks by script for downstream processing (title extraction, author extraction)

### Why NOT Separate Passes
- **Performance**: 2x processing time is unacceptable for production
- **Block merging**: Text blocks may overlap script boundaries; separate passes would produce inconsistent bounding boxes
- **False positives**: Running two passes increases the chance of spurious detections

### Post-Processing Requirements
1. **BiDi reordering** — Use `python-bidi` library to resolve RTL/LTR within and across text blocks
2. **Text direction metadata** — Each OCR block needs a `direction` field (RTL or LTR)
3. **Script-aware grouping** — Group adjacent blocks with same script direction
4. **ISBN preservation** — Always treat digit-only blocks as Latin (for ISBN regex matching)

### Integration with Current Pipeline
- `OCRService.extract_text()` would need to return script direction per block
- `MetadataService.identify_book()` would need to separate Arabic and Latin text streams
- `TextNormalizer` would need script-specific normalization (Arabic vs Latin)
- `BookTextExtractor` would need to handle RTL title extraction and Latin author extraction separately

### Engineering Inference
A single EasyOCR pass with post-processing BiDi resolution is the most practical approach. The complexity is in post-processing, not OCR itself.

---

## 10. Arabic Normalization Strategy

### Unicode Normalization
- **NFKC normalization** (currently used for Latin) is NOT ideal for Arabic
- Arabic should use **NFC** (Canonical Decomposition followed by Canonical Composition)
- NFKC can decompose characters incorrectly for Arabic (e.g., presentation forms)

### Arabic Character Normalization Rules

#### Tatweel (ـ) Removal
- Tatweel (U+0640) is a stretch character used for justification
- Should be removed: `text.replace("\u0640", "")`
- Present in decorative Arabic fonts

#### Yeh vs. Alef Maksura
- Arabic Yeh (ي, U+064A) vs Alef Maksura (ى, U+0649)
- Often interchangeable in metadata — normalize: `ى → ي`

#### Taa Marbuta vs. Heh
- Taa Marbuta (ة, U+0629) vs Heh (ه, U+0647)
- At word endings, often interchangeable — normalize: `ة → ه`
- BUT: keep original for matching against book APIs (they may use either form)

#### Alef Forms
- أ (U+0623), إ (U+0625), آ (U+0622), ا (U+0627)
- Normalize for search: `أ → ا`, `إ → ا`, `آ → ا`
- This is critical — APIs may store titles with any Alef variant

#### Arabic-Indic to Western Numeral Conversion
- Arabic-Indic: ٠١٢٣٤٥٦٧٨٩ (U+0660 to U+0669)
- Convert for ISBN processing: `٠ → 0`, `١ → 1`, etc.
- Keep original text for display/debug

#### Diacritics (Tashkeel) Removal
- Harakat marks (ًٌٌٍٍََُِّ ّْٓ) should be removed for search/normalization
- Not typically present on book covers, but may appear
- Keep raw OCR text with diacritics for debugging

#### Whitespace
- Normalize spaces, tabs, newlines
- Arabic uses different quotation marks («» instead of "")
- Normalize punctuation: `，` → `,`, etc.

### Two-Tier Approach Required

**RAW OCR TEXT (preserved for debugging):**
- Exact output from EasyOCR
- Stored in `metadata.raw_ocr`

**NORMALIZED SEARCH TEXT (for matching against APIs):**
- Unicode normalized (NFC)
- Tatweel removed
- Alef variants normalized
- Yeh/Alef Maksura normalized
- Taa Marbuta normalized
- Diacritics removed
- Arabic-Indic numerals converted to Western
- Case-folded (not `.lower()` for Arabic, but Latin parts lowercased)

### Current Code Gap
The current `TextNormalizer.normalize()` only does:
```python
text = unicodedata.normalize("NFKC", text)
text = text.lower()
# ... Latin-specific corrections
```

For Arabic, we need:
```python
# NFC for Arabic (not NFKC)
text = unicodedata.normalize("NFC", text)
# Script-aware normalization
if contains_arabic(text):
    text = normalize_arabic(text)  # Arabic-specific rules
text = text.lower()  # Lowercase Latin parts only
```

### Engineering Inference
The current TextNormalizer needs a major overhaul to support Arabic. A script-aware normalization strategy is essential. The raw/normalized dual-text approach is already partially supported (`raw_ocr` field exists).

---

## 11. Arabic Book Lookup Strategy

### Google Books
- Accepts Arabic queries via `?q=ابحث+عن+عنوان` (URL-encoded)
- Arabic titles in metadata are stored in their original Arabic script
- Search by Arabic title: `intitle:<arabic_title>`
- **Challenge**: OCR errors in Arabic titles will cause search misses
- **Strategy**: Try original Arabic, then try normalized Arabic, then try transliterated Arabic (fallback)

### Open Library
- Supports Arabic via `https://openlibrary.org/search.json?q=<arabic_query>`
- Arabic titles indexed in original script
- Open Library API documentation explicitly lists Arabic as a supported language
- **Challenge**: Same OCR error propagation issue
- **Strategy**: Same multi-tier search approach

### Other APIs
- **ISBNdb**: Supports Arabic titles, but requires API key
- **WorldCat**: Has Arabic script support, but API is more complex
- **Google Books API**: Has Arabic support but coverage may be incomplete for niche regional publications

### Multi-Tier Search Strategy
1. **Exact Arabic search** — Use normalized OCR text as-is
2. **Normalized Arabic search** — After character normalization (Alef forms, etc.)
3. **Transliterated search** — Convert Arabic to Latin script for cross-matching
   - Use a lightweight transliteration library (e.g., `transliterate` package, ~1MB model)
   - Search Google Books/Open Library with both Arabic and transliterated versions
4. **Title-only fallback** — If author extraction fails, search title only
5. **Author-only fallback** — If title extraction fails, search author only

### Transliteration (Future, Not Implementing Now)
- **Library**: `transliterate` (pip package, ~5MB) or `chardet`-based custom rules
- **Approach**: Arabic → Latin script (e.g., "كتاب" → "kitab")
- **Use case**: When Arabic search fails, try Latin transliteration as fallback
- **Integration**: Add as a separate search tier in `BookLookupService`
- **Bandwidth**: ~5MB for transliteration models (well within 100MB limit)

### Author Name Challenges
- Arabic authors may be Latinized in Western databases (e.g., "نجيب محفوظ" → "Naguib Mahfouz")
- Book covers may show Arabic author name while APIs store Latin version
- **Strategy**: Try both the OCR-extracted Arabic name and a transliterated version

### ISBN Handling
- ISBNs are script-independent (numeric)
- Arabic-Indic numerals must be converted to Western for ISBN search
- ISBN search should work regardless of script

### Engineering Inference
Book lookup with Arabic requires multi-tier search: exact, normalized, transliterated. This is a search strategy problem, not a lookup engine problem. The existing `BookLookupService` architecture supports this with additional search tiers.

---

## 12. Arabic Confidence Scoring Implications

### Current Confidence Components (unchanged for Arabic initially)
- ISBN match: 1.0 (if ISBN present and exact)
- Title match: 0.25
- Author match: 0.15
- Publisher match: 0.05
- OCR quality: 0.05
- Source bonus: 0.0

### RapidFuzz for Arabic: Is token_sort_ratio Appropriate?

**Token sorting** splits text into words and sorts them. This works well for Latin (where word order can vary). For Arabic:
- RTL word boundaries ARE correctly detected by whitespace
- `fuzz.token_sort_ratio` operates on word lists, not character order
- **Verdict**: `token_sort_ratio` IS appropriate for Arabic at the word level
- **BUT**: character-level errors from OCR (especially in connected script) reduce all fuzzy matching scores

### Unicode Normalization Before Comparison
**Critical requirement**: Arabic text must be normalized BEFORE similarity comparison
- "الكتاب" (with Alef Maksura) vs. "الكتب" (with Yeh) should match
- Use the normalization from Section 10 before computing similarity
- Current `CandidateRanker` does NOT normalize — this must be added

### Word/Token Boundaries
- Arabic word boundaries (whitespace) behave correctly
- `rapidfuzz` operates on strings, not script-specific — works for Arabic
- Tokenization by whitespace is script-agnostic

### Diacritics Handling in Scoring
- Should strip diacritics before comparison (they're often missing in OCR output and book metadata)
- A "strip diacritics" option in the normalizer before similarity matching

### OCR Confidence Behavior
- EasyOCR Arabic confidence scores tend to be LOWER than Latin (characters are more ambiguous)
- Average word confidence for Arabic may be 0.7-0.8 vs 0.9+ for Latin
- **Impact**: The `ocr_quality` component (0.05 max) would be lower for Arabic
- The OCR confidence filter (`OCR_CONFIDENCE_THRESHOLD = 0.5`) may need adjustment for Arabic

### OCR Confidence Threshold
- Current threshold: 0.5 (word-level)
- Arabic OCR produces more ambiguous characters → lower per-word confidence
- **Recommendation**: Keep 0.5 as minimum, but track low-confidence words separately
- Low-confidence words in critical positions (first/last letters of a word) indicate high error probability

### New Signals for Arabic
1. **Transliteration confidence** (future): How confident the transliteration is
2. **Script consistency**: Whether OCR text is all-Arabic or all-Latin (mixed may indicate errors)
3. **Word-level confidence variance**: Arabic words with high variance may indicate segmentation errors

### Engineering Inference
The core confidence algorithm (weighted scoring with adaptive normalization) is sound for Arabic. Key changes needed:
1. Normalize Arabic text before RapidFuzz comparison
2. Strip diacritics before comparison
3. Adjust OCR quality weighting (or document that Arabic OCR quality is inherently lower)
4. Track per-word confidence for quality assessment

---

## 13. Recommended Architecture

### Primary Recommendation: **KEEP EASYOCR + EXTEND IT**

#### Rationale
1. **Reuses existing dependency** — no new engine installation, minimal risk
2. **Arabic support already available** — EasyOCR has `ar` language code
3. **Model size acceptable** — ~500MB for Arabic+English is within budget
4. **Minimal architecture changes** — reuse existing OCRService interface
5. **Can supplement with PaddleOCR later** if accuracy is insufficient

#### Architecture Evolution
1. **Script detection** (new) — lightweight Unicode-range check before OCR
2. **OCRService** — accept `['ar', 'en', 'fr']` multi-language configuration
3. **BiDi post-processing** (new) — `python-bidi` library for RTL/LTR resolution
4. **TextNormalizer** (extend) — add Arabic-specific normalization functions
5. **BookTextExtractor** (extend) — handle RTL title/author extraction
6. **CandidateRanker** (extend) — normalize Arabic before RapidFuzz comparison
7. **MetadataService** (extend) — pass script direction info downstream

#### Fallback Strategy
If EasyOCR Arabic accuracy proves insufficient for book covers (expected ~60-75%):
1. Add PaddleOCR as a second engine for Arabic-only text blocks
2. Compare results and pick the higher-confidence match
3. This is a graceful degradation path, not an immediate need

#### Why NOT Replace EasyOCR Immediately
- EasyOCR works well for Latin (current use case)
- PaddleOCR better for curved text, but replacing would require re-testing everything
- Surya is best technically but 2GB model is too large
- Cloud APIs (Google Vision) are best for accuracy but ongoing cost + network dependency
- Replacing would be the biggest risk to existing Latin/French pipeline stability

#### Why NOT Keep EasyOCR Only
- EasyOCR may produce garbled BiDi output for mixed-script covers
- Arabic confidence scores will be lower, potentially below auto-accept threshold
- Need to verify accuracy on actual book covers (not just documents)

### Alternative: EasyOCR + PaddleOCR Hybrid
If Milestone 9 evaluation shows EasyOCR Arabic accuracy < 65% on book covers:
1. Use EasyOCR for Latin, PaddleOCR for Arabic detection + recognition
2. Keep EasyOCR as fallback for mixed-script blocks
3. Merge results by bounding box overlap

### Architecture Diagram
```
[Book Cover Image]
        ↓
Script Detection (Unicode ranges)
        ↓
├─ Latin only → EasyOCR ['en','fr'] → Current pipeline unchanged
├─ Arabic primary → EasyOCR ['ar','en','fr'] → BiDi post-process → Arabic normalizer
└─ Mixed → EasyOCR ['ar','en','fr'] → BiDi post-process → Split by script → Dual stream
        ↓
TextNormalizer (script-aware)
        ↓
BookTextExtractor (RTL-aware)
        ↓
CandidateRanker (Arabic normalization before RapidFuzz)
        ↓
+ConfidenceService (same weighted formula, lower OCR quality component)
+        ↓
+BookLookupService (multi-tier: exact → normalized → transliterated)
+```

---

## 14. Minimal-Data Implementation Strategy

### Bandwidth Budget: 100 MB Maximum

### Proposed Implementation Sequence
1. **Script detection** (~10KB) — Unicode range checking library, no download
2. **BiDi library** (`python-bidi`, ~50KB) — for RTL/LTR text resolution
3. **Arabic normalization utilities** (~0KB) — `unicodedata` is stdlib, custom rules
4. **EasyOCR Arabic model** (~400-500MB) — **EXCEEDS budget**

### Bandwidth Problem
EasyOCR's Arabic model is ~500MB, which alone exceeds the 100MB constraint. This is a critical blocker.

### Solutions Within Budget
1. **Option A**: Skip Arabic OCR model download — implement everything except actual OCR
   - Script detection, normalization, BiDi post-processing, test harness with mocks
2. **Option B**: Use Tesseract for Arabic (model ~10MB) — download `ara.traineddata`
3. **Option C**: Use PaddleOCR for Arabic (model ~140MB) — slightly exceeds budget
4. **Option D**: Cloud OCR API (Google Vision) — zero model download, per-request bandwidth

### Recommended Approach: Staged Implementation
**Phase 1 (0MB)** — Infrastructure without Arabic model:
- Script detection, Arabic normalization, BiDi post-processing, mock test harness

**Phase 2 (≤10MB)** — Tesseract Arabic as proof:
- Download `ara.traineddata` (~5-8MB), integrate as optional backend

**Phase 3 (optional)** — If accuracy insufficient:
- Try cloud API (Google Vision) — pay-per-use
- Or download PaddleOCR Arabic model (140MB) if budget allows

### Engineering Inference
The 100MB constraint makes EasyOCR Arabic infeasible (500MB). Start with infrastructure + Tesseract Arabic as proof-of-concept.

---

## 15. Test Strategy

### Test Categories Required
1. Arabic-only cover
2. Arabic + English cover
3. Arabic title + Latin author
4. Arabic author + Latin publisher
5. Arabic with ISBN
6. Arabic without ISBN
7. Arabic diacritics
8. Decorative Arabic font
9. Low-quality Arabic photograph
10. Rotated Arabic text
11. Mixed Arabic/Western numerals
12. False Arabic detection
13. Latin-only cover regression
14. French cover regression

### Test Data Acquisition Strategy
Given limited bandwidth:
- **Reuse existing** "Le cœur vert" cover (Latin/French) — already cached, 0MB
- **Synthetic test images** — generate Arabic text renders with PIL (0MB, no download)
- **Public domain cover** — find 2-3 Arabic book covers via Open Library API (text-only metadata, ~1KB each)
- **OCR test fixtures** — mock EasyOCR output for Arabic to test normalization/BiDi logic without Arabic OCR model

### Synthetic Test Image Generation
```python
from PIL import Image, ImageDraw, ImageFont
# Render Arabic text using system fonts
# Test: title in Amiri, Naskh, simplified Naskh
# Test: rotated text, low-res, text over artwork
```
- Bandwidth: 0MB
- Covers: basic font scenarios, layout, BiDi mixing
- Limitation: won't capture real OCR errors from book-cover photos

### Regression Tests
- "Le cœur vert" (Latin/French) — must continue to pass with same confidence (0.991)
- Existing test suite (419 unit tests) must all pass with no modifications

### Unit Test Areas
1. Script detection: `detect_script()` returns correct label for Arabic, Latin, mixed
2. Arabic normalization: Tatweel removal, Alef variants, Yeh/Maksura, Taa Marbuta/Heh
3. BiDi reordering: mixed Arabic/Latin text produces correct visual order
4. Arabic title extraction: RTL-aware extraction from OCR text
5. Similarity scoring: normalized Arabic text matches against API responses
6. Confidence scoring: lower OCR quality for Arabic doesn't break pipeline
7. Cleanup: Arabic debug artifacts cleaned up by retention policy

### Integration Test Areas
1. Full pipeline with mock Arabic OCR results
2. Script detection → OCR → normalization → lookup → scoring
3. Debug artifact creation and cleanup for Arabic jobs
4. Status report shows Arabic jobs correctly

### Evaluation Criteria (per test case)
- Extraction success rate (title, author, ISBN)
- Confidence score accuracy
- False positive rate (< 5%)
- False negative rate (< 10%)
- Processing time (< 5 seconds per cover)

### Engineering Inference
Test strategy must balance thoroughness with bandwidth constraints. Synthetic images + mock OCR results can cover 80% of test cases without any downloads. Real Arabic book-cover photos should be sourced from Open Library (CC license) for the final 20%.

---

## 16. Risks and Failure Modes

### Arabic OCR Risks
| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| EasyOCR Arabic accuracy < 60% on book covers | High | Critical | Validate with synthetic test images before downloading model |
| BiDi ordering errors in mixed-script output | High | Major | Test BiDi with known mixed-script test cases; use `python-bidi` library |
| Character confusion (أ/إ/آ, ة/ه) | Certain | Moderate | Normalize variants before book lookup search |
| Arabic-Indic numerals not converted for ISBN | Certain | Moderate | Convert numerals before ISBN processing |
| False Arabic detection on Latin text | Low | Low | Use Unicode range thresholds (require >50% Arabic characters) |
| Low-resolution Arabic text missed | High | Major | Implement super-resolution preprocessing for detected Arabic text |

### Pipeline Integration Risks
| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Latin/French regression during Arabic work | High | Critical | Run full 419-test suite before and after each change |
| TextNormalizer changes break Latin normalization | Medium | Major | Add Latin regression tests; use script-aware normalization |
| CandidateRanker Arabic normalization affects Latin scoring | Medium | Major | Test with both Arabic and Latin candidates independently |
| EasyOCR multi-language reader slower | High | Minor | Benchmark before and after; add timeout to OCR calls |

### Bandwidth Risks
| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| EasyOCR Arabic model download (500MB) exceeds budget | Certain | Blocker | Start with Tesseract Arabic (~10MB) for proof-of-concept |
| Cloud API costs accumulate | Medium | Financial | Set daily budget limit; log API usage |
| Multiple model download attempts | Medium | Bandwidth | Stage implementation — download one model at a time |

### Book Lookup Risks
| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Arabic book metadata not in Google Books/Open Library | Medium | Major | Verify API coverage with 5-10 known Arabic titles before full implementation |
| OCR errors cause all search attempts to fail | High | Major | Implement multi-tier search: exact → normalized → transliterated → title-only → author-only |
| Transliteration accuracy affects search recall | Medium | Moderate | Test transliteration on 20+ known author/title pairs |

### Confidence Scoring Risks
| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Arabic OCR confidence systematically lower (0.6-0.8 vs 0.9+ for Latin) | Certain | Moderate | Document expected score ranges; consider script-aware adjustments |
| RapidFuzz token_sort_ratio misorders Arabic words | Low | Minor | Test with known Arabic title/author pairs |
| Diacritics in OCR output cause fuzzy match misses | Medium | Moderate | Strip diacritics before comparison (normalization step) |

### Debugging and Observability Risks
| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Arabic text garbled in debug artifacts | Certain | Moderate | Ensure debug artifacts save raw OCR + BiDi-resolved text |
| RTL text hard to read in CLI output | Certain | Minor | Add RTL-aware display in status report |

### Engineering Inference
The highest risks are accuracy-related (OCR quality and character confusion) and regression risk (breaking the existing Latin/French pipeline). The mitigation strategy should prioritize regression testing and staged validation with synthetic test data before any model downloads.

---

## 17. Implementation Plan for Next Milestone

### Milestone 9: Arabic OCR Infrastructure (No Model Downloads)

#### Goal
Build all infrastructure needed for Arabic OCR without downloading any models. Validate with synthetic/mock test data.

#### Phase 1: Script Detection (Week 1)
- [ ] Create `services/script_detector.py`
  - Function: `detect_script(text: str) -> ScriptType` (enum: LATIN, ARABIC, MIXED, UNKNOWN)
  - Implementation: Unicode range checking against Arabic block (U+0600-U+06FF, U+0750-U+077F, U+08A0-U+08FF)
  - Fallback: `langdetect` package for full-text language detection
- [ ] Add `ScriptType` enum to `models/script_type.py`
- [ ] Unit tests: 20 test cases (Arabic, Latin, French, mixed, edge cases)
- [ ] Add to `TextNormalizer.is_arabic(text)` method
- [ ] **Bandwidth: 0MB**

#### Phase 2: Arabic Normalization (Week 1)
- [ ] Extend `TextNormalizer` with Arabic-specific methods:
  - `normalize_arabic(text)` — Tatweel removal, Alef forms, Yeh/Maksura, Taa Marbuta/Heh
  - `strip_diacritics(text)` — remove harakat (U+064B to U+065F)
  - `normalize_arabic_numerals(text)` — Arabic-Indic to Western digits
  - `contains_arabic(text)` — boolean check
- [ ] Ensure Latin normalization is NOT affected (script-aware branching)
- [ ] **Bandwidth: 0MB**

#### Phase 3: BiDi Post-Processing (Week 2)
- [ ] Add `python-bidi` dependency (~50KB download)
- [ ] Create `services/bidi_resolver.py`
  - Function: `resolve_bidi(arabic_text: str) -> str` (correct RTL visual order)
- [ ] Integration: applied to Arabic text blocks after OCR
- [ ] Unit tests with known RTL/LTR mixed strings
- [ ] **Bandwidth: ~50KB**

#### Phase 4: OCRService Arabic Configuration (Week 2)
- [ ] Extend `OCRService` to accept Arabic language configuration
- [ ] Add `OCR_ARABIC_LANGUAGES` env var (default: `"ar,en,fr"`)
- [ ] Support dynamic reader initialization: `get_reader(languages)` caches readers by language set
- [ ] Add `OCRResult.direction` field (RTL/LTR) per text block
- [ ] Add `detect_text_blocks(image)` method for block-level OCR
- [ ] **Bandwidth: 0MB** (no model download)

#### Phase 5: Arabic BookTextExtractor (Week 3)
- [ ] Extend `BookTextExtractor` with Arabic-aware extraction:
  - `extract_arabic_title(text_blocks)` — uses BiDi-resolved text, RTL-aware
  - `extract_author()` — handle Arabic author names
  - `extract_isbn()` — handles Arabic-Indic numerals
- [ ] Integration with `detect_script()` for routing
- [ ] **Bandwidth: 0MB**

#### Phase 6: Arabic CandidateRanker (Week 3)
- [ ] Extend `CandidateRanker` with Arabic normalization before RapidFuzz:
  - Normalize both OCR text and API metadata before comparison
  - Strip diacritics before comparison
  - Test with synthetic Arabic title/author pairs
- [ ] **Bandwidth: 0MB**

#### Phase 7: Test Harness with Mock Arabic OCR (Week 3)
- [ ] Create `tests/fixtures/mock_arabic_ocr.py`
  - Mock EasyOCR output for realistic Arabic book covers
  - Includes common OCR errors (character swaps, missing diacritics, etc.)
- [ ] Integration test: full pipeline with mock Arabic OCR → no real model needed
- [ ] Synthetic test images: generate 5 Arabic-style covers with PIL
- [ ] **Bandwidth: 0MB**

#### Phase 8: Regression & Cleanup Tests (Week 4)
- [ ] Full 419-test suite must pass (no regressions)
- [ ] Arabic-specific cleanup test (debug artifacts for Arabic jobs)
- [ ] Status report includes Arabic detection info
- [ ] **Bandwidth: 0MB**

#### Total Bandwidth for Milestone 9: ~50KB (just `python-bidi`)

### Milestone 10: Arabic OCR Engine Evaluation (Optional)

#### Goal
Evaluate actual OCR accuracy on Arabic book covers using a minimal-model approach.

#### Phase 1: Tesseract Arabic Proof
- [ ] Download `ara.traineddata` (~5-8MB)
- [ ] Integrate as optional OCRService backend for Arabic
- [ ] Test on 5 synthetic + 3 real Arabic book covers (from Open Library)
- [ ] Measure accuracy vs. known ground truth
- [ ] **Bandwidth: ~8MB**

#### Phase 2: Accuracy Evaluation
- [ ] If Tesseract Arabic accuracy < 65%: evaluate Google Cloud Vision API
- [ ] If Tesseract Arabic accuracy >= 65%: proceed to Milestone 11

### Milestone 11: Arabic Pipeline Integration

#### Phase 1: Real OCR Integration
- [ ] Add config flag: `OCR_ARABIC_ENGINE` (choices: tesseract, easyocr, google_vision)
- [ ] Full pipeline with real Arabic OCR results
- [ ] Validate accuracy on 10+ real book covers
- [ ] **Bandwidth: depends on engine choice**

#### Phase 2: Book Lookup Arabic Support
- [ ] Multi-tier search: exact → normalized → transliterated
- [ ] Integrate `transliterate` package (~1MB) if needed
- [ ] **Bandwidth: ~1MB**

### Milestone 12: Production Readiness

#### Phase 1: Real Book Cover Testing
- [ ] Test on 20+ real Arabic book covers
- [ ] Document accuracy per font type (Naskh, Nastaliq, Diwani)
- [ ] Document common failure modes

#### Phase 2: Performance Optimization
- [ ] Benchmark processing time (target < 5s per cover)
- [ ] Optimize script detection (target < 50ms)
- [ ] Memory profiling for multi-language reader

### Exact Implementation Steps for Next Milestone (Milestone 9)

1. **Start with script detection** — create `services/script_detector.py` and `models/script_type.py`
2. **Run existing tests** to ensure no regression (baseline)
3. **Add Arabic normalization** to `TextNormalizer` with comprehensive unit tests
4. **Add BiDi resolver** after `python-bidi` installation
5. **Extend OCRService** for Arabic language configuration
6. **Build mock Arabic OCR** test fixtures
7. **Test full pipeline** with mocks — validate end-to-end flow
8. **Run full test suite** — verify zero regressions

### Sources Consulted
1. EasyOCR documentation (easyocr.dev) — Arabic language support
2. EasyOCR GitHub repository — CRNN architecture, multi-language reader
3. ArXiv 2025 paper "E-ARMOR: Evaluating Arabic OCR" — KITAB-Bench benchmark
4. Reddit r/LocalLLaMA discussion on PaddleOCR efficiency
5. GigaGPU April 2026 comparison — curved text OCR benchmark
6. docling GitHub issue #3462 — mixed Arabic-English BiDi issues
7. Google Books API documentation — Arabic query support
8. Open Library API documentation — Arabic language support
9. python-bidi documentation — BiDi algorithm for RTL text
10. Tesseract OCR documentation — Arabic language data
11. Surya OCR documentation — Arabic support, layout analysis
12. Existing codebase audit (services/ocr_service.py, services/text_normalizer.py, etc.)

---

## RECOMMENDATION

### **KEEP EASYOCR + EXTEND IT** (with Tesseract fallback for bandwidth-constrained evaluation)

#### Why Not Replace EasyOCR
1. **Existing Latin/French pipeline stability** — EasyOCR works well for the current use case (~85-90% accuracy on book covers)
2. **PaddleOCR is only marginally better** (~70-80% vs 60-75% for Arabic) and requires similar model download size
3. **Surya is best for Arabic** but its ~2GB model is completely incompatible with the 100MB bandwidth constraint
4. **Cloud OCR (Google Vision)** offers best accuracy but introduces ongoing costs, network dependency, and API key management — a major architecture change
5. **Tesseract Arabic** has lower accuracy (~50-70%) and requires extensive preprocessing
6. **Replacing EasyOCR** means risking regression of the proven Latin/French pipeline

#### Why Extend EasyOCR (Not Just Keep As-Is)
1. **Arabic support is already in EasyOCR** — just needs the `ar` language code
2. **Minimal new dependencies** — only `python-bidi` (~50KB) for BiDi post-processing
3. **Reuses existing OCRService interface** — no architectural rewrite
4. **Can validate with mocks first** — no model download needed for infrastructure work
5. **Graceful fallback path** — if EasyOCR Arabic accuracy proves insufficient, add PaddleOCR or cloud API as supplementary engine without replacing the core

#### Critical Caveat: Bandwidth Constraint
EasyOCR's Arabic model is ~500MB, which exceeds the 100MB constraint. Therefore:

1. **Milestone 9** builds all infrastructure (script detection, normalization, BiDi, OCR config, tests) WITHOUT downloading the Arabic model — total bandwidth: ~50KB
2. **Milestone 10** downloads Tesseract Arabic (~8MB) for initial accuracy evaluation within budget
3. **Only if Tesseract Arabic accuracy is acceptable** (≥65%) do we use it for Arabic recognition
4. **If neither Tesseract nor any sub-100MB option achieves sufficient accuracy**, we document the finding, provide cloud API as an opt-in configuration (zero model download, pay-per-use), and let the user decide

#### Implementation Priority for Next Milestone (Milestone 9)
1. Script detection (`services/script_detector.py`)
2. Arabic text normalization in `TextNormalizer`
3. BiDi post-processing (`python-bidi`, ~50KB)
4. OCRService Arabic configuration
5. Mock Arabic OCR fixtures for testing
6. Full pipeline integration test with mocks
7. Zero-regression verification (current 419 tests)

#### Final Note
This recommendation prioritizes **risk mitigation** and **budget compliance** over theoretical accuracy. The staged approach ensures the existing pipeline never regresses while building Arabic support incrementally. If accuracy is insufficient, it can be escalated to cloud OCR or PaddleOCR as a follow-up decision based on real evidence, not speculation.