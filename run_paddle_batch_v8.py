"""Phase 43: Final batch processor v8 - removes v7 hyphen gate regression."""
import os
import sys
import json
import time

sys.path.insert(0, 'C:/Users/hashe/Documents/Book Auth')

with open('.env', 'r') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")

from services.paddleocr_vl_service import PaddleOCRVlService
from services.paddleocr_simple_extractor import extract_title_author
from services.ocr_service import OCRService
from models.ocr_result import OCRResult, OCRWord

paddle = PaddleOCRVlService()
easyocr = OCRService(languages=["en", "fr"], use_gpu=False)

downloads_dir = 'data/downloads'
images = sorted([
    os.path.join(downloads_dir, f) 
    for f in os.listdir(downloads_dir) 
    if f.lower().endswith(('.jpg', '.jpeg', '.png'))
])

print(f"Found {len(images)} images")

results = {}
stats = {"paddleocr_vl": 0, "easyocr_fallback": 0, "failed": 0, "manual_review": 0, "identified": 0}

# Subtitle patterns that indicate the extracted text is NOT the main title
SUBTITLE_PATTERNS = [
    "roman tout en couleurs",
    "tout en couleurs et en lambeaux",
    "traduit de",
    "traduit par",
    "avec la collaboration de",
    "postface de",
    "préface de",
]

# Genre words that should not be authors (including accented versions)
GENRE_WORDS = {
    "roman", "nouvelles", "noüvelles", "poèmes", "récit", "essai", "étude", 
    "théâtre", "poésie", "conte", "chronique", "mémoires", "biographie", 
    "autobiographie", "correspondance", "album", "guide", "manuel", 
    "dictionnaire", "anthologie", "nouvelle",
}

for i, img_path in enumerate(images):
    basename = os.path.basename(img_path)
    print(f"\n[{i+1}/{len(images)}] {basename}")
    
    engine = "none"
    ocr_result = None
    
    for attempt in range(3):
        try:
            vl_result = paddle.extract(img_path, drive_id=basename)
            title, author = extract_title_author(vl_result.layout_blocks)
            
            all_text = vl_result.raw_markdown or "\n".join(b.text for b in vl_result.layout_blocks)
            words = [OCRWord(text=b.text, confidence=b.score, bounding_box=b.bbox) for b in vl_result.layout_blocks]
            
            ocr_result = OCRResult(
                text=all_text,
                confidence=0.9 if vl_result.layout_blocks else 0.5,
                words=words,
                language="unknown",
                image_path=img_path,
            )
            
            engine = "paddleocr_vl"
            stats["paddleocr_vl"] += 1
            
            # Quality gates (v6 logic - no hyphen gate)
            
            # Gate 1: No title AND no author
            if not title and not author:
                print(f"  QUALITY: No title or author - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": None, "extracted_author": None, "quality_gate": "no_content", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 2: Missing author
            if title and not author:
                print(f"  QUALITY: No author - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": None, "quality_gate": "no_author", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 3: Missing title
            if author and not title:
                print(f"  QUALITY: No title - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": None, "extracted_author": author, "quality_gate": "no_title", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 4: Title is a subtitle (not main title)
            title_lower = title.lower()
            is_subtitle = any(pattern in title_lower for pattern in SUBTITLE_PATTERNS)
            if is_subtitle:
                print(f"  QUALITY: Title is subtitle - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "subtitle", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 5: Author is a genre word (with accent normalization)
            author_lower = author.lower()
            import unicodedata
            author_normalized = unicodedata.normalize('NFD', author_lower)
            author_normalized = ''.join(c for c in author_normalized if unicodedata.category(c) != 'Mn')
            
            if author_normalized in GENRE_WORDS or author_lower in GENRE_WORDS:
                print(f"  QUALITY: Author is genre word '{author}' - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "author_is_genre", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 6: Title too short or too long
            if len(title) < 5 or len(title) > 40:
                print(f"  QUALITY: Title length suspicious ({len(title)} chars) - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "title_length", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 7: Author too short or too long
            if len(author) < 5 or len(author) > 30:
                print(f"  QUALITY: Author length suspicious ({len(author)} chars) - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "author_length", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 8: Cyrillic in title
            if any('\u0400' <= c <= '\u04FF' for c in title):
                print(f"  QUALITY: Cyrillic in title - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "wrong_script", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 9: Title and author too similar
            title_lower_norm = title.lower().replace(" ", "")
            author_lower_norm = author.lower().replace(" ", "")
            if title_lower_norm == author_lower_norm or title_lower_norm in author_lower_norm or author_lower_norm in title_lower_norm:
                print(f"  QUALITY: Title and author too similar - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "title_author_similar", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 10: Title has only 1 word
            if len(title.split()) < 2:
                print(f"  QUALITY: Title has only {len(title.split())} word(s) - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "title_too_short", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 11: Author has no capital letters
            if not any(c.isupper() for c in author):
                print(f"  QUALITY: Author has no capitals - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "author_no_caps", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Gate 12: Author doesn't look like a person name (too many words or numbers)
            author_words = author.split()
            if len(author_words) > 4 or any(w.isdigit() for w in author_words):
                print(f"  QUALITY: Author doesn't look like person name - MANUAL_REVIEW")
                results[basename] = {"engine": "paddleocr_vl", "extracted_title": title, "extracted_author": author, "quality_gate": "author_not_name", "decision": "MANUAL_REVIEW"}
                stats["manual_review"] += 1
                break
            
            # Quality passed
            print(f"  PaddleOCR-VL: {len(vl_result.layout_blocks)} blocks")
            print(f"  Title: {title}")
            print(f"  Author: {author}")
            
            results[basename] = {"engine": engine, "extracted_title": title, "extracted_author": author, "quality_gate": "passed"}
            stats["identified"] += 1
            break
            
        except Exception as e:
            if '10010' in str(e):
                wait = 30 * (attempt + 1)
                print(f"  Queue full, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Error: {e}")
                break
    
    if basename not in results:
        print(f"  Falling back to EasyOCR...")
        try:
            ocr_result = easyocr.extract_text(img_path)
            engine = "easyocr_fallback"
            stats["easyocr_fallback"] += 1
            results[basename] = {"engine": engine, "extracted_title": "(see metadata service)", "extracted_author": "(see metadata service)"}
        except Exception as e:
            print(f"  EasyOCR failed: {e}")
            stats["failed"] += 1
            results[basename] = {"engine": "failed", "error": str(e)[:100]}

# Save
with open('paddle_batch_results_v8.json', 'w') as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print(f"\n{'='*60}")
print("BATCH COMPLETE")
print(f"{'='*60}")
print(f"PaddleOCR-VL: {stats['paddleocr_vl']}")
print(f"EasyOCR fallback: {stats['easyocr_fallback']}")
print(f"Failed: {stats['failed']}")
print(f"Manual review (quality gate): {stats['manual_review']}")
print(f"Identified: {stats['identified']}")
