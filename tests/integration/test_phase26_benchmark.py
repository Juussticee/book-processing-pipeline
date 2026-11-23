"""Phase 26 — PaddleOCR-VL Book-Cover Extraction Benchmark.

Renames the Phase 25 benchmark output to avoid overwriting it.
This benchmark uses the NEW BookCoverExtractor (services/paddleocr_vl_bookcover_extractor.py)
which treats doc_title as a candidate, not ground truth, and uses spatial clustering
to identify the primary book.

Key differences from Phase 25:
  - Does NOT pre-rotate images (relies on API's built-in orientation detection + preprocessedImage)
  - Uses spatial clustering to group blocks into regions
  - Does NOT trust doc_title label as authoritative
  - Attempts title/author splitting for merged blocks
  - Reports failure categories (OCR, orientation, multiple books, ambiguity, layout, API, extraction)

Usage:
    Set PADDLEOCR_API_TOKEN env var, then:
    python -m pytest tests/integration/test_phase26_benchmark.py -s
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import json
import os
import sys
import time
import glob
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
CACHE_FILE = DATA_DIR / "paddleocr_vl_results.json"
GROUND_TRUTH_FILE = DATA_DIR / "clean_run_results.json"
EASYOCR_RESULTS_FILE = DATA_DIR / "phase23_bakeoff_results.json"
PHASE26_RESULTS_FILE = DATA_DIR / "phase26_benchmark_results.json"


def load_ground_truth() -> Dict[str, Dict[str, str]]:
    """Load ground truth from clean_run_results.json."""
    with open(GROUND_TRUTH_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    results = {}
    for file_id, entry in data.items():
        if entry.get("status") == "exception":
            continue
        # Use 14-char short ID
        drive_id = file_id[:14] if len(file_id) > 14 else file_id
        title = entry.get("match_candidate_title", "")
        author = ""
        authors = entry.get("match_candidate_authors", [])
        if authors:
            author = ", ".join(authors)
        results[drive_id] = {"title": title, "author": author}

    return results


def _normalize_for_compare(text: str) -> str:
    """Normalize text for comparison: lowercase, strip accents, collapse whitespace."""
    import re
    import unicodedata
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[\u0300-\u036f]", "", text)
    text = re.sub(r"^(le |la |les |des |de |l'|d'|un |une |the |a |an )", "", text)
    return text


def fuzzy_match(predicted: str, expected: str) -> bool:
    """Fuzzy match for title/author comparison."""
    import re
    p = _normalize_for_compare(predicted)
    e = _normalize_for_compare(expected)
    if not p or not e:
        return False
    if p == e:
        return True
    if e in p:
        return True
    p_clean = re.sub(r"[^a-z0-9 ]", "", p)
    e_clean = re.sub(r"[^a-z0-9 ]", "", e)
    if p_clean == e_clean:
        return True
    if e_clean in p_clean:
        return True
    return False


def get_image_paths() -> List[Tuple[str, str, str]]:
    """Get list of (short_drive_id, full_drive_id, image_path).

    Avoids duplicate entries for .jpg/.jpeg variants of the same image.
    """
    gt = load_ground_truth()
    seen_full_ids = set()
    paths = []
    for f in sorted(os.listdir(str(DOWNLOADS_DIR))):
        if f.endswith((".jpeg", ".jpg")):
            full_id = f.replace(".jpeg", "").replace(".jpg", "")
            if full_id in seen_full_ids:
                continue  # Skip duplicate (.jpg vs .jpeg)
            seen_full_ids.add(full_id)
            drive_id = full_id[:14]
            paths.append((drive_id, full_id, str(DOWNLOADS_DIR / f)))
    return paths


def classify_failure(
    predicted_title: str,
    predicted_author: str,
    expected_title: str,
    expected_author: str,
    blocks: List[Any],
    orientation: int,
    other_visible: List[str],
) -> str:
    """Classify WHY the extraction failed, based on the evidence.

    Returns one of: 'OCR_recognition', 'orientation', 'multiple_books',
    'title_author_ambiguity', 'layout_detection', 'extraction_logic', 'none'
    """
    title_ok = fuzzy_match(predicted_title, expected_title) if expected_title else False
    author_ok = fuzzy_match(predicted_author, expected_author) if expected_author else False

    if title_ok and author_ok:
        return "none"

    # Check if the expected text appears in ANY block (OCR recognition failure)
    all_text = " ".join(b.text for b in blocks if b.text)
    all_text_norm = _normalize_for_compare(all_text)
    expected_norm = _normalize_for_compare(expected_title) if expected_title else ""

    if expected_norm and all_text_norm != "" and expected_norm not in all_text_norm:
        # The expected title text doesn't appear anywhere in the OCR output
        # This is an OCR recognition failure
        # But also check if the text appears in other_visible_text
        other_norm = " ".join(_normalize_for_compare(t) for t in other_visible)
        if expected_norm not in other_norm:
            return "OCR_recognition"

    # Check if orientation might be wrong (upside-down text)
    if orientation != 0:
        # PaddleOCR detected non-zero orientation — if the API handled it
        # correctly, the text should be readable
        # If text is still garbled, orientation might have been applied
        # incorrectly
        if expected_norm and all_text_norm != "":
            # Check if expected text appears but in a corrupted form
            if expected_norm[:4] in all_text_norm and not title_ok:
                # Partial match suggests garbled OCR from bad rotation
                return "orientation"

    # Check for multiple books — if other_visible has title-like text,
    # the extraction may have picked the wrong book
    if other_visible:
        for other_text in other_visible:
            if _normalize_for_compare(other_text) == _normalize_for_compare(expected_title):
                return "multiple_books"

    # If title is partially correct or title+author are merged, it's ambiguity
    if expected_norm and all_text_norm != "" and expected_norm in all_text_norm:
        # Text was recognized but not extracted correctly — this is
        # extraction logic or title/author ambiguity
        if predicted_title and expected_norm in _normalize_for_compare(predicted_title):
            # Title is partially there but not exactly matched
            return "title_author_ambiguity"

    # Default: extraction logic issue
    return "extraction_logic"


def load_cache() -> Dict[str, Any]:
    """Load cached PaddleOCR-VL results."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"results": {}, "metadata": {}}


def save_cache(cache: Dict[str, Any]) -> None:
    """Save cache to disk."""
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def run_benchmark(force_refresh: bool = False) -> Dict[str, Any]:
    """Run the Phase 26 PaddleOCR-VL book-cover extraction benchmark.

    Args:
        force_refresh: If True, bypass cache and re-call the API for all images.
    """
    sys.path.insert(0, str(PROJECT_ROOT))

    from core.logger import setup_logger
    from services.paddleocr_vl_service import PaddleOCRVlService
    from services.paddleocr_vl_bookcover_extractor import BookCoverExtractor, LayoutBlock

    logger = setup_logger(__name__)

    token = os.environ.get("PADDLEOCR_API_TOKEN", "")
    svc = None
    if token:
        svc = PaddleOCRVlService(token=token)
        if not svc.is_configured():
            print("WARNING: Token set but service not properly configured. Using cache-only mode.")
            svc = None
    else:
        print("WARNING: PADDLEOCR_API_TOKEN not set. Running in cache-only mode.")
        svc = None

    gt = load_ground_truth()
    image_paths = get_image_paths()

    cache = load_cache()
    if "paddleocr_vl" not in cache.get("results", {}):
        cache["results"]["paddleocr_vl"] = {}

    results = []
    title_correct = 0
    author_correct = 0
    both_correct = 0
    total_api_latency = 0.0
    total_processing_time = 0.0
    total_requests = 0
    failed_requests = 0
    orientation_correct = 0

    # Track failure categories
    failure_categories = {
        "OCR_recognition": 0,
        "orientation": 0,
        "multiple_books": 0,
        "title_author_ambiguity": 0,
        "layout_detection": 0,
        "extraction_logic": 0,
    }

    images_with_manual_review = []

    for short_id, full_id, image_path in image_paths:
        expected = gt.get(short_id, {"title": "", "author": ""})
        expected_title = expected["title"]
        expected_author = expected["author"]

        # Get image dimensions
        try:
            img = Image.open(image_path)
            img_w, img_h = img.size
        except Exception:
            img_w, img_h = 900, 1600

        # --- Call PaddleOCR-VL API ---
        cache_key = f"phase26_{short_id}"
        cached = None
        if not force_refresh:
            cached = cache["results"]["paddleocr_vl"].get(cache_key)

        if cached and cached.get("status") == "success" and "raw_blocks" in cached:
            logger.info("Cache hit for %s -- skipping API call", short_id)
            blocks = [
                LayoutBlock(
                    text=b["text"],
                    bbox=b["bbox"],
                    label=b.get("label", "text"),
                    score=b.get("score", 1.0),
                    order=b.get("order", idx),
                )
                for idx, b in enumerate(cached["raw_blocks"])
            ]
            orientation = cached.get("orientation", 0)
            raw_markdown = cached.get("raw_markdown", "")
            api_latency = cached.get("api_latency", 0)
            processing_time = cached.get("processing_time", 0)
            request_count = cached.get("request_count", 2)
        else:
            logger.info("Processing %s with PaddleOCR-VL API", short_id)
            if svc is None:
                print(f"  ERROR: No API token for {short_id} -- cannot extract")
                result_entry = {
                    "drive_id": short_id,
                    "status": "error",
                    "error": "No API token (cache miss)",
                    "expected_title": expected_title,
                    "expected_author": expected_author,
                    "paddle_title": "",
                    "paddle_author": "",
                    "title_correct": False,
                    "author_correct": False,
                    "both_correct": False,
                }
                results.append(result_entry)
                continue

            t0 = time.time()
            extraction_result = svc.extract(image_path, drive_id=short_id)
            api_latency = time.time() - t0

            if extraction_result.error:
                logger.error("API error for %s: %s", short_id, extraction_result.error)
                failed_requests += 1
                result_entry = {
                    "drive_id": short_id,
                    "status": "error",
                    "error": extraction_result.error,
                    "expected_title": expected_title,
                    "expected_author": expected_author,
                }
                results.append(result_entry)
                cache["results"]["paddleocr_vl"][cache_key] = result_entry
                save_cache(cache)
                continue

            blocks = [LayoutBlock(
                text=b.text, bbox=b.bbox, label=b.label,
                score=b.score, order=b.order,
            ) for b in extraction_result.layout_blocks]
            orientation = extraction_result.orientation
            raw_markdown = extraction_result.raw_markdown
            api_latency = extraction_result.api_latency_s
            processing_time = extraction_result.total_processing_time
            request_count = extraction_result.request_count

            # Cache the raw blocks for reuse
            cache["results"]["paddleocr_vl"][cache_key] = {
                "drive_id": short_id,
                "image_path": image_path,
                "status": "success",
                "orientation": orientation,
                "raw_blocks": [{"text": b.text, "bbox": b.bbox, "label": b.label,
                                "score": b.score, "order": b.order} for b in blocks],
                "raw_markdown": raw_markdown,
                "api_latency": round(api_latency, 2),
                "processing_time": round(processing_time, 2),
                "request_count": request_count,
            }
            save_cache(cache)

        total_api_latency += api_latency
        total_processing_time += processing_time
        total_requests += request_count

        # --- Run BookCoverExtractor (Phase 26 approach) ---
        extractor = BookCoverExtractor(image_width=img_w, image_height=img_h)
        output = extractor.extract(
            blocks,
            raw_markdown=raw_markdown,
            image_width=img_w,
            image_height=img_h,
            orientation=orientation,
        )

        paddle_title = output.title or ""
        paddle_author = output.author or ""

        # --- Evaluate ---
        title_ok = fuzzy_match(paddle_title, expected_title) if expected_title else False
        author_ok = fuzzy_match(paddle_author, expected_author) if expected_author else False
        both_ok = title_ok and author_ok

        # Orientation: check if the API detected it correctly
        # We can verify against known rotations
        if orientation == 0:
            orientation_correct += 1  # No rotation needed

        if expected_title and title_ok:
            title_correct += 1
        if expected_author and author_ok:
            author_correct += 1
        if expected_title and expected_author and both_ok:
            both_correct += 1

        # Classify failure
        failure_reason = classify_failure(
            paddle_title, paddle_author, expected_title, expected_author,
            blocks, orientation, output.other_visible_text
        )
        if failure_reason != "none" and not both_ok:
            failure_categories[failure_reason] = failure_categories.get(failure_reason, 0) + 1

        result_entry = {
            "drive_id": short_id,
            "image_path": image_path,
            "status": "success",
            "expected_title": expected_title,
            "expected_author": expected_author,
            "paddle_title": paddle_title,
            "paddle_author": paddle_author,
            "title_correct": title_ok,
            "author_correct": author_ok,
            "both_correct": both_ok,
            "orientation_detected": orientation,
            "api_latency": round(api_latency, 2),
            "processing_time": round(processing_time, 2),
            "n_layout_blocks": len(blocks),
            "n_regions": output.debug.get("n_regions", 0),
            "multiple_books_detected": output.multiple_books_detected,
            "primary_book_reason": output.primary_book_reason,
            "publisher": output.publisher,
            "other_visible_text": output.other_visible_text,
            "failure_category": failure_reason if not both_ok else None,
            "confidence": output.confidence,
            "error": None,
            "timestamp": datetime.now().isoformat(),
        }

        # Collect images for manual review
        if not both_ok and expected_title and expected_author:
            images_with_manual_review.append(result_entry)

        results.append(result_entry)

        print(f"  [{short_id}] T:{'Y' if title_ok else 'N'} A:{'Y' if author_ok else 'N'} "
              f"orient={orientation} {api_latency:.1f}s blocks={len(blocks)} "
              f"regions={output.debug.get('n_regions', 0)} multibook={output.multiple_books_detected}"
              f"{' [' + failure_reason + ']' if failure_reason != 'none' else ''}")

    # Historical OCR baseline (PaddleOCR-VL is current production OCR)
    historical_ocr_baseline = {"both_correct": 8, "title_correct": 8, "author_correct": 9,
                               "pct_both": 57.1, "avg_latency_s": 8.38}
    if os.path.exists(EASYOCR_RESULTS_FILE):
        with open(EASYOCR_RESULTS_FILE) as f:
            eo_data = json.load(f)
        for s in eo_data.get("model_summaries", []):
            if s.get("model") == "PaddleOCR-VL":
                historical_ocr_baseline = s
                break

    summary = {
        "benchmark": "Phase 26 - PaddleOCR-VL Book-Cover Extraction",
        "model": "PaddleOCR-VL-1.6 (Baidu AI Studio)",
        "api_endpoint": "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
        "n_images": n_valid,
        "title_correct": title_correct,
        "author_correct": author_correct,
        "both_correct": both_correct,
        "pct_title": round(title_correct / n_valid * 100, 1) if n_valid else 0,
        "pct_author": round(author_correct / n_valid * 100, 1) if n_valid else 0,
        "pct_both": round(both_correct / n_valid * 100, 1) if n_valid else 0,
        "avg_api_latency_s": round(total_api_latency / n_valid, 2) if n_valid else 0,
        "total_api_latency_s": round(total_api_latency, 2),
        "total_processing_time_s": round(total_processing_time, 2),
        "total_api_requests": total_requests,
        "failed_requests": failed_requests,
        "orientation_correct": orientation_correct,
        "failure_categories": failure_categories,
        "images_with_manual_review": len(images_with_manual_review),
        "historical_ocr_baseline": {
            "title_correct": historical_ocr_baseline.get("title_correct", 8),
            "author_correct": historical_ocr_baseline.get("author_correct", 9),
            "both_correct": historical_ocr_baseline.get("both_correct", 8),
            "pct_both": historical_ocr_baseline.get("pct_both", 57.1),
            "pct_title": round(historical_ocr_baseline.get("title_correct", 8) / 14 * 100, 1),
            "pct_author": round(historical_ocr_baseline.get("author_correct", 9) / 14 * 100, 1),
            "avg_latency_s": historical_ocr_baseline.get("avg_latency_s", 8.38),
        },
        "results": results,
    }

    # Save Phase 26 results
    with open(PHASE26_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # Update cache with summary
    cache["results"]["paddleocr_vl_phase26_summary"] = {
        k: v for k, v in summary.items() if k != "results"
    }
    cache["results"]["paddleocr_vl_phase26_summary"]["results"] = results
    cache["metadata"]["paddleocr_vl_phase26"] = {
        "started_at": datetime.now().isoformat(),
        "completed_at": datetime.now().isoformat(),
        "model": "PaddleOCR-VL-1.6",
        "api_endpoint": "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
    }
    save_cache(cache)

    return summary


if __name__ == "__main__":
    summary = run_benchmark()
    print("\n" + "=" * 70)
    print("PHASE 26 BENCHMARK SUMMARY")
    print("=" * 70)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                      indent=2, ensure_ascii=False))
