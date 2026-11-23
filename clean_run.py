"""Clean end-to-end run of app.py pipeline on all 14 Drive images."""
import json, os, shutil, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.config import Config
from core.logger import setup_logger
from repositories.job_repository import JobRepository
from repositories.book_metadata_repository import BookMetadataRepository
from repositories.export_repository import ExportRepository
from repositories.sqlite_repository import SQLiteRepository
from services.book_lookup_service import BookLookupService
from services.candidate_ranker import CandidateRanker
from services.confidence_service import ConfidenceService
from services.image_preprocessor import ImagePreprocessor
from services.metadata_service import MetadataService
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer
from workers.processing_worker import ProcessingWorker
from services.job_service import JobService

def main():
    config = Config.from_env()
    config.validate()
    data_dir = config.data_directory
    app_db_path = os.path.join(data_dir, "app.db")
    jobs_db_path = os.path.join(data_dir, "jobs.db")
    metadata_db_path = os.path.join(data_dir, "metadata.db")
    downloads_dir = os.path.join(data_dir, "downloads")

    # Clean stale DBs
    print("=== STEP 1: Cleaning stale DBs ===")
    for db in [jobs_db_path, metadata_db_path, os.path.join(data_dir, "metadata_after.db")]:
        if os.path.exists(db):
            os.remove(db)
            print(f"  Removed: {db}")
    debug_dir = os.path.join(data_dir, "debug")
    if os.path.exists(debug_dir):
        shutil.rmtree(debug_dir)
        print(f"  Removed: {debug_dir}")
    os.makedirs(debug_dir, exist_ok=True)

    # Find all images
    print("\n=== STEP 2: Finding downloaded images ===")
    image_files = []
    seen_ids = set()
    search_dirs = [downloads_dir, os.path.join(data_dir, "diagnostic_downloads"),
                   os.path.join(data_dir, "batch_downloads")]
    for search_dir in search_dirs:
        if os.path.isdir(search_dir):
            for fname in sorted(os.listdir(search_dir)):
                if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.tiff', '.bmp', '.webp')):
                    file_id = fname.rsplit('.', 1)[0]
                    if file_id not in seen_ids:
                        seen_ids.add(file_id)
                        local_path = os.path.join(search_dir, fname)
                        image_files.append({'file_id': file_id, 'file_name': f"{file_id}.jpg", 'local_path': local_path})
    print(f"  Found {len(image_files)} images")

    # Set up same services as app.py
    print("\n=== STEP 3: Setting up production services ===")
    confidence_threshold = getattr(config, "confidence_threshold", 0.85)
    confidence_svc = ConfidenceService(
        threshold=confidence_threshold,
        manual_review_threshold=getattr(config, "confidence_manual_review_threshold", 0.50),
        rejected_threshold=getattr(config, "confidence_rejected_threshold", 0.50),
    )
    ocr_conf_threshold = getattr(config, "ocr_confidence_threshold", 0.5)
    book_lookup = BookLookupService(
        google_books_api_key=getattr(config, "google_books_api_key", None),
        open_library_enabled=getattr(config, "open_library_enabled", True),
        google_books_timeout=getattr(config, "google_books_timeout", 15),
        open_library_timeout=getattr(config, "open_library_timeout", 10),
    )
    metadata_service = MetadataService(
        image_preprocessor=ImagePreprocessor(),
        ocr_service=OCRService(
            languages=config.ocr_language.split(","),
            use_gpu=getattr(config, "ocr_use_gpu", False),
            confidence_threshold=ocr_conf_threshold,
        ),
        text_normalizer=TextNormalizer(),
        book_lookup=book_lookup,
        candidate_ranker=CandidateRanker(),
        confidence_service=confidence_svc,
        data_directory=data_dir,
    )

    results = {}
    with JobRepository(jobs_db_path) as job_repo:
        with SQLiteRepository(app_db_path) as file_repo:
            job_service = JobService(job_repo)
            metadata_repo = BookMetadataRepository(metadata_db_path)
            metadata_service.set_metadata_repo(metadata_repo)
            export_repo = ExportRepository(jobs_db_path, metadata_db_path)
            metadata_service.set_export_repo(export_repo)

            worker = ProcessingWorker(job_service, metadata_service=metadata_service, metadata_repo=metadata_repo)
            worker.initialize()

            # Create jobs
            jobs = []
            for img in image_files:
                job = job_repo.create_job(file_id=img['file_id'], file_name=img['file_name'], local_path=img['local_path'])
                if job:
                    jobs.append((img, job))
                    print(f"  Created job for {img['file_id']}")

            # Process each
            for img, job in jobs:
                print(f"\n  Processing {img['file_id']}...")
                try:
                    worker.process(job)
                    updated_job = job_repo.get_job_by_id(job.job_id)
                    metadata = metadata_repo.get_by_job_id(job.job_id) if updated_job else None
                    if updated_job and metadata:
                        results[img['file_id']] = {
                            'job_id': job.job_id, 'file_id': img['file_id'],
                            'status': updated_job.status.value,
                            'raw_ocr': metadata.raw_ocr,
                            'ocr_text': metadata.ocr_text,
                            'extracted_title': metadata.extracted_title,
                            'extracted_author': metadata.extracted_author,
                            'extracted_isbn': metadata.extracted_isbn,
                            'extracted_publisher': metadata.extracted_publisher,
                            'match_candidate_title': metadata.title,
                            'match_candidate_authors': metadata.authors,
                            'confidence': metadata.confidence,
                            'source': metadata.source,
                            'match_score': metadata.match_score,
                            'candidate_sources': metadata.candidate_sources,
                            'rejection_reason': metadata.rejection_reason,
                            'error_message': updated_job.error_message,
                        }
                        print(f"    -> {updated_job.status.value}, conf={metadata.confidence}, title={metadata.title}")
                    elif updated_job:
                        results[img['file_id']] = {
                            'job_id': job.job_id, 'file_id': img['file_id'],
                            'status': updated_job.status.value,
                            'error_message': updated_job.error_message,
                        }
                        print(f"    -> {updated_job.status.value}, error={updated_job.error_message}")
                    else:
                        results[img['file_id']] = {'status': 'unknown', 'error_message': 'Not found'}
                        print("    -> ERROR: job not found")
                except Exception as e:
                    print(f"    -> EXCEPTION: {e}")
                    job_repo.fail_job(job.job_id, error_message=str(e))
                    results[img['file_id']] = {'status': 'exception', 'error_message': str(e)}

            worker.shutdown()
            metadata_repo.close()
            export_repo.close()

    # Save results
    output_path = os.path.join(data_dir, "clean_run_results.json")
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nResults saved to: {output_path}")

    # Summary
    print("\n=== SUMMARY ===")
    status_counts = {}
    for fid, r in results.items():
        s = r.get('status', 'unknown')
        status_counts[s] = status_counts.get(s, 0) + 1
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    print("\n=== DETAILS ===")
    for fid, r in sorted(results.items()):
        print(f"\n  {fid}:")
        print(f"    status: {r.get('status', 'N/A')}")
        print(f"    raw_ocr: {r.get('raw_ocr', 'N/A')}")
        print(f"    extracted_title: {r.get('extracted_title', 'N/A')}")
        print(f"    extracted_author: {r.get('extracted_author', 'N/A')}")
        title = r.get('match_candidate_title', 'N/A')
        authors = r.get('match_candidate_authors', 'N/A')
        print(f"    match_candidate: {title} by {authors}")
        print(f"    confidence: {r.get('confidence', 'N/A')}")
        print(f"    candidate_sources: {r.get('candidate_sources', 'N/A')}")
        print(f"    rejection_reason: {r.get('rejection_reason', 'N/A')}")

if __name__ == "__main__":
    main()