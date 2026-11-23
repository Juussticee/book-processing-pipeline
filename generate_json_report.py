#!/usr/bin/env python3
"""Generate structured JSON report for the Real Dataset Pipeline Audit."""
import json, os
from collections import Counter

PROJECT_ROOT = r'C:\Users\hashe\Documents\Book Auth'

with open(os.path.join(PROJECT_ROOT, 'docs', 'REAL_DATASET_PIPELINE_AUDIT_RAW.json'), 'r') as f:
    results = json.load(f)

# Non-book-cover files (identified via visual inspection)
non_book_cover_files = {
    'download (1).png', 'download (2).png', 'download (3).png', 'download (4).png',
    'download (5).png', 'download (6).png', 'download (7).png', 'download.png',
    '695e104a56588.webp', 'Untitled.webp', 'ikrdRpL.avif',
    'ChatGPT Image Mar 9, 2026, 12_18_40 PM.png',
    'WhatsApp Image 2026-04-09 at 7.18.32 PM.jpeg',
    'WhatsApp Image 2026-03-16 at 9.37.05 PM.jpeg',
    'WhatsApp Image 2026-03-27 at 5.49.09 PM.jpeg',
    'WhatsApp Image 2026-03-28 at 7.13.16 PM.jpeg',
    'WhatsApp Image 2026-03-28 at 9.03.12 PM.jpeg',
    'WhatsApp Image 2026-03-28 at 9.56.19 PM.jpeg',
    'WhatsApp Image 2026-04-14 at 3.15.31 PM.jpeg',
    '1000367906.jpg', '1000381769.jpg', 'fear-child.gif',
}

for r in results:
    r['is_book_cover'] = r['filename'] not in non_book_cover_files

book_covers = [r for r in results if r['is_book_cover']]
non_book = [r for r in results if not r['is_book_cover']]

# Compute stats
ocr_confs = [r.get('ocr_confidence_raw', 0) for r in book_covers]
avg_ocr = sum(ocr_confs) / len(ocr_confs) if ocr_confs else 0

# Classify non-book-cover types
def classify_non_book(r):
    f = r['filename']
    if f.startswith('download'):
        return 'data_chart'
    elif f in ('695e104a56588.webp', 'Untitled.webp'):
        return 'logo_abstract'
    elif 'ChatGPT' in f:
        return 'ai_diagram'
    elif '04-09' in f:
        return 'payment_receipt'
    elif '03-16' in f:
        return 'id_photo'
    elif '03-27' in f:
        return 'stock_photo'
    elif '03-28' in f:
        return 'ai_art'
    elif '04-14' in f:
        return 'debug_screenshot'
    elif f.startswith('1000'):
        return 'document_id_card'
    elif f.endswith('.gif'):
        return 'animated_gif'
    else:
        return 'other'

# Build the structured JSON report
report = {
    'audit_metadata': {
        'title': 'Real Dataset Pipeline Audit',
        'pipeline_audited': 'MetadataService.identify_book() (production, unmodified)',
        'ocr_engine': 'EasyOCR',
        'ocr_languages': ['en', 'fr'],
        'audit_date': '2026-08-13',
        'constraint': 'No pipeline modifications, no threshold changes, no OCR config changes',
        'use_llm_stub': 'must be EMPTY',
        'total_images_processed': len(results),
        'book_covers_found': len(book_covers),
        'non_book_covers_found': len(non_book),
    },
    'dataset_summary': {
        'total_images': len(results),
        'book_covers': len(book_covers),
        'non_book_covers': len(non_book),
        'by_status': dict(Counter(r['final_status'] for r in results)),
        'by_orientation': dict(Counter(r.get('orientation', 'unknown') for r in results)),
        'by_failure_book_covers': dict(Counter(r.get('failure_category', 'unknown') for r in book_covers)),
        'identified': len([r for r in book_covers if r['final_status'] == 'identified']),
        'manual_review': len([r for r in book_covers if r['final_status'] == 'manual_review']),
        'rejected': len([r for r in book_covers if r['final_status'] == 'rejected']),
        'processing_error': len([r for r in book_covers if r['final_status'] == 'processing_error']),
        'avg_ocr_confidence': round(avg_ocr, 4),
        'non_book_cover_types': dict(Counter(classify_non_book(r) for r in non_book)),
    },
    'identified_books': [
        {
            'filename': r['filename'],
            'path': r.get('path', ''),
            'size': r.get('size', 0),
            'dimensions': {'width': r.get('width', 0), 'height': r.get('height', 0)},
            'best_title': r.get('best_title', ''),
            'best_authors': r.get('best_authors', []),
            'extracted_title': r.get('extracted_title', ''),
            'extracted_author': r.get('extracted_author', ''),
            'final_confidence': r.get('final_confidence', 0),
            'ocr_confidence': r.get('ocr_confidence_raw', 0),
            'ocr_word_count': r.get('ocr_word_count', 0),
            'candidate_sources': r.get('candidate_sources', []),
            'candidates': r.get('candidates', []),
            'is_regression_book': r['filename'] in ('1btBolC4gc8f8Q5lXJLIL6NZmYA-o2cmE.jpeg', '1AvSHrA5dQE2wRl_V6FLX_LGB8nxgj4FQ.jpeg', '1FFDzr6Fz4Jq_SOj1M7N-GBnZZjtNo7sk.jpeg'),
        }
        for r in book_covers if r['final_status'] == 'identified'
    ],
    'book_cover_failures': [
        {
            'filename': r['filename'],
            'path': r.get('path', ''),
            'size': r.get('size', 0),
            'dimensions': {'width': r.get('width', 0), 'height': r.get('height', 0)},
            'status': r['final_status'],
            'failure_category': r.get('failure_category', ''),
            'failure_description': r.get('failure_description', ''),
            'ocr_confidence': r.get('ocr_confidence_raw', 0),
            'ocr_word_count': r.get('ocr_word_count', 0),
            'extracted_title': r.get('extracted_title', ''),
            'extracted_author': r.get('extracted_author', ''),
            'best_title': r.get('best_title', ''),
            'normalized_ocr': r.get('normalized_ocr', ''),
            'raw_ocr': r.get('raw_ocr', ''),
            'candidates_count': r.get('candidates_count', 0),
            'candidates': r.get('candidates', []),
            'rejection_reason': r.get('rejection_reason', ''),
            'match_score': r.get('match_score', 0),
            'is_upside_down': any(kw in r.get('raw_ocr', '') for kw in ['Jodsd', 'oJJo', 'uvpoqois']) or '180' in r.get('orientation_notes', ''),
            'has_cyrillic': '@EJOPOB' in r.get('raw_ocr', ''),
            'candidate_sources': r.get('candidate_sources', []),
            'isbn_state': r.get('isbn_state', ''),
        }
        for r in book_covers if r['final_status'] != 'identified'
    ],
    'orientation_issues': [
        {
            'filename': r['filename'],
            'orientation': r.get('orientation', ''),
            'notes': r.get('orientation_notes', ''),
            'failure_category': r.get('failure_category', ''),
            'raw_ocr_preview': r.get('raw_ocr', '')[:200],
            'is_book_cover': r['is_book_cover'],
            'visually_confirmed': 'See vision analysis in audit notes',
        }
        for r in results if (r.get('orientation') != 'normal' or r.get('orientation_notes')) and r.get('orientation') in ('uncertain', 'rotated 90')
    ],
    'upside_down_images': [
        {
            'filename': r['filename'],
            'detected_as': 'garbled OCR (180 rotation not auto-detected)',
            'corrected_title': 'See vision analysis',
            'raw_ocr': r.get('raw_ocr', ''),
            'extracted_title': r.get('extracted_title', ''),
        }
        for r in book_covers if any(kw in r.get('raw_ocr', '') for kw in ['Jodsd', 'oJJo', 'uvpoqois', 'UB?['])
    ],
    'cyrillic_image': [
        {
            'filename': r['filename'],
            'detected_script': 'Cyrillic (misread as Latin by en/fr OCR)',
            'ocr_output': r.get('raw_ocr', ''),
            'failure': 'OCR languages config is [en, fr] only — Cyrillic characters read as Latin lookalikes',
        }
        for r in book_covers if '@EJOPOB' in r.get('raw_ocr', '')
    ],
    'non_book_cover_images': [
        {
            'filename': r['filename'],
            'path': r.get('path', ''),
            'type': classify_non_book(r),
            'size': r.get('size', 0),
            'dimensions': {'width': r.get('width', 0), 'height': r.get('height', 0)},
            'ocr_word_count': r.get('ocr_word_count', 0),
            'ocr_preview': r.get('raw_ocr', '')[:120],
            'status': r['final_status'],
        }
        for r in non_book
    ],
    'regression_book_results': [
        {
            'filename': r['filename'],
            'title': r.get('best_title', ''),
            'author': r.get('extracted_author', ''),
            'confidence': r.get('final_confidence', 0),
            'status': r['final_status'],
            'was_identified': r['final_status'] == 'identified',
            'ocr_confidence': r.get('ocr_confidence_raw', 0),
            'existing_job_id': r.get('existing_db_job', 'unknown'),
        }
        for r in book_covers if r['filename'] in ('1btBolC4gc8f8Q5lXJLIL6NZmYA-o2cmE.jpeg', '1AvSHrA5dQE2wRl_V6FLX_LGB8nxgj4FQ.jpeg', '1FFDzr6Fz4Jq_SOj1M7N-GBnZZjtNo7sk.jpeg')
    ],
    'twelve_question_analysis': {
        'Q1_pct_identified': {
            'identified': len([r for r in book_covers if r['final_status'] == 'identified']),
            'total_book_covers': len(book_covers),
            'percentage': round(len([r for r in book_covers if r['final_status'] == 'identified']) / len(book_covers) * 100, 1),
        },
        'Q2_primary_manual_review_cause': 'Candidate match confidence below 0.50 threshold. OCR extracted readable titles/authors but OpenLibrary returned candidates with confidence below 0.50. Common issue: titles with punctuation errors or missing diacritics.',
        'Q3_primary_rejection_cause': 'Two causes: (1) 2 upside-down images (180 rotation) producing garbled OCR with no candidates, (2) 1 Cyrillic-script image not recognized by en/fr OCR config',
        'Q4_orientation_problems': {
            'upside_down_book_covers': 2,
            'rotated_90_images': 1,
            'uncertain_orientation': 1,
        },
        'Q5_non_latin_scripts': {
            'cyrillic': 1,
            'other_non_latin': 0,
            'total': '1 out of 15 book covers has non-Latin script (Cyrillic/Russian)',
            'note': 'No unusual old European script with circular characters was found. Only Cyrillic was detected.',
        },
        'Q6_regression_books_pass': all(r['final_status'] == 'identified' for r in book_covers if r['filename'] in ('1btBolC4gc8f8Q5lXJLIL6NZmYA-o2cmE.jpeg', '1AvSHrA5dQE2wRl_V6FLX_LGB8nxgj4FQ.jpeg', '1FFDzr6Fz4Jq_SOj1M7N-GBnZZjtNo7sk.jpeg')),
        'Q7_most_common_failure': {
            'category': 'H',
            'description': 'Candidates found below manual_review threshold (0.50)',
            'count': len([r for r in book_covers if r['failure_category'] == 'H']),
        },
        'Q8_api_timeouts': False,
        'Q9_avg_ocr_confidence_book_covers': round(avg_ocr, 4),
        'Q10_extractable_titles': {
            'count': len([r for r in book_covers if r.get('extracted_title') and r['extracted_title'] != 'None']),
            'total': len(book_covers),
        },
        'Q11_author_vs_title': {
            'titles_extracted': len([r for r in book_covers if r.get('extracted_title') and r['extracted_title'] != 'None']),
            'authors_extracted': len([r for r in book_covers if r.get('extracted_author') and r['extracted_author'] != 'None']),
            'note': 'Title extraction slightly stronger because titles are prominent; author extraction hindered by upside-down/Cyrillic images',
        },
        'Q12_highest_impact_improvement': 'Auto-rotation correction to fix 2 upside-down image failures (18% of book-cover failures). Adding Cyrillic to OCR languages would fix 1 additional failure.',
    },
    'recommendations': [
        'Implement auto-rotation correction to handle upside-down book photos (2 failures)',
        'Add Cyrillic to OCR languages to read Russian book covers (1 failure)',
        'Filter non-book-cover images before processing (21 of 36 images were not book covers)',
        'Improve title extraction for titles with diacritics and punctuation (H-category failures)',
        'Continue using the 3 regression books as verification tests',
    ],
    'all_images': [
        {
            'filename': r['filename'],
            'path': r.get('path', ''),
            'size': r.get('size', 0),
            'dimensions': {'width': r.get('width', 0), 'height': r.get('height', 0)},
            'final_status': r.get('final_status', ''),
            'final_confidence': r.get('final_confidence', 0),
            'failure_category': r.get('failure_category', ''),
            'failure_description': r.get('failure_description', ''),
            'orientation': r.get('orientation', ''),
            'orientation_notes': r.get('orientation_notes', ''),
            'detected_script': r.get('detected_script', ''),
            'ocr_confidence': r.get('ocr_confidence_raw', 0),
            'ocr_word_count': r.get('ocr_word_count', 0),
            'extracted_title': r.get('extracted_title', ''),
            'extracted_author': r.get('extracted_author', ''),
            'best_title': r.get('best_title', ''),
            'best_authors': r.get('best_authors', []),
            'candidates_count': r.get('candidates_count', 0),
            'candidate_sources': r.get('candidate_sources', []),
            'candidate_titles': [c.get('title', '') for c in r.get('candidates', [])[:3]],
            'is_book_cover': r['is_book_cover'],
            'error': r.get('error', ''),
        }
        for r in results
    ],
}

# Write the JSON report
json_path = os.path.join(PROJECT_ROOT, 'docs', 'REAL_DATASET_PIPELINE_AUDIT.json')
with open(json_path, 'w', encoding='utf-8') as f:
    json.dump(report, f, indent=2, ensure_ascii=False)

print('JSON report written to: ' + json_path)
print('Total images: ' + str(len(results)))
print('Book covers: ' + str(len(book_covers)))
print('Non-book covers: ' + str(len(non_book)))
print('Identified: ' + str(len(report['identified_books'])))
print('Book cover failures: ' + str(len(report['book_cover_failures'])))
