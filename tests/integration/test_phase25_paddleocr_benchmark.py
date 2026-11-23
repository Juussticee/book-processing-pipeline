"""Phase 25 - PaddleOCR-VL-1.6 Benchmark on 14 Real Book-Cover Images.

BEFORE RUNNING:
    Set PADDLEOCR_API_TOKEN in your .env or environment.

Caches results in data/paddleocr_vl_results.json to avoid repeating API calls.
Skips images that already have successful cached results.
"""
from __future__ import annotations

import json
import os
import time
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

# Images known to be rotated. The ids belong to one private collection, so the
# map is read from a local file (``data/rotated_images.json``, short id prefix ->
# rotation degrees) rather than hard-coded here. Missing file -> no pre-rotation.
ROTATED_IMAGES_FILE = DATA_DIR / "rotated_images.json"


def _load_rotated_images() -> Dict[str, int]:
    if not ROTATED_IMAGES_FILE.is_file():
        return {}
    with open(ROTATED_IMAGES_FILE, "r", encoding="utf-8") as handle:
        return {str(k): int(v) for k, v in json.load(handle).items()}


ROTATED_IMAGES = _load_rotated_images()


def load_ground_truth() -> Dict[str, Dict[str, str]]:
    """Load ground truth from clean_run_results.json, extracting match_candidate titles."""
    with open(GROUND_TRUTH_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    results = {}
    for file_id, entry in data.items():
        if entry.get("status") in ("exception",):
            continue
        drive_id = file_id[:14]  # Extract the short drive ID
        title = entry.get("match_candidate_title", "")
        author = ""
        authors = entry.get("match_candidate_authors", [])
        if authors:
            author = ", ".join(authors)
        results[drive_id] = {"title": title, "author": author}

    return results


def _short_id(filename: str) -> str:
    """Extract the short Drive ID from a filename like '13oww9ct1us7a...jpeg'."""
    # Filename is "{drive_id}.{ext}" — the short ID is the prefix before any
    # Google Drive suffix. The short IDs are the 9-18 char prefixes used
    # throughout the benchmark (e.g., "13oww9ct1", "17wexxIYK").
    # We match against known short IDs.
    name = filename.replace(".jpeg", "").replace(".jpg", "")
    return name


def get_image_paths() -> List[Tuple[str, str]]:
    """Get list of (drive_id, image_path) for all 14 images.

    Image filenames contain the full Google Drive file ID (e.g.
    '13oww9ct1us7aGGeFZVgjTAnBq8ZVIEAo.jpeg'). We map each to a short
    drive ID for consistent referencing.
    """
    gt = load_ground_truth()
    paths = []
    for f in sorted(os.listdir(str(DOWNLOADS_DIR))):
        if f.endswith((".jpeg", ".jpg")):
            full_id = f.replace(".jpeg", "").replace(".jpg", "")
            # Match against ground truth keys
            drive_id = None
            for gt_key in gt:
                if full_id == gt_key:
                    drive_id = gt_key[:14]  # Use short ID (prefix)
                    break
            if drive_id is None:
                drive_id = full_id[:14]
            # Skip .jpg duplicates when .jpeg already exists
            stem = f.rsplit(".", 1)[0]
            ext = f.rsplit(".", 1)[1]
            already = any(p[0] == drive_id for p in paths)
            if ext == "jpg" and already:
                continue
            paths.append((drive_id, str(DOWNLOADS_DIR / f)))
    return paths


def detect_orientation(image_path: str) -> int:
    """Detect image orientation (0, 90, 180, 270) via EXIF or known rotation."""
    try:
        img = Image.open(image_path)
        exif = img.getexif()
        orientation = exif.get(274, 0)
        if orientation == 1:
            return 0
        elif orientation == 3:
            return 180
        elif orientation == 6:
            return 90
        elif orientation == 8:
            return 270
    except Exception:
        pass
    return 0


def correct_orientation(image_path: str, angle: int) -> Optional[str]:
    """Rotate image locally if needed. Returns corrected image path."""
    if angle == 0:
        return image_path
    try:
        import tempfile
        img = Image.open(image_path)
        rotate_map = {90: Image.ROTATE_90, 180: Image.ROTATE_180, 270: Image.ROTATE_270}
        if angle in rotate_map:
            img = img.transpose(rotate_map[angle])
            # Use temp directory to avoid polluting downloads dir
            suffix = ".jpeg" if image_path.endswith(".jpeg") else ".jpg"
            fd, corrected_path = tempfile.mkstemp(suffix=suffix, prefix="paddleocr_rot_")
            os.close(fd)
            img.save(corrected_path, "JPEG")
            return corrected_path
    except Exception:
        pass
    return image_path


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


def fuzzy_match(predicted: str, expected: str) -> bool:
    """Fuzzy match for title/author comparison."""
    import re
    import unicodedata

    def normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"\s+", " ", s)
        s = s.replace("ʼ", "'").replace("’", "'")
        # Normalize accents: remove diacritical marks
        s = unicodedata.normalize("NFD", s)
        s = re.sub(r"[\u0300-\u036f]", "", s)
        s = re.sub(r"^(le |la |les |des |de |l'|d'|un |une |the |a |an )", "", s)
        return s

    p = normalize(predicted)
    e = normalize(expected)

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


def run_benchmark() -> Dict[str, Any]:
    """Run the PaddleOCR-VL benchmark on all 14 images."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))

    from core.logger import setup_logger
    from services.paddleocr_vl_service import PaddleOCRVlService
    from services.paddleocr_vl_extractor import PaddleOCRVlExtractor

    logger = setup_logger(__name__)

    token = os.environ.get("PADDLEOCR_API_TOKEN", "")
    if not token:
        logger.error("PADDLEOCR_API_TOKEN not set. Cannot run benchmark.")
        print("ERROR: PADDLEOCR_API_TOKEN not set. Please set it in .env or environment.")
        return {"error": "PADDLEOCR_API_TOKEN not set"}

    svc = PaddleOCRVlService(token=token)
    if not svc.is_configured():
        print("ERROR: Service not configured. Set PADDLEOCR_API_TOKEN.")
        return {"error": "Service not configured"}

    gt = load_ground_truth()
    image_paths = get_image_paths()

    cache = load_cache()
    if "paddleocr_vl" not in cache["results"]:
        cache["results"]["paddleocr_vl"] = {}
        cache["metadata"]["paddleocr_vl"] = {
            "started_at": datetime.now().isoformat(),
            "model": "PaddleOCR-VL-1.6",
            "api_endpoint": "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
        }

    results = []
    title_correct = 0
    author_correct = 0
    both_correct = 0
    total_api_latency = 0.0
    total_processing_time = 0.0
    total_requests = 0
    failed_requests = 0

    for drive_id, image_path in image_paths:
        expected = gt.get(drive_id, {"title": "", "author": ""})
        expected_title = expected["title"]
        expected_author = expected["author"]

        # Check cache
        cached = cache["results"]["paddleocr_vl"].get(drive_id)
        if cached and cached.get("status") == "success" and not cached.get("error"):
            logger.info("Cache hit for %s -- skipping API call", drive_id)
            results.append(cached)
            total_api_latency += cached.get("api_latency", 0)
            total_processing_time += cached.get("processing_time", 0)
            total_requests += cached.get("request_count", 0)
            if cached.get("title_correct"):
                title_correct += 1
            if cached.get("author_correct"):
                author_correct += 1
            if cached.get("both_correct"):
                both_correct += 1
            continue

        # Orientation detection (local, free)
        orient = detect_orientation(image_path)
        if drive_id in ROTATED_IMAGES:
            logger.info("Image %s is known-rotated, pre-rotating %d", drive_id, ROTATED_IMAGES[drive_id])
            orient = ROTATED_IMAGES[drive_id]

        corrected_path = image_path
        if orient != 0:
            corrected_path = correct_orientation(image_path, orient)

        # Call PaddleOCR-VL API
        logger.info("Processing %s (drive_id=%s) with PaddleOCR-VL", image_path, drive_id)
        t0 = time.time()
        extraction_result = svc.extract(corrected_path, drive_id=drive_id)
        api_latency = time.time() - t0

        # Get image dimensions
        try:
            img = Image.open(image_path)
            img_w, img_h = img.size
        except Exception:
            img_w, img_h = 900, 1600

        # Extract title/author from layout
        extractor = PaddleOCRVlExtractor(image_width=img_w, image_height=img_h)
        extraction = extractor.extract(
            extraction_result.layout_blocks,
            extraction_result.raw_markdown,
            image_width=img_w,
            image_height=img_h,
        )

        paddle_title = extraction.selected_title or ""
        paddle_author = extraction.selected_author or ""

        # Fuzzy match
        title_ok = fuzzy_match(paddle_title, expected_title) if expected_title else False
        author_ok = fuzzy_match(paddle_author, expected_author) if expected_author else False
        both_ok = title_ok and author_ok

        if expected_title and title_ok:
            title_correct += 1
        if expected_author and author_ok:
            author_correct += 1
        if expected_title and expected_author and both_ok:
            both_correct += 1

        total_api_latency += api_latency
        total_processing_time += extraction_result.total_processing_time
        total_requests += extraction_result.request_count
        if extraction_result.error:
            failed_requests += 1

        result_entry = {
            "drive_id": drive_id,
            "image_path": image_path,
            "status": "success" if not extraction_result.error else "error",
            "expected_title": expected_title,
            "expected_author": expected_author,
            "paddle_title": paddle_title,
            "paddle_author": paddle_author,
            "title_correct": title_ok,
            "author_correct": author_ok,
            "both_correct": both_ok,
            "orientation_detected": orient,
            "api_latency": round(api_latency, 2),
            "processing_time": round(extraction_result.total_processing_time, 2),
            "request_count": extraction_result.request_count,
            "error": extraction_result.error,
            "n_layout_blocks": len(extraction_result.layout_blocks),
            "n_title_candidates": len(extraction.title_candidates),
            "timestamp": datetime.now().isoformat(),
        }

        results.append(result_entry)
        cache["results"]["paddleocr_vl"][drive_id] = result_entry
        save_cache(cache)

        print(f"  [{drive_id}] T:{'Y' if title_ok else 'N'} A:{'Y' if author_ok else 'N'} "
              f"orient={orient} {api_latency:.1f}s blocks={len(extraction_result.layout_blocks)}")

    # Historical OCR baseline (PaddleOCR-VL is current production OCR)
    historical_ocr_baseline = {"title_correct": 8, "author_correct": 9, "both_correct": 8, "avg_latency_s": 8.4, "n_images": 14}
    if os.path.exists(EASYOCR_RESULTS_FILE):
        with open(EASYOCR_RESULTS_FILE) as f:
            eo_data = json.load(f)
        for s in eo_data.get("model_summaries", []):
            if s.get("model") == "PaddleOCR-VL":
                historical_ocr_baseline = s
                break

    n_valid = len(image_paths)
    summary = {
        "benchmark": "Phase 25 - PaddleOCR-VL-1.6",
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
        "historical_ocr_baseline": {
            "both_correct": historical_ocr_baseline["both_correct"],
            "pct_both": round(historical_ocr_baseline["both_correct"] / 14 * 100, 1),
            "avg_latency_s": historical_ocr_baseline["avg_latency_s"],
        },
    }

    cache["results"]["paddleocr_vl_summary"] = summary
    cache["metadata"]["paddleocr_vl"]["completed_at"] = datetime.now().isoformat()
    save_cache(cache)

    return summary


if __name__ == "__main__":
    summary = run_benchmark()
    print("\n" + "=" * 70)
    print("PHASE 25 BENCHMARK SUMMARY")
    print("=" * 70)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
