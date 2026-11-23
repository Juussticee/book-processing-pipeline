#!/usr/bin/env python
"""Phase 23: Controlled Vision/OCR Model Bake-Off (historical)

Benchmarks multiple OCR/vision models on book cover images.
Isolated experiment — no production code changes.

NOTE: EasyOCR is no longer the production OCR engine.
The production pipeline uses PaddleOCR-VL (Baidu OCR API).
The PaddleOCR-VL run below is retained as historical baseline data.
"""
import os
import sys
import json
import time
import base64
import httpx
import psutil
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DOWNLOADS_DIR = PROJECT_ROOT / "data" / "downloads"
GROUND_TRUTH_FILE = PROJECT_ROOT / "data" / "clean_run_results.json"
OLLAMA_HOST = "http://127.0.0.1:11434"

# 14 unique image ID prefixes
IMAGE_IDS = [
    "10--_zsGu", "13oww9ct1", "17wexxIYK", "1a3eH0ngb", "1AvSHrA5d",
    "1btBolC4g", "1FFDzr6Fz", "1Hy-jAFIk", "1TtcfjbF5", "1iZb9J4Hp",
    "1mBXTYblg", "1nFyRFDKc", "1t0xdRgDs", "1vxPPAVED",
]


def check_ollama_model(model_name: str) -> bool:
    """Check if a model is available via Ollama."""
    try:
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        tags = r.json().get("models", [])
        return any(m["name"] == model_name for m in tags)
    except Exception:
        return False


def find_image_files() -> dict:
    """Map image ID prefix -> full path for .jpeg or .jpg."""
    result = {}
    for f in sorted(os.listdir(DOWNLOADS_DIR)):
        if f.endswith((".jpeg", ".jpg")):
            prefix = f[:9]
            if prefix not in result:
                result[prefix] = DOWNLOADS_DIR / f
    return result


def load_ground_truth() -> dict:
    with open(GROUND_TRUTH_FILE) as f:
        return json.load(f)


def get_gt_entry(image_id: str, ground_truth: dict):
    for key, entry in ground_truth.items():
        if key.startswith(image_id):
            return entry
    return None


def normalize_text(s: str) -> str:
    """Normalize for comparison: lowercase, unicode-normalize, collapse whitespace."""
    if not s:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = s.replace("|", "").replace("-", " ").replace("'", " ")
    s = s.replace("é", "e").replace("è", "e").replace("ê", "e").replace("à", "a").replace("â", "a").replace("ô", "o").replace("ç", "c")
    s = " ".join(s.split())
    return s


def is_fuzzy_match(extracted: str, expected: str, threshold: float = 0.6) -> bool:
    """Check if extracted text fuzzy-matches expected."""
    if not extracted or not expected:
        return extracted == expected
    norm_e = normalize_text(extracted)
    norm_g = normalize_text(expected)
    if norm_e == norm_g:
        return True
    tokens_e = set(norm_e.split())
    tokens_g = set(norm_g.split())
    if not tokens_e or not tokens_g:
        return norm_e == norm_g
    overlap = len(tokens_e & tokens_g)
    total = len(tokens_e | tokens_g)
    if total == 0:
        return False
    ratio = overlap / total
    return ratio >= threshold


def measure_ram() -> float:
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024 ** 3)


# === PaddleOCR-VL (OCRService) ===
def run_ocr_service_on_all():
    """Run PaddleOCR-VL (OCRService) + BookTextExtractor on all 14 images."""
    from services.ocr_service import OCRService
    from services.book_text_extractor import BookTextExtractor

    image_files = find_image_files()
    gt = load_ground_truth()
    results = []

    print("\n=== PaddleOCR-VL (OCRService) ===\n")

    ocr_service = OCRService(languages=["en", "fr"])
    extractor = BookTextExtractor()

    start = time.time()
    for img_id in IMAGE_IDS:
        if img_id not in image_files:
            print(f"  {img_id}: IMAGE NOT FOUND")
            continue
        img_path = image_files[img_id]
        t0 = time.time()
        try:
            ocr_result = ocr_service.extract_text(str(img_path))
            fields = extractor.extract(ocr_result.text)
            title = fields.get("title") or ""
            author = fields.get("author") or ""
        except Exception as e:
            title, author = "", ""
            print(f"  {img_id}: ERROR: {e}")
            continue

        elapsed = time.time() - t0
        gt_entry = get_gt_entry(img_id, gt)
        gt_title = gt_entry.get("extracted_title", "") if gt_entry else ""
        gt_author = gt_entry.get("extracted_author", "") if gt_entry else ""

        title_match = is_fuzzy_match(title, gt_title)
        author_match = is_fuzzy_match(author, gt_author)
        both_correct = title_match and author_match

        ram = measure_ram()
        print(f"  [{img_id}] title=\"{title[:40]}\" author=\"{author[:30]}\" | "
              f"T:{'YES' if title_match else 'NO'} A:{'YES' if author_match else 'NO'} | "
              f"{elapsed:.1f}s | RAM:{ram:.1f}GB")

        results.append({
            "image_id": img_id,
            "model": "PaddleOCR-VL",
            "title": title,
            "author": author,
            "title_correct": title_match,
            "author_correct": author_match,
            "both_correct": both_correct,
            "hallucination": False,
            "latency_s": elapsed,
            "ram_gb": ram,
        })

    total_time = time.time() - start
    return results, total_time


# === Moondream (via Ollama) ===
def run_moondream_on_all():
    if not check_ollama_model("moondream:latest"):
        print("\n=== Moondream ===\nMODEL NOT AVAILABLE — NOT TESTED\n")
        return [], 0

    image_files = find_image_files()
    gt = load_ground_truth()
    results = []

    print("\n=== Moondream (via Ollama) ===\n")

    start = time.time()
    for img_id in IMAGE_IDS:
        if img_id not in image_files:
            print(f"  {img_id}: IMAGE NOT FOUND")
            continue
        img_path = image_files[img_id]
        t0 = time.time()
        try:
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

            prompt = "Extract the book title and author from this cover image. Return JSON: {\"title\": \"...\", \"author\": \"...\"}."

            payload = {
                "model": "moondream:latest",
                "messages": [
                    {"role": "user", "content": prompt, "images": [img_b64]}
                ],
                "stream": False,
                "options": {"num_ctx": 4096, "num_predict": 256, "temperature": 0.1},
            }

            r = httpx.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=300)
            content = r.json()["message"]["content"]

            try:
                data = json.loads(content.strip().strip("```"))
                title = data.get("title", "") or ""
                author = data.get("author", "") or ""
            except (json.JSONDecodeError, KeyError):
                title = ""
                author = ""
        except Exception as e:
            title, author = "", ""
            print(f"  {img_id}: ERROR: {e}")
            continue

        elapsed = time.time() - t0
        gt_entry = get_gt_entry(img_id, gt)
        gt_title = gt_entry.get("extracted_title", "") if gt_entry else ""
        gt_author = gt_entry.get("extracted_author", "") if gt_entry else ""

        title_match = is_fuzzy_match(title, gt_title)
        author_match = is_fuzzy_match(author, gt_author)
        both_correct = title_match and author_match
        hallucination = bool(title) and len(title.split()) > 10

        ram = measure_ram()
        print(f"  [{img_id}] title=\"{title[:40]}\" author=\"{author[:30]}\" | "
              f"T:{'YES' if title_match else 'NO'} A:{'YES' if author_match else 'NO'} | "
              f"{elapsed:.1f}s | RAM:{ram:.1f}GB")

        results.append({
            "image_id": img_id,
            "model": "moondream:latest",
            "title": title,
            "author": author,
            "title_correct": title_match,
            "author_correct": author_match,
            "both_correct": both_correct,
            "hallucination": hallucination,
            "latency_s": elapsed,
            "ram_gb": ram,
        })

    total_time = time.time() - start
    return results, total_time


# === Qwen2.5-VL-3B (via Ollama) ===
def run_qwen_on_all():
    model_name = None
    for name in ["qwen2.5vl:3b", "qwen2.5-vl-3b", "qwen2.5-vl:3b"]:
        if check_ollama_model(name):
            model_name = name
            break

    if not model_name:
        print("\n=== Qwen2.5-VL-3B ===\nMODEL NOT AVAILABLE — NOT TESTED\n")
        return [], 0

    image_files = find_image_files()
    gt = load_ground_truth()
    results = []

    print(f"\n=== Qwen2.5-VL-3B (via Ollama, model={model_name}) ===\n")

    start = time.time()
    for img_id in IMAGE_IDS:
        if img_id not in image_files:
            print(f"  {img_id}: IMAGE NOT FOUND")
            continue
        img_path = image_files[img_id]
        t0 = time.time()
        try:
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

            prompt = "Extract the book title and author from this cover image. Return JSON: {\"title\": \"...\", \"author\": \"...\"}."

            payload = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": prompt, "images": [img_b64]}
                ],
                "stream": False,
                "options": {"num_ctx": 4096, "num_predict": 256, "temperature": 0.1},
            }

            r = httpx.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=300)
            content = r.json()["message"]["content"]

            try:
                data = json.loads(content.strip().strip("```"))
                title = data.get("title", "") or ""
                author = data.get("author", "") or ""
            except (json.JSONDecodeError, KeyError):
                title = ""
                author = ""
        except Exception as e:
            title, author = "", ""
            print(f"  {img_id}: ERROR: {e}")
            continue

        elapsed = time.time() - t0
        gt_entry = get_gt_entry(img_id, gt)
        gt_title = gt_entry.get("extracted_title", "") if gt_entry else ""
        gt_author = gt_entry.get("extracted_author", "") if gt_entry else ""

        title_match = is_fuzzy_match(title, gt_title)
        author_match = is_fuzzy_match(author, gt_author)
        both_correct = title_match and author_match
        hallucination = bool(title) and len(title.split()) > 10

        ram = measure_ram()
        print(f"  [{img_id}] title=\"{title[:40]}\" author=\"{author[:30]}\" | "
              f"T:{'YES' if title_match else 'NO'} A:{'YES' if author_match else 'NO'} | "
              f"{elapsed:.1f}s | RAM:{ram:.1f}GB")

        results.append({
            "image_id": img_id,
            "model": model_name,
            "title": title,
            "author": author,
            "title_correct": title_match,
            "author_correct": author_match,
            "both_correct": both_correct,
            "hallucination": hallucination,
            "latency_s": elapsed,
            "ram_gb": ram,
        })

    total_time = time.time() - start
    return results, total_time


# === PP-OCRv5 ===
def run_ppocrv5_on_all():
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        print("\n=== PP-OCRv5 ===\nMODEL NOT AVAILABLE — NOT TESTED (paddleocr not installed)\n")
        return [], 0

    image_files = find_image_files()
    gt = load_ground_truth()
    results = []

    print("\n=== PP-OCRv5 ===\n")

    try:
        ocr_reader = PaddleOCR(
            lang="fr",
            use_gpu=False,
            show_log=False,
            use_angle_cls=False,
            cls=False,
        )
    except Exception as e:
        print(f"  Failed to initialize PP-OCRv5: {e}\n")
        return [], 0

    from services.book_text_extractor import BookTextExtractor
    extractor = BookTextExtractor()

    start = time.time()
    for img_id in IMAGE_IDS:
        if img_id not in image_files:
            print(f"  {img_id}: IMAGE NOT FOUND")
            continue
        img_path = image_files[img_id]
        t0 = time.time()
        try:
            result = ocr_reader.ocr(str(img_path), cls=False)
            lines = []
            if result and result[0]:
                lines = [line[1][0] for line in result[0] if line[1][1] > 0.5]
            text = "\n".join(lines)
            fields = extractor.extract(text)
            title = fields.get("title") or ""
            author = fields.get("author") or ""
        except Exception as e:
            title, author = "", ""
            print(f"  {img_id}: ERROR: {e}")
            continue

        elapsed = time.time() - t0
        gt_entry = get_gt_entry(img_id, gt)
        gt_title = gt_entry.get("extracted_title", "") if gt_entry else ""
        gt_author = gt_entry.get("extracted_author", "") if gt_entry else ""

        title_match = is_fuzzy_match(title, gt_title)
        author_match = is_fuzzy_match(author, gt_author)
        both_correct = title_match and author_match

        ram = measure_ram()
        print(f"  [{img_id}] title=\"{title[:40]}\" author=\"{author[:30]}\" | "
              f"T:{'YES' if title_match else 'NO'} A:{'YES' if author_match else 'NO'} | "
              f"{elapsed:.1f}s | RAM:{ram:.1f}GB")

        results.append({
            "image_id": img_id,
            "model": "PP-OCRv5",
            "title": title,
            "author": author,
            "title_correct": title_match,
            "author_correct": author_match,
            "both_correct": both_correct,
            "hallucination": False,
            "latency_s": elapsed,
            "ram_gb": ram,
        })

    total_time = time.time() - start
    return results, total_time


def summarize(results: list, model_name: str, total_time: float):
    if not results:
        print(f"\n=== {model_name} Summary: NOT TESTED ===")
        return None

    title_correct = sum(1 for r in results if r["title_correct"])
    author_correct = sum(1 for r in results if r["author_correct"])
    both_correct = sum(1 for r in results if r["both_correct"])
    hallucinations = sum(1 for r in results if r["hallucination"])

    latencies = [r["latency_s"] for r in results]
    avg_lat = sum(latencies) / len(latencies) if latencies else 0

    print(f"\n=== {model_name} Summary ===")
    print(f"  Title accuracy: {title_correct}/{len(results)}")
    print(f"  Author accuracy: {author_correct}/{len(results)}")
    print(f"  Both correct: {both_correct}/{len(results)}")
    print(f"  Hallucinations: {hallucinations}")
    print(f"  Avg latency: {avg_lat:.1f}s")
    print(f"  Total time: {total_time:.1f}s")
    print(f"  Fastest: {min(latencies):.1f}s | Slowest: {max(latencies):.1f}s")

    return {
        "model": model_name,
        "title_correct": title_correct,
        "author_correct": author_correct,
        "both_correct": both_correct,
        "hallucinations": hallucinations,
        "avg_latency_s": avg_lat,
        "total_time_s": total_time,
        "min_latency_s": min(latencies) if latencies else 0,
        "max_latency_s": max(latencies) if latencies else 0,
        "n_images": len(results),
    }


def main():
    print("=" * 70)
    print("PHASE 23: Controlled Vision/OCR Model Bake-Off")
    print("=" * 70)
    print(f"Images: {len(IMAGE_IDS)} unique book covers")
    print(f"Available RAM: {psutil.virtual_memory().available / 1e9:.1f} GB")

    image_files = find_image_files()
    print(f"Image files found: {len(image_files)}")
    gt = load_ground_truth()
    print(f"Ground truth entries: {len(gt)}")

    print(f"\nOllama server at {OLLAMA_HOST}")
    moondream_available = check_ollama_model("moondream:latest")
    qwen_available = check_ollama_model("qwen2.5vl:3b") or check_ollama_model("qwen2.5-vl-3b")
    print(f"Moondream:latest available: {moondream_available}")
    print(f"Qwen2.5-VL-3B available: {qwen_available}")

    all_results = []
    summaries = []

    # 1. PaddleOCR-VL (OCRService)
    ocr_results, ocr_time = run_ocr_service_on_all()
    all_results.extend(ocr_results)
    summaries.append(summarize(ocr_results, "PaddleOCR-VL", ocr_time))

    # 2. Moondream (already installed)
    moon_results, moon_time = run_moondream_on_all()
    all_results.extend(moon_results)
    summaries.append(summarize(moon_results, "Moondream", moon_time))

    # 3. Qwen2.5-VL-3B (if available)
    qwen_results, qwen_time = run_qwen_on_all()
    all_results.extend(qwen_results)
    summaries.append(summarize(qwen_results, "Qwen2.5-VL-3B", qwen_time))

    # 4. PP-OCRv5 (if installed)
    ppocr_results, ppocr_time = run_ppocrv5_on_all()
    all_results.extend(ppocr_results)
    summaries.append(summarize(ppocr_results, "PP-OCRv5", ppocr_time))

    # Save all results
    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hardware": {
            "total_ram_gb": psutil.virtual_memory().total / 1e9,
            "available_ram_gb": psutil.virtual_memory().available / 1e9,
        },
        "model_summaries": [s for s in summaries if s],
        "detailed_results": all_results,
    }
    output_file = PROJECT_ROOT / "data" / "phase23_bakeoff_results.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nResults saved to: {output_file}")

    # Print final comparison table
    print("\n" + "=" * 80)
    print("FINAL COMPARISON TABLE")
    print("=" * 80)
    print(f"{'Model':<20} {'Title':>8} {'Author':>8} {'Both':>8} {'AvgLat':>8} {'Status':<10}")
    print("-" * 80)
    for s in summaries:
        if s is None:
            continue
        n = s["n_images"]
        status = "OK" if n == 14 else "PARTIAL"
        print(f"{s['model']:<20} {s['title_correct']}/{n:>5} {s['author_correct']}/{n:>5} {s['both_correct']}/{n:>5} {s['avg_latency_s']:>7.1f}s {status:<10}")
    print("=" * 80)


if __name__ == "__main__":
    main()