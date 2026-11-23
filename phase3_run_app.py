"""
Phase 3: Run the ACTUAL app.py end-to-end.
No modifications, no substitutions, just execution.
"""

import os
import subprocess
import time

# Clean up old databases for a fresh run
db_files = [
    "data/app.db",
    "data/jobs.db",
    "data/metadata.db",
    "data/export.db",
    "data/cache.db",
]
for db in db_files:
    if os.path.exists(db):
        os.remove(db)
    for ext in ["-wal", "-shm"]:
        wal_path = db + ext
        if os.path.exists(wal_path):
            os.remove(wal_path)

# Also clear downloads
download_dir = "data/downloads"
if os.path.exists(download_dir):
    for filepath in os.listdir(download_dir):
        os.remove(os.path.join(download_dir, filepath))

# Clear processed
processed_dir = "data/processed"
if os.path.exists(processed_dir):
    for filepath in os.listdir(processed_dir):
        os.remove(os.path.join(processed_dir, filepath))

print("=== Databases and downloads cleaned ===")
print("=== Starting app.py execution ===")

env = os.environ.copy()
env["PYTHONPATH"] = "."
env["USE_LLM_STUB"] = ""

log_path = os.path.join(os.getcwd(), "app_phase3_run.log")
with open(log_path, "w") as log:
    proc = subprocess.Popen(
        ['"C:/Program Files/Python311/python.exe"', "app.py"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=os.getcwd(),
        shell=True,
    )

    # Wait for completion (app.py runs continuous loop)
    time.sleep(45)  # Wait for first sync cycle

    # Check if still running
    poll = proc.poll()
    if poll is None:
        print("App still running, killing after 45 seconds...")
        proc.kill()
        proc.wait()
    else:
        print(f"App exited with code: {poll}")

print("=== app.py execution complete ===")
print(f"Log file: {log_path}")
