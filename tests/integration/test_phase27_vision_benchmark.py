"""Phase 27 — Vision-model book-cover extraction benchmark.

Tests the VisionBookCoverExtractor against the same 14 images used in
Phases 25-26. The vision model receives the raw image and must extract
title/author via visual reasoning ONLY — no prior OCR dependency.

NOTE: EasyOCR is no longer the production OCR engine.
The production pipeline uses PaddleOCR-VL (Baidu OCR API).
Historical EasyOCR baseline data from Phase 23 is retained for comparison.
Results are cached and compared against historical PaddleOCR-VL + PaddleOCR-VL-1.6 baselines.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import logging
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.vision_bookcover_extractor import (
    VisionBookCoverExtractor,
    VisionMetadata,
    compare_title,
    compare_author,
)

# Load environment
from dotenv import load_dotenv
load_dotenv(str(PROJECT_ROOT / ".env"))

# Configuration
RESULTS_CACHE_PATH = PROJECT_ROOT / "data/vision_benchmark_results.json"
GROUND_TRUTH_PATH = PROJECT_ROOT / "data/clean_run_results.json"
PADDLEOCR_CACHE_PATH = PROJECT_ROOT / "data/paddleocr_vl_results.json"
DOWNLOADS_DIR = PROJECT_ROOT / "data/downloads"


def get_image_paths() -> list[tuple[str, str]]:
    """Return (drive_id, image_path) for all 14 images."""
    # Load ground truth to get drive IDs and image paths
    with open(GROUND_TRUTH_PATH, 'r', encoding='utf-8') as f:
        ground_truth = json.load(f)

    results = []
    seen = set()
    for entry in ground_truth:
        drive_id = entry.get('drive_id', '')
        if not drive_id or drive_id in seen:
            continue
        seen.add(drive_id)

        # Find the actual image file
        img_path = None
        for ext in ['.jpeg', '.jpg', '.png']:
            candidate = DOWNLOADS_DIR / f"{drive_id}{ext}"
            if candidate.exists():
                img_path = str(candidate)
                break

        if img_path:
            results.append((drive_id, img_path))

    return results


def run_benchmark() -> dict:
    """Run vision-model extraction on all 14 images.

    Returns results dict and writes to cache.
    """
    extractor = VisionBookCoverExtractor()

    # Load ground truth
    with open(GROUND_TRUTH_PATH, 'r', encoding='utf-8') as f:
        ground_truth_data = json.load(f)

    ground_truth = {}
    for entry in ground_truth_data:
        drive_id = entry.get('drive_id', '')
        if drive_id:
            ground_truth[drive_id] = {
                "title": entry.get("match_candidate_title", ""),
                "author": entry.get("match_candidate_author", ""),
                "title_confidence": entry.get("title_confidence", 0),
            }

    # Load historical OCR baseline results (PaddleOCR-VL is current production OCR)
    historical_ocr_baseline = {}
    if (PROJECT_ROOT / "data" / "phase23_bakeoff_results.json").exists():
        with open(PROJECT_ROOT / "data" / "phase23_bakeoff_results.json", 'r', encoding='utf-8') as f:
            bakeoff = json.load(f)
            for s in bakeoff.get("model_summaries", []):
                if s.get("model") == "PaddleOCR-VL":
                    historical_ocr_baseline = {
                        "both_correct": s.get("both_correct", False),
                    }
                    break

    # Load PaddleOCR-VL-1.6 results (Phase 26)
    paddle_results = {}
    if PADDLEOCR_CACHE_PATH.exists():
        with open(PADDLEOCR_CACHE_PATH, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        paddle_bench = PROJECT_ROOT / "data/phase26_benchmark_results.json"
        if paddle_bench.exists():
            with open(paddle_bench, 'r', encoding='utf-8') as f:
                for entry in json.load(f).get("results", []):
                    drive_id = entry.get("drive_id", "")
                    if drive_id:
                        paddle_results[drive_id] = {
                            "title": entry.get("paddle_title", ""),
                            "author": entry.get("paddle_author", ""),
                            "both_correct": entry.get("both_correct", False),
                        }

    images = get_image_paths()

    # Load existing results cache
    existing_results = {}
    if RESULTS_CACHE_PATH.exists():
        with open(RESULTS_CACHE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            existing_results = {r["drive_id"]: r for r in data.get("results", [])}

    results = []
    title_correct = 0
    author_correct = 0
    both_correct = 0

    for drive_id, img_path in images:
        if drive_id in existing_results:
            # Use cached result
            cached = existing_results[drive_id]
            meta = VisionMetadata(
                title=cached.get("title"),
                author=cached.get("author"),
                publisher=cached.get("publisher"),
                other_text=cached.get("other_text", []),
                orientation=cached.get("orientation", "upright"),
                multiple_books_detected=cached.get("multiple_books_detected", False),
                target_selection_reason=cached.get("target_selection_reason", ""),
                confidence=cached.get("confidence", 0.0),
                notes=cached.get("notes", ""),
            )
            logger.info(f"Cache hit for {drive_id} — skipping vision model call")
        else:
            # Call the vision model
            logger.info(f"Calling vision model for {drive_id}")
            raise NotImplementedError(
                "Vision model call not implemented in this environment. "
                "Use vision_analyze tool to get model responses, "
                "then run with --cache-only to use cached results."
            )

        # Compare against ground truth
        gt = ground_truth.get(drive_id, {})
        expected_title = gt.get("title", "")
        expected_author = gt.get("author", "")

        title_ok = compare_title(meta.title, expected_title) if expected_title else False
        author_ok = compare_author(meta.author, expected_author) if expected_author else False

        if title_ok:
            title_correct += 1
        if author_ok:
            author_correct += 1
        if title_ok and author_ok:
            both_correct += 1

        result = {
            "drive_id": drive_id,
            "image_path": img_path,
            "title": meta.title,
            "author": meta.author,
            "publisher": meta.publisher,
            "other_text": meta.other_text,
            "orientation": meta.orientation,
            "multiple_books_detected": meta.multiple_books_detected,
            "target_selection_reason": meta.target_selection_reason,
            "confidence": meta.confidence,
            "notes": meta.notes,
            "expected_title": expected_title,
            "expected_author": expected_author,
            "title_correct": title_ok,
            "author_correct": author_ok,
            "both_correct": title_ok and author_ok,
            "historical_ocr_both_correct": historical_ocr_baseline.get("both_correct", False),
            "paddle_both_correct": paddle_results.get(drive_id, {}).get("both_correct", False),
        }
        results.append(result)

        status = "✓✓" if title_ok and author_ok else "✗✗"
        logger.info(
            f"  [{status}] {drive_id} "
            f"title={meta.title}  author={meta.author}  "
            f"orient={meta.orientation}  conf={meta.confidence}"
        )

    summary = {
        "benchmark": "Phase 27 - Vision-Model Book-Cover Extraction",
        "approach": "Direct visual analysis (no OCR pre-processing, no heuristics)",
        "n_images": len(images),
        "title_correct": title_correct,
        "author_correct": author_correct,
        "both_correct": both_correct,
        "pct_title": round(title_correct / len(images) * 100, 1) if images else 0,
        "pct_author": round(author_correct / len(images) * 100, 1) if images else 0,
        "pct_both": round(both_correct / len(images) * 100, 1) if images else 0,
        "results": results,
    }

    # Write cache
    RESULTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return summary


def print_summary(summary: dict):
    """Print benchmark summary."""
    print()
    print("=" * 70)
    print("PHASE 27 BENCHMARK SUMMARY")
    print("=" * 70)
    print(f"Model: Vision model (direct visual analysis)")
    print(f"Images: {summary['n_images']}")
    print(f"Title correct: {summary['title_correct']}/{summary['n_images']} ({summary['pct_title']}%)")
    print(f"Author correct: {summary['author_correct']}/{summary['n_images']} ({summary['pct_author']}%)")
    print(f"Both correct: {summary['both_correct']}/{summary['n_images']} ({summary['pct_both']}%)")
    print()
    print(f"\n{'Drive ID':<20} {'Title':<10} {'Author':<10} {'HistOCR':<8} {'Paddle':<8} {'Notes'}")
    print("-" * 70)
    for r in summary.get("results", []):
        drive_id = r.get("drive_id", "")[:18]
        title = r.get("title", "")[:10]
        author = r.get("author", "")[:10]
        hist = "Y" if r.get("historical_ocr_both_correct") else "N"
        paddle = "Y" if r.get("paddle_both_correct") else "N"
        notes = r.get("notes", "")
        print(f"{drive_id:<20} {title:<10} {author:<10} {hist:<8} {paddle:<8} {notes}")

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    # Check for cache-only mode (no vision model calls needed)
    cache_only = os.environ.get("VISION_CACHE_ONLY", "false").lower() == "true"

    if cache_only:
        if RESULTS_CACHE_PATH.exists():
            with open(RESULTS_CACHE_PATH, 'r', encoding='utf-8') as f:
                summary = json.load(f)
            print_summary = None  # type: ignore
            # Re-print
            print()
            print("=" * 70)
            print("PHASE 27 BENCHMARK SUMMARY (cache-only)")
            print("=" * 70)
            print(f"Images: {summary['n_images']}")
            print(f"Title correct: {summary['title_correct']}/{summary['n_images']} ({summary['pct_title']}%)")
            print(f"Author correct: {summary['author_correct']}/{summary['n_images']} ({summary['pct_author']}%)")
            print(f"Both correct: {summary['both_correct']}/{summary['n_images']} ({summary['pct_both']}%)")
        else:
            print("No cache found. Run without --cache-only to generate.")
            sys.exit(1)
    else:
        summary = run_benchmark()
        print_summary_func = None
        print()
        print("=" * 70)
        print("PHASE 27 BENCHMARK SUMMARY")
        print("=" * 70)
        print(f"Images: {summary['n_images']}")
        print(f"Title correct: {summary['title_correct']}/{summary['n_images']} ({summary['pct_title']}%)")
        print(f"Author correct: {summary['author_correct']}/{summary['n_images']} ({summary['pct_author']}%)")
        print(f"Both correct: {summary['both_correct']}/{summary['n_images']} ({summary['pct_both']}%)")

    # Also show comparison with baselines
    print("\n--- Baseline Comparison ---")
    print(f"Historical PaddleOCR-VL (Phase 23):  8/14 (57.1%) both correct")
    print(f"PaddleOCR-VL-1.6 (Phase 26):   8/14 (57.1%) both correct")
