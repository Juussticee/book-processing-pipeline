#!/usr/bin/env python
"""Phase 24: HF Remote Vision Bake-Off - Qwen3-VL-30B run
Run Qwen3-VL-30B on all 14 images with 30s delay between requests to avoid 402 quota wall.
"""
import os, sys, json, time, base64, re, unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

HF_TOKEN = os.environ.get("HF_TOKEN", "")  # never hard-code a token
HF_BASE_URL = "https://router.huggingface.co/v1"

IMAGE_IDS = [
    "10--_zsGu", "13oww9ct1", "17wexxIYK", "1a3eH0ngb", "1AvSHrA5d",
    "1btBolC4g", "1FFDzr6Fz", "1Hy-jAFIk", "1TtcfjbF5", "1iZb9J4Hp",
    "1mBXTYblg", "1nFyRFDKc", "1t0xdRgDs", "1vxPPAVED",
]

DOWNLOADS_DIR = PROJECT_ROOT / "data" / "downloads"
GROUND_TRUTH_FILE = PROJECT_ROOT / "data" / "clean_run_results.json"

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

MODEL_ID = "Qwen/Qwen3-VL-30B-A3B-Instruct"


def find_image_files():
    result = {}
    for f in sorted(os.listdir(DOWNLOADS_DIR)):
        if f.endswith((".jpeg", ".jpg")):
            prefix = f[:9]
            if prefix not in result:
                result[prefix] = DOWNLOADS_DIR / f
    return result


def load_ground_truth():
    with open(GROUND_TRUTH_FILE) as f:
        raw = json.load(f)
    gt = {}
    for img_id in IMAGE_IDS:
        for key, entry in raw.items():
            if key.startswith(img_id):
                title = entry.get("match_candidate_title", "") or entry.get("extracted_title", "")
                authors = entry.get("match_candidate_authors", [])
                author = authors[0] if authors else entry.get("extracted_author", "")
                gt[img_id] = {"title": title, "author": author}
                break
    return gt


def normalize_text(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    for ch in ["|", "-", "'", "&", "!", "?", "."]:
        s = s.replace(ch, " ")
    for k, v in {"é":"e","è":"e","ê":"e","ë":"e","à":"a","â":"a","ä":"a",
                 "î":"i","ï":"i","ô":"o","ö":"o","û":"u","ü":"u","ç":"c","ñ":"n"}.items():
        s = s.replace(k, v)
    s = " ".join(s.split())
    return s


def is_fuzzy_match(extracted, expected, threshold=0.7):
    if not extracted or not expected:
        return False
    norm_e = normalize_text(extracted)
    norm_g = normalize_text(expected)
    if norm_e == norm_g:
        return True
    if not norm_e or not norm_g:
        return False
    tokens_e = set(norm_e.split())
    tokens_g = set(norm_g.split())
    if not tokens_e or not tokens_g:
        return False
    overlap = len(tokens_e & tokens_g)
    total = len(tokens_e | tokens_g)
    return overlap / total >= threshold if total > 0 else False


def extract_json(content):
    if not content:
        return None
    text = content.strip()
    text = re.sub(r'^```(?:json|python|`)\n?', '', text)
    text = re.sub(r'\n?```$', '', text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        matches = re.findall(r'\{[^}]+\}', text)
        for m in matches:
            try:
                return json.loads(m)
            except:
                pass
        return None


def call_hf_inference(model_id, image_path):
    from huggingface_hub import InferenceClient
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    img_uri = f"data:image/jpeg;base64,{img_b64}"

    t0 = time.time()
    try:
        client = InferenceClient(base_url=HF_BASE_URL, api_key=HF_TOKEN, timeout=120)
        result = client.chat.completions.create(
            model=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACTION_PROMPT},
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
            return str(title) if title else "", str(author) if author else "", float(conf) if conf else 0.0, latency, 200, None
        else:
            return "", "", 0.0, latency, 200, f"No JSON: {content[:200]}"
    except Exception as e:
        latency = time.time() - t0
        err_str = str(e)
        status = 402 if "402" in err_str else (429 if "429" in err_str or "too many" in err_str.lower() else 500)
        return "", "", 0.0, latency, status, err_str[:300]


def main():
    print(f"=== Qwen3-VL-30B Benchmark (14 images, 30s delay between requests) ===")
    image_files = find_image_files()
    gt = load_ground_truth()

    results = []
    for i, img_id in enumerate(IMAGE_IDS):
        img_path = image_files.get(img_id)
        if not img_path:
            print(f"[{i+1}/14] {img_id}: IMAGE NOT FOUND")
            continue

        gt_entry = gt.get(img_id, {})
        expected_title = gt_entry.get("title", "")
        expected_author = gt_entry.get("author", "")

        print(f"\n[{i+1}/14] {img_id}: ", end="", flush=True)
        title, author, conf, latency, status, error = call_hf_inference(MODEL_ID, img_path)

        retry = 0
        while status == 402 and retry < 2:
            retry += 1
            print(f"402, retry {retry} after 30s...", end="", flush=True)
            time.sleep(30)
            title, author, conf, latency, status, error = call_hf_inference(MODEL_ID, img_path)

        if status == 402:
            print(f"402 (failed)")
        else:
            title_correct = is_fuzzy_match(title, expected_title) if title and expected_title else False
            author_correct = is_fuzzy_match(author, expected_author) if author and expected_author else False
            print(f"OK ({latency:.1f}s) title=\"{title[:45]}\" author=\"{author[:30]}\" T:{title_correct} A:{author_correct}")

        results.append({
            "image_id": img_id,
            "model": MODEL_ID,
            "title": title,
            "author": author,
            "confidence": conf,
            "expected_title": expected_title,
            "expected_author": expected_author,
            "title_correct": is_fuzzy_match(title, expected_title) if title and expected_title else False,
            "author_correct": is_fuzzy_match(author, expected_author) if author and expected_author else False,
            "both_correct": (is_fuzzy_match(title, expected_title) if title and expected_title else False) and
                           (is_fuzzy_match(author, expected_author) if author and expected_author else False),
            "latency_s": latency,
            "http_status": status,
            "error": error,
        })

        if i < len(IMAGE_IDS) - 1:
            print(f"  (waiting 30s before next request...)", flush=True)
            time.sleep(30)

    title_c = sum(1 for r in results if r["title_correct"])
    author_c = sum(1 for r in results if r["author_correct"])
    both_c = sum(1 for r in results if r["both_correct"])
    
    print(f"\n=== Qwen3-VL-30B Summary ===")
    print(f"  Title: {title_c}/{len(results)}")
    print(f"  Author: {author_c}/{len(results)}")
    print(f"  Both: {both_c}/{len(results)}")

    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": MODEL_ID,
        "model_display": "Qwen3-VL-30B",
        "prompt": EXTRACTION_PROMPT,
        "results": results,
        "summary": {
            "title_correct": title_c,
            "author_correct": author_c,
            "both_correct": both_c,
            "n_images": len(results),
        },
    }
    output_file = PROJECT_ROOT / "data" / "phase24_qwen3vl30b_results.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    main()