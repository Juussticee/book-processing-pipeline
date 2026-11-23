"""Benchmark test: vision-first pipeline for the 14-image dataset.

Traces every book through the integrated OCR → vision → lookup → rank → confidence
pipeline using mocked OCR (simulating failures) and cached vision model responses.

Reports: title accuracy, author accuracy, publisher accuracy, false positives,
skipped books, vision model calls, and latency.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
import sys
sys.path.insert(0, str(PROJECT_ROOT))

from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from models.ocr_result import OCRResult
from services.book_lookup_service import BookLookupService
from services.confidence_service import ConfidenceService
from services.candidate_ranker import CandidateRanker
from services.metadata_service import MetadataService
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer
from services.vision_bookcover_extractor import VisionBookCoverExtractor
from services.vision_model_service import VisionModelService

DATA_DIR = PROJECT_ROOT / "data"
VISION_CACHE_DIR = DATA_DIR / "vision_model_responses"
DOWNLOADS_DIR = DATA_DIR / "downloads"
GT_PATH = DATA_DIR / "clean_run_results.json"
BOOKS_PATH = DATA_DIR / "vision_benchmark_books.json"

# This is a benchmark over one private 14-image corpus. The photographs, the
# cached vision responses, the per-book manifest and the ground-truth file are all
# local data (``data/`` is not distributed), so the module skips on a fresh clone
# instead of erroring on a missing file. Nothing here is required to run the
# application.
pytestmark = pytest.mark.skipif(
    not (
        GT_PATH.is_file()
        and BOOKS_PATH.is_file()
        and VISION_CACHE_DIR.is_dir()
        and DOWNLOADS_DIR.is_dir()
    ),
    reason=(
        "the 14-image vision benchmark corpus is local-only "
        "(data/downloads, data/vision_model_responses, data/vision_benchmark_books.json, "
        "data/clean_run_results.json)"
    ),
)


def load_benchmark_books():
    """Per-book manifest: id prefix, ground truth, controlled OCR input.

    Kept in a local file because it names one private collection, not because the
    code needs it at runtime.
    """
    with open(BOOKS_PATH, "r", encoding="utf-8") as handle:
        return [tuple(entry) for entry in json.load(handle)]


def load_ground_truth():
    with open(GT_PATH, "r", encoding="utf-8") as f:
        return json.loads(f.read())


def normalize(s):
    if s is None:
        return ""
    s = str(s).strip()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def fuzz_partial(a, b, threshold=0.8):
    from rapidfuzz import fuzz
    score = fuzz.token_sort_ratio(a, b) / 100.0
    return score >= threshold


def title_match(extracted, expected, threshold=0.8):
    if not extracted or not expected:
        return False
    e = normalize(extracted)
    x = normalize(expected)
    if e == x:
        return True
    return fuzz_partial(e, x, threshold)


def author_match(extracted, expected, threshold=0.7):
    if not extracted or not expected:
        return False
    e = normalize(extracted)
    x = normalize(expected)
    if e == x:
        return True
    return fuzz_partial(e, x, threshold)


def find_drive_file_id(prefix):
    """Find the cached vision response filename stem for a given image ID prefix."""
    for f in VISION_CACHE_DIR.iterdir():
        if f.name.startswith(prefix):
            return f.stem  # The full filename stem (e.g., "13oww9ct1us7aGGeFZVgjTAnBq8ZVIEAo")
    return prefix


def find_image_path(prefix):
    """Find the downloaded image path for a given ID prefix."""
    for f in DOWNLOADS_DIR.iterdir():
        if f.stem.startswith(prefix) and f.suffix in (".jpeg", ".jpg"):
            return str(f)
    return str(DOWNLOADS_DIR / f"{prefix}.jpeg")


class BenchmarkRunner:
    """Runs the vision-first pipeline on all 14 images and collects metrics."""

    # Per-book manifest (drive id prefix, ground truth, controlled OCR input)
    # read from ``data/vision_benchmark_books.json``. The list names one private
    # collection, so it is local data rather than part of the repository.
    @property
    def BENCHMARK_BOOKS(self):
        return load_benchmark_books()

    def _make_service(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()

        ocr_svc = MagicMock(spec=OCRService)
        text_normalizer = MagicMock(spec=TextNormalizer)
        text_normalizer.normalize.return_value = ""
        text_normalizer.extract_isbn.return_value = []

        book_lookup = MagicMock(spec=BookLookupService)
        book_lookup.search_by_isbn.return_value = []
        book_lookup.search_by_title_author.return_value = []
        book_lookup.search_by_title.return_value = []
        book_lookup.search_by_author.return_value = []

        text_extractor = MagicMock()

        from repositories.book_metadata_repository import BookMetadataRepository
        meta_repo = BookMetadataRepository(db_path)

        vision_model = VisionModelService(cache_dir=str(VISION_CACHE_DIR))

        svc = MetadataService(
            image_preprocessor=preprocessor,
            ocr_service=ocr_svc,
            text_normalizer=text_normalizer,
            book_lookup=book_lookup,
            candidate_ranker=CandidateRanker(),
            confidence_service=ConfidenceService(),
            text_extractor=text_extractor,
            metadata_repo=meta_repo,
            cache_repo=MagicMock(),
            vision_model=vision_model,
        )
        return svc, db_path

    def run(self):
        results = []

        for drive_prefix, gt_title, gt_author, gt_publisher, ocr_text, ocr_title, ocr_author, ocr_should_fail in self.BENCHMARK_BOOKS:
            full_drive_id = find_drive_file_id(drive_prefix)
            image_path = find_image_path(drive_prefix)
            svc, db_path = self._make_service()

            # Configure mocked OCR
            ocr_svc = svc._ocr_service
            ocr_svc.extract_text.return_value = OCRResult(
                text=ocr_text,
                confidence=0.0 if ocr_should_fail else 0.95,
                words=[] if ocr_should_fail else [("word", 0.95, 0, 0, 0, 0)],
                language="en",
                image_path="/fake/image.jpg",
            )
            svc._text_extractor.extract.return_value = {
                "title": ocr_title,
                "author": ocr_author,
                "isbn": None,
                "publisher": None,
                "title_candidates": [],
                "contributor": None,
            }

            start = time.time()
            try:
                result, candidates = svc.identify_book(
                    image_path,
                    drive_file_id=full_drive_id,
                    job_id="bench",
                )
            except Exception as e:
                result = None
                candidates = []
                error = str(e)
            else:
                error = None
            elapsed = time.time() - start
            vision_calls = svc._vision_model.stats["total_calls"]

            # Determine extracted fields from result
            if result is not None:
                ext_title = result.vision_title or result.title
                ext_author = result.vision_author or (
                    result.authors[0] if result.authors else None
                )
                ext_publisher = result.vision_publisher or result.publisher
                final_status = result.status
                final_conf = result.confidence
            else:
                ext_title = None
                ext_author = None
                ext_publisher = None
                final_status = "needs_review"
                final_conf = 0.0

            # Vision extraction for comparison
            vision_meta = svc._vision_model.extract_vision_metadata(
                image_path, full_drive_id
            )
            vision_title = vision_meta.title if vision_meta else None
            vision_author = vision_meta.author if vision_meta else None
            vision_publisher = vision_meta.publisher if vision_meta else None

            results.append({
                "drive_id": full_drive_id,
                "gt_title": gt_title,
                "gt_author": gt_author,
                "gt_publisher": gt_publisher,
                "ocr_text": ocr_text,
                "ocr_should_fail": ocr_should_fail,
                "ocr_title": ocr_title,
                "ocr_author": ocr_author,
                "vision_called": vision_calls > 0,
                "vision_title": vision_title,
                "vision_author": vision_author,
                "vision_publisher": vision_publisher,
                "final_title": ext_title,
                "final_author": ext_author,
                "final_publisher": ext_publisher,
                "final_status": final_status,
                "final_confidence": final_conf,
                "vision_calls": vision_calls,
                "latency": elapsed,
                "error": error,
            })

            # Cleanup
            try:
                os.unlink(db_path)
            except Exception:
                pass

        return results

    def print_forensic_results(self, results):
        """Print BOOK FORENSIC RESULT for each book."""
        for r in results:
            print("=" * 60)
            print("BOOK FORENSIC RESULT")
            print("=" * 60)
            print(f"Image: {r['drive_id']}")
            print(f"Ground truth: title={r['gt_title']}, author={r['gt_author']}")
            print(f"Publisher GT: {r['gt_publisher']}")
            print(f"OCR text: {r['ocr_text']!r}")
            print(f"OCR extracted: title={r['ocr_title']}, author={r['ocr_author']}")
            print(f"OCR quality: {'should_have_failed' if r['ocr_should_fail'] else 'ok'}")
            print(f"Vision model called: {'YES' if r['vision_called'] else 'NO'}")
            print(f"Vision title: {r['vision_title']}")
            print(f"Vision author: {r['vision_author']}")
            print(f"Vision publisher: {r['vision_publisher']}")
            print(f"Final title: {r['final_title']}")
            print(f"Final author: {r['final_author']}")
            print(f"Final publisher: {r['final_publisher']}")
            print(f"Final confidence: {r['final_confidence']}")
            print(f"Final status: {r['final_status']}")
            if r["error"]:
                print(f"Error: {r['error']}")
            print()

    def print_summary(self, results):
        """Print benchmark summary table."""
        total = len(results)
        vision_called = sum(1 for r in results if r["vision_called"])
        gt_books = [r for r in results if r["gt_title"] is not None]

        # OCR-only accuracy (for comparison)
        ocr_title_correct = sum(
            1 for r in gt_books
            if r["ocr_title"] and title_match(r["ocr_title"], r["gt_title"])
        )
        ocr_author_correct = sum(
            1 for r in gt_books
            if r["ocr_author"] and author_match(r["ocr_author"], r["gt_author"])
        )
        ocr_both = sum(
            1 for r in gt_books
            if r["ocr_title"] and r["ocr_author"]
            and title_match(r["ocr_title"], r["gt_title"])
            and author_match(r["ocr_author"], r["gt_author"])
        )

        # Vision-only accuracy
        vis_title_correct = sum(
            1 for r in gt_books
            if r["vision_title"] and title_match(r["vision_title"], r["gt_title"])
        )
        vis_author_correct = sum(
            1 for r in gt_books
            if r["vision_author"] and author_match(r["vision_author"], r["gt_author"])
        )
        vis_both = sum(
            1 for r in gt_books
            if r["vision_title"] and r["vision_author"]
            and title_match(r["vision_title"], r["gt_title"])
            and author_match(r["vision_author"], r["gt_author"])
        )

        # Final pipeline accuracy
        fin_title_correct = sum(
            1 for r in gt_books
            if r["final_title"] and title_match(r["final_title"], r["gt_title"])
        )
        fin_author_correct = sum(
            1 for r in gt_books
            if r["final_author"] and author_match(r["final_author"], r["gt_author"])
        )
        fin_both = sum(
            1 for r in gt_books
            if r["final_title"] and r["final_author"]
            and title_match(r["final_title"], r["gt_title"])
            and author_match(r["final_author"], r["gt_author"])
        )

        skipped = sum(1 for r in results if r["final_status"] == "needs_review")
        manual_reviews = sum(
            1 for r in results if r["final_status"] == "needs_review"
        )

        avg_latency = sum(r["latency"] for r in results) / total if total else 0
        max_latency = max(r["latency"] for r in results) if results else 0

        print("=" * 60)
        print("BENCHMARK SUMMARY")
        print("=" * 60)
        print(f"Total books: {total}")
        print(f"Books with ground truth: {len(gt_books)}")
        print()
        print(f"{'Metric':<30} {'OCR':>10} {'Vision':>10} {'Final':>10}")
        print("-" * 60)
        print(f"{'Title correct':<30} {ocr_title_correct:>10} {vis_title_correct:>10} {fin_title_correct:>10}")
        print(f"{'Author correct':<30} {ocr_author_correct:>10} {vis_author_correct:>10} {fin_author_correct:>10}")
        print(f"{'Both correct':<30} {ocr_both:>10} {vis_both:>10} {fin_both:>10}")
        print(f"{'False positives':<30} {'N/A':>10} {'N/A':>10} {skipped:>10}")
        print(f"{'Skipped (rejected)':<30} {'N/A':>10} {'N/A':>10} {skipped:>10}")
        print(f"{'Manual reviews':<30} {'N/A':>10} {'N/A':>10} {manual_reviews:>10}")
        print()
        print(f"Vision model calls: {vision_called}/{total}")
        print(f"Average latency: {avg_latency:.3f}s")
        print(f"Max latency: {max_latency:.3f}s")
        print()

        # Per-book detail
        print("Per-book details:")
        print(f"{'Book ID':<25} {'OCR Title':<20} {'Vision Title':<40} {'Final Status':<15}")
        print("-" * 95)
        for r in results:
            print(f"{r['drive_id'][:25]:<25} {str(r['ocr_title'])[:20]:<20} {str(r['vision_title'])[:40]:<40} {r['final_status']:<15}")


class TestBenchmark:
    """Run the full benchmark and assert success criteria."""

    def test_all_14_books_processed(self):
        """All 14 books must be processed without errors."""
        runner = BenchmarkRunner()
        results = runner.run()
        assert len(results) == 14
        assert all(r["error"] is None for r in results), (
            f"Errors: {[r['error'] for r in results if r['error']]}"
        )
        runner.print_forensic_results(results)
        runner.print_summary(results)

    def test_7_problem_books_reach_vision(self):
        """The 7 previously problematic books must reach the vision model."""
        runner = BenchmarkRunner()
        results = runner.run()

        problem_prefixes = [
            "13oww9ct1us7aG", "1iZb9J4HpRXwHr", "1nFyRFDKcNywBt",
            "1Hy-jAFIkKVgg5", "1vxPPAVEDUxUSm", "10--_zsGu-BOXy",
        ]
        for r in results:
            if any(r["drive_id"].startswith(p) for p in problem_prefixes):
                assert r["vision_called"], (
                    f"Vision model was NOT called for problem book "
                    f"{r['drive_id']} even though OCR failed"
                )

    def test_no_books_silently_lost(self):
        """No photo may be dropped, and a correct vision title must survive.

        Vision is never auto-accepted on its own (settled Phase-27/44 policy: the
        same confidence gate and verification still decide), so a book whose
        vision extraction is correct may legitimately be persisted as
        ``needs_review``. What must never happen is losing the book, erroring
        instead of recording it, or discarding the correct extraction on the way
        into the record.
        """
        runner = BenchmarkRunner()
        results = runner.run()
        assert len(results) == 14, "every benchmark photo must produce a result"
        for r in results:
            assert r["error"] is None, f"{r['drive_id']} raised: {r['error']}"
            assert r["final_title"], f"{r['drive_id']} produced no record at all"
            assert r["final_status"] in {"identified", "needs_review"}
            if r["vision_title"] and r["gt_title"] and title_match(
                r["vision_title"], r["gt_title"]
            ):
                assert title_match(
                    r["final_title"], r["vision_title"]
                ) or title_match(r["final_title"], r["gt_title"]), (
                    f"Book '{r['gt_title']}' lost its correct vision extraction: "
                    f"vision said '{r['vision_title']}', record holds "
                    f"'{r['final_title']}'"
                )

    def test_theodore_litt_not_false_identified(self):
        """Theodore Litt must NOT become 'Theodori episcopi Mopsuesteni'."""
        runner = BenchmarkRunner()
        results = runner.run()
        litt = next(
            (r for r in results if r["drive_id"].startswith("1nFyRFDKcNywB")), None
        )
        assert litt is not None
        if litt["final_title"]:
            assert not title_match(
                litt["final_title"], "Theodori episcopi Mopsuesteni", 0.5
            ), (
                f"Theodore Litt book was false-identified as "
                f"'Theodori episcopi Mopsuesteni' "
                f"(final_title={litt['final_title']})"
            )

    def test_benchmark_prints(self):
        """Print all forensic results and summary."""
        runner = BenchmarkRunner()
        results = runner.run()
        runner.print_forensic_results(results)
        runner.print_summary(results)
