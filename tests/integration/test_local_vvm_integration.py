#!/usr/bin/env python3
"""Integration test: run moondream VVM on all book cover images.

Measures hardware (RAM), runs VVM inference on each image, and captures
results for comparison against ground truth from data/clean_run_results.json.

Usage:
    .venv/Scripts/python.exe tests/integration/test_local_vvm_integration.py
"""
import json
import time
import base64
import httpx
from pathlib import Path

# --- Project root ---
ROOT = Path(__file__).resolve().parents[2]

# --- Ground truth data ---
GROUND_TRUTH_PATH = ROOT / "data" / "clean_run_results.json"

# --- Images to test ---
IMAGE_DIR = ROOT / "data" / "downloads"

# --- VVM config ---
OLLAMA_HOST = "http://127.0.0.1:11434"
MODEL_NAME = "moondream:latest"

# Prompt: clean, minimal, no placeholder text or examples that model can echo
VVM_PROMPT = (
    "Analyze this book cover image. Extract the title and primary author "
    "name that appear as text on the cover.\n"
    "\n"
    "Return ONLY valid JSON with these keys:\n"
    "  title - the book title text visible on the cover\n"
    "  author - the primary author name visible on the cover\n"
    "  confidence - your visual confidence (0.0 to 1.0)\n"
    "\n"
    "If no text is visible, use empty strings and 0.0 confidence.\n"
    "Do not include any other text in your response.\n"
)


def get_ram_info():
    """Get RAM usage stats."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "total_gb": round(vm.total / (1024**3), 2),
            "available_gb": round(vm.available / (1024**3), 2),
            "used_gb": round(vm.used / (1024**3), 2),
            "percent": vm.percent,
        }
    except ImportError:
        pass
    return {"total_gb": 0, "available_gb": 0, "used_gb": 0, "percent": 0}


def check_ollama():
    """Check if Ollama server is running and model is available."""
    try:
        resp = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if resp.status_code != 200:
            print(f"  ERROR: Ollama server returned {resp.status_code}")
            return False
        models = resp.json().get("models", [])
        model_names = [m.get("name", "") for m in models]
        if MODEL_NAME in model_names:
            print(f"  OK: Model '{MODEL_NAME}' is available")
            for m in models:
                if m.get("name") == MODEL_NAME:
                    print(f"    Size: {m.get('size', 'unknown')}")
                    print(f"    Modified: {m.get('modified_at', 'unknown')}")
            return True
        else:
            print(f"  ERROR: Model '{MODEL_NAME}' not found. Available: {model_names}")
            return False
    except Exception as exc:
        print(f"  ERROR: Cannot connect to Ollama: {exc}")
        return False


def parse_vlm_response(content):
    """Parse VLM JSON response, handling moondream's repeated output.

    moondream:latest:
    1. Wraps output in JSON array [...], possibly with repeated objects
    2. Sometimes includes extra fields (age, text) that should be ignored
    3. Sometimes has duplicate keys (first wins in Python's json.loads)
    """
    cleaned = content.strip()

    # Remove markdown code fences
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Try direct parse
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return {
                "title": parsed.get("title", ""),
                "author": parsed.get("author", ""),
                "confidence": parsed.get("confidence", 0.0),
            }
        elif isinstance(parsed, list):
            # Find the dict with highest confidence, or with text
            candidates = [item for item in parsed if isinstance(item, dict)]
            if candidates:
                # Prefer with non-zero confidence
                with_conf = [f for f in candidates if f.get("confidence", 0) > 0]
                if with_conf:
                    best = max(with_conf, key=lambda x: x.get("confidence", 0))
                    return {
                        "title": best.get("title", ""),
                        "author": best.get("author", ""),
                        "confidence": best.get("confidence", 0.0),
                    }
                # Fallback: first with non-empty text
                with_text = [f for f in candidates if f.get("title") or f.get("author")]
                if with_text:
                    return {
                        "title": with_text[0].get("title", ""),
                        "author": with_text[0].get("author", ""),
                        "confidence": with_text[0].get("confidence", 0.0),
                    }
                return {
                    "title": candidates[0].get("title", ""),
                    "author": candidates[0].get("author", ""),
                    "confidence": candidates[0].get("confidence", 0.0),
                }
    except json.JSONDecodeError:
        pass

    # Find all complete JSON objects
    brace_depth = 0
    json_start = -1
    candidates = []
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if brace_depth == 0:
                json_start = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0 and json_start >= 0:
                candidate = cleaned[json_start : i + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        candidates.append(parsed)
                except json.JSONDecodeError:
                    pass
                json_start = -1

    # Handle truncated responses (moondream produces long output that Ollama truncates)
    if not candidates and json_start >= 0:
        import re
        obj_text = cleaned[json_start:]
        result = {}
        # Extract title (may be truncated)
        title_match = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)', obj_text)
        if title_match:
            result["title"] = title_match.group(1)
        # Extract author (may be truncated)
        author_match = re.search(r'"author"\s*:\s*"((?:[^"\\]|\\.)*)', obj_text)
        if author_match:
            result["author"] = author_match.group(1)
        # Extract confidence
        conf_match = re.search(r'"confidence"\s*:\s*([0-9.]+)', obj_text)
        if conf_match:
            try:
                result["confidence"] = float(conf_match.group(1))
            except ValueError:
                pass
        if result:
            candidates.append(result)

    if candidates:
        with_conf = [f for f in candidates if f.get("confidence", 0) > 0]
        if with_conf:
            best = max(with_conf, key=lambda x: x.get("confidence", 0))
            return {
                "title": best.get("title", ""),
                "author": best.get("author", ""),
                "confidence": best.get("confidence", 0.0),
            }
        with_text = [f for f in candidates if f.get("title") or f.get("author")]
        if with_text:
            return {
                "title": with_text[0].get("title", ""),
                "author": with_text[0].get("author", ""),
                "confidence": with_text[0].get("confidence", 0.0),
            }

    # Try after separator
    if "---" in cleaned:
        parts = cleaned.split("---", 1)
        if len(parts) > 1:
            cleaned = parts[1].strip()
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return {
                        "title": parsed.get("title", ""),
                        "author": parsed.get("author", ""),
                        "confidence": parsed.get("confidence", 0.0),
                    }
            except json.JSONDecodeError:
                pass

    return None


def call_vlm(image_path):
    """Call moondream model via Ollama API with an image.

    Returns dict with title, author, confidence, raw_response.
    """
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": VVM_PROMPT,
                "images": [image_b64],
            }
        ],
        "stream": False,
    }

    timeout = httpx.Timeout(300.0)
    resp = httpx.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    result = resp.json()
    content = result.get("message", {}).get("content", "")

    parsed = parse_vlm_response(content)
    if parsed is None:
        raise RuntimeError(f"JSON parse error. Raw: {content[:500]}")

    return {
        "title": str(parsed.get("title", "")).strip(),
        "author": str(parsed.get("author", "")).strip(),
        "confidence": float(parsed.get("confidence", 0.0)),
        "raw_response": content,
    }


def load_ground_truth():
    """Load ground truth from clean_run_results.json.

    Returns a dict mapping file-id-prefix -> ground truth entry.
    Each entry contains: extracted_title, extracted_author,
    match_candidate_title, match_candidate_authors, confidence.
    """
    with open(GROUND_TRUTH_PATH) as f:
        data = json.load(f)

    gt_map = {}
    for file_id, entry in data.items():
        if isinstance(entry, dict):
            key = file_id[:15] if file_id else ""
            gt_map[key] = entry
    return gt_map


def list_images():
    """List all image files in data/downloads."""
    extensions = ["*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"]
    image_files = []
    for ext in extensions:
        image_files.extend(IMAGE_DIR.glob(ext))
    return sorted(set(image_files))


def main():
    print("=" * 70)
    print("LOCAL VVM INTEGRATION TEST - moondream:latest on book covers")
    print("=" * 70)

    # --- Step 1: Check Ollama ---
    print("\n[1] Checking Ollama server and model availability...")
    if not check_ollama():
        print("\nFAILED: Ollama server or model not available.")
        return

    # --- Step 2: Load ground truth ---
    print("\n[2] Loading ground truth from clean_run_results.json...")
    gt_map = load_ground_truth()
    print(f"   Loaded {len(gt_map)} ground truth entries")

    # --- Step 3: List image files ---
    print("\n[3] Listing image files...")
    image_files = list_images()
    print(f"   Found {len(image_files)} test images")
    for img in image_files:
        print(f"   - {img.name} ({img.stat().st_size // 1024} KB)")

    # --- Step 4: Measure RAM before ---
    print("\n[4] Measuring RAM before inference (model loaded)...")
    ram_before = get_ram_info()
    print(f"   Total: {ram_before['total_gb']:.1f} GB")
    print(f"   Available: {ram_before['available_gb']:.1f} GB")
    print(f"   Used: {ram_before['used_gb']:.1f} GB ({ram_before['percent']:.1f}%)")

    # --- Step 5: Run VVM on all images ---
    print(f"\n[5] Running VVM inference (model={MODEL_NAME}) on {len(image_files)} images...")
    print(f"   Timeout per image: 300s")
    results = []
    total_start = time.time()

    for i, img_path in enumerate(sorted(image_files), 1):
        img_name = img_path.name
        img_size = img_path.stat().st_size // 1024
        print(f"\n   [{i}/{len(image_files)}] {img_name} ({img_size} KB)")

        t0 = time.time()
        try:
            result = call_vlm(str(img_path))
            elapsed = time.time() - t0
            result["image_file"] = img_name
            result["latency_s"] = round(elapsed, 2)
            print(f"     VVM: title='{result['title']}', author='{result['author']}', "
                  f"vvm_conf={result['confidence']:.2f}, time={elapsed:.1f}s")
            results.append(result)
        except Exception as exc:
            elapsed = time.time() - t0
            print(f"     ERROR ({elapsed:.1f}s): {exc}")
            results.append({
                "image_file": img_name,
                "title": None,
                "author": None,
                "confidence": 0.0,
                "error": str(exc),
                "latency_s": round(elapsed, 2),
            })

    total_elapsed = time.time() - total_start

    # --- Step 6: Measure RAM after ---
    print(f"\n[6] Measuring RAM after inference ({total_elapsed:.1f}s total)...")
    ram_after = get_ram_info()
    print(f"   Available: {ram_after['available_gb']:.1f} GB")
    print(f"   Used: {ram_after['used_gb']:.1f} GB ({ram_after['percent']:.1f}%)")
    ram_delta = ram_before["used_gb"] - ram_after["used_gb"]
    print(f"   RAM delta (model loaded): {ram_delta:+.1f} GB")

    # --- Step 7: Compare against ground truth ---
    print(f"\n[7] Comparison against ground truth:")
    print(f"   {'Image':<30} {'VVM Title':<28} {'GT Title':<28} {'Match':<8} {'VVM Conf':<10} {'GT Conf':<10}")
    print("-" * 120)

    correct = 0
    total = 0
    for result in results:
        img_name = result["image_file"]
        vvm_title = result.get("title", "") or "N/A"
        vvm_author = result.get("author", "") or "N/A"
        vvm_conf = result.get("confidence", 0.0)
        vvm_error = result.get("error", "")

        img_stem = img_name.replace(".jpeg", "").replace(".jpg", "").replace(".png", "").replace(".JPG", "").replace(".JPEG", "").replace(".PNG", "")
        img_key = img_stem[:15] if len(img_stem) > 15 else img_stem

        gt = gt_map.get(img_key)
        if gt:
            gt_title = gt.get("match_candidate_title", "") or ""
            gt_author = gt.get("match_candidate_authors", "") or ""
            gt_conf = gt.get("confidence", 0.0)
            if not gt_title:
                gt_title = gt.get("extracted_title", "") or ""
            if not gt_author:
                gt_author = gt.get("extracted_author", "") or ""
        else:
            gt_title = "N/A"
            gt_author = "N/A"
            gt_conf = 0.0

        # Check if VVM title matches ground truth
        title_match = "NO"
        if vvm_title and vvm_title != "N/A" and gt_title and gt_title != "N/A":
            vvm_lower = vvm_title.lower().strip()
            gt_lower = gt_title.lower().strip()
            if vvm_lower in gt_lower or gt_lower in vvm_lower or vvm_lower[:20] == gt_lower[:20]:
                title_match = "YES"
                correct += 1
        total += 1

        print(f"   {img_name[:28]:<30} {vvm_title[:26]:<28} {str(gt_title)[:26]:<28} {title_match:<8} {vvm_conf:<10.2f} {gt_conf:<10.4f}")
        if vvm_error:
            print(f"     ERROR: {vvm_error}")

    accuracy = correct / total if total > 0 else 0
    print(f"\n   Accuracy: {correct}/{total} = {accuracy:.0%}")

    # --- Step 8: Hardware timing summary ---
    avg_latency = sum(r.get("latency_s", 0) for r in results) / len(results) if results else 0
    max_latency = max(r.get("latency_s", 0) for r in results) if results else 0
    min_latency = min(r.get("latency_s", 0) for r in results) if results else 0

    print(f"\n[8] Performance summary:")
    print(f"   Total test time: {total_elapsed:.1f}s")
    print(f"   Average inference time: {avg_latency:.1f}s")
    print(f"   Min inference time: {min_latency:.1f}s")
    print(f"   Max inference time: {max_latency:.1f}s")
    print(f"   RAM used by model: {abs(ram_delta):.1f} GB")

    # --- Step 9: Save results ---
    output_path = ROOT / "data" / "vvm_test_results.json"
    output_data = {
        "model": MODEL_NAME,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hardware_before": ram_before,
        "hardware_after": ram_after,
        "ram_delta_gb": round(ram_delta, 2),
        "total_images": len(results),
        "total_time_s": round(total_elapsed, 1),
        "avg_latency_s": round(avg_latency, 1),
        "max_latency_s": round(max_latency, 1),
        "min_latency_s": round(min_latency, 1),
        "accuracy": accuracy,
        "correct": correct,
        "total": total,
        "results": results,
    }
    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)
    print(f"\n[9] Results saved to: {output_path}")

    # --- Final Summary ---
    print(f"\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")
    print(f"  Model: {MODEL_NAME}")
    print(f"  Images tested: {len(results)}")
    print(f"  Correct title matches: {correct}/{total} ({accuracy:.0%})")
    print(f"  Average inference time: {avg_latency:.1f}s")
    print(f"  RAM used by model: {abs(ram_delta):.1f} GB")

    if accuracy >= 0.5:
        verdict = "GO"
        reason = f"VVM achieves {accuracy:.0%} accuracy on test set (>= 50% threshold)"
    elif accuracy > 0:
        verdict = "NO-GO"
        reason = f"VVM achieves only {accuracy:.0%} accuracy on test set"
    else:
        verdict = "NO-GO"
        reason = f"VVM achieved 0% accuracy - model not extracting titles correctly"

    print(f"\n  VERDICT: {verdict}")
    print(f"  Reason: {reason}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()