"""
Phase 5: Check orientation correction behavior and compile full forensic report.
"""

# Read app.py log
with open("C:\\Users\\hashe\\AppData\\Local\\Temp\\app_phase3.log", "r") as f:
    log = f.read()

# Check for preprocessing/rotation messages
rotation_lines = [
    ln
    for ln in log.split("\n")
    if "rotate" in ln.lower()
    or "orient" in ln.lower()
    or "auto_rot" in ln.lower()
    or "exif" in ln.lower()
]
print("=== ROTATION/ORIENTATION RELATED LOG LINES ===")
for ln in rotation_lines:
    print(ln)

# Check preprocessing output
preprocess_lines = [
    ln for ln in log.split("\n") if "Preprocessing" in ln or "preprocess" in ln.lower()
]
print(f"\n=== PREPROCESSING LINES ({len(preprocess_lines)}) ===")
for ln in preprocess_lines[:5]:
    print(ln)

# Check the image_preprocessor.py to understand orientation handling
print("\n=== Checking image_preprocessor.py for orientation handling ===")
