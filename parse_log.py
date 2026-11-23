"""Extract app.py results from log."""

import re

with open("C:\\Users\\hashe\\AppData\\Local\\Temp\\app_phase3.log", "r") as f:
    log = f.read()

lines = log.split("\n")

results = {}
current_drive_id = None
sync_stats = []

for line in lines:
    # Track sync cycle stats
    if "Sync cycle stats:" in line:
        sync_stats.append(line)

    # Track job start
    if "Starting book identification (Drive ID:" in line:
        match = re.search(r"Drive ID: ([a-zA-Z0-9_-]+)", line)
        if match:
            current_drive_id = match.group(1)
            results[current_drive_id] = {"lines": []}
    elif current_drive_id:
        results[current_drive_id]["lines"].append(line)

    # Extract key results
    if "OCR complete:" in line and current_drive_id:
        m = re.search(r"OCR complete: (\d+) words, confidence=([0-9.]+)", line)
        if m:
            results[current_drive_id]["ocr_words"] = m.group(1)
            results[current_drive_id]["ocr_conf"] = m.group(2)
    if "Extraction complete:" in line and current_drive_id:
        pattern = (
            r"title=(.*?), author=(.*?), contributor=(.*?), "
            r"isbn=(.*?), publisher=(.*?), candidates=\[(.*?)\]"
        )
        m = re.search(pattern, line)
        if m:
            results[current_drive_id]["title"] = (
                m.group(1) if m.group(1) != "None" else None
            )
            results[current_drive_id]["author"] = (
                m.group(2) if m.group(2) != "None" else None
            )
    if "Identification complete:" in line and current_drive_id:
        m = re.search(
            r"complete: '([^']*)' \(confidence=([0-9.]+), status=(\w+)",
            line,
        )
        if m:
            results[current_drive_id]["final_title"] = m.group(1)
            results[current_drive_id]["final_conf"] = m.group(2)
            results[current_drive_id]["final_status"] = m.group(3)
    if "Title search returned" in line and current_drive_id:
        m = re.search(r"Title search returned (\d+) candidates", line)
        if m:
            results[current_drive_id]["candidates_found"] = m.group(1)
    if "No candidates found" in line and current_drive_id:
        if "final_status" not in results[current_drive_id]:
            results[current_drive_id]["final_status"] = "FAILED"
    if "Max retries" in line and current_drive_id:
        results[current_drive_id]["final_status"] = "FAILED"
    if "manual review" in line.lower() and current_drive_id:
        if "final_status" not in results[current_drive_id]:
            results[current_drive_id]["final_status"] = "MANUAL_REVIEW"

# Print sync stats
print("=== SYNC CYCLE STATS ===")
for s in sync_stats:
    print(s)
print()

# Print results
print("=== JOB RESULTS ===")
for did, r in results.items():
    print(f"\n--- {did} ---")
    print(f"  OCR: {r.get('ocr_words', '?')} words, conf={r.get('ocr_conf', '?')}")
    print(f"  Extracted title: {r.get('title', '?')}")
    print(f"  Extracted author: {r.get('author', '?')}")
    print(f"  Candidates found: {r.get('candidates_found', '?')}")
    final_title = r.get("final_title", "?")
    final_conf = r.get("final_conf", "?")
    final_status = r.get("final_status", "?")
    print(f"  Final: {final_title} (conf={final_conf}, status={final_status})")
