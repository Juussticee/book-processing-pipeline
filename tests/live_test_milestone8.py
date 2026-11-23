#!/usr/bin/env python3
"""Milestone 8.9: Fresh live test with existing Google Drive image."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config

def main():
    config = Config.from_env()
    image_path = os.path.join(
        config.data_directory,
        "downloads",
        "1btBolC4gc8f8Q5lXJLIL6NZmYA-o2cmE.jpeg",
    )

    passed = []
    failed = []

    if os.path.exists(image_path):
        passed.append(f"Image found at {image_path}")
    else:
        failed.append(f"Image not found at {image_path}")
        image_path = None

    if image_path:
        from services.metadata_service import MetadataService
        from services.ocr_service import OCRService
        from services.text_normalizer import TextNormalizer
        from services.book_lookup_service import BookLookupService
        from services.candidate_ranker import CandidateRanker
        from services.confidence_service import ConfidenceService
        from services.image_preprocessor import ImagePreprocessor

        with tempfile.TemporaryDirectory() as tmpdir:
            confidence_svc = ConfidenceService(
                threshold=config.confidence_threshold,
                manual_review_threshold=config.confidence_manual_review_threshold,
                rejected_threshold=config.confidence_rejected_threshold,
            )
            metadata_service = MetadataService(
                image_preprocessor=ImagePreprocessor(),
                ocr_service=OCRService(
                    languages=config.ocr_language.split(","),
                    use_gpu=getattr(config, "ocr_use_gpu", False),
                    confidence_threshold=getattr(config, "ocr_confidence_threshold", 0.5),
                ),
                text_normalizer=TextNormalizer(),
                book_lookup=BookLookupService(
                    google_books_api_key=getattr(config, "google_books_api_key", None),
                    open_library_enabled=getattr(config, "open_library_enabled", True),
                ),
                candidate_ranker=CandidateRanker(),
                confidence_service=confidence_svc,
                data_directory=tmpdir,
            )

            try:
                metadata, candidates = metadata_service.identify_book(image_path)
                passed.append("Extraction completed without exceptions")

                # Use candidate if metadata is None (below auto-accept threshold)
                result = metadata or (candidates[0].metadata if candidates else None)

                if result:
                    conf = result.confidence
                    if conf is not None and conf >= 0.9:
                        passed.append(f"Confidence score: {conf:.4f} (>= 0.90)")
                    else:
                        failed.append(f"Confidence too low: {conf}")
                else:
                    failed.append("No results returned")

                if result and result.isbn_10 is None and result.isbn_13 is None:
                    passed.append("ISBN is None (optional field, OK)")
                elif result:
                    passed.append(f"ISBN present: isbn_10={result.isbn_10}, isbn_13={result.isbn_13}")

                if result and result.title:
                    passed.append(f"Title: {result.title}")

            except Exception as exc:
                failed.append(f"Extraction failed: {exc}")
                import traceback
                traceback.print_exc()

    print("\n=== Milestone 8.9 Live Test Results ===")
    print(f"Passed: {len(passed)}")
    print(f"Failed: {len(failed)}")
    for r in passed:
        print(f"  [PASS] {r}")
    for r in failed:
        print(f"  [FAIL] {r}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
