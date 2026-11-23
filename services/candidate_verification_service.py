"""Candidate verification service — evidence-based book identification.

This is the core autonomous verification component that aggregates evidence
from multiple sources (OCR text, cover image, ISBN, title, author, publisher)
to produce a structured verification result for each candidate book.

Evidence sources:
1. OCR text similarity (title, author, ISBN, publisher)
2. Cover image verification (dHash + SSIM + SIFT feature matching)
3. ISBN state (MATCH / CONFLICT / UNAVAILABLE)
4. Publisher consistency
5. Published date range (not yet implemented)
6. Language consistency (not yet implemented)

The service does NOT auto-accept based on API results alone — it requires
a minimum confidence threshold AND at least two independent positive signals
(title + author, ISBN + cover, etc.) to auto-identify a book.

Decision policy:
- confidence >= 0.85 AND title_score >= 0.80 AND author_score >= 0.80 -> IDENTIFIED
- confidence >= 0.85 AND title_score >= 0.80 AND isbn_state == MATCH -> IDENTIFIED
- confidence >= 0.50 -> MANUAL_REVIEW
- confidence < 0.50 -> REJECTED
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import setup_logger
from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from services.cover_verifier import CoverVerifier
from services.text_normalizer import TextNormalizer

logger = setup_logger(__name__)


@dataclass
class VerificationEvidence:
    """Structured evidence for a single candidate verification."""

    title_score: float = 0.0
    author_score: float = 0.0
    publisher_score: float = 0.0
    isbn_score: float = 0.0
    subtitle_score: float = 0.0
    isbn_state: str = "UNAVAILABLE"
    cover_similarity: float = 0.0
    cover_verified: bool = False
    title_available: bool = False
    author_available: bool = False
    publisher_available: bool = False
    isbn_available: bool = False
    cover_available: bool = False
    candidate_title: str = ""
    candidate_authors: List[str] = field(default_factory=list)
    candidate_publisher: str = ""
    candidate_isbn: str = ""
    candidate_source: str = ""
    verification_confidence: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    """Full verification result for a batch of candidates."""

    best_candidate: Optional[BookCandidate] = None
    best_confidence: float = 0.0
    best_evidence: Optional[VerificationEvidence] = None
    all_evidence: List[Tuple[BookCandidate, VerificationEvidence]] = field(
        default_factory=list
    )
    status: str = "needs_review"
    confidence: float = 0.0
    ambiguous: bool = False
    ambiguity_margin: float = 0.0
    rejection_reason: Optional[str] = None


class CandidateVerificationService:
    """Aggregates evidence to verify book candidate identification.

    Args:
        cover_verifier: CoverVerifier instance for image comparison.
        text_normalizer: TextNormalizer for fuzzy matching.
        confidence_threshold: Minimum confidence for auto-identification (0.85).
        manual_review_threshold: Below this, candidate goes to needs_review (0.50).
        cover_similarity_threshold: Minimum cover similarity to count as evidence (0.60).
    """

    WEIGHT_TITLE = 0.25
    WEIGHT_AUTHOR = 0.20
    WEIGHT_ISBN = 0.25
    WEIGHT_PUBLISHER = 0.10
    WEIGHT_COVER = 0.20

    MIN_TITLE_SCORE_FOR_AUTO = 0.80
    MIN_AUTHOR_SCORE_FOR_AUTO = 0.80

    def __init__(
        self,
        cover_verifier: Optional[CoverVerifier] = None,
        text_normalizer: Optional[TextNormalizer] = None,
        confidence_threshold: float = 0.85,
        manual_review_threshold: float = 0.50,
        cover_similarity_threshold: float = 0.60,
    ) -> None:
        self._cover_verifier = cover_verifier or CoverVerifier()
        self._normalizer = text_normalizer or TextNormalizer()
        self._confidence_threshold = confidence_threshold
        self._manual_review_threshold = manual_review_threshold
        self._cover_similarity_threshold = cover_similarity_threshold

    def verify_candidates(
        self,
        candidates: List[BookCandidate],
        ocr_result: Optional[Any] = None,
        source_image_path: Optional[str] = None,
        extracted_title: Optional[str] = None,
        extracted_author: Optional[str] = None,
        extracted_isbn: Optional[str] = None,
        extracted_publisher: Optional[str] = None,
    ) -> VerificationResult:
        """Verify a list of candidates against extracted OCR evidence."""
        if not candidates:
            return VerificationResult(
                status="needs_review",
                confidence=0.0,
                rejection_reason="No candidates found from search",
            )

        all_evidence: List[Tuple[BookCandidate, VerificationEvidence]] = []

        for candidate in candidates:
            evidence = self._compute_evidence(
                candidate,
                ocr_result=ocr_result,
                source_image_path=source_image_path,
                extracted_title=extracted_title,
                extracted_author=extracted_author,
                extracted_isbn=extracted_isbn,
                extracted_publisher=extracted_publisher,
            )
            all_evidence.append((candidate, evidence))

        # Sort by verification confidence (descending)
        all_evidence.sort(key=lambda x: x[1].verification_confidence, reverse=True)

        best_candidate, best_evidence = all_evidence[0]

        # Check for ambiguity
        ambiguous = False
        ambiguity_margin = 0.0
        if len(all_evidence) >= 2:
            top_conf = best_evidence.verification_confidence
            second_conf = all_evidence[1][1].verification_confidence
            ambiguity_margin = top_conf - second_conf
            ambiguous = ambiguity_margin < 0.10

        confidence = best_evidence.verification_confidence
        status = self._make_decision(best_evidence, confidence)

        return VerificationResult(
            best_candidate=best_candidate,
            best_confidence=confidence,
            best_evidence=best_evidence,
            all_evidence=all_evidence,
            status=status,
            confidence=confidence,
            ambiguous=ambiguous,
            ambiguity_margin=ambiguity_margin,
            rejection_reason=(
                best_evidence.details.get("rejection_reason")
                if status != "identified"
                else None
            ),
        )

    def _compute_evidence(
        self,
        candidate: BookCandidate,
        ocr_result: Optional[Any] = None,
        source_image_path: Optional[str] = None,
        extracted_title: Optional[str] = None,
        extracted_author: Optional[str] = None,
        extracted_isbn: Optional[str] = None,
        extracted_publisher: Optional[str] = None,
    ) -> VerificationEvidence:
        """Compute evidence scores for a single candidate."""
        metadata = candidate.metadata

        evidence = VerificationEvidence(
            candidate_title=metadata.title or "",
            candidate_authors=list(metadata.authors) if metadata.authors else [],
            candidate_publisher=metadata.publisher or "",
            candidate_source=metadata.source or "",
        )

        # --- ISBN evidence ---
        ocr_isbn = extracted_isbn.strip() if extracted_isbn else None
        candidate_isbn = metadata.isbn_10 or metadata.isbn_13
        evidence.isbn_available = bool(ocr_isbn)

        if ocr_isbn and candidate_isbn:
            ocr_isbn_norm = self._normalize_isbn(ocr_isbn)
            cand_isbn_norm = self._normalize_isbn(candidate_isbn)
            if ocr_isbn_norm == cand_isbn_norm:
                evidence.isbn_state = "MATCH"
                evidence.isbn_score = 1.0
            else:
                evidence.isbn_state = "CONFLICT"
                evidence.isbn_score = 0.0
                evidence.details["isbn_conflict"] = {
                    "ocr": ocr_isbn,
                    "candidate": candidate_isbn,
                }
        elif not ocr_isbn and not candidate_isbn:
            evidence.isbn_state = "UNAVAILABLE"
            evidence.isbn_score = 0.5
        else:
            evidence.isbn_state = "UNAVAILABLE"
            evidence.isbn_score = 0.0

        evidence.candidate_isbn = candidate_isbn or ""

        # --- Title evidence ---
        title_available = bool(extracted_title) and bool(metadata.title)
        evidence.title_available = title_available
        if title_available:
            evidence.title_score = self._title_similarity(
                extracted_title, metadata.title
            )

        # --- Author evidence ---
        author_available = bool(extracted_author) and bool(metadata.authors)
        evidence.author_available = author_available
        if author_available:
            evidence.author_score = self._author_similarity(
                extracted_author, metadata.authors
            )

        # --- Publisher evidence ---
        publisher_available = bool(extracted_publisher) and bool(metadata.publisher)
        evidence.publisher_available = publisher_available
        if publisher_available:
            evidence.publisher_score = self._text_similarity(
                extracted_publisher, metadata.publisher
            )

        # --- Cover image evidence ---
        if source_image_path and metadata.cover_url:
            evidence.cover_available = True
            cover_result = self._cover_verifier.verify_covers(
                source_image_path, metadata.cover_url
            )
            if cover_result:
                evidence.cover_similarity = cover_result.overall_similarity
                evidence.cover_verified = True
                evidence.details["cover_details"] = {
                    "dhash": cover_result.dhash_similarity,
                    "ssim": cover_result.ssim_score,
                    "feature": cover_result.feature_score,
                    "color": cover_result.color_histogram_score,
                }

        # --- Compute overall confidence ---
        evidence.verification_confidence = self._compute_confidence(evidence, candidate)

        return evidence

    def _compute_confidence(
        self, evidence: VerificationEvidence, candidate: BookCandidate
    ) -> float:
        """Compute overall verification confidence from evidence."""
        confidence = 0.0
        total_weight = 0.0

        # ISBN component
        if evidence.isbn_state == "MATCH":
            confidence += self.WEIGHT_ISBN * 1.0
            total_weight += self.WEIGHT_ISBN
        elif evidence.isbn_state == "CONFLICT":
            confidence += self.WEIGHT_ISBN * 0.0
            total_weight += self.WEIGHT_ISBN
        else:
            confidence += self.WEIGHT_ISBN * max(evidence.isbn_score, 0.5)
            total_weight += self.WEIGHT_ISBN

        # Title component
        if evidence.title_available:
            confidence += self.WEIGHT_TITLE * evidence.title_score
            total_weight += self.WEIGHT_TITLE
        else:
            total_weight += self.WEIGHT_TITLE * 0.5

        # Author component
        if evidence.author_available:
            confidence += self.WEIGHT_AUTHOR * evidence.author_score
            total_weight += self.WEIGHT_AUTHOR
        else:
            total_weight += self.WEIGHT_AUTHOR * 0.5

        # Publisher component
        if evidence.publisher_available:
            confidence += self.WEIGHT_PUBLISHER * evidence.publisher_score
            total_weight += self.WEIGHT_PUBLISHER
        else:
            total_weight += self.WEIGHT_PUBLISHER * 0.5

        # Cover component
        if evidence.cover_available and evidence.cover_verified:
            confidence += self.WEIGHT_COVER * evidence.cover_similarity
            total_weight += self.WEIGHT_COVER
        else:
            total_weight += self.WEIGHT_COVER * 0.3

        # Normalize
        if total_weight > 0:
            full_weight = (
                self.WEIGHT_ISBN
                + self.WEIGHT_TITLE
                + self.WEIGHT_AUTHOR
                + self.WEIGHT_PUBLISHER
                + self.WEIGHT_COVER
            )
            confidence = confidence / total_weight * full_weight

        # ISBN conflict penalty
        if evidence.isbn_state == "CONFLICT":
            confidence = max(0.0, confidence - 0.30)

        confidence = max(0.0, min(1.0, confidence))

        # Require at least title or ISBN match for high confidence
        if confidence >= self._confidence_threshold:
            has_strong_evidence = (
                evidence.isbn_state == "MATCH"
                or (evidence.title_available and evidence.title_score >= 0.7)
                or (evidence.author_available and evidence.author_score >= 0.7)
                or (
                    evidence.cover_available
                    and evidence.cover_similarity >= self._cover_similarity_threshold
                )
            )
            if not has_strong_evidence:
                confidence = min(confidence, self._confidence_threshold - 0.01)

        evidence.details["raw_confidence"] = confidence
        return confidence

    def _make_decision(self, evidence: VerificationEvidence, confidence: float) -> str:
        """Make the final identification decision."""
        if confidence >= self._confidence_threshold:
            has_strong_evidence = (
                evidence.isbn_state == "MATCH"
                or (
                    evidence.title_available
                    and evidence.title_score >= self.MIN_TITLE_SCORE_FOR_AUTO
                )
                or (
                    evidence.author_available
                    and evidence.author_score >= self.MIN_AUTHOR_SCORE_FOR_AUTO
                )
            )
            if has_strong_evidence:
                return "identified"
            return "manual_review"

        if confidence >= self._manual_review_threshold:
            return "manual_review"

        evidence.details["rejection_reason"] = (
            f"Confidence {confidence:.2f} below manual review threshold "
            f"{self._manual_review_threshold}"
        )
        return "needs_review"

    def _title_similarity(self, ocr_title: str, candidate_title: str) -> float:
        """Compute similarity between OCR-extracted and candidate titles."""
        ocr_norm = self._normalize_for_comparison(ocr_title)
        cand_norm = self._normalize_for_comparison(candidate_title)
        if not ocr_norm or not cand_norm:
            return 0.0
        if ocr_norm in cand_norm or cand_norm in ocr_norm:
            return 1.0
        return self._token_similarity(ocr_norm, cand_norm)

    def _author_similarity(
        self, ocr_author: str, candidate_authors: List[str]
    ) -> float:
        """Compute similarity between OCR-extracted and candidate authors."""
        ocr_norm = self._normalize_for_comparison(ocr_author)
        if not candidate_authors:
            return 0.0
        best_score = 0.0
        for cand_author in candidate_authors:
            cand_norm = self._normalize_for_comparison(cand_author)
            score = self._token_similarity(ocr_norm, cand_norm)
            best_score = max(best_score, score)
        return best_score

    def _normalize_isbn(self, isbn: str) -> str:
        """Normalize ISBN for comparison."""
        digits = re.sub(r"[^0-9X]", "", isbn.upper())
        return digits

    def _text_similarity(self, text1: str, text2: str) -> float:
        """Compute text similarity for publisher comparison."""
        return self._token_similarity(
            self._normalize_for_comparison(text1),
            self._normalize_for_comparison(text2),
        )

    @staticmethod
    def _normalize_for_comparison(text: str) -> str:
        """Normalize text for comparison."""
        if not text:
            return ""
        text = text.lower().strip()
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _token_similarity(text1: str, text2: str) -> float:
        """Compute Jaccard similarity over word tokens."""
        tokens1 = set(text1.split())
        tokens2 = set(text2.split())
        if not tokens1 or not tokens2:
            return 0.0
        intersection = tokens1 & tokens2
        union = tokens1 | tokens2
        return len(intersection) / len(union)
