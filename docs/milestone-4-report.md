# Milestone 4: Book Identification Pipeline

## Summary

Replaced the placeholder processing in `ProcessingWorker.process()` with a real
book identification pipeline: OCR → text normalization → book lookup →
candidate ranking → confidence scoring.

## New Files

### Models
- `models/book_metadata.py` — `BookMetadata` dataclass (title, subtitle, authors,
  ISBN-10/13, publisher, published date, language, page count, categories,
  description, cover URLs, confidence score, source, raw OCR text)
- `models/book_candidate.py` — `BookCandidate` dataclass (metadata + per-field
  similarity scores + total weighted score)
- `models/ocr_result.py` — `OCRResult` and `OCRWord` dataclasses

### Services
- `services/image_preprocessor.py` — Preprocesses book cover images:
  load, auto-rotate (EXIF), resize, grayscale, contrast enhance, denoise
- `services/ocr_service.py` — EasyOCR wrapper (lazy import). Extracts text
  and structured fields (title, author, ISBN, publisher) from images
- `services/text_normalizer.py` — Normalizes OCR text: Unicode NFKC,
  smart quote/dash fixes, broken word merging, OCR char fixes (pipe→I, etc.),
  ISBN extraction with checksum validation (ISBN-10 and ISBN-13)
- `services/book_lookup_service.py` — Google Books API (primary) and
  Open Library (fallback) book lookup by ISBN or title+author
- `services/candidate_ranker.py` — RapidFuzz-based similarity ranking of
  book candidates against OCR-extracted fields
- `services/confidence_service.py` — Weighted confidence scoring:
  ISBN (50%) + Title (25%) + Author (15%) + Publisher (5%) +
  OCR quality (5%) + Source bonus (5%)
- `services/metadata_service.py` — Orchestrates the full pipeline:
  preprocess → OCR → normalize → lookup → rank → confidence

### Tests
- `tests/unit/test_text_normalizer.py` (16 tests)
- `tests/unit/test_ocr_service.py` (13 tests)
- `tests/unit/test_image_preprocessor.py` (13 tests)
- `tests/unit/test_book_lookup_service.py` (12 tests)
- `tests/unit/test_candidate_ranker.py` (12 tests)
- `tests/unit/test_confidence_service.py` (14 tests)
- `tests/unit/test_metadata_service.py` (8 tests)
- `tests/integration/test_processing_pipeline.py` (7 scenarios, rewritten)

## Modified Files

- `models/processing_job.py` — Added `MANUAL_REVIEW="manual_review"` to
  `JobStatus` enum
- `repositories/job_repository.py` — Added `manual_review` to partial unique
  index exclusions and terminal states; added `set_manual_review()` method
- `services/job_service.py` — Added `set_manual_review()` delegating to repo
- `workers/processing_worker.py` — Replaced placeholder processing with
  `MetadataService.identify_book()` pipeline
- `workers/base_worker.py` — Updated to handle `None` return from `process()`
  (manual review case without triggering failure chain)
- `app.py` — Integrated `MetadataService` into startup with configurable
  confidence threshold and `ocr_languages`
- `core/config.py` — Added config fields: `ocr_use_gpu`,
  `ocr_confidence_threshold`, `open_library_enabled`, `open_library_timeout`,
  `confidence_threshold`
- `.env.example` — Added Milestone 4 config variables

## Pipeline Flow

```
[Book Cover Image]
        |
        v
[ImagePreprocessor] -- load, auto-rotate, resize, grayscale, contrast, denoise
        |
        v
[OCRService] -- EasyOCR extracts text + structured fields (title, author, ISBN...)
        |
        v
[TextNormalizer] -- NFKC normalization, OCR fixes, merge broken words, ISBN extraction
        |
        v
[BookLookupService] -- Google Books (primary, by ISBN then title+author)
                       Open Library (fallback)
        |
        v
[CandidateRanker] -- RapidFuzz similarity scoring for title, author, ISBN, publisher
        |
        v
[ConfidenceService] -- Weighted confidence: ISBN 50% + Title 25% + Author 15%
                       + Publisher 5% + OCR quality 5% + Source bonus 5%
        |
        v
[Decision] -- confidence >= threshold → COMPLETED
               no candidates → FAILED
               confidence < threshold → MANUAL_REVIEW
```

## Configuration (.env)

```bash
# OCR
OCR_LANGUAGE=en
OCR_USE_GPU=false
OCR_CONFIDENCE_THRESHOLD=0.5

# Book Lookup
GOOGLE_BOOKS_API_KEY=<key>
OPEN_LIBRARY_ENABLED=true
OPEN_LIBRARY_TIMEOUT=10

# Identification
CONFIDENCE_THRESHOLD=0.80
```

## Test Results

- **189 tests, all passing** (106 Milestone 3 + 83 Milestone 4)
- Quality gates: `black` (47 files clean), `ruff` (all checks pass), `mypy` (0 errors)
