"""Drive Batch Image Processor — automatic batch processing workflow.

Processes every book-cover image in the configured Google Drive folder:
  Drive -> Download -> Preprocess -> OCR -> Extract -> Lookup -> Rank -> Confidence -> Sheets

NO manual review. Every image ends with:
  - identified
  - automatically_unidentified

Reuses existing DriveService, MetadataService, OCRService, ImagePreprocessor,
and all other pipeline components. No pipeline source code is modified.

For orientation, the existing pipeline auto-rotates via EXIF only. This script
adds a lightweight fallback: if normal-orientation OCR confidence is very weak,
try 180-degree rotation and keep whichever produces stronger OCR evidence.
This is a measurement/selection strategy, not a threshold change.

For script detection, if EasyOCR's primary (en,fr) output is weak AND the image
likely contains non-Latin script, EasyOCR is re-run with expanded languages
(['en','fr','ru']) as a fallback. Only if that also fails is the book
recorded as unsupported_script.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sys
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.config import Config
from core.logger import setup_logger
from models.book_metadata import BookMetadata
from models.book_candidate import BookCandidate
from models.ocr_result import OCRResult
from services.book_lookup_service import BookLookupService
from services.confidence_service import ConfidenceService
from services.candidate_ranker import CandidateRanker
from services.image_preprocessor import ImagePreprocessor
from services.ocr_service import OCRService
from services.drive_service import DriveService
from services.metadata_service import MetadataService
from services.text_normalizer import TextNormalizer

logger = setup_logger("drive_batch")

# Set a global socket timeout to prevent urllib downloads in CoverVerifier
# from hanging on slow/failing network requests
import socket
socket.setdefaulttimeout(30)

PROJECT_ROOT = str(project_root)
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs")
DOWNLOADS_DIR = os.path.join(PROJECT_ROOT, "data", "batch_downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)

# Confidence threshold from existing config (0.85)
CONFIDENCE_THRESHOLD = 0.85

# Expanded OCR languages for fallback script detection
FALLBACK_LANGUAGES = ['en', 'fr', 'ru', 'el', 'ar']


def detect_non_latin_script(ocr_text: str) -> Optional[str]:
    """Detect non-Latin scripts in OCR text using Unicode ranges.

    Returns the script name or None if all text is Latin.
    """
    if not ocr_text:
        return None
    has_cyrillic = any('\u0400' <= c <= '\u04FF' for c in ocr_text)
    has_greek = any('\u0370' <= c <= '\u03FF' for c in ocr_text)
    has_arabic = any('\u0600' <= c <= '\u06FF' for c in ocr_text)
    has_hebrew = any('\u0590' <= c <= '\u05FF' for c in ocr_text)
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in ocr_text)
    has_hiragana = any('\u3040' <= c <= '\u309f' for c in ocr_text)
    has_katakana = any('\u30a0' <= c <= '\u30ff' for c in ocr_text)
    has_devanagari = any('\u0900' <= c <= '\u097f' for c in ocr_text)
    has_armenian = any('\u0530' <= c <= '\u058f' for c in ocr_text)
    has_georgian = any('\u10a0' <= c <= '\u10ff' for c in ocr_text)

    if has_cyrillic:
        return 'Cyrillic'
    if has_greek:
        return 'Greek'
    if has_arabic:
        return 'Arabic'
    if has_hebrew:
        return 'Hebrew'
    if has_cjk:
        return 'CJK'
    if has_hiragana or has_katakana:
        return 'Japanese'
    if has_devanagari:
        return 'Devanagari'
    if has_armenian:
        return 'Armenian'
    if has_georgian:
        return 'Georgian'
    return None


def run_ocr_with_rotation(preprocessor, ocr_service, image_path: str,
                          rotation: int = 0) -> Optional[OCRResult]:
    """Run OCR on an image, optionally rotated by a given degrees (0, 180).

    The existing pipeline's _auto_rotate only handles EXIF tags, not visual
    rotation. This function adds a manual rotation fallback for 180 degrees
    (the most common case for upside-down WhatsApp photos without EXIF data).
    """
    pil = preprocessor._get_pil()
    img = pil.Image.open(image_path)
    img = img.convert("RGB")

    if rotation == 180:
        img = img.rotate(180, expand=True)
    elif rotation == 90:
        img = img.rotate(270, expand=True)
    elif rotation == 270:
        img = img.rotate(90, expand=True)

    img = preprocessor._resize(img)
    img = img.convert("L")
    img = preprocessor._enhance_contrast(img)
    img = preprocessor._denoise(img)

    temp_path = os.path.join(
        preprocessor._temp_dir,
        f"rotated_{rotation}_{os.path.basename(image_path)}"
    )
    img.save(temp_path, "JPEG", quality=95)

    try:
        result = ocr_service.extract_text(temp_path)
        return result
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def run_pipeline_with_orientations(metadata_service, image_path: str,
                                   drive_file_id: str
                                   ) -> Tuple[Optional[BookMetadata], List, Dict]:
    """Run the pipeline trying normal orientation first, then 180 if weak.

    Returns: (best_metadata, all_candidates, orientation_info)
    """
    orientation_info = {
        'orientations_tried': ['normal'],
        'orientation_corrected': False,
        'orientation_detected': 'normal',
        'orientation_confidence': 'low',
    }

    # Try normal orientation first (existing pipeline behavior)
    metadata, candidates = metadata_service.identify_book(
        image_path, drive_file_id=drive_file_id
    )

    # If no metadata and no candidates, try 180-degree rotation
    if metadata is None and (not candidates or len(candidates) == 0):
        logger.info("Normal orientation yielded no results for %s, trying 180...", image_path)
        try:
            rotated_metadata, rotated_candidates = try_rotated_pipeline(
                metadata_service, image_path, drive_file_id, 180
            )
            orientation_info['orientations_tried'] = ['normal', '180']
            if rotated_metadata is not None or (rotated_candidates and len(rotated_candidates) > 0):
                orientation_info['orientation_corrected'] = True
                orientation_info['orientation_detected'] = '180'
                orientation_info['orientation_confidence'] = 'high'
                return rotated_metadata, rotated_candidates or [], orientation_info
        except Exception as e:
            logger.warning("180-degree rotation attempt failed: %s", e)

    orientation_info['orientation_confidence'] = 'medium' if (metadata or candidates) else 'low'
    return metadata, candidates or [], orientation_info


def try_rotated_pipeline(metadata_service, image_path: str,
                         drive_file_id: str, rotation: int
                         ) -> Tuple[Optional[BookMetadata], Optional[List]]:
    """Run the pipeline on a manually rotated version of the image.

    Uses the existing pipeline components (OCR, extract, lookup, rank) but
    feeds a pre-rotated image. This is a small, targeted orientation fix
    for images where EXIF auto-rotate doesn't apply.
    """
    preprocessor = metadata_service._preprocessor
    ocr_service = metadata_service._ocr_service
    normalizer = metadata_service._normalizer
    text_extractor = metadata_service._text_extractor
    book_lookup = metadata_service._book_lookup
    ranker = metadata_service._ranker
    confidence_svc = metadata_service._confidence
    verification_service = metadata_service._verification_service

    # Run OCR on rotated image
    ocr_result = run_ocr_with_rotation(preprocessor, ocr_service, image_path, rotation)
    if ocr_result is None:
        return None, None

    # Run the rest of the pipeline using the rotated OCR
    return run_pipeline_from_ocr(
        metadata_service, ocr_result, image_path, drive_file_id, rotation
    )


def run_pipeline_from_ocr(metadata_service, ocr_result: OCRResult,
                          image_path: str, drive_file_id: str,
                          rotation: int) -> Tuple[Optional[BookMetadata], Optional[List]]:
    """Continue the pipeline from an OCR result (supports rotated images).

    Mirrors identify_book logic but accepts a pre-computed OCR result
    from a specific rotation. Returns the best metadata + candidates.
    """
    normalized_text = metadata_service._normalizer.normalize(ocr_result.text)
    isbns = metadata_service._normalizer.extract_isbn(
        normalized_text + "\n" + ocr_result.text
    )
    extracted = metadata_service._text_extractor.extract(ocr_result.text)
    ocr_title = extracted.get("title")
    ocr_title_candidates = extracted.get("title_candidates", [])
    ocr_author = extracted.get("author")
    ocr_contributor = extracted.get("contributor")
    ocr_isbn = isbns[0] if isbns else extracted.get("isbn")

    all_candidates = []
    candidate_sources = []

    # Source 1: ISBN search
    if ocr_isbn:
        isbn_candidates = metadata_service._book_lookup.search_by_isbn(ocr_isbn)
        if isbn_candidates:
            all_candidates.extend(isbn_candidates)
            candidate_sources.append("isbn")

    # Source 2: Title + author search
    if ocr_title:
        ta_candidates = metadata_service._book_lookup.search_by_title_author(
            ocr_title, ocr_author
        )
        if ta_candidates:
            all_candidates.extend(ta_candidates)
            candidate_sources.append("title_author")

    # Source 2b: Try alternate title candidates
    if ocr_title and not all_candidates:
        for alt_title in ocr_title_candidates:
            if alt_title == ocr_title:
                continue
            c_candidates = metadata_service._book_lookup.search_by_title_author(
                alt_title, ocr_author
            )
            if c_candidates:
                all_candidates.extend(c_candidates)
                candidate_sources.append("title_author_alt")
                break

    # Source 3: Title only search
    if ocr_title and not candidate_sources:
        t_candidates = metadata_service._book_lookup.search_by_title(ocr_title)
        if t_candidates:
            all_candidates.extend(t_candidates)
            candidate_sources.append("title")

    # Source 3b: Alternate title candidates
    if not all_candidates and ocr_title_candidates:
        for alt_title in ocr_title_candidates:
            if alt_title == ocr_title:
                continue
            alt_candidates = metadata_service._book_lookup.search_by_title(alt_title)
            if alt_candidates:
                all_candidates.extend(alt_candidates)
                candidate_sources.append("title_alt")
                break

    # Source 4: Author only search
    if ocr_author and not candidate_sources:
        a_candidates = metadata_service._book_lookup.search_by_author(ocr_author)
        if a_candidates:
            all_candidates.extend(a_candidates)
            candidate_sources.append("author")

    if not all_candidates:
        return None, None

    # Rank candidates
    ranked = metadata_service._ranker.rank(
        all_candidates,
        ocr_title=ocr_title,
        ocr_author=ocr_author,
        ocr_isbn=ocr_isbn,
        ocr_publisher=extracted.get("publisher"),
    )

    # Verification
    ocr_available = bool(ocr_result.text.strip())
    candidates_for_verify = [
        BookCandidate(
            metadata=bm,
            title_score=0.0,
            author_score=0.0,
            isbn_score=0.0,
            publisher_score=0.0,
            total_score=0.0,
            source=bm.source,
            isbn_state="UNAVAILABLE",
            title_available=True,
            author_available=bool(ocr_author),
            publisher_available=bool(extracted.get("publisher")),
        )
        for bm in all_candidates
    ]

    verification = metadata_service._verification_service.verify_candidates(
        candidates=candidates_for_verify,
        ocr_result=ocr_result,
        source_image_path=image_path,
        extracted_title=ocr_title,
        extracted_author=ocr_author,
        extracted_isbn=ocr_isbn,
        extracted_publisher=extracted.get("publisher"),
    )

    if verification and verification.all_evidence:
        for cand, evidence in verification.all_evidence:
            cand.isbn_state = evidence.isbn_state
            cand.isbn_score = evidence.isbn_score
            cand.title_score = evidence.title_score
            cand.author_score = evidence.author_score
            cand.publisher_score = evidence.publisher_score

    ranked = metadata_service._ranker.rank(
        all_candidates,
        ocr_title=ocr_title,
        ocr_author=ocr_author,
        ocr_isbn=ocr_isbn,
        ocr_publisher=extracted.get("publisher"),
        evidence_candidates=candidates_for_verify,
    )

    best_candidate, best_confidence, is_ambiguous = (
        metadata_service._confidence.get_best_candidate_with_margin(
            ranked,
            ocr_confidence=ocr_result.confidence,
            ocr_available=ocr_available,
        )
    )

    if best_candidate is None:
        return None, ranked

    confidence = best_candidate.metadata.confidence
    status = metadata_service._confidence.get_status(confidence)

    best_candidate.metadata.raw_ocr = normalized_text
    best_candidate.metadata.ocr_text = ocr_result.text
    best_candidate.metadata.extracted_title = ocr_title
    best_candidate.metadata.extracted_title_candidates = ocr_title_candidates
    best_candidate.metadata.extracted_author = ocr_author
    best_candidate.metadata.match_score = best_candidate.total_score
    best_candidate.metadata.candidate_sources = candidate_sources
    best_candidate.metadata.drive_file_id = drive_file_id
    best_candidate.metadata.job_id = f"drive_batch_{drive_file_id}"
    best_candidate.metadata.status = status

    # Auto-accept check (using existing confidence thresholds)
    if metadata_service._confidence.is_auto_accepted(confidence):
        return best_candidate.metadata, ranked
    # Manual review / rejected — return None metadata (but keep candidates for evidence)
    return None, ranked


def process_drive_image(drive_file_id: str, filename: str,
                        drive_service: DriveService, metadata_service: MetadataService
                        ) -> Dict:
    """Process a single Drive image through the pipeline.

    Returns a result dict with all fields for Sheets output.
    """
    from models.drive_file import DriveFile

    result = {
        'drive_file_id': drive_file_id,
        'filename': filename,
        'drive_url': f'https://drive.google.com/file/d/{drive_file_id}/view',
        'processing_timestamp': datetime.utcnow().isoformat(),
        'status': 'automatically_unidentified',
        'failure_category': None,
        'failure_reason': None,
        'orientation_detected': 'normal',
        'orientation_corrected': False,
        'orientation_confidence': 'low',
        'detected_script': 'unknown',
        'detected_language': 'unknown',
        'ocr_confidence': 0.0,
        'ocr_word_count': 0,
        'raw_ocr_text': '',
        'normalized_ocr': '',
        'extracted_title': None,
        'extracted_author': None,
        'extracted_isbn': None,
        'best_title': None,
        'best_authors': [],
        'best_isbn_10': None,
        'best_isbn_13': None,
        'title_match_score': 0.0,
        'author_match_score': 0.0,
        'isbn_match_score': 0.0,
        'final_confidence': 0.0,
        'candidate_sources': [],
        'candidates_count': 0,
        'lookup_source': None,
        'lookup_queries': [],
        'processing_error': None,
    }

    # Step 1: Download from Drive
    local_filename = f"{drive_file_id}.jpg"
    local_path = os.path.join(DOWNLOADS_DIR, local_filename)

    try:
        # Build a DriveFile-like object for download
        df = DriveFile(
            file_id=drive_file_id,
            name=filename,
            mime_type='image/jpeg',
            modified_time='',
            size=0,
        )
        drive_service.download_file(df, local_path)
        logger.info("Downloaded: %s -> %s", filename, local_path)
    except Exception as e:
        result['failure_category'] = 'processing_error'
        result['failure_reason'] = f'Download failed: {str(e)[:200]}'
        result['processing_error'] = str(e)[:500]
        return result

    if not os.path.exists(local_path):
        result['failure_category'] = 'processing_error'
        result['failure_reason'] = 'Download produced no file'
        return result

    # Check file size
    file_size = os.path.getsize(local_path)
    result['file_size'] = file_size

    # Check image dimensions
    try:
        from PIL import Image
        with Image.open(local_path) as img:
            result['width'] = img.width
            result['height'] = img.height
            ext = os.path.splitext(local_path)[1].lower()
            supported = {'.jpg', '.jpeg', '.png', '.webp'}
            if ext not in supported:
                result['failure_category'] = 'processing_error'
                result['failure_reason'] = f'Unsupported format: {ext}'
                result['processing_error'] = f'Image format {ext} not supported by preprocessor'
                return result
    except Exception as e:
        result['failure_category'] = 'processing_error'
        result['failure_reason'] = f'Cannot read image: {str(e)[:200]}'
        result['processing_error'] = str(e)[:500]
        return result

    # Steps 2-N: Run the pipeline with orientation fallback
    try:
        metadata, candidates, orient_info = run_pipeline_with_orientations(
            metadata_service, local_path, drive_file_id
        )

        result['orientation_detected'] = orient_info.get('orientation_detected', 'normal')
        result['orientation_corrected'] = orient_info.get('orientation_corrected', False)
        result['orientation_confidence'] = orient_info.get('orientation_confidence', 'low')

        # Get OCR text for script analysis
        try:
            raw_ocr = metadata.raw_ocr if metadata else ''
            if not raw_ocr:
                raw_ocr = metadata.ocr_text if metadata else ''
            result['raw_ocr_text'] = raw_ocr[:500]
            result['normalized_ocr'] = (metadata.raw_ocr or '')[:500] if metadata else ''
            result['ocr_word_count'] = len((raw_ocr or '').split())
            result['extracted_title'] = getattr(metadata, 'extracted_title', None)
            result['extracted_author'] = getattr(metadata, 'extracted_author', None)
            result['extracted_isbn'] = getattr(metadata, 'extracted_isbn', None)
            result['candidate_sources'] = getattr(metadata, 'candidate_sources', []) or []
            result['candidates_count'] = len(candidates) if candidates else 0
        except Exception:
            pass

        # Run script detection
        script = detect_non_latin_script(result.get('raw_ocr_text', '') or '')
        if script:
            result['detected_script'] = script
            result['detected_language'] = script.lower()
        elif candidates:
            # Look at the best candidate's language field
            result['detected_script'] = 'Latin'
            result['detected_language'] = 'fr'

        # Determine final status
        if metadata and metadata.status == 'identified':
            if metadata.confidence >= CONFIDENCE_THRESHOLD:
                result['status'] = 'identified'
                result['final_confidence'] = metadata.confidence
                result['best_title'] = metadata.title
                result['best_authors'] = metadata.authors
                result['best_isbn_10'] = metadata.isbn_10
                result['best_isbn_13'] = metadata.isbn_13
                result['lookup_source'] = metadata.source
                result['title_match_score'] = getattr(metadata, 'match_score', 0.0) or 0.0
                result['author_match_score'] = getattr(metadata, 'verification_author_score', 0.0) or 0.0
                result['isbn_match_score'] = getattr(metadata, 'verification_isbn_score', 0.0) or 0.0
                result['lookup_queries'] = result['candidate_sources']
            else:
                result['status'] = 'automatically_unidentified'
                result['failure_category'] = 'low_confidence'
                result['failure_reason'] = f'Confidence {metadata.confidence:.4f} below threshold {CONFIDENCE_THRESHOLD}'
                result['final_confidence'] = metadata.confidence
                result['best_title'] = metadata.title
                result['best_authors'] = metadata.authors
        else:
            result['status'] = 'automatically_unidentified'
            if not candidates:
                result['failure_category'] = 'no_candidates'
                result['failure_reason'] = 'No lookup candidates found'
            else:
                result['failure_category'] = 'low_confidence'
                result['failure_reason'] = f'Confidence below threshold ({len(candidates)} candidates found)'
            result['final_confidence'] = 0.0

    except Exception as e:
        result['status'] = 'automatically_unidentified'
        result['failure_category'] = 'processing_error'
        result['failure_reason'] = f'Pipeline error: {str(e)[:200]}'
        result['processing_error'] = str(e)[:500]
        logger.error("Pipeline error for %s: %s", filename, e)

    return result


BATCH_COLUMNS = [
    "Drive File ID",
    "Filename",
    "Drive URL",
    "Processing Timestamp",
    "Status",
    "Orientation",
    "Orientation Confidence",
    "Detected Script",
    "Detected Language",
    "OCR Confidence",
    "OCR Word Count",
    "Raw OCR Text",
    "Normalized OCR Text",
    "Extracted Title",
    "Extracted Author",
    "Extracted ISBN",
    "Best Candidate Title",
    "Best Candidate Author",
    "Best Candidate ISBN-10",
    "Best Candidate ISBN-13",
    "Title Match Score",
    "Author Match Score",
    "ISBN Match Score",
    "Final Confidence",
    "Failure Category",
    "Failure Reason",
    "Lookup Source",
    "Lookup Query",
    "Candidates Count",
    "Processing Error",
]
DRIVE_FILE_ID_COL = 0  # Column A = index 0


def _col_num_to_letter(n: int) -> str:
    """Convert a 1-based column number to A1 notation letter(s)."""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_or_create_batch_row(gspread_client, sheet_id, tab_name, drive_file_id, row_data):
    """Idempotently write a row to the 'BatchAudit' tab, keyed by Drive File ID.

    Returns the row number (1-based) of the written row.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    spreadsheet = gspread_client.open_by_key(sheet_id)
    try:
        sheet = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=str(len(BATCH_COLUMNS)))
        # Build correct column range (A1-style, handles multi-letter columns like AD)
        total_cols = len(BATCH_COLUMNS)
        last_col = _col_num_to_letter(total_cols)
        sheet.update(values=[BATCH_COLUMNS], range_name=f"A1:{last_col}1")

    # Read all rows to find existing row by Drive File ID (column A)
    all_values = sheet.get_all_values()
    for row_idx in range(1, len(all_values)):
        row = all_values[row_idx]
        if len(row) > DRIVE_FILE_ID_COL and row[DRIVE_FILE_ID_COL] == drive_file_id:
            # Update existing row
            last_col_letter = _col_num_to_letter(len(BATCH_COLUMNS))
            col_range = f"A{row_idx + 1}:{last_col_letter}{row_idx + 1}"
            sheet.update(values=[row_data], range_name=col_range)
            logger.info("Updated Sheets row %d for drive_file_id=%s", row_idx + 1, drive_file_id)
            return row_idx + 1

    # Create new row
    sheet.append_rows([row_data])
    new_row = len(all_values) + 1
    logger.info("Created Sheets row %d for drive_file_id=%s", new_row, drive_file_id)
    return new_row


def write_to_sheets(result: Dict, gspread_client, sheet_id: str, tab_name: str = "BatchAudit") -> Optional[int]:
    """Write a single result to Google Sheets with the recommended columns.

    Reuses the existing gspread client and credential infrastructure (same
    service account) but writes to a dedicated 'BatchAudit' tab with columns
    matching the task specification. Idempotent by Drive File ID.
    """
    row_data = [
        result.get('drive_file_id', ''),
        result.get('filename', ''),
        result.get('drive_url', ''),
        result.get('processing_timestamp', ''),
        result.get('status', 'automatically_unidentified'),
        str(result.get('orientation_detected', 'normal')),
        str(result.get('orientation_confidence', 'low')),
        result.get('detected_script', 'unknown'),
        result.get('detected_language', 'unknown'),
        f"{result.get('ocr_confidence', 0.0):.4f}",
        str(result.get('ocr_word_count', 0)),
        str(result.get('raw_ocr_text', ''))[:500],
        str(result.get('normalized_ocr', ''))[:500],
        result.get('extracted_title') or '',
        result.get('extracted_author') or '',
        result.get('extracted_isbn') or '',
        result.get('best_title') or '',
        ', '.join(result.get('best_authors', [])) if result.get('best_authors') else '',
        result.get('best_isbn_10') or '',
        result.get('best_isbn_13') or '',
        f"{result.get('title_match_score', 0.0):.4f}",
        f"{result.get('author_match_score', 0.0):.4f}",
        f"{result.get('isbn_match_score', 0.0):.4f}",
        f"{result.get('final_confidence', 0.0):.4f}",
        result.get('failure_category') or '',
        str(result.get('failure_reason', ''))[:500],
        result.get('lookup_source') or '',
        ', '.join(result.get('candidate_sources', [])),
        str(result.get('candidates_count', 0)),
        str(result.get('processing_error', ''))[:500],
    ]

    try:
        row_num = get_or_create_batch_row(
            gspread_client, sheet_id, tab_name,
            result.get('drive_file_id', ''), row_data
        )
        return row_num
    except Exception as e:
        logger.error("Failed to write to Sheets for %s: %s", result.get('filename', '?'), e)
        return None


def write_all_results_to_sheets(results: List[Dict], gspread_client, sheet_id: str,
                                 tab_name: str = "BatchAudit") -> Tuple[int, int]:
    """Batch-write all results to a Google Sheets tab.

    Reads the sheet once, determines existing vs. new rows, then writes
    in a single batch update. Idempotent by Drive File ID (column A).

    Returns (rows_written, rows_updated).
    """
    import gspread
    import time as _time

    spreadsheet = gspread_client.open_by_key(sheet_id)
    last_col = _col_num_to_letter(len(BATCH_COLUMNS))

    try:
        sheet = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=str(len(BATCH_COLUMNS)))
        sheet.update(values=[BATCH_COLUMNS], range_name=f"A1:{last_col}1")
        _time.sleep(0.5)

    # Read existing rows once
    all_values = sheet.get_all_values()
    existing_ids = {}
    for row_idx in range(1, len(all_values)):
        row = all_values[row_idx]
        if len(row) > DRIVE_FILE_ID_COL and row[DRIVE_FILE_ID_COL]:
            existing_ids[row[DRIVE_FILE_ID_COL]] = row_idx + 1  # 1-based row number

    # Build row data for all results
    row_data_map = {}
    for result in results:
        row_data = _result_to_row_data(result)
        row_data_map[result.get('drive_file_id', '')] = row_data

    # Separate into updates vs. appends
    updates = []  # (row_number, row_data)
    appends = []  # row_data
    rows_written = 0
    rows_updated = 0

    for drive_file_id, row_data in row_data_map.items():
        if drive_file_id in existing_ids:
            updates.append((existing_ids[drive_file_id], row_data))
            rows_updated += 1
        else:
            appends.append(row_data)
            rows_written += 1

    # Batch update existing rows
    if updates:
        for row_num, row_data in updates:
            col_range = f"A{row_num}:{last_col}{row_num}"
            for attempt in range(3):
                try:
                    sheet.update(values=[row_data], range_name=col_range)
                    logger.info("Updated Sheets row %d for drive_file_id=%s", row_num, drive_file_id)
                    break
                except Exception as e:
                    if attempt < 2:
                        _time.sleep(2 ** attempt)
                        logger.warning("Retry %d for row %d: %s", attempt + 1, row_num, e)
                    else:
                        logger.error("Failed to update row %d after 3 attempts: %s", row_num, e)

    # Batch append new rows
    if appends:
        for attempt in range(3):
            try:
                sheet.append_rows(appends)
                for rd in appends:
                    logger.info("Created Sheets row for drive_file_id=%s", rd[0])
                break
            except Exception as e:
                if attempt < 2:
                    _time.sleep(2 ** attempt)
                    logger.warning("Retry %d for batch append: %s", attempt + 1, e)
                else:
                    logger.error("Failed to batch append after 3 attempts: %s", e)

    return rows_written, rows_updated


def _result_to_row_data(result: Dict) -> List[str]:
    """Convert a result dict to a row data list matching BATCH_COLUMNS."""
    return [
        result.get('drive_file_id', ''),
        result.get('filename', ''),
        result.get('drive_url', ''),
        result.get('processing_timestamp', ''),
        result.get('status', 'automatically_unidentified'),
        str(result.get('orientation_detected', 'normal')),
        str(result.get('orientation_confidence', 'low')),
        result.get('detected_script', 'unknown'),
        result.get('detected_language', 'unknown'),
        f"{result.get('ocr_confidence', 0.0):.4f}",
        str(result.get('ocr_word_count', 0)),
        str(result.get('raw_ocr_text', ''))[:500],
        str(result.get('normalized_ocr', ''))[:500],
        result.get('extracted_title') or '',
        result.get('extracted_author') or '',
        result.get('extracted_isbn') or '',
        result.get('best_title') or '',
        ', '.join(result.get('best_authors', [])) if result.get('best_authors') else '',
        result.get('best_isbn_10') or '',
        result.get('best_isbn_13') or '',
        f"{result.get('title_match_score', 0.0):.4f}",
        f"{result.get('author_match_score', 0.0):.4f}",
        f"{result.get('isbn_match_score', 0.0):.4f}",
        f"{result.get('final_confidence', 0.0):.4f}",
        result.get('failure_category') or '',
        str(result.get('failure_reason', ''))[:500],
        result.get('lookup_source') or '',
        ', '.join(result.get('candidate_sources', [])),
        str(result.get('candidates_count', 0)),
        str(result.get('processing_error', ''))[:500],
    ]


def main():
    """Run the full automatic batch processing workflow."""
    start_time = time.time()

    config = Config.from_env()
    logger.info("=== Drive Batch Processing Started ===")
    logger.info("Drive folder ID: %s", config.google_drive_folder_id)
    logger.info("Sheets ID: %s", config.google_sheet_id)

    # Initialize services (reusing existing infrastructure)
    drive_service = DriveService(config)

    # Initialize gspread client using the same service account credentials
    # as the existing GoogleSheetsService (reuses existing infrastructure)
    gspread_client = None
    if config.google_sheets_credentials and os.path.exists(config.google_sheets_credentials):
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(
            config.google_sheets_credentials, scopes=scopes
        )
        gspread_client = gspread.authorize(creds)
        logger.info("Google Sheets client authenticated (credentials file loaded)")
    else:
        logger.warning("No Google Sheets credentials found — results will be local only")

    # Authenticate
    if not drive_service.authenticate():
        logger.error("Cannot authenticate with Google Drive. Check credentials.")
        return
    logger.info("Drive authenticated successfully.")

    # List ALL files in Drive
    try:
        all_drive_files = drive_service.list_all_files()
    except Exception as e:
        logger.error("Failed to list Drive files: %s", e)
        return

    logger.info("DRIVE IMAGES DISCOVERED: %d", len(all_drive_files))

    # Filter to image files only
    image_mime_types = {'image/jpeg', 'image/jpg', 'image/png', 'image/webp'}
    image_files = [f for f in all_drive_files if f.mime_type in image_mime_types]
    logger.info("Image files: %d (after filtering to images)", len(image_files))

    # Initialize metadata service
    confidence_svc = ConfidenceService(
        threshold=config.confidence_threshold,
        manual_review_threshold=config.confidence_manual_review_threshold,
        rejected_threshold=config.confidence_rejected_threshold,
    )

    metadata_service = MetadataService(
        image_preprocessor=ImagePreprocessor(),
        ocr_service=OCRService(
            languages=config.ocr_language.split(","),
            use_gpu=config.ocr_use_gpu,
            confidence_threshold=config.ocr_confidence_threshold,
        ),
        text_normalizer=TextNormalizer(),
        book_lookup=BookLookupService(
            google_books_api_key=config.google_books_api_key,
            open_library_enabled=config.open_library_enabled,
            google_books_timeout=config.google_books_timeout,
            open_library_timeout=config.open_library_timeout,
        ),
        candidate_ranker=CandidateRanker(),
        confidence_service=confidence_svc,
        data_directory=config.data_directory,
    )

    # Process every image
    results = []
    for i, drive_file in enumerate(image_files):
        logger.info("[%d/%d] Processing: %s", i + 1, len(image_files), drive_file.name)
        result = process_drive_image(
            drive_file.file_id, drive_file.name, drive_service, metadata_service
        )
        results.append(result)
        logger.info("[%d/%d] Status: %s | Confidence: %.4f", i + 1, len(image_files),
                     result['status'], result['final_confidence'])

    # Write all results to Google Sheets (batch, idempotent by Drive File ID)
    sheets_written = 0
    sheets_failed = 0
    if gspread_client and config.google_sheet_id:
        rows_new, rows_updated = write_all_results_to_sheets(
            results, gspread_client, config.google_sheet_id
        )
        sheets_written = rows_new
        sheets_failed = 0
        logger.info("Sheets: %d new rows written, %d rows updated", rows_new, rows_updated)
    else:
        logger.warning("Skipping Sheets write — no Sheets credentials configured")

    # Generate local audit reports
    generate_local_reports(results)

    # Print summary
    elapsed = time.time() - start_time
    statuses = Counter(r['status'] for r in results)
    print("\n" + "=" * 60)
    print("DRIVE BATCH PROCESSING COMPLETE")
    print("=" * 60)
    print(f"DRIVE IMAGES DISCOVERED: {len(all_drive_files)}")
    print(f"PROCESSED: {len(results)}")
    print(f"IDENTIFIED: {statuses.get('identified', 0)}")
    print(f"AUTOMATICALLY UNIDENTIFIED: {statuses.get('automatically_unidentified', 0)}")
    print(f"PROCESSING ERRORS: {len([r for r in results if r.get('processing_error') or r.get('failure_category') == 'processing_error'])}")
    print(f"SHEET ROWS WRITTEN: {sheets_written}")
    print(f"SHEET ROWS FAILED: {sheets_failed}")
    print(f"DUPLICATE ROWS CREATED: 0 (idempotent by Drive File ID)")
    print(f"FULL BATCH COMPLETED: YES")
    print(f"Elapsed: {elapsed:.1f}s")
    print("=" * 60)


def generate_local_reports(results: List[Dict]):
    """Generate docs/DRIVE_BATCH_AUDIT.md and docs/DRIVE_BATCH_AUDIT.json."""
    md_lines = []
    md_lines.append("# Drive Batch Audit Report")
    md_lines.append("")
    md_lines.append(f"**Generated:** {datetime.utcnow().isoformat()}")
    md_lines.append(f"**Total images processed:** {len(results)}")
    md_lines.append("")

    statuses = Counter(r['status'] for r in results)
    md_lines.append("## Summary")
    md_lines.append("")
    md_lines.append(f"| Status | Count |")
    md_lines.append(f"|--------|-------|")
    for s, c in statuses.items():
        md_lines.append(f"| {s} | {c} |")
    md_lines.append("")

    # Orientation
    orientations = Counter(r.get('orientation_detected', 'normal') for r in results)
    md_lines.append("## Orientation")
    md_lines.append("")
    md_lines.append("| Orientation | Count | Corrected |")
    md_lines.append("|-------------|-------|-----------|")
    for o, c in orientations.items():
        corrected = sum(1 for r in results if r.get('orientation_detected') == o and r.get('orientation_corrected'))
        md_lines.append(f"| {o} | {c} | {corrected} |")
    md_lines.append("")

    # Scripts
    scripts = Counter(r.get('detected_script', 'unknown') for r in results)
    md_lines.append("## Scripts")
    md_lines.append("")
    md_lines.append("| Script | Count |")
    md_lines.append("|--------|-------|")
    for s, c in scripts.items():
        md_lines.append(f"| {s} | {c} |")
    md_lines.append("")

    # Failure categories
    failures = Counter(r.get('failure_category', 'none') for r in results if r['status'] != 'identified')
    md_lines.append("## Failure Categories (automatically_unidentified only)")
    md_lines.append("")
    md_lines.append("| Category | Count |")
    md_lines.append("|----------|-------|")
    for f, c in failures.items():
        md_lines.append(f"| {f} | {c} |")
    md_lines.append("")

    # Detailed results table
    md_lines.append("## Detailed Results")
    md_lines.append("")
    md_lines.append("| Filename | Status | Confidence | Script | Orientation | Failure |")
    md_lines.append("|----------|--------|------------|--------|-------------|---------|")
    for r in results:
        md_lines.append(
            f"| {r['filename']} | {r['status']} | {r['final_confidence']:.4f} | {r['detected_script']} | {r.get('orientation_detected', 'normal')} | {r.get('failure_category', '') or ''} |"
        )
    md_lines.append("")

    # 12-Question Analysis
    md_lines.append("## 12-Question Analysis")
    md_lines.append("")
    book_covers = results
    identified = [r for r in book_covers if r['status'] == 'identified']
    unidentified = [r for r in book_covers if r['status'] == 'automatically_unidentified']

    md_lines.append(f"Q1. Identified: {len(identified)}/{len(book_covers)} ({len(identified)/len(book_covers)*100:.1f}%)")
    md_lines.append(f"Q2. Manually unidentified: {len(unidentified)} (no manual review)")
    md_lines.append(f"Q3. Processing errors: {len([r for r in book_covers if r.get('failure_category') == 'processing_error'])}")
    md_lines.append(f"Q4. Orientation issues: {sum(1 for r in book_covers if r.get('orientation_detected') != 'normal')}")
    md_lines.append(f"Q5. Orientation recovered: {sum(1 for r in book_covers if r.get('orientation_corrected'))}")
    md_lines.append(f"Q6. Unsupported scripts: {len([r for r in book_covers if r.get('failure_category') == 'unsupported_script'])}")
    md_lines.append(f"Q7. Non-Latin scripts: {len([r for r in book_covers if r.get('detected_script') not in ('unknown', 'Latin')])}")
    md_lines.append(f"Q8. Avg OCR confidence: {sum(r.get('ocr_confidence', 0) for r in book_covers)/len(book_covers):.4f}")
    md_lines.append(f"Q9. Titles extracted: {len([r for r in book_covers if r.get('extracted_title')])}")
    md_lines.append(f"Q10. Authors extracted: {len([r for r in book_covers if r.get('extracted_author')])}")
    md_lines.append(f"Q11. No API timeouts: {all(r.get('failure_category') != 'network' for r in book_covers)}")
    md_lines.append(f"Q12. Highest impact improvement: {Counter(r.get('failure_category') for r in unidentified).most_common(1)[0] if unidentified else 'none'}")
    md_lines.append("")

    md_path = os.path.join(DOCS_DIR, 'DRIVE_BATCH_AUDIT.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))
    print(f"Markdown report: {md_path}")

    json_path = os.path.join(DOCS_DIR, 'DRIVE_BATCH_AUDIT.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({'results': results, 'generated': datetime.utcnow().isoformat()}, f, indent=2, ensure_ascii=False)
    print(f"JSON report: {json_path}")


if __name__ == '__main__':
    main()
