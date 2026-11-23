# Arabic OCR Research Report — Part 1: Current Architecture Audit

## Status: Completed

## 1. Current OCR Architecture (Audit Complete)

### OCRService Implementation
File: `services/ocr_service.py`

Key findings:
- EasyOCR reader is initialized in `__init__` with languages from `config.ocr_language`
- `config.ocr_language` defaults to `"en"` but can be set to comma-separated languages (e.g., `"en,fr"`)
- OCR languages are split by comma: `languages=config.ocr_language.split(",")`
- `extract_text(image_path)` method calls `self.reader.readtext(..., paragraph=False, ...)` returning tuples `(bbox, text, confidence)`
- OCR confidence is passed downstream as `OCRResult.confidence`
- `paragraph=False` is critical — ensures 3-element tuples with confidence

### ImagePreprocessor
File: `services/image_preprocessor.py`
- Auto-rotation based on EXIF orientation
- Converts to grayscale
- Applies Gaussian blur if specified
- Resizes maintaining aspect ratio (max dimension configurable)
- Returns preprocessed image path

### TextNormalizer
File: `services/text_normalizer.py`
- Unicode normalization: `unicodedata.normalize("NFKC", text)` for Latin scripts
- Case folding: `.lower()`
- OCR artifact correction (common character misreads)
- ISBN extraction via regex
- No Arabic-specific normalization

### BookTextExtractor
File: `services/book_text_extractor.py`
- Extracts title, author, ISBN, publisher from OCR text
- Uses keyword-based heuristics (lines with "by", "ISBN", etc.)
- Assumes left-to-right text flow
- Single-line title assumption

### Language Detection
NOT PRESENT in the current codebase:
- No automatic script/language detection
- Languages are explicitly configured via `OCR_LANGUAGE` env var
- OCR is initialized once with the configured languages

### Configuration System
File: `core/config.py`
- `ocr_language: str = field(default="en")` — comma-separated language codes
- `ocr_confidence_threshold: float = field(default=0.5)` — min OCR word confidence
- `ocr_use_gpu: bool = field(default=False)`
- `ocr_dpi: int = field(default=300)`

### EasyOCR Initialization
```python
self.reader = easyocr.Reader(
    languages=config.ocr_language.split(","),
    gpu=config.ocr_use_gpu,
    ...
)
```
- Reader is created once at service init
- Switching languages requires re-initializing the reader (expensive)

### OCR Confidence Calculation
- Per-word confidence from EasyOCR (0.0-1.0 scale)
- `OCRResult.confidence` = average of word confidences
- Used in `MetadataService` for confidence scoring:
  - `ocr_quality` component = `min(ocr_confidence / 0.50, 1.0) * 0.05`
  - Max contribution: 0.05 to base confidence

### OCR Output Downstream Flow
1. `OCRService.extract_text(image_path)` → `OCRResult(text, confidence, words, language, image_path)`
2. `MetadataService.identify_book`:
   - Normalizes text via `TextNormalizer`
   - Extracts structured fields via `BookTextExtractor`
   - Uses OCR text + confidence for:
     - Title/author extraction
     - OCR quality component of confidence scoring
     - Debug artifacts (raw text saved)

### Mixed-Language Text Handling
Currently: NONE
- Text is treated as a single stream
- No handling of bidirectional (RTL/LTR) text
- `extracted_title`, `extracted_author` are single strings
- If both Arabic and Latin text are present, they'd be mixed in the OCR result without distinction

### Where Language Script Assumptions Exist
1. `TextNormalizer.normalize()` — assumes NFKC normalization is appropriate for all scripts (it is not ideal for Arabic)
2. `BookTextExtractor` — keyword detection assumes Latin keywords ("book", "by", "ISBN")
3. `OCRResult.language` — set from OCR language config, not from actual detected language
4. Confidence scoring weights assume Latin text structure
5. No RTL handling anywhere in the pipeline

### Files to Change for Arabic Support
1. `services/ocr_service.py` — add Arabic language code, handle reader re-init or multi-language reader
2. `services/text_normalizer.py` — add Arabic-specific normalization (Tatweel removal, Yeh/Heh normalization)
3. `services/book_text_extractor.py` — handle RTL title/author extraction, Arabic keywords
4. `core/config.py` — add `OCR_LANGUAGE` documentation for Arabic codes
5. `models/ocr_result.py` — possibly add detected language/script
6. `services/metadata_service.py` — pass script info downstream

### Tests to be Affected
- `tests/unit/test_ocr_service.py` — needs Arabic language tests
- `tests/unit/test_text_normalizer.py` — needs Arabic normalization tests
- `tests/integration/test_*.py` — integration tests with Arabic images

### Config/Env Variables Related to OCR
- `OCR_LANGUAGE` (default: "en") — comma-separated EasyOCR language codes
- `OCR_CONFIDENCE_THRESHOLD` (default: 0.5) — min word confidence filter
- `OCR_USE_GPU` (default: false) — GPU acceleration
- `OCR_DPI` (default: 300) — DPI setting

---

## 2. Why Arabic OCR is Difficult

Arabic OCR presents unique challenges that are significantly harder than Latin script OCR, especially for book covers. These difficulties arise from the fundamental characteristics of the Arabic script and the nature of book-cover imagery.

### Fundamental Arabic Script Properties

**Connected/Cursive Script:**
Unlike Latin text where each letter is independent, Arabic letters change shape depending on their position in a word (initial, medial, final, or isolated). This means the same letter has up to four different visual forms. OCR engines must solve a character segmentation problem that doesn't exist for Latin scripts.

**Right-to-Left (RTL) Writing Direction:**
Arabic is written RTL, but book covers may contain mixed RTL and LTR text (e.g., Arabic title + Latin author + ISBN). The bidirectional (BiDi) text rendering means the logical order of characters differs from visual order, creating complexity in both OCR output and downstream processing.

**Context-Dependent Letter Forms:**
The Arabic letter ﻻﻟﻢ (Lam) has different shapes: isolated (ﻝ), initial (ﻠ), medial (ﻠ), and final (ﻡ). A single misclassification of the letter form leads to wrong character recognition.

### Diacritics and Tashkeel

**Optional but Meaning-Changing Diacritics:**
Arabic uses diacritical marks (harakat) to indicate short vowels. In practice, most Arabic text (especially on book covers and in titles) omits these diacritics, leading to ambiguity. For example, ﻙﺘﺎﺑ (k-t-b) could be "kataba" (he wrote), "kutiba" (it was written), or "kātab" (writer) depending on diacritics that may be absent.

**Book Cover Typography:**
Book covers use decorative fonts where diacritics may be stylized or absent. Diacritics positioned above or below letters can interfere with OCR character detection.

### Numeral Systems

**Arabic-Indic vs. Western Numerals:**
Arabic text may contain Arabic-Indic numerals (٠١٢٣٤٥٦٧٨٩) alongside Western numerals (0123456789). ISBN numbers on Arabic books may use either system, requiring dual numeral recognition and conversion.

### Character Ambiguities

Several critical character pairs are commonly confused:

| Character Pair | Issue |
|----------------|-------|
| أ / إ / آ | Different forms of Alef with Hamza — visually similar, semantically distinct |
| ة / ه | Taa Marbuta vs. Heh — appear at word endings, often confused |
| ي / ى | Yeh vs. Alef Maksura — final form looks identical |
| ك / ک | Kaf vs. Keheh — depends on font/transliteration |

### Book-Cover-Specific Challenges

**Perspective Distortion:**
Book covers photographed at an angle create trapezoidal distortion, making Arabic text harder to read. Arabic's connected nature amplifies this — a perspective distortion that slightly distorts a Latin letter still allows recognition, but a distorted connection between Arabic letters breaks the entire word segmentation.

**Curved Book Covers:**
Photographing a curved book cover (especially large-format books) creates non-uniform distortion across the image. This is more problematic for Arabic because the connected script makes it harder for the OCR engine to recover the correct character sequence.

**Low Resolution:**
Book cover thumbnails or low-quality photographs may have character heights of only 10-20 pixels. At this resolution, Arabic's fine diacritical marks and subtle shape differences between letter forms become nearly invisible.

**Decorative Fonts:**
Book covers frequently use stylized, calligraphic, or decorative fonts. Arabic calligraphy has centuries of ornamental tradition (Naskh, Nastaliq, Diwani, etc.), and many decorative fonts alter the standard letter shapes significantly. This is more problematic for Arabic than Latin because Latin has fewer distinct letter shapes.

**Text Over Artwork:**
Book covers often have text overlaid on complex backgrounds — patterns, gradients, or illustrations. Arabic text, being denser (more characters per word), is more susceptible to background interference.

**Shadows and Glare:**
Photographs of book covers often contain shadows (from book spine, lighting) and glare (from glossy covers). These artifacts disproportionately affect Arabic OCR because the subtle differences between letter forms can be obscured.

**Mixed Script Complexity:**
Arabic book covers frequently mix Arabic and Latin text (Arabic title + English author + ISBN). Determining script boundaries, handling RTL/LTR direction changes, and choosing appropriate OCR strategies per region adds significant complexity.

### Comparison: Why Arabic is Harder Than French/Latin

| Aspect | Latin (English/French) | Arabic |
|--------|------------------------|--------|
| Character independence | Each letter is independent | Letters change shape (4 forms) |
| Segmentation | Natural word boundaries | Must infer word boundaries from connected text |
| Direction | Always LTR | RTL, with potential LTR elements (mixed) |
| Diacritics | Rare and non-critical | Present in some texts, affect meaning |
| Font variation | Moderate | Extreme (calligraphic tradition) |
| Numerals | Single system (0-9) | Dual systems (Arabic-Indic + Western) |
| Text density | Low (letters per word) | High (more characters per semantic unit) |

### Sources Consulted

This analysis is based on established knowledge of Arabic script properties, documented in Unicode Standard Annex #9 (Bidirectional Algorithm), and widely discussed in Arabic NLP research literature. The specific challenges for book covers are inferred from the intersection of Arabic script properties with computer vision challenges documented in document analysis research.

### Engineering Inference

For our pipeline, the most impactful challenges will be:
1. **Connected text segmentation** — EasyOCR's character-level recognition handles this, but accuracy drops on decorative fonts
2. **Mixed RTL/LTR handling** — requires careful post-processing and BiDi resolution
3. **Character normalization ambiguity** — without diacritics, meaning can be ambiguous, affecting book lookup
4. **Book-cover typography** — decorative fonts will likely produce the highest error rates
