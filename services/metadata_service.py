"""
Metadata service — orchestrates the book identification pipeline.

Runs:
OCR -> Normalize text -> Extract structured fields
-> Lookup books (multi-source) -> Rank candidates
-> Calculate confidence -> Validate -> Persist

Also handles persisting results via BookMetadataRepository and creating
export queue entries (Milestone 5).

Multi-source identification (Milestone 5 improvements):
1. ISBN search (highest priority — exact match)
2. Exact title + author search
3. Title only search
4. Author only search

Strict confidence thresholds:
- confidence >= 0.85 -> identified (auto-accept)
- 0.50 - 0.85 -> needs_review (manual review)
- < 0.50 -> needs_review (never rejected; all books reach review)
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from rapidfuzz import fuzz

from core.logger import setup_logger
from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from models.ocr_result import OCRResult
from repositories.book_metadata_repository import BookMetadataRepository
from services.book_lookup_service import BookLookupService
from services.book_text_extractor import BookTextExtractor
from services.candidate_ranker import CandidateRanker
from services.candidate_verification_service import (
    CandidateVerificationService,
    VerificationEvidence,
)
from services.confidence_service import ConfidenceService
from services.content_identity import (
    DUPLICATE_EXACT,
    DUPLICATE_NEAR,
    DUPLICATE_POSSIBLE_NEAR,
    DUPLICATE_POSSIBLE_SAME_BOOK,
    DUPLICATE_SAME_BOOK,
    NO_MATCH,
    ImageFingerprint,
    SameBookVerdict,
    best_same_book_match,
    classify_fingerprint,
    encode_color,
    hamming_distance,
)
from services.cover_verifier import CoverVerifier
from services.enhanced_text_extractor import EnhancedBookTextExtractor
from services.image_preprocessor import ImagePreprocessor
from services.ocr_service import OCRService
from services.text_normalizer import TextNormalizer
from services.vision_model_service import VisionModelService

logger = setup_logger(__name__)


class MetadataService:
    """Orchestrates the full book identification pipeline.

    The worker calls ONLY this service. It hides all implementation
    details (OCR, lookup, ranking, confidence) from the worker.

    Pipeline:
    1. Preprocess image (load, auto-rotate, resize, grayscale, denoise)
    2. Run OCR (PaddleOCR-VL / Baidu OCR API) with multilingual support
    3. Normalize OCR text (fix common mistakes, extract ISBN)
    4. Extract structured fields via BookTextExtractor (title, author, ISBN, publisher)
    5. Multi-source lookup: ISBN → title+author → title → author
    6. Rank all candidates against OCR fields
    7. Calculate confidence (weighted formula with strict thresholds)
    8. Validate OCR vs API overlap (reject false positives)
    9. Persist metadata to database with evidence fields
    10. Return best BookMetadata (or signal for manual review/rejection)
    """

    def __init__(
        self,
        image_preprocessor: Optional[ImagePreprocessor] = None,
        ocr_service: Optional[OCRService] = None,
        text_normalizer: Optional[TextNormalizer] = None,
        book_lookup: Optional[BookLookupService] = None,
        candidate_ranker: Optional[CandidateRanker] = None,
        confidence_service: Optional[ConfidenceService] = None,
        metadata_repo: Optional[BookMetadataRepository] = None,
        export_repo=None,
        text_extractor: Optional[BookTextExtractor] = None,
        data_directory: str = "data",
        cache_repo=None,
        vision_model: Optional[VisionModelService] = None,
        paddleocr_service: Optional[Any] = None,
    ) -> None:
        self._preprocessor = image_preprocessor or ImagePreprocessor()
        self._ocr_service = ocr_service or OCRService(languages=["en", "fr"])
        self._paddleocr_service = paddleocr_service
        self._normalizer = text_normalizer or TextNormalizer()
        self._book_lookup = book_lookup or BookLookupService()
        self._ranker = candidate_ranker or CandidateRanker()
        self._confidence = confidence_service or ConfidenceService()
        self._metadata_repo = metadata_repo
        self._export_repo = export_repo
        self._text_extractor = text_extractor or EnhancedBookTextExtractor()
        self._data_directory = data_directory
        self._cache_repo = cache_repo
        self._vision_model = vision_model or VisionModelService()
        # Verification service (used for evidence-based contradiction detection)
        self._cover_verifier = CoverVerifier()
        self._verification_service = CandidateVerificationService(
            cover_verifier=self._cover_verifier,
            text_normalizer=self._normalizer,
        )
        # Sync verification thresholds with confidence_service thresholds
        self._sync_verification_thresholds()
        # Duplicate protection (optional; configured by the worker/app)
        self._duplicate_repo = None
        self._ocr_call_count = 0
        self._duplicate_checks = 0
        self._duplicates_skipped = 0
        self._pending_fingerprint = None

    # ------------------------------------------------------------------
    # Duplicate protection
    # ------------------------------------------------------------------

    def set_duplicate_repo(self, repo) -> None:
        """Inject the fingerprint/duplicate repository."""
        self._duplicate_repo = repo

    def reset_stats(self) -> None:
        """Reset per-run counters used by the sync summary."""
        self._ocr_call_count = 0
        self._duplicate_checks = 0
        self._duplicates_skipped = 0
        self._pending_fingerprint = None

    @property
    def ocr_call_count(self) -> int:
        return self._ocr_call_count

    @property
    def duplicate_checks(self) -> int:
        return self._duplicate_checks

    @property
    def duplicates_skipped(self) -> int:
        return self._duplicates_skipped

    def _check_image_duplicate(
        self,
        preprocessed_path: str,
        original_path: str,
        drive_file_id: str,
        job_id: str,
    ) -> Optional[BookMetadata]:
        """Fingerprint the normalized image and look for an existing copy.

        Returns a ``status="duplicate"`` metadata stub when the same image
        (byte-identical or perceptually near-identical) was already processed,
        otherwise ``None``. The fingerprint of the current image is registered
        so later uploads can match against it.
        """
        if self._duplicate_repo is None or not drive_file_id:
            return None

        self._duplicate_checks += 1
        try:
            fingerprint = ImageFingerprint.from_images(preprocessed_path, original_path)
        except Exception as exc:  # corrupt image -> let normal pipeline fail
            logger.warning("Could not fingerprint image: %s", exc)
            return None

        existing = self._duplicate_repo.find_by_content_hash(fingerprint.content_hash)
        if existing is not None and existing["drive_file_id"] != drive_file_id:
            self._duplicates_skipped += 1
            self._duplicate_repo.record_duplicate(
                drive_file_id=drive_file_id,
                match_kind=DUPLICATE_EXACT,
                original_drive_file_id=existing["drive_file_id"],
                original_job_id=existing["job_id"],
                content_hash=fingerprint.content_hash,
                perceptual_hash=fingerprint.perceptual_hash,
                hash_distance=0,
                detail="identical normalized image bytes",
            )
            logger.info(
                "Exact duplicate of Drive ID %s -- skipping OCR",
                existing["drive_file_id"],
            )
            return self._duplicate_stub(
                drive_file_id,
                job_id,
                f"exact_duplicate_of:{existing['drive_file_id']}",
            )

        # Only persisted images become canonical: registering here would let a
        # photo whose OCR later failed mask a good copy of the same photo.
        self._pending_fingerprint = (drive_file_id, job_id, fingerprint)

        best_row = None
        best_distance = 10**6
        best_kind = "not_duplicate"
        for row in self._duplicate_repo.list_fingerprints_excluding(drive_file_id):
            kind = classify_fingerprint(
                fingerprint,
                row["perceptual_hash"],
                row["color_signature"] or "",
                row["aspect_ratio"] or 0.0,
            )
            if kind == "not_duplicate":
                continue
            distance = hamming_distance(
                fingerprint.perceptual_hash, row["perceptual_hash"]
            )
            if distance < best_distance:
                best_row, best_distance, best_kind = row, distance, kind

        if best_kind == DUPLICATE_NEAR:
            self._duplicates_skipped += 1
            self._duplicate_repo.record_duplicate(
                drive_file_id=drive_file_id,
                match_kind=DUPLICATE_NEAR,
                original_drive_file_id=best_row["drive_file_id"],
                original_job_id=best_row["job_id"],
                content_hash=fingerprint.content_hash,
                perceptual_hash=fingerprint.perceptual_hash,
                hash_distance=best_distance,
                detail="perceptual match on normalized image",
            )
            logger.info(
                "Near duplicate (distance=%d) of Drive ID %s -- skipping OCR",
                best_distance,
                best_row["drive_file_id"],
            )
            return self._duplicate_stub(
                drive_file_id,
                job_id,
                f"near_duplicate_of:{best_row['drive_file_id']}",
            )

        if best_kind == DUPLICATE_POSSIBLE_NEAR:
            self._duplicate_repo.record_duplicate(
                drive_file_id=drive_file_id,
                match_kind=DUPLICATE_POSSIBLE_NEAR,
                original_drive_file_id=best_row["drive_file_id"],
                original_job_id=best_row["job_id"],
                content_hash=fingerprint.content_hash,
                perceptual_hash=fingerprint.perceptual_hash,
                hash_distance=best_distance,
                detail="possible duplicate; processed and flagged for review",
            )
            logger.warning(
                "Possible duplicate (distance=%d) of Drive ID %s -- "
                "processing anyway and flagging for review",
                best_distance,
                best_row["drive_file_id"],
            )

        return None

    @staticmethod
    def _duplicate_stub(drive_file_id: str, job_id: str, reason: str) -> BookMetadata:
        """Metadata stub representing a skipped duplicate (never persisted)."""
        return BookMetadata(
            title="(duplicate)",
            confidence=0.0,
            source="dedupe",
            status="duplicate",
            drive_file_id=drive_file_id,
            job_id=job_id,
            rejection_reason=reason,
            candidate_sources=["dedupe"],
        )

    def _find_same_book(self, metadata: BookMetadata) -> SameBookVerdict:
        """Compare a new identification against already persisted books."""
        if self._duplicate_repo is None or self._metadata_repo is None:
            return NO_MATCH
        try:
            existing = [
                row
                for row in self._metadata_repo.list_identity_rows()
                if row[0] != metadata.drive_file_id
            ]
        except Exception as exc:
            logger.warning("Same-book check skipped: %s", exc)
            return NO_MATCH
        return best_same_book_match(
            metadata.isbn_13 or metadata.isbn_10,
            metadata.title,
            metadata.authors,
            existing,
        )

    def set_metadata_repo(self, repo: BookMetadataRepository) -> None:
        """Inject the metadata repository for persistence."""
        self._metadata_repo = repo

    def set_export_repo(self, repo) -> None:
        """Inject the export repository for milestone 5 export queue creation."""
        self._export_repo = repo

    def set_cache_repo(self, repo) -> None:
        """Inject the cache repository for OCR/API result caching."""
        self._cache_repo = repo

    def set_confidence_service(self, svc: ConfidenceService) -> None:
        """Update confidence thresholds and sync with verification service."""
        self._confidence = svc
        self._sync_verification_thresholds()

    def _sync_verification_thresholds(self) -> None:
        """Synchronize verification service thresholds with confidence service.

        This ensures backward compatibility with tests/configs that set
        custom confidence thresholds on the ConfidenceService.
        """
        try:
            threshold = self._confidence.threshold
            manual_review = self._confidence.manual_review_threshold
            if isinstance(threshold, (int, float)) and isinstance(
                manual_review, (int, float)
            ):
                self._verification_service._confidence_threshold = float(threshold)
                self._verification_service._manual_review_threshold = float(
                    manual_review
                )
        except (AttributeError, TypeError):
            pass  # Use default thresholds; confidence_service may be a mock

    def identify_book(
        self,
        image_path: str,
        raw_ocr_text: str = "",
        drive_file_id: str = "",
        job_id: str = "",
    ) -> Tuple[Optional[BookMetadata], Optional[List[BookCandidate]]]:
        """Run the full identification pipeline on a book cover image.

        Multi-source candidate generation:
        1. ISBN search (exact match — highest reliability)
        2. Title + author search
        3. Title only search
        4. Author only search

        Each source produces candidates, all are ranked together.
        The highest-scoring candidate that passes title/author overlap
        validation is returned.

        Args:
            image_path: Path to the book cover image.
            raw_ocr_text: Pre-extracted OCR text (for testing).
            drive_file_id: Drive file ID for metadata persistence.
            job_id: Job ID for metadata persistence and debug artifacts.

        Returns:
            Tuple of (best_metadata, all_candidates).
            - best_metadata is None if no candidates found, confidence
              is below 0.50 (rejected), or title/author overlap fails
              (false positive rejection).
            - all_candidates is the full ranked list (None if no candidates).
        """
        logger.info("Starting book identification for: %s", image_path)

        preprocessed_path: Optional[str] = None
        self._pending_fingerprint = None
        try:
            # 1. Preprocess image
            preprocessed_path = self._preprocessor.preprocess(image_path)

            # 1b. Duplicate gate: identical or near-identical image already
            # processed => skip OCR, metadata lookups and persistence entirely.
            duplicate_stub = self._check_image_duplicate(
                preprocessed_path, image_path, drive_file_id, job_id
            )
            if duplicate_stub is not None:
                return duplicate_stub, []

            # 2. OCR
            if raw_ocr_text:
                logger.info("Using pre-provided OCR text (skipping OCR)")
                ocr_result = OCRResult(
                    text=raw_ocr_text,
                    confidence=0.9,
                    words=[],
                    language="en",
                    image_path=image_path,
                )
            else:
                self._ocr_call_count += 1
                ocr_result = self._ocr_service.extract_text(preprocessed_path)

                # 2a. Geometry gate: a sideways page is wider than it is tall.
                # Book covers are portrait, so a landscape image is a strong
                # orientation signal that does not depend on OCR quality.
                ocr_result = self._apply_geometry_orientation_correction(
                    preprocessed_path, ocr_result
                )

                # 2b. Orientation fallback: if OCR quality is very poor
                # (few words or low confidence), try detecting physical
                # orientation and re-running OCR.
                ocr_result = self._try_orientation_correction(image_path, ocr_result)

            # 3. Normalize OCR text
            normalized_text = self._normalizer.normalize(ocr_result.text)
            isbns = self._normalizer.extract_isbn(
                normalized_text + "\n" + ocr_result.text
            )

            # 4. Extract structured fields via BookTextExtractor
            extracted = self._text_extractor.extract(ocr_result.text)
            ocr_title = extracted.get("title")
            ocr_title_candidates = extracted.get("title_candidates", [])
            ocr_author = extracted.get("author")
            ocr_contributor = extracted.get("contributor")
            ocr_isbn = isbns[0] if isbns else extracted.get("isbn")

            logger.info(
                "OCR extracted: title=%s, author=%s, contributor=%s, isbn=%s, "
                "publisher=%s, candidates=%s",
                ocr_title,
                ocr_author,
                ocr_contributor,
                ocr_isbn,
                extracted.get("publisher"),
                ocr_title_candidates,
            )

            # 5. Multi-source candidate generation
            all_candidates: List[BookMetadata] = []
            candidate_sources: List[str] = []

            # Source 1: ISBN search (highest priority)
            if ocr_isbn:
                isbn_candidates = self._book_lookup.search_by_isbn(ocr_isbn)
                if isbn_candidates:
                    all_candidates.extend(isbn_candidates)
                    candidate_sources.append("isbn")
                    logger.info("ISBN search: %d candidates", len(isbn_candidates))

            # Source 2: Title + author search (use primary title candidate)
            if ocr_title:
                ta_candidates = self._book_lookup.search_by_title_author(
                    ocr_title, ocr_author
                )
                if ta_candidates:
                    all_candidates.extend(ta_candidates)
                    candidate_sources.append("title_author")
                    logger.info(
                        "Title+author search: %d candidates", len(ta_candidates)
                    )

            # Source 2b: If primary title didn't yield results and we have
            # alternative title candidates, try them
            if ocr_title and not all_candidates:
                for alt_title in ocr_title_candidates:
                    if alt_title == ocr_title:
                        continue
                    c_candidates = self._book_lookup.search_by_title_author(
                        alt_title, ocr_author
                    )
                    if c_candidates:
                        all_candidates.extend(c_candidates)
                        candidate_sources.append("title_author_alt")
                        logger.info(
                            "Alternate title candidate search: %d candidates",
                            len(c_candidates),
                        )
                        break

            # Source 3: Title only search
            if ocr_title and not candidate_sources:
                t_candidates = self._book_lookup.search_by_title(ocr_title)
                if t_candidates:
                    all_candidates.extend(t_candidates)
                    candidate_sources.append("title")
                    logger.info("Title-only search: %d candidates", len(t_candidates))

            # Source 3b: If no candidates yet, try alternate title candidates
            if not all_candidates and ocr_title_candidates:
                for alt_title in ocr_title_candidates:
                    if alt_title == ocr_title:
                        continue
                    logger.info("Trying alternate title candidate: %s", alt_title)
                    alt_candidates = self._book_lookup.search_by_title(alt_title)
                    if alt_candidates:
                        all_candidates.extend(alt_candidates)
                        candidate_sources.append("title_alt")
                        logger.info(
                            "Alternate title search ('%s'): %d candidates",
                            alt_title,
                            len(alt_candidates),
                        )
                        break  # Use first productive alternate

            # Source 4: Author only search (if we have author but no title+author match)
            # Also run when only title-based sources were used and the title was
            # a generic/single-word fragment — the title results may be wrong books
            # and author-only search can find the correct one.
            # Skip if we already did title+author or ISBN search (those include
            # the author in matching), to avoid redundant lookups for passing images.
            if ocr_author and not any(
                s in candidate_sources
                for s in ("title_author", "title_author_alt", "isbn")
            ):
                a_candidates = self._book_lookup.search_by_author(ocr_author)
                if a_candidates:
                    all_candidates.extend(a_candidates)
                    candidate_sources.append("author")
                    logger.info("Author-only search: %d candidates", len(a_candidates))

            if not all_candidates:
                logger.warning("No candidates found for image: %s", image_path)

                # Vision model recovery: OCR produced no candidates.
                # Try the vision model to extract title/author from the image.
                vision_meta = self._vision_model.extract_vision_metadata(
                    image_path, drive_file_id
                )
                # Vision model was invoked (FINAL ANALYSIS step) — even if it
                # returned None or target_book_identified is False.
                vision_invoked = True
                if (
                    vision_invoked
                    and vision_meta is not None
                    and vision_meta.target_book_identified
                ):
                    vision_title, vision_author, vision_publisher = (
                        self._vision_model.vision_to_candidate_data(vision_meta)
                    )
                    if vision_title and vision_author:
                        logger.info(
                            "Vision recovery (no OCR candidates): "
                            "trying lookup with vision title='%s' author='%s'",
                            vision_title,
                            vision_author,
                        )
                        vision_candidates = self._book_lookup.search_by_title_author(
                            vision_title, vision_author
                        )
                        if vision_candidates:
                            vision_ranked = self._ranker.rank(
                                vision_candidates,
                                ocr_title=vision_title,
                                ocr_author=vision_author,
                                ocr_isbn=None,
                                ocr_publisher=vision_publisher,
                            )
                            vision_scored = [
                                BookCandidate(
                                    metadata=bm,
                                    title_score=0.0,
                                    author_score=0.0,
                                    isbn_score=0.0,
                                    publisher_score=0.0,
                                    total_score=0.0,
                                    source=bm.source,
                                    isbn_state="UNAVAILABLE",
                                    title_available=True,
                                    author_available=True,
                                    publisher_available=bool(vision_publisher),
                                )
                                for bm in vision_candidates
                            ]
                            vision_verification = (
                                self._verification_service.verify_candidates(
                                    candidates=vision_scored,
                                    ocr_result=ocr_result,
                                    source_image_path=image_path,
                                    extracted_title=vision_title,
                                    extracted_author=vision_author,
                                    extracted_isbn=None,
                                    extracted_publisher=vision_publisher,
                                )
                            )
                            if vision_verification and vision_verification.all_evidence:
                                for cand, evidence in vision_verification.all_evidence:
                                    cand.isbn_state = evidence.isbn_state
                                    cand.isbn_score = evidence.isbn_score
                                    cand.title_score = evidence.title_score
                                    cand.author_score = evidence.author_score
                                    cand.publisher_score = evidence.publisher_score

                            vision_ranked = self._ranker.rank(
                                vision_candidates,
                                ocr_title=vision_title,
                                ocr_author=vision_author,
                                ocr_isbn=None,
                                ocr_publisher=vision_publisher,
                                evidence_candidates=vision_scored,
                            )
                            all_candidates = vision_candidates
                            ranked = vision_ranked
                            ocr_title = vision_title
                            ocr_author = vision_author
                            # Continue to ranking/confidence below instead of
                            # returning rejected
                        else:
                            # Vision extracted title+author but no metadata match
                            # Preserve vision result for manual review
                            best_metadata = BookMetadata(
                                title=vision_title,
                                authors=[vision_author] if vision_author else [],
                                publisher=vision_publisher,
                                source="vision_model",
                                confidence=vision_meta.title_confidence,
                                status="needs_review",
                                vision_title=vision_title,
                                vision_author=vision_author,
                                vision_publisher=vision_publisher,
                                vision_confidence=vision_meta.title_confidence,
                                vision_used=True,
                                ocr_text=ocr_result.text,
                                raw_ocr=normalized_text,
                                extracted_title=None,
                                extracted_author=None,
                                match_score=0.0,
                                candidate_sources=["vision_model"],
                                drive_file_id=drive_file_id,
                                job_id=job_id,
                            )
                            self._persist_metadata(best_metadata)
                            self._save_debug_artifacts(
                                job_id or "unknown",
                                ocr_result,
                                extracted,
                                [],
                                "needs_review_vision_no_match",
                            )
                            return best_metadata, []
                    else:
                        # Vision identified a book but couldn't extract title/author
                        logger.warning(
                            "Vision recovery: book identified but title/author "
                            "uncertain — creating minimal metadata for manual review"
                        )
                        minimal_meta = self._create_minimal_metadata(
                            ocr_title=ocr_title or "(unreadable)",
                            ocr_author=ocr_author,
                            ocr_text=ocr_result.text,
                            normalized_text=normalized_text,
                            ocr_title_candidates=ocr_title_candidates,
                            ocr_contributor=ocr_contributor,
                            ocr_isbn=ocr_isbn,
                            extracted_publisher=extracted.get("publisher"),
                            ocr_confidence=ocr_result.confidence,
                            candidate_sources=candidate_sources,
                            drive_file_id=drive_file_id,
                            job_id=job_id,
                            vision_meta=vision_meta,
                        )
                        self._persist_metadata(minimal_meta)
                        self._save_debug_artifacts(
                            job_id or "unknown",
                            ocr_result,
                            extracted,
                            [],
                            "needs_review_vision_uncertain",
                        )
                        return minimal_meta, []
                else:
                    # No vision available -- create minimal metadata
                    minimal_meta = self._create_minimal_metadata(
                        ocr_title=ocr_title or "(unreadable)",
                        ocr_author=ocr_author,
                        ocr_text=ocr_result.text,
                        normalized_text=normalized_text,
                        ocr_title_candidates=ocr_title_candidates,
                        ocr_contributor=ocr_contributor,
                        ocr_isbn=ocr_isbn,
                        extracted_publisher=extracted.get("publisher"),
                        ocr_confidence=ocr_result.confidence,
                        candidate_sources=candidate_sources,
                        drive_file_id=drive_file_id,
                        job_id=job_id,
                        vision_meta=None,
                    )
                    self._persist_metadata(minimal_meta)
                    self._save_debug_artifacts(
                        job_id or "unknown",
                        ocr_result,
                        extracted,
                        [],
                        "needs_review_no_candidates_no_vision",
                    )
                    logger.warning(
                        "No candidates, no vision available -- created "
                        "minimal metadata for manual review (drive_file_id=%s)",
                        drive_file_id,
                    )
                    return minimal_meta, []

            # 6. Rank all candidates against OCR fields
            ranked = self._ranker.rank(
                all_candidates,
                ocr_title=ocr_title,
                ocr_author=ocr_author,
                ocr_isbn=ocr_isbn,
                ocr_publisher=extracted.get("publisher"),
            )

            # 7. Compute evidence scores via verification service,
            #    then use ConfidenceService for final confidence + status.
            #    This keeps verification evidence decoupled from threshold logic.
            ocr_available = bool(ocr_result.text.strip())

            # Convert List[BookMetadata] to List[BookCandidate] for verification
            candidates = [
                BookCandidate(
                    metadata=bm,
                    title_score=0.0,
                    author_score=0.0,
                    isbn_score=0.0,
                    publisher_score=0.0,
                    total_score=0.0,
                    source=bm.source,
                    isbn_state="UNAVAILABLE",
                    title_available=True,
                    author_available=bool(ocr_author),
                    publisher_available=bool(extracted.get("publisher")),
                )
                for bm in all_candidates
            ]

            # Run verification to populate evidence scores on each candidate
            verification = self._verification_service.verify_candidates(
                candidates=candidates,
                ocr_result=ocr_result,
                source_image_path=preprocessed_path or image_path,
                extracted_title=ocr_title,
                extracted_author=ocr_author,
                extracted_isbn=ocr_isbn,
                extracted_publisher=extracted.get("publisher"),
            )

            # Propagate verification evidence (correct isbn_state) back onto
            # the candidate objects so the second rank() call does not overwrite
            # the correct isbn_state with the hardcoded "UNAVAILABLE" placeholder.
            if verification and verification.all_evidence:
                for cand, evidence in verification.all_evidence:
                    cand.isbn_state = evidence.isbn_state
                    cand.isbn_score = evidence.isbn_score
                    cand.title_score = evidence.title_score
                    cand.author_score = evidence.author_score
                    cand.publisher_score = evidence.publisher_score
                    if evidence.cover_similarity is not None:
                        cand.cover_similarity = float(
                            max(0.0, min(1.0, evidence.cover_similarity))
                        )

            # Use ranked candidates (now with populated evidence scores)
            # for confidence calculation via ConfidenceService
            ranked = self._ranker.rank(
                all_candidates,
                ocr_title=ocr_title,
                ocr_author=ocr_author,
                ocr_isbn=ocr_isbn,
                ocr_publisher=extracted.get("publisher"),
                evidence_candidates=candidates,
            )

            best_candidate, best_confidence, is_ambiguous = (
                self._confidence.get_best_candidate_with_margin(
                    ranked,
                    ocr_confidence=ocr_result.confidence,
                    ocr_available=ocr_available,
                )
            )

            if best_candidate is None:
                # NO OCR candidates found -- vision model is the FINAL ANALYSIS step.
                # Per the non-negotiable requirement, no discovered book may be
                # rejected -- every book must produce an extraction and be persisted.
                logger.info(
                    "No OCR candidates found for drive_file_id=%s -- "
                    "invoking vision model as final analysis",
                    drive_file_id,
                )
                vision_meta = self._vision_model.extract_vision_metadata(
                    image_path, drive_file_id
                )
                if vision_meta is not None and vision_meta.target_book_identified:
                    vision_title, vision_author, vision_publisher = (
                        self._vision_model.vision_to_candidate_data(vision_meta)
                    )
                    if vision_title and vision_author:
                        # Vision-only metadata -- preserve as needs_review
                        vision_only_meta = self._create_vision_only_metadata(
                            vision_title=vision_title,
                            vision_author=vision_author,
                            vision_publisher=vision_publisher,
                            vision_confidence=vision_meta.title_confidence,
                            ocr_title=ocr_title,
                            ocr_author=ocr_author,
                            ocr_text=ocr_result.text,
                            normalized_text=normalized_text,
                            ocr_title_candidates=ocr_title_candidates,
                            ocr_contributor=ocr_contributor,
                            ocr_isbn=ocr_isbn,
                            extracted_publisher=extracted.get("publisher"),
                            candidate_sources=candidate_sources,
                            drive_file_id=drive_file_id,
                            job_id=job_id,
                            image_path=image_path,
                        )
                        self._persist_metadata(vision_only_meta)
                        self._save_debug_artifacts(
                            job_id or "unknown",
                            ocr_result,
                            extracted,
                            ranked,
                            "vision_only_no_candidates",
                        )
                        logger.info(
                            "Vision-only extraction (no OCR candidates): '%s' "
                            "by '%s' -- persisted as needs_review",
                            vision_title,
                            vision_author,
                        )
                        return vision_only_meta, ranked
                # Vision failed or returned empty -- create minimal metadata
                # with OCR fields so the book is NOT rejected and reaches
                # needs_review for human inspection.
                minimal_meta = self._create_minimal_metadata(
                    ocr_title=ocr_title or "(unreadable)",
                    ocr_author=ocr_author,
                    ocr_text=ocr_result.text,
                    normalized_text=normalized_text,
                    ocr_title_candidates=ocr_title_candidates,
                    ocr_contributor=ocr_contributor,
                    ocr_isbn=ocr_isbn,
                    extracted_publisher=extracted.get("publisher"),
                    ocr_confidence=ocr_result.confidence,
                    candidate_sources=candidate_sources,
                    drive_file_id=drive_file_id,
                    job_id=job_id,
                    vision_meta=vision_meta,
                )
                self._persist_metadata(minimal_meta)
                self._save_debug_artifacts(
                    job_id or "unknown",
                    ocr_result,
                    extracted,
                    ranked,
                    "minimal_no_candidates",
                )
                logger.warning(
                    "No candidates and no vision extraction -- created minimal "
                    "metadata for manual review (drive_file_id=%s)",
                    drive_file_id,
                )
                return minimal_meta, ranked

            confidence = best_candidate.metadata.confidence
            status = self._confidence.get_status(confidence)

            # Attach all evidence fields
            best_candidate.metadata.raw_ocr = normalized_text
            best_candidate.metadata.ocr_text = ocr_result.text
            best_candidate.metadata.extracted_title = ocr_title
            best_candidate.metadata.extracted_title_candidates = ocr_title_candidates
            best_candidate.metadata.extracted_author = ocr_author
            best_candidate.metadata.extracted_contributor = ocr_contributor
            best_candidate.metadata.extracted_isbn = ocr_isbn
            best_candidate.metadata.extracted_publisher = extracted.get("publisher")
            best_candidate.metadata.match_score = best_candidate.total_score
            best_candidate.metadata.candidate_sources = candidate_sources
            best_candidate.metadata.drive_file_id = drive_file_id
            best_candidate.metadata.job_id = job_id
            best_candidate.metadata.status = status

            # Attach verification evidence to the best candidate's metadata
            # so it is persisted and available for auditing.
            best_evidence = None
            if verification and verification.best_evidence:
                best_evidence = verification.best_evidence
            if best_evidence:
                best_candidate.metadata.verification_title_score = (
                    best_evidence.title_score
                )
                best_candidate.metadata.verification_author_score = (
                    best_evidence.author_score
                )
                best_candidate.metadata.verification_isbn_state = (
                    best_evidence.isbn_state
                )
                best_candidate.metadata.verification_isbn_score = (
                    best_evidence.isbn_score
                )
                best_candidate.metadata.verification_cover_similarity = (
                    best_evidence.cover_similarity
                )

            logger.info(
                "Identification complete: '%s' (confidence=%.4f, status=%s, "
                "ambiguous=%s, isbn_state=%s)",
                best_candidate.metadata.title,
                confidence,
                status,
                is_ambiguous,
                best_candidate.isbn_state,
            )

            # 8. Title/author overlap validation (reject false positives)
            rejection_reason = self._validate_overlap(best_candidate.metadata)
            if rejection_reason:
                best_candidate.metadata.rejection_reason = rejection_reason
                best_candidate.metadata.status = "needs_review"
                logger.warning(
                    "Overlap validation flagged mismatch: %s "
                    "(OCR title=%s vs API title=%s)",
                    rejection_reason,
                    ocr_title,
                    best_candidate.metadata.title,
                )
            else:
                best_candidate.metadata.rejection_reason = None

            # 8b. Check for contradictory evidence from verification
            # A genuine ISBN conflict (OCR ISBN != candidate ISBN) is
            # contradictory evidence — it means the candidate is the wrong book.
            # UNAVAILABLE is NOT contradictory (missing data is not a mismatch).
            verification_rejection_reason = None
            if best_candidate.isbn_state == "CONFLICT":
                verification_rejection_reason = "isbn_conflict"
                best_candidate.metadata.status = "needs_review"
                logger.warning(
                    "ISBN conflict detected: OCR ISBN=%s, candidate ISBN=%s"
                    " — not auto-accepting",
                    ocr_isbn,
                    best_candidate.metadata.isbn_10 or best_candidate.metadata.isbn_13,
                )
            # Use the first rejection reason found (overlap takes priority)
            if not rejection_reason:
                rejection_reason = verification_rejection_reason

            vision_only_preserved = False
            vision_invoked = False  # Will be set True when vision model is called
            ocr_title_suspicious = self._is_suspicious_ocr_title(ocr_title)

            # 8c. Vision model FINAL ANALYSIS: always invoked for every book
            # to extract the best available title/author/publisher directly
            # from the image pixels. Vision results are NEVER auto-accepted
            # on their own — they must pass the same confidence threshold (0.85)
            # and metadata verification. Vision is ALWAYS called as the final
            # analysis step, per the non-negotiable pipeline requirement.
            if True:  # Vision always runs as FINAL ANALYSIS
                vision_invoked = True
                vision_title: Optional[str] = None
                vision_author: Optional[str] = None
                vision_publisher: Optional[str] = None
                vision_meta = self._vision_model.extract_vision_metadata(
                    image_path, drive_file_id, current_confidence=confidence
                )
                if vision_meta is not None and vision_meta.target_book_identified:
                    vision_title, vision_author, vision_publisher = (
                        self._vision_model.vision_to_candidate_data(vision_meta)
                    )
                    # Mark vision as used on the best candidate (FINAL ANALYSIS)
                    best_candidate.metadata.vision_used = True
                    best_candidate.metadata.candidate_sources = list(
                        best_candidate.metadata.candidate_sources
                    ) + ["vision_model"]
                    # Store vision extraction on metadata for auditing
                    best_candidate.metadata.vision_title = vision_title
                    best_candidate.metadata.vision_author = vision_author
                    best_candidate.metadata.vision_publisher = vision_publisher
                    best_candidate.metadata.vision_confidence = (
                        vision_meta.title_confidence
                    )

                    # If vision has strong title+author but OCR result is
                    # unreliable (no candidates, low confidence (<0.50),
                    # suspicious title, or missing author/title), use vision
                    # results for a fresh metadata lookup.
                    if ocr_title_suspicious:
                        logger.info(
                            "Suspicious OCR title detected: '%s' — vision must "
                            "override",
                            ocr_title,
                        )
                    if (
                        vision_title
                        and vision_author
                        and (
                            not all_candidates
                            or (confidence is not None and confidence < 0.50)
                            or rejection_reason is not None
                            or ocr_title_suspicious  # vision overrides suspicious OCR
                            or best_candidate.metadata.status
                            != "identified"  # grey-zone recovery
                        )
                    ):
                        logger.info(
                            "Vision recovery: using vision-extracted '%s' by '%s' "
                            "for fresh metadata lookup",
                            vision_title,
                            vision_author,
                        )
                        vision_candidates = self._book_lookup.search_by_title_author(
                            vision_title, vision_author
                        )
                        if vision_candidates:
                            # Re-rank with vision-extracted fields
                            vision_ranked = self._ranker.rank(
                                vision_candidates,
                                ocr_title=vision_title,
                                ocr_author=vision_author,
                                ocr_isbn=None,
                                ocr_publisher=vision_publisher,
                            )
                            vision_scored = [
                                BookCandidate(
                                    metadata=bm,
                                    title_score=0.0,
                                    author_score=0.0,
                                    isbn_score=0.0,
                                    publisher_score=0.0,
                                    total_score=0.0,
                                    source=bm.source,
                                    isbn_state="UNAVAILABLE",
                                    title_available=True,
                                    author_available=True,
                                    publisher_available=bool(vision_publisher),
                                )
                                for bm in vision_candidates
                            ]
                            vision_verification = (
                                self._verification_service.verify_candidates(
                                    candidates=vision_scored,
                                    ocr_result=ocr_result,
                                    source_image_path=image_path,
                                    extracted_title=vision_title,
                                    extracted_author=vision_author,
                                    extracted_isbn=None,
                                    extracted_publisher=vision_publisher,
                                )
                            )
                            if vision_verification and vision_verification.all_evidence:
                                for cand, evidence in vision_verification.all_evidence:
                                    cand.isbn_state = evidence.isbn_state
                                    cand.isbn_score = evidence.isbn_score
                                    cand.title_score = evidence.title_score
                                    cand.author_score = evidence.author_score
                                    cand.publisher_score = evidence.publisher_score

                            vision_ranked = self._ranker.rank(
                                vision_candidates,
                                ocr_title=vision_title,
                                ocr_author=vision_author,
                                ocr_isbn=None,
                                ocr_publisher=vision_publisher,
                                evidence_candidates=vision_scored,
                            )

                            vision_best, vision_conf, _ = (
                                self._confidence.get_best_candidate_with_margin(
                                    vision_ranked,
                                    ocr_confidence=vision_meta.title_confidence,
                                    ocr_available=True,
                                )
                            )

                            if vision_best is not None and vision_conf > confidence:
                                logger.info(
                                    "Vision recovery improved result: "
                                    "confidence %.4f > %.4f",
                                    vision_conf,
                                    confidence,
                                )
                                best_candidate = vision_best
                                confidence = vision_conf
                                status = self._confidence.get_status(confidence)
                                best_candidate.metadata.status = status
                                best_candidate.metadata.vision_used = True
                                # Re-attach evidence fields
                                best_candidate.metadata.raw_ocr = normalized_text
                                best_candidate.metadata.ocr_text = ocr_result.text
                                best_candidate.metadata.extracted_title = ocr_title
                                best_candidate.metadata.extracted_title_candidates = (
                                    ocr_title_candidates
                                )
                                best_candidate.metadata.extracted_author = ocr_author
                                best_candidate.metadata.extracted_contributor = (
                                    ocr_contributor
                                )
                                best_candidate.metadata.extracted_isbn = ocr_isbn
                                best_candidate.metadata.extracted_publisher = (
                                    extracted.get("publisher")
                                )
                                best_candidate.metadata.match_score = (
                                    best_candidate.total_score
                                )
                                best_candidate.metadata.candidate_sources = (
                                    candidate_sources + ["vision_model"]
                                )
                                best_candidate.metadata.drive_file_id = drive_file_id
                                best_candidate.metadata.job_id = job_id
                                # Vision-only results: do NOT auto-accept.
                                # Require explicit metadata verification match.
                                vision_title_sim = (
                                    fuzz.token_sort_ratio(
                                        vision_title.lower(),
                                        best_candidate.metadata.title.lower(),
                                    )
                                    / 100.0
                                )
                                if vision_title_sim < 0.70:
                                    logger.warning(
                                        "Vision title '%s' vs candidate '%s' "
                                        "similarity %.4f < 0.70 — needs review",
                                        vision_title,
                                        best_candidate.metadata.title,
                                        vision_title_sim,
                                    )
                                    best_candidate.metadata.status = "needs_review"
                                    status = "needs_review"
                                    confidence = min(confidence, 0.70)
                        else:
                            # Vision extracted a strong title+author but no
                            # metadata match exists. Preserve the vision-only
                            # extraction as needs_review rather than falling
                            # back to the unreliable OCR-based candidate.
                            logger.info(
                                "Vision recovery: no metadata match for '%s' "
                                "by '%s' — preserving vision extraction as "
                                "needs_review",
                                vision_title,
                                vision_author,
                            )
                            vision_meta_obj = BookMetadata(
                                title=vision_title,
                                authors=[vision_author] if vision_author else [],
                                publisher=vision_publisher,
                                source="vision_model",
                                confidence=vision_meta.title_confidence,
                                status="needs_review",
                                vision_title=vision_title,
                                vision_author=vision_author,
                                vision_publisher=vision_publisher,
                                vision_confidence=vision_meta.title_confidence,
                                vision_used=True,
                                ocr_text=ocr_result.text,
                                raw_ocr=normalized_text,
                                extracted_title=ocr_title,
                                extracted_author=ocr_author,
                                match_score=0.0,
                                candidate_sources=candidate_sources + ["vision_model"],
                                drive_file_id=drive_file_id,
                                job_id=job_id,
                                rejection_reason=rejection_reason,
                            )
                            best_candidate.metadata = vision_meta_obj
                            best_candidate.metadata.vision_used = True
                            confidence = vision_meta.title_confidence
                            status = "needs_review"
                            # Vision-only preservation: return the vision
                            # extraction metadata so it reaches the worker
                            # for review/export instead of being discarded.
                            vision_only_preserved = True

            # Vision was invoked as FINAL ANALYSIS — mark it on metadata
            if vision_invoked:
                best_candidate.metadata.vision_used = True
                best_candidate.metadata.candidate_sources = list(
                    best_candidate.metadata.candidate_sources
                ) + ["vision_model"]

            # Vision OVERRIDES OCR: when the vision model extracted a valid
            # title+author, it is the final visual extraction authority.
            # Replace the metadata's title/authors/publisher with vision-extracted
            # values regardless of OCR confidence. Vision never invented values —
            # use null where a field cannot be reliably determined.
            if (
                vision_title
                and vision_author
                # vision_only_preserved already sets title from vision
                and not vision_only_preserved
            ):
                logger.info(
                    "Vision OVERRIDING suspicious OCR title '%s' with '%s'",
                    ocr_title,
                    vision_title,
                )
                best_candidate.metadata.title = vision_title
                best_candidate.metadata.authors = [vision_author]
                if vision_publisher:
                    best_candidate.metadata.publisher = vision_publisher
                # Strip trailing visual/OCR artifacts from title
                if best_candidate.metadata.title:
                    best_candidate.metadata.title = (
                        best_candidate.metadata.title.rstrip().rstrip("-- — —").rstrip()
                    )

            # 9. Persist metadata if repository is available
            self._persist_metadata(best_candidate.metadata)

            # 10. Save debug artifacts
            self._save_debug_artifacts(
                job_id or "unknown",
                ocr_result,
                extracted,
                ranked,
                best_candidate.metadata.status,
            )

            # Make decision based on confidence and validation
            if vision_only_preserved:
                # Vision-only preservation: OCR result was suspicious but
                # vision extracted a valid title+author with no metadata match.
                # Return the vision extraction so it reaches review/export.
                return best_candidate.metadata, ranked
            elif rejection_reason:
                # Overlap validation detected a contradiction — no auto-accept.
                # The candidate IS persisted with needs_review (never rejected).
                best_candidate.metadata.status = "needs_review"
                self._persist_metadata(best_candidate.metadata)
                return best_candidate.metadata, ranked
            elif self._confidence.is_auto_accepted(confidence):
                return best_candidate.metadata, ranked
            elif self._confidence.is_manual_review(confidence):
                # Confidence between manual review and auto-accept thresholds.
                # Persisted with needs_review (never rejected).
                logger.warning(
                    "Confidence %.4f below auto-accept threshold — manual review",
                    confidence,
                )
                best_candidate.metadata.status = "needs_review"
                self._persist_metadata(best_candidate.metadata)
                return best_candidate.metadata, ranked
            else:
                # Confidence below manual review threshold.
                # Persisted with needs_review (never rejected).
                logger.warning(
                    "Confidence %.4f below accept threshold — needs_review "
                    "(not rejected per non-negotiable requirement)",
                    confidence,
                )
                best_candidate.metadata.status = "needs_review"
                best_candidate.metadata.rejection_reason = (
                    best_candidate.metadata.rejection_reason or "low_confidence"
                )
                self._persist_metadata(best_candidate.metadata)
                return best_candidate.metadata, ranked

        finally:
            if preprocessed_path is not None:
                self._preprocessor.cleanup(preprocessed_path)

    def _create_vision_only_metadata(
        self,
        vision_title: str,
        vision_author: str,
        vision_publisher: Optional[str],
        vision_confidence: float,
        ocr_title: Optional[str],
        ocr_author: Optional[str],
        ocr_text: str,
        normalized_text: str,
        ocr_title_candidates: List[str],
        ocr_contributor: Optional[str],
        ocr_isbn: Optional[str],
        extracted_publisher: Optional[str],
        candidate_sources: List[str],
        drive_file_id: str,
        job_id: str,
        image_path: str,
    ) -> BookMetadata:
        """Create a BookMetadata from vision-only extraction (no API match).

        The vision model is the source of truth for cover extraction. When
        vision produces a valid title+author but no API match exists, this
        creates a metadata entry that goes to needs_review for human verification.
        """
        return BookMetadata(
            title=vision_title,
            authors=[vision_author] if vision_author else [],
            publisher=vision_publisher,
            confidence=vision_confidence,
            source="vision_model",
            status="needs_review",
            raw_ocr=normalized_text or ocr_text,
            ocr_text=ocr_text,
            extracted_title=ocr_title,
            extracted_title_candidates=ocr_title_candidates,
            extracted_author=ocr_author,
            extracted_contributor=ocr_contributor,
            extracted_isbn=ocr_isbn,
            extracted_publisher=extracted_publisher,
            match_score=0.0,
            candidate_sources=candidate_sources + ["vision_model"],
            drive_file_id=drive_file_id,
            job_id=job_id,
            rejection_reason="low_confidence",
            vision_title=vision_title,
            vision_author=vision_author,
            vision_publisher=vision_publisher,
            vision_confidence=vision_confidence,
            vision_used=True,
        )

    def _is_suspicious_ocr_title(self, title: Optional[str]) -> bool:
        """Detect OCR titles that are garbage and should be overridden by vision.

        Examples of suspicious OCR:
        - Pure digit/operator sequences: '1+1+1=1', '1 1 1 1'
        - Character sequences with excessive symbols: '@ @EJOPOB', 'H. @ @'
        - Extremely short fragments that look like noise
        - Strings containing impossible substitutions

        Vision model is the authority for visual extraction — when OCR
        produces clearly unreadable output, vision results override.
        """
        if not title:
            return False
        t = title.strip()
        if not t:
            return False
        # Check for obvious OCR garbage patterns
        # Pattern: mostly digits and operators with little alphabetic content
        # e.g., '1+1+1=1', '1 1 1 1', '6 H. @ @EJOPOB'
        alpha_chars = sum(1 for c in t if c.isalpha())
        non_alnum_symbols = sum(1 for c in t if not c.isalnum() and not c.isspace())
        total_chars = len(t)
        # If less than 30% alphabetic and has operator/symbol noise, suspicious
        if (
            total_chars >= 4
            and alpha_chars / total_chars < 0.30
            and non_alnum_symbols >= 2
        ):
            return True
        # Pure digit+operator strings
        if t.replace("+", "").replace("=", "").replace(".", "").isdigit() and any(
            c in t for c in "+="
        ):
            return True
        # Contains @ symbol in title (OCR noise)
        if "@" in t and alpha_chars < 3:
            return True
        return False

    def _create_minimal_metadata(
        self,
        ocr_title: str,
        ocr_author: Optional[str],
        ocr_text: str,
        normalized_text: str,
        ocr_title_candidates: List[str],
        ocr_contributor: Optional[str],
        ocr_isbn: Optional[str],
        extracted_publisher: Optional[str],
        ocr_confidence: Optional[float],
        candidate_sources: List[str],
        drive_file_id: str,
        job_id: str,
        vision_meta=None,
        vision_attempted: bool = True,
    ) -> BookMetadata:
        """Create a minimal BookMetadata when no candidates and no vision.

        Per the non-negotiable requirement, no discovered book may be rejected.
        Even when OCR produces garbage and vision fails, we create a metadata
        entry with whatever fields are available so the book reaches
        needs_review for human inspection.
        """
        v_title = None
        v_author = None
        v_publisher = None
        v_confidence = 0.0
        # Vision was invoked (FINAL ANALYSIS step). Mark vision_used=True
        # whenever vision_meta is not None (model was called and returned
        # a response, even if target_book_identified is False).
        v_used = False
        if vision_meta is not None:
            v_title, v_author, v_publisher = (
                self._vision_model.vision_to_candidate_data(vision_meta)
            )
            v_confidence = vision_meta.title_confidence
            v_used = True
        # If vision was attempted but returned None, still mark vision_used=True
        # (the model was called, it just didn't produce usable results)
        if vision_attempted and not v_used:
            v_used = True

        return BookMetadata(
            title=ocr_title,
            authors=[ocr_author] if ocr_author else [],
            publisher=extracted_publisher,
            confidence=float(ocr_confidence or 0.0),
            source="ocr_fallback",
            status="needs_review",
            raw_ocr=normalized_text or ocr_text,
            ocr_text=ocr_text,
            extracted_title=ocr_title if ocr_title != "(unreadable)" else None,
            extracted_title_candidates=ocr_title_candidates,
            extracted_author=ocr_author,
            extracted_contributor=ocr_contributor,
            extracted_isbn=ocr_isbn,
            extracted_publisher=extracted_publisher,
            match_score=0.0,
            candidate_sources=candidate_sources,
            drive_file_id=drive_file_id,
            job_id=job_id,
            rejection_reason="no_candidates_vision_attempted",
            vision_title=v_title,
            vision_author=v_author,
            vision_publisher=v_publisher,
            vision_confidence=v_confidence,
            vision_used=v_used,
        )

    def _find_best_title_match(
        self, candidates: List[BookMetadata], ocr_title: str
    ) -> Optional[BookMetadata]:
        """Find the candidate whose title best matches the OCR title.

        Used to decide whether to try alternate title candidates.
        Returns the best-matching candidate or None if no good match.
        """
        from rapidfuzz import fuzz

        if not candidates or not ocr_title:
            return None
        best_candidate = None
        best_score = 0.0
        for candidate in candidates:
            if candidate.title:
                sim = (
                    fuzz.token_sort_ratio(ocr_title.lower(), candidate.title.lower())
                    / 100.0
                )
                if sim > best_score:
                    best_score = sim
                    best_candidate = candidate
        # Consider it a "good match" if similarity >= 0.5
        if best_score >= 0.5:
            return best_candidate
        return None

    def _is_suspicious_title(self, title: str) -> bool:
        """Check if a title candidate is suspicious (likely boilerplate).

        Returns True if the title matches known contributor phrases,
        publication keywords, or other non-title patterns.
        """
        if not title:
            return False
        lower = title.lower().strip()
        # Import the extractor to check contributor phrases
        from services.book_text_extractor import BookTextExtractor

        extractor = BookTextExtractor()
        if extractor._is_contributor_phrase(lower):
            return True
        # Check for publication keywords
        if extractor._is_publication_line(lower):
            return True
        # Single word that's a common function word
        single_word_titles = {"et", "de", "la", "le", "les", "du", "des", "par", "pour"}
        words = lower.split()
        if len(words) == 1 and words[0] in single_word_titles:
            return True
        return False

    def _validate_overlap(self, metadata: BookMetadata) -> Optional[str]:
        """Validate that OCR-extracted fields overlap with the API result.

        Returns a rejection reason string if the match is clearly wrong,
        or None if the match is acceptable.
        """
        from rapidfuzz import fuzz

        # If we had ISBN from OCR and it matches the API result, skip title check
        if metadata.extracted_isbn:
            api_isbns = []
            if metadata.isbn_10:
                api_isbns.append(metadata.isbn_10.replace("-", ""))
            if metadata.isbn_13:
                api_isbns.append(metadata.isbn_13.replace("-", ""))
            if any(
                metadata.extracted_isbn.replace("-", "") == isbn for isbn in api_isbns
            ):
                logger.info("ISBN match validated — skipping title overlap check")
                return None

        # Check title overlap
        if metadata.extracted_title and metadata.title:
            # If the extracted title itself is suspicious (boilerplate),
            # reject immediately regardless of similarity
            if self._is_suspicious_title(metadata.extracted_title):
                return "suspicious_title"

            title_sim = (
                fuzz.token_sort_ratio(
                    metadata.extracted_title.lower(),
                    metadata.title.lower(),
                )
                / 100.0
            )
            if title_sim < 0.3:
                return f"title_mismatch_{title_sim:.2f}"

        # Check author overlap (if OCR had author and API has authors)
        if metadata.extracted_author and metadata.authors:
            best_author_sim = max(
                (
                    fuzz.token_sort_ratio(
                        metadata.extracted_author.lower(),
                        a.lower(),
                    )
                    / 100.0
                    for a in metadata.authors
                ),
                default=0.0,
            )
            if best_author_sim < 0.2:
                return f"author_mismatch_{best_author_sim:.2f}"

        return None

    def _try_orientation_correction(
        self, image_path: str, ocr_result: OCRResult
    ) -> OCRResult:
        """Attempt orientation detection and re-OCR when the initial OCR
        quality is suspiciously poor.

        This is an **opt-in fallback** — invoked only when the initial OCR
        produces very few words or extremely low confidence, which are
        tell-tale signs of a physically rotated image whose EXIF orientation
        tag is absent (EXIF-based rotation in ImagePreprocessor cannot fix
        this).

        Strategy:
        1. If OCR quality is sufficient, return immediately (no cost).
        2. If OCR is poor (few words / low confidence):
           a. Run the fast visual heuristic to get a candidate angle.
           b. ALWAYS try ALL 4 rotations (0°, 90°, 180°, 270°) with OCR and
              pick the one with the highest combined score (words * avg_conf).
           c. Word count alone is unreliable — garbage at a wrong rotation can
              produce more "words" than the correct one. The combined score
              (words * confidence) weights readability quality.
        3. Only use the corrected result if it produces more words than
           the original.

        The original downloaded image is never modified.
        """
        word_count = len(ocr_result.words)
        avg_conf = ocr_result.confidence

        # Heuristic: fewer than 3 words OR avg confidence below 0.30
        # strongly suggests rotation.  This avoids the 4x rotation cost
        # for the normal (correctly oriented) case.
        if word_count >= 3 and avg_conf >= 0.30:
            logger.debug(
                "OCR quality sufficient (words=%d, conf=%.4f) -- "
                "skipping orientation detection",
                word_count,
                avg_conf,
            )
            return ocr_result

        logger.info(
            "OCR quality poor (words=%d, conf=%.4f) -- "
            "attempting orientation detection",
            word_count,
            avg_conf,
        )

        # Load the original image for orientation analysis.
        try:
            import PIL.Image as _PILImage  # noqa: WPS433

            img = _PILImage.open(image_path).convert("RGB")
        except Exception as exc:
            logger.warning("Could not load image for orientation: %s", exc)
            return ocr_result

        # Fast visual heuristic to get a candidate angle (for logging/debug).
        orientation = self._preprocessor.detect_orientation(img)

        # Run the heuristic angle first (potential early win).
        candidates = [(0, ocr_result)]
        if orientation.was_corrected:
            corrected_pil = self._preprocessor.correct_orientation(img, orientation)
            corrected_ocr = self._ocr_on_image(
                corrected_pil, image_path, orientation.angle
            )
            candidates.append((orientation.angle, corrected_ocr))
            logger.info(
                "Heuristic suggested angle %d, OCR: %d words, conf=%.4f",
                orientation.angle,
                len(corrected_ocr.words),
                corrected_ocr.confidence,
            )

        # Exhaustive OCR-based rotation search across ALL 4 angles.
        # Word count alone is unreliable (garbage at wrong rotation can have
        # more "words"), so we use a combined score: words * avg_confidence.
        # This ensures readability quality, not just token count, drives the pick.
        best_combined = word_count * avg_conf
        best_ocr = ocr_result
        best_angle = 0
        for angle in (90, 180, 270):
            if angle in (a for a, _ in candidates):
                continue  # Already tried via heuristic.
            rotated = self._preprocessor._rotate_image(img, angle)
            angle_ocr = self._ocr_on_image(rotated, image_path, angle)
            angle_words = len(angle_ocr.words)
            angle_conf = angle_ocr.confidence
            angle_combined = angle_words * angle_conf
            logger.debug(
                "Rotation %d: %d words, conf=%.4f, combined=%.4f",
                angle,
                angle_words,
                angle_conf,
                angle_combined,
            )
            if angle_combined > best_combined:
                best_combined = angle_combined
                best_ocr = angle_ocr
                best_angle = angle

        # Also compare heuristic candidate against exhaustive results.
        for angle, angle_ocr in candidates:
            if angle == 0:
                continue  # Skip original.
            angle_combined = len(angle_ocr.words) * angle_ocr.confidence
            if angle_combined > best_combined:
                best_combined = angle_combined
                best_ocr = angle_ocr
                best_angle = angle

        if best_angle != 0 and len(best_ocr.words) > word_count:
            logger.info(
                "Best rotation %d improved OCR: %d -> %d words, conf %.4f -> %.4f",
                best_angle,
                word_count,
                len(best_ocr.words),
                avg_conf,
                best_ocr.confidence,
            )
            return self._attach_orientation(best_ocr, best_angle)

        logger.info("No orientation improved OCR; keeping original")
        return ocr_result

    def _ocr_on_image(self, pil_img: Any, image_path: str, angle: int) -> OCRResult:
        """Save a PIL image to a temp file and run OCR on it."""
        corrected_path = os.path.join(
            self._preprocessor._temp_dir,  # noqa: SLF001
            f"oriented_{angle}_{os.path.basename(image_path)}",
        )
        pil_img.save(corrected_path, "JPEG", quality=95)
        self._ocr_call_count += 1
        return self._ocr_service.extract_text(corrected_path)

    # Aspect ratio (width / height) above which a page is treated as
    # "landscape" and therefore likely rotated relative to a portrait cover.
    LANDSCAPE_ASPECT_THRESHOLD = 1.15

    def _apply_geometry_orientation_correction(
        self, image_path: str, ocr_result: OCRResult
    ) -> OCRResult:
        """Rotate an obviously landscape page before trusting its OCR text.

        The previous behaviour only re-oriented images when OCR quality was
        poor, so a sideways cover that produced a few plausible-looking words
        was accepted unrotated. This check is independent of OCR quality:
        if the image is landscape and line detection says the text runs
        vertically, one corrected OCR pass is made and kept only when it
        scores better (words x confidence). Bounded: at most one extra OCR.
        """
        try:
            import PIL.Image as _PILImage  # noqa: WPS433

            with _PILImage.open(image_path) as probe:
                width, height = probe.size
            if not height or (width / height) < self.LANDSCAPE_ASPECT_THRESHOLD:
                return ocr_result

            img = _PILImage.open(image_path).convert("RGB")
            orientation = self._preprocessor.detect_orientation(img)
            if not orientation.was_corrected or orientation.angle == 0:
                return ocr_result

            corrected = self._preprocessor.correct_orientation(img, orientation)
            corrected_ocr = self._ocr_on_image(corrected, image_path, orientation.angle)
            logger.info(
                "Landscape page: geometry suggested %d deg, OCR %d -> %d words "
                "(conf %.4f -> %.4f)",
                orientation.angle,
                len(ocr_result.words),
                len(corrected_ocr.words),
                ocr_result.confidence,
                corrected_ocr.confidence,
            )
            current_score = len(ocr_result.words) * ocr_result.confidence
            corrected_score = len(corrected_ocr.words) * corrected_ocr.confidence
            if corrected_score > current_score:
                return self._attach_orientation(corrected_ocr, orientation.angle)
        except Exception as exc:
            logger.warning("Geometry orientation check skipped: %s", exc)
        return ocr_result

    @staticmethod
    def _attach_orientation(ocr_result: OCRResult, angle: int) -> OCRResult:
        """Attach orientation metadata to an OCRResult for logging."""
        try:
            ocr_result.orientation_angle = angle  # type: ignore
            ocr_result.orientation_corrected = True  # type: ignore
        except Exception as exc:
            # Orientation is diagnostic only; failing to attach it must not
            # fail OCR, but silently losing it hid a frozen-dataclass bug.
            logger.debug("Could not attach orientation metadata: %s", exc)
        return ocr_result

    def _save_debug_artifacts(
        self,
        job_id: str,
        ocr_result: OCRResult,
        extracted: dict,
        ranked: List[BookCandidate],
        final_status: str,
    ) -> None:
        """Save debug artifacts for manual inspection.

        Creates data/debug/job_id/ containing:
        - ocr_raw.txt: Raw OCR text
        - ocr_processed.txt: Normalized OCR text + extracted fields
        - candidate_results.json: All ranked candidates with scores
        - final_decision.json: Final decision and status
        """
        try:
            debug_dir = os.path.join(self._data_directory, "debug", job_id)
            os.makedirs(debug_dir, exist_ok=True)

            # OCR raw text
            with open(
                os.path.join(debug_dir, "ocr_raw.txt"), "w", encoding="utf-8"
            ) as f:
                f.write(ocr_result.text)

            # OCR processed text + extracted fields
            processed = (
                "=== Normalized OCR ===\n"
                f"{self._normalizer.normalize(ocr_result.text)}\n\n"
                "=== Extracted Fields ===\n"
                f"title: {extracted.get('title')}\n"
                f"title_candidates: {extracted.get('title_candidates', [])}\n"
                f"author: {extracted.get('author')}\n"
                f"contributor: {extracted.get('contributor')}\n"
                f"isbn: {extracted.get('isbn') or extracted.get('isbn')}\n"
                f"publisher: {extracted.get('publisher')}\n"
                f"edition: {extracted.get('edition')}\n"
                f"language: {extracted.get('language')}\n"
            )
            with open(
                os.path.join(debug_dir, "ocr_processed.txt"), "w", encoding="utf-8"
            ) as f:
                f.write(processed)

            # Candidate results
            candidates_data = []
            for c in ranked:
                m = c.metadata
                candidates_data.append(
                    {
                        "title": m.title,
                        "authors": m.authors,
                        "isbn_10": m.isbn_10,
                        "isbn_13": m.isbn_13,
                        "publisher": m.publisher,
                        "source": m.source,
                        "isbn_state": c.isbn_state,
                        "title_score": c.title_score,
                        "author_score": c.author_score,
                        "isbn_score": c.isbn_score,
                        "publisher_score": c.publisher_score,
                        "total_score": c.total_score,
                        "confidence": m.confidence,
                    }
                )
            with open(
                os.path.join(debug_dir, "candidate_results.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(candidates_data, f, indent=2, default=str)

            # Final decision
            decision = {
                "job_id": job_id,
                "final_status": final_status,
                "best_title": ranked[0].metadata.title if ranked else None,
                "best_confidence": ranked[0].metadata.confidence if ranked else None,
                "ocr_title": extracted.get("title"),
                "ocr_title_candidates": extracted.get("title_candidates", []),
                "ocr_author": extracted.get("author"),
                "ocr_contributor": extracted.get("contributor"),
                "ocr_isbn": extracted.get("isbn"),
            }
            with open(
                os.path.join(debug_dir, "final_decision.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(decision, f, indent=2, default=str)

            logger.info("Debug artifacts saved to %s", debug_dir)

        except Exception as exc:
            logger.warning("Failed to save debug artifacts: %s", exc)

    def cleanup_old_debug_artifacts(self, retention_days: int = 3) -> int:
        """Remove debug artifact directories older than the retention period.

        Debug artifacts are saved in ``data/debug/<job_id>/``. This method
        walks the debug directory and removes any job directory whose
        contents are older than ``retention_days`` days.

        Args:
            retention_days: Number of days to retain debug artifacts.
                Set to 0 to disable cleanup (keep everything).

        Returns:
            Number of directories removed.
        """
        if retention_days <= 0:
            logger.info("Debug retention cleanup disabled (retention_days <= 0)")
            return 0

        debug_dir = os.path.join(self._data_directory, "debug")
        if not os.path.isdir(debug_dir):
            logger.debug("Debug directory does not exist: %s", debug_dir)
            return 0

        # Resolve once so the per-entry containment check compares real paths
        # (this also neutralises a symlinked or ``..``-laden entry).
        debug_base = os.path.realpath(debug_dir)
        if debug_base in (os.path.realpath(os.sep), os.path.realpath(os.getcwd())):
            logger.error(
                "Refusing debug cleanup: debug directory resolved to %s", debug_base
            )
            return 0

        removed = 0
        cutoff_seconds = retention_days * 86400  # seconds per day
        current_time = time.time()

        for entry in os.listdir(debug_dir):
            job_dir = os.path.join(debug_dir, entry)
            if not os.path.isdir(job_dir):
                continue
            # Only ever delete a direct child of data/debug. Without this a
            # misconfigured or traversal-style entry could delete data/covers,
            # the data root, or anything else the process can write to.
            real_job_dir = os.path.realpath(job_dir)
            if os.path.dirname(real_job_dir) != debug_base:
                logger.warning(
                    "Refusing to remove %s: not a direct child of %s",
                    real_job_dir,
                    debug_base,
                )
                continue
            try:
                dir_mtime = os.path.getmtime(real_job_dir)
                age_seconds = current_time - dir_mtime
                if age_seconds > cutoff_seconds:
                    # Do not pass ignore_errors: a failed removal must not be
                    # reported as a successful one.
                    shutil.rmtree(real_job_dir)
                    removed += 1
                    logger.info("Removed old debug artifacts: %s", real_job_dir)
            except Exception as exc:
                logger.warning(
                    "Failed to check/remove debug dir %s: %s",
                    real_job_dir,
                    exc,
                )

        if removed > 0:
            logger.info(
                "Debug retention cleanup: removed %d old directory(ies)",
                removed,
            )
        else:
            logger.debug("Debug retention cleanup: no old artifacts found")

        return removed

    def _persist_metadata(self, metadata: BookMetadata) -> None:
        """Persist metadata to the repository.

        If persistence fails, raises an exception so the worker can
        mark the job as FAILED (no partial writes — upsert is atomic).

        Also creates an export queue entry (Milestone 5) so that the
        ExportWorker picks up the record for publication.
        """
        # Same-book gate: a different photograph of a book that is already
        # stored must not create a second book row or a second Sheet row.
        verdict = self._find_same_book(metadata)
        if verdict.is_certain and verdict.matched_drive_file_id:
            logger.info(
                "Same book already stored as Drive ID %s (%s) -- "
                "not creating a second record",
                verdict.matched_drive_file_id,
                verdict.reason,
            )
            if self._duplicate_repo is not None:
                self._duplicates_skipped += 1
                self._duplicate_repo.record_duplicate(
                    drive_file_id=metadata.drive_file_id,
                    match_kind=DUPLICATE_SAME_BOOK,
                    original_drive_file_id=verdict.matched_drive_file_id,
                    content_hash=None,
                    detail=verdict.reason,
                )
            metadata.status = "duplicate"
            metadata.rejection_reason = f"same_book_as:{verdict.matched_drive_file_id}"
            return
        if verdict.is_possible and verdict.matched_drive_file_id:
            logger.warning(
                "Possible same book as Drive ID %s (%s) -- recorded for "
                "review, record still created",
                verdict.matched_drive_file_id,
                verdict.reason,
            )
            if self._duplicate_repo is not None:
                self._duplicate_repo.record_duplicate(
                    drive_file_id=metadata.drive_file_id,
                    match_kind=DUPLICATE_POSSIBLE_SAME_BOOK,
                    original_drive_file_id=verdict.matched_drive_file_id,
                    detail=verdict.reason,
                )

        if self._metadata_repo is None:
            logger.warning(
                "No metadata repository configured; skipping persistence "
                "for drive_file_id=%s",
                metadata.drive_file_id,
            )
            return
        try:
            self._metadata_repo.upsert_metadata(metadata)
            logger.info(
                "Metadata persisted for drive_file_id=%s, job_id=%s",
                metadata.drive_file_id,
                metadata.job_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to persist metadata for drive_file_id=%s: %s",
                metadata.drive_file_id,
                exc,
            )
            raise

        # Milestone 5: Create export queue entry
        if self._export_repo is not None:
            try:
                existing = self._export_repo.get_export_by_drive_file_id(
                    metadata.drive_file_id
                )
                if existing is None:
                    self._export_repo.create_export_record(
                        metadata.drive_file_id, metadata.job_id
                    )
                    logger.info(
                        "Export queue entry created for drive_file_id=%s",
                        metadata.drive_file_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to create export queue entry for " "drive_file_id=%s: %s",
                    metadata.drive_file_id,
                    type(exc).__name__,
                )

        # The image now has a persisted record, so it becomes the canonical
        # fingerprint later uploads are compared against.
        pending = self._pending_fingerprint
        if (
            self._duplicate_repo is not None
            and pending is not None
            and pending[0] == metadata.drive_file_id
        ):
            _drive_file_id, _job_id, fingerprint = pending
            self._duplicate_repo.register_fingerprint(
                content_hash=fingerprint.content_hash,
                perceptual_hash=fingerprint.perceptual_hash,
                drive_file_id=_drive_file_id,
                job_id=_job_id,
                color_signature=encode_color(fingerprint.color_signature),
                aspect_ratio=fingerprint.aspect_ratio,
            )
        self._pending_fingerprint = None


@dataclass
class ManualReviewPackage:
    """Actionable evidence package for manual review.

    When the verification confidence is below the auto-accept threshold
    but above the rejection threshold, this package provides the human
    reviewer with all the visual and textual evidence needed to make
    a decision.

    Fields:
        image_path: Path to the source book cover image.
        ocr_text: Full OCR text.
        extracted_fields: Dict of extracted title/author/isbn/publisher.
        candidates: Top N candidate books with evidence scores.
        best_candidate: The highest-scoring candidate (may still be wrong).
        confidence: Verification confidence score.
        verification_evidence: Full evidence for the best candidate.
        debug_dir: Path to debug artifacts directory (OCR raw text, etc.).
    """

    image_path: str
    ocr_text: str
    extracted_fields: dict[str, Any]
    candidates: List[BookCandidate]
    best_candidate: Optional[BookCandidate]
    confidence: float
    verification_evidence: Optional[VerificationEvidence]
    debug_dir: Optional[str] = None
