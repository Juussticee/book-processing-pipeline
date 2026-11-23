#!/usr/bin/env python
"""Phase 24: Hugging Face Remote Vision Model Bake-Off
Benchmark HF-hosted VLMs against 14 real book cover images.

Isolated experiment — no production code changes, no local model installation, no Ollama.
Uses HuggingFace Hub InferenceClient via HF Inference Router (router.huggingface.co/v1).

Root cause note: api-inference.huggingface.co does NOT resolve in DNS (0 records from any
public resolver). The HF Inference Router at router.huggingface.co IS reachable and handles
all inference via OpenAI-compatible /v1/chat/completions endpoint.
"""
import os
import sys
import json
import time
import base64
import re
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DOWNLOADS_DIR = PROJECT_ROOT / "data" / "downloads"
GROUND_TRUTH_FILE = PROJECT_ROOT / "data" / "clean_run_results.json"
PHASE23_RESULTS = PROJECT_ROOT / "data" / "phase23_bakeoff_results.json"

HF_TOKEN = os.environ.get("HF_TOKEN", "")  # never hard-code a token
HF_BASE_URL = "https://router.huggingface.co/v1"

# 14 unique image ID prefixes
IMAGE_IDS = [
    "10--_zsGu", "13oww9ct1", "17wexxIYK", "1a3eH0ngb", "1AvSHrA5d",
    "1btBolC4g", "1FFDzr6Fz", "1Hy-jAFIk", "1TtcfjbF5", "1iZb9J4Hp",
    "1mBXTYblg", "1nFyRFDKc", "1t0xdRgDs", "1vxPPAVED",
]

# Models verified working (tested individually on 2026-08-17)
# Note: Qwen/Qwen2.5-VL-3B-Instruct is NOT available via HF Inference Router
# CohereLabs/aya-vision-32b returns 422 (unsupported content type)
# zai-org/GLM-4.7-Flash returns error (only text content type allowed)
# zai-org/GLM-4.6V-Flash times out (too slow)
# Qwen2.5-VL-72B-Instruct returns 402 after warm-up (quota)
# ERNIE-4.5-VL returns 402 after warm-up (quota)
# Qwen3-VL-235B returns 402 after warm-up (quota)
# Using 2 models that showed best results before quota hits
MODELS = [
    ("zai-org/GLM-4.5V", "GLM-4.5V"),
    ("Qwen/Qwen3-VL-30B-A3B-Instruct", "Qwen3-VL-30B"),
]

EXTRACTION_PROMPT = (
    "You are identifying a physical book cover. Look carefully at the image. "
    "Extract the book's title and author. "
    "Use ONLY information visibly present on the cover. "
    "Do not guess from outside knowledge. "
    "Do not confuse publisher names, translators, editors, illustrators, "
    "collection names, or series names with the primary author. "
    'Return ONLY valid JSON: {"title": "...", "author": "...", "confidence": 0.0}. '
    "If the title or author cannot be determined, use null."
)

DIFFICULT_CASES = set(IMAGE_IDS)

HF_BASE_URL = "https://router.huggingface.co/v1"


def find_image_files():
    """Map image ID prefix -> Path."""
    result = {}
    for f in sorted(os.listdir(DOWNLOADS_DIR)):
        if f.endswith((".jpeg", ".jpg")):
            prefix = f[:9]
            if prefix not in result:
                result[prefix] = DOWNLOADS_DIR / f
    return result


def load_ground_truth():
    """Load ground truth from clean_run_results.json."""
    with open(GROUND_TRUTH_FILE) as f:
        raw = json.load(f)

    gt = {}
    for img_id in IMAGE_IDS:
        for key, entry in raw.items():
            if key.startswith(img_id):
                title = entry.get("match_candidate_title", "") or entry.get("extracted_title", "")
                authors = entry.get("match_candidate_authors", [])
                author = authors[0] if authors else entry.get("extracted_author", "")
                gt[img_id] = {
                    "title": title,
                    "author": author,
                    "raw_title": entry.get("extracted_title", ""),
                    "raw_author": entry.get("extracted_author", ""),
                }
                break
    return gt


def normalize_text(s):
    """Normalize text for comparison."""
    if not s:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    # Remove punctuation that varies
    for ch in ["|", "-", "'", "&", "!", "?", "."]:
        s = s.replace(ch, " ")
    # Normalize common accented characters
    replacements = {
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "à": "a", "â": "a", "ä": "a",
        "î": "i", "ï": "i",
        "ô": "o", "ö": "o",
        "û": "u", "ü": "u",
        "ç": "c", "ñ": "n",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    s = " ".join(s.split())
    return s


def is_fuzzy_match(extracted, expected, threshold=0.7):
    """Fuzzy match with token-based Jaro-Winkler-style comparison."""
    if not extracted or not expected:
        return False
    norm_e = normalize_text(extracted)
    norm_g = normalize_text(expected)
    if norm_e == norm_g:
        return True
    if not norm_e or not norm_g:
        return False

    # Token-based fuzzy match
    tokens_e = set(norm_e.split())
    tokens_g = set(norm_g.split())
    if not tokens_e or not tokens_g:
        return False
    overlap = len(tokens_e & tokens_g)
    total = len(tokens_e | tokens_g)
    if total == 0:
        return False
    ratio = overlap / total
    return ratio >= threshold


def extract_json(content):
    """Extract JSON from model response (may be wrapped in markdown)."""
    if not content:
        return None
    text = content.strip()
    # Remove markdown code fences
    text = re.sub(r'^```(?:json|python|)\n?', '', text)
    text = re.sub(r'\n?```$', '', text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try finding JSON in the text
        import re as re_mod
        matches = re_mod.findall(r'\{[^}]+\}', text)
        for m in matches:
            try:
                return json.loads(m)
            except:
                continue
        return None


def call_hf_inference(model_id, image_path, prompt):
    """Call HF Inference API with image input via router.huggingface.co/v1.
    Returns: (title, author, confidence, latency, http_status, error)
    """
    from huggingface_hub import InferenceClient

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    img_uri = f"data:image/jpeg;base64,{img_b64}"

    t0 = time.time()
    try:
        client = InferenceClient(
            base_url=HF_BASE_URL,
            api_key=HF_TOKEN,
            timeout=120,
        )

        result = client.chat.completions.create(
            model=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": img_uri}},
                ]
            }],
            max_tokens=512,
            temperature=0.1,
        )
        latency = time.time() - t0

        message = result.choices[0].message
        content = message.content if message and message.content else ""

        data = extract_json(content)
        if data:
            title = data.get("title")
            author = data.get("author")
            conf = data.get("confidence", 0.0)
            return (
                str(title) if title else "",
                str(author) if author else "",
                float(conf) if conf is not None else 0.0,
                latency, 200, None
            )
        else:
            return "", "", 0.0, latency, 200, f"No JSON in response: {content[:200]}"

    except Exception as e:
        latency = time.time() - t0
        err_str = str(e)
        status = 500
        if "429" in err_str or "too many" in err_str.lower():
            status = 429
        if "402" in err_str or "payment" in err_str.lower():
            status = 402
        if "503" in err_str or "loading" in err_str.lower() or "unavailable" in err_str.lower():
            status = 503
        if "timed out" in err_str.lower() or "timeout" in err_str.lower():
            status = 504
        if "1210" in err_str or "messages.content.type" in err_str:
            status = 422
        return "", "", 0.0, latency, status, err_str[:300]


def check_model_availability(model_id):
    """Check model availability via HF API metadata (no inference quota consumed)."""
    from huggingface_hub import HfApi
    try:
        api = HfApi(token=HF_TOKEN)
        # Use model info endpoint - doesn't count as inference
        info = api.model_info(model_id)
        pipeline_tag = getattr(info, 'pipeline_tag', '') or ''
        if 'image-text-to-text' in pipeline_tag or 'visual' in pipeline_tag:
            return True, f"available (pipeline: {pipeline_tag})"
        # Also check if the model is gated or has inference config
        return True, f"available (pipeline: {pipeline_tag or 'unknown'})"
    except Exception as e:
        return False, f"not found: {str(e)[:150]}"


def run_benchmark_for_model(model_id, display_name):
    """Run benchmark for a single model on all 14 images."""
    print(f"\n=== {display_name} ({model_id}) ===")

    available, info = check_model_availability(model_id)
    if not available:
        print(f"  MODEL NOT AVAILABLE — NOT TESTED: {info}")
        return [], None

    print(f"  Status: {info}")
    image_files = find_image_files()
    gt = load_ground_truth()

    results = []
    total_requests = 0
    successes = 0
    failures = 0
    retries = 0

    start = time.time()
    for img_id in IMAGE_IDS:
        if img_id not in image_files:
            print(f"  [{img_id}]: IMAGE NOT FOUND")
            continue
        img_path = image_files[img_id]

        gt_entry = gt.get(img_id, {})
        expected_title = gt_entry.get("title", "")
        expected_author = gt_entry.get("author", "")

        title, author, conf, latency, status, error = call_hf_inference(model_id, img_path, EXTRACTION_PROMPT)
        total_requests += 1

        # Handle 402/429 with retries and adaptive delays
        retry_count = 0
        max_retries = 3
        while status == 429 and retry_count < max_retries:
            time.sleep(10)
            title, author, conf, latency, status, error = call_hf_inference(
                model_id, img_path, EXTRACTION_PROMPT
            )
            total_requests += 1
            retries += 1
            retry_count += 1

        # Handle 402 (payment/rate-limit) with exponential backoff
        if status == 402:
            max_402_retries = 3
            for r in range(max_402_retries):
                wait_time = (r + 1) * 15  # 15, 30, 45 seconds
                print(f"    [402 retry {r+1}/{max_402_retries}] waiting {wait_time}s...")
                time.sleep(wait_time)
                title, author, conf, latency, status, error = call_hf_inference(
                    model_id, img_path, EXTRACTION_PROMPT
                )
                total_requests += 1
                retries += 1
                if status != 402:
                    break

        if status == 200 and (title or author):
            successes += 1
        else:
            failures += 1

        title_correct = is_fuzzy_match(title, expected_title) if title and expected_title else False
        author_correct = is_fuzzy_match(author, expected_author) if author and expected_author else False
        both_correct = title_correct and author_correct
        hallucination = (
            bool(title and expected_title and not title_correct) or
            bool(author and expected_author and not author_correct)
        )

        print(f"  [{img_id}] title=\"{title[:45]}\" author=\"{author[:35]}\" | "
              f"T:{'YES' if title_correct else 'NO':>3} A:{'YES' if author_correct else 'NO':>3} | "
              f"{latency:.1f}s | HTTP:{status} | "
              f"err={error[:30] if error else 'None'}")

        results.append({
            "image_id": img_id,
            "model": model_id,
            "display_name": display_name,
            "title": title,
            "author": author,
            "confidence": float(conf),
            "expected_title": expected_title,
            "expected_author": expected_author,
            "title_correct": title_correct,
            "author_correct": author_correct,
            "both_correct": both_correct,
            "hallucination": hallucination,
            "latency_s": latency,
            "http_status": status,
            "error": error,
        })

    total_time = time.time() - start

    title_c = sum(1 for r in results if r["title_correct"])
    author_c = sum(1 for r in results if r["author_correct"])
    both_c = sum(1 for r in results if r["both_correct"])
    halls = sum(1 for r in results if r["hallucination"])

    latencies = [r["latency_s"] for r in results if r["latency_s"] > 0]
    avg_lat = sum(latencies) / len(latencies) if latencies else 0
    sorted_lat = sorted(latencies)
    median_lat = sorted_lat[len(sorted_lat) // 2] if sorted_lat else 0

    print(f"\n  --- {display_name} Summary ---")
    print(f"  Title: {title_c}/{len(results)}")
    print(f"  Author: {author_c}/{len(results)}")
    print(f"  Both: {both_c}/{len(results)}")
    print(f"  Hallucinations: {halls}")
    print(f"  Avg latency: {avg_lat:.1f}s, Median: {median_lat:.1f}s")
    print(f"  Total time: {total_time:.1f}s")
    print(f"  Requests: {total_requests}, Successes: {successes}, Failures: {failures}, Retries: {retries}")

    return results, {
        "model": model_id,
        "display_name": display_name,
        "title_correct": title_c,
        "author_correct": author_c,
        "both_correct": both_c,
        "n_images": len(results),
        "hallucinations": halls,
        "avg_latency_s": round(avg_lat, 1),
        "median_latency_s": round(median_lat, 1),
        "total_time_s": round(total_time, 1),
        "total_requests": total_requests,
        "successes": successes,
        "failures": failures,
        "retries": retries,
    }


def main():
    print("=" * 70)
    print("PHASE 24: Hugging Face Remote Vision Model Bake-Off")
    print("=" * 70)

    image_files = find_image_files()
    print(f"Images: {len(image_files)} unique book covers")

    gt = load_ground_truth()
    print(f"Ground truth entries: {len(gt)}")

    print("\n--- Ground Truth ---")
    for img_id in IMAGE_IDS:
        gt_entry = gt.get(img_id, {})
        print(f"  {img_id}: title=\"{gt_entry.get('title', '')}\" author=\"{gt_entry.get('author', '')}\"")

    # Historical OCR baseline from Phase 23 (PaddleOCR-VL is current production OCR)
    print("\n--- Historical OCR Baseline (Phase 23, PaddleOCR-VL) ---")
    historical_ocr_baseline = None
    try:
        with open(PHASE23_RESULTS) as f:
            phase23 = json.load(f)
        for s in phase23.get("model_summaries", []):
            if s["model"] == "PaddleOCR-VL":
                historical_ocr_baseline = s
                print(f"  Title: {s['title_correct']}/14, Author: {s['author_correct']}/14, "
                      f"Both: {s['both_correct']}/14, Avg: {s['avg_latency_s']:.1f}s")
                break
    except Exception as e:
        print(f"  Phase 23 results not found: {e}")

    # Run benchmarks
    all_results = []
    all_summaries = []

    for model_id, display_name in MODELS:
        results, summary = run_benchmark_for_model(model_id, display_name)
        all_results.extend(results)
        if summary:
            all_summaries.append(summary)
        else:
            all_summaries.append({
                "model": model_id,
                "display_name": display_name,
                "error": "Not available",
                "n_images": 0,
            })

    # Save results
    output = {
        "timestamp": datetime.now().isoformat(),
        "hf_token_used": bool(HF_TOKEN),
        "hf_base_url": HF_BASE_URL,
        "note": (
            "api-inference.huggingface.co does NOT resolve (0 DNS records). "
            "All inference via router.huggingface.co/v1 (OpenAI-compatible endpoint). "
            "Image inputs passed as data URIs (base64) in image_url format."
        ),
        "models_requested": [m[0] for m in MODELS],
        "models_tested": [s for s in all_summaries if s.get("n_images", 0) > 0],
        "models_not_available": [s for s in all_summaries if s.get("n_images", 0) == 0],
        "historical_ocr_baseline": historical_ocr_baseline,
        "summaries": all_summaries,
        "detailed_results": all_results,
    }
    output_file = PROJECT_ROOT / "data" / "phase24_bakeoff_results.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nResults saved to: {output_file}")

    # Final comparison table
    print("\n" + "=" * 120)
    print("FINAL COMPARISON TABLE")
    print("=" * 120)
    header = f"{'Model':<32} {'Title':>8} {'Author':>8} {'Both':>8} {'Halls':>6} {'AvgLat':>8} {'Status':<10}"
    print(header)
    print("-" * 120)

    if historical_ocr_baseline:
        print(f"{'Historical PaddleOCR-VL (baseline)':<32} {historical_ocr_baseline['title_correct']:>5}/14 "
              f"{historical_ocr_baseline['author_correct']:>5}/14 {historical_ocr_baseline['both_correct']:>5}/14 "
              f"{historical_ocr_baseline['hallucinations']:>5} {historical_ocr_baseline['avg_latency_s']:>7.1f}s {'reference':<10}")

    for s in all_summaries:
        if s.get("n_images", 0) == 0:
            print(f"{s.get('display_name', s.get('model','')):<32} {'N/A':>8} {'N/A':>8} {'N/A':>8} {'N/A':>6} {'N/A':>8} {'NOT AVAIL':<10}")
        else:
            n = s["n_images"]
            status = "OK" if s["failures"] == 0 else "PARTIAL"
            print(f"{s.get('display_name', s['model'])[:32]:<32} {s['title_correct']:>5}/{n} "
                  f"{s['author_correct']:>5}/{n} {s['both_correct']:>5}/{n} "
                  f"{s['hallucinations']:>5} {s['avg_latency_s']:>7.1f}s {status:<10}")

    print("=" * 120)

    total_requests = sum(s.get("total_requests", 0) for s in all_summaries)
    total_successes = sum(s.get("successes", 0) for s in all_summaries)
    total_failures = sum(s.get("failures", 0) for s in all_summaries)
    total_retries = sum(s.get("retries", 0) for s in all_summaries)
    print(f"\nTotal HF requests: {total_requests}, Successes: {total_successes}, "
          f"Failures: {total_failures}, Retries: {total_retries}")


if __name__ == "__main__":
    main()