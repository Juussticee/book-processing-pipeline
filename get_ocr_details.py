"""Get detailed metadata for all 14 images."""

import sqlite3

conn = sqlite3.connect("data/metadata.db")
c = conn.cursor()
c.execute("SELECT * FROM book_metadata")
cols = [d[0] for d in c.description]
rows = c.fetchall()

for r in rows:
    row_dict = dict(zip(cols, r))
    did = row_dict.get("drive_file_id", "?")
    print(f"\n=== {did} ===")
    print(f"  title: {row_dict.get('title')}")
    print(f"  subtitle: {row_dict.get('subtitle')}")
    print(f"  authors: {row_dict.get('authors')}")
    print(f"  publisher: {row_dict.get('publisher')}")
    print(f"  confidence: {row_dict.get('confidence')}")
    print(f"  status: {row_dict.get('status')}")
    print(f"  source: {row_dict.get('source')}")
    print(f"  raw_ocr: {row_dict.get('raw_ocr')}")
    print(f"  extracted_title: {row_dict.get('extracted_title')}")
    print(f"  extracted_author: {row_dict.get('extracted_author')}")
    print(f"  match_score: {row_dict.get('match_score')}")
    print(f"  candidate_sources: {row_dict.get('candidate_sources')}")
    print(f"  rejection_reason: {row_dict.get('rejection_reason')}")

# Also get failed jobs (no metadata)
conn2 = sqlite3.connect("data/jobs.db")
c2 = conn2.cursor()
c2.execute(
    "SELECT file_id, status, error_message, retry_count "
    "FROM jobs WHERE status='failed'"
)
print("\n=== FAILED JOBS (no metadata) ===")
for row in c2.fetchall():
    file_id, status, error_msg, retries = row
    print(f"  {file_id}: status={status}, error={error_msg}, retries={retries}")

conn.close()
conn2.close()
