"""Confidence calculation service.

Adaptive confidence calculation that distinguishes between:
- MATCH: ISBN present in both OCR and candidate and they match
- CONFLICT: ISBN present in both but they differ (negative signal)
- UNAVAILABLE: ISBN not present in OCR or candidate (neutral)

Missing metadata is NOT penalized. Conflicting metadata IS penalized.
Confidence is normalized over available evidence so that a book with
perfect title + author but no ISBN can still reach the acceptance
threshold.
"""

from __future__ import annotations

from typing import List, Optional

from core.logger import setup_logger
from models.book_candidate import BookCandidate

logger = setup_logger(__name__)


class ConfidenceService:
    """Calculates adaptive final confidence scores for book identification.

    Strict thresholds to prevent false positives:
    - >= 0.85: Automatically accepted (no review needed)
    - 0.50 - 0.85: Manual review required
    - < 0.50: needs_review (never rejected; all books reach review)

    Scoring approach:
    - Each available evidence component (title, author, isbn, publisher) is
      weighted. Available evidence is normalized so that missing fields do
      not cap the maximum achievable confidence.
    - ISBN CONFLICT applies a strong penalty.
    - ISBN UNAVAILABLE is neutral (excluded from normalization).
    - OCR confidence scales the OCR component.
    - Source bonus is a small additive bonus.
    """

    DEFAULT_THRESHOLD = 0.85
    DEFAULT_MANUAL_REVIEW_THRESHOLD = 0.50
    DEFAULT_REJECTED_THRESHOLD = 0.50

    # Weights for each evidence component (used for normalization)
    ISBN_WEIGHT = 0.50
    TITLE_WEIGHT = 0.25
    AUTHOR_WEIGHT = 0.15
    PUBLISHER_WEIGHT = 0.05
    OCR_QUALITY_WEIGHT = 0.05

    # Penalty applied when ISBN signals a conflict
    ISBN_CONFLICT_PENALTY = 0.30

    # Source reliability bonuses
    SOURCE_BONUS = {
        "google_books": 0.05,
        "open_library": 0.00,
    }

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        manual_review_threshold: float = DEFAULT_MANUAL_REVIEW_THRESHOLD,
        rejected_threshold: float = DEFAULT_REJECTED_THRESHOLD,
    ) -> None:
        # Validate ranges (0 <= threshold <= 1.0)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be between 0.0 and 1.0, got {threshold}")
        if not 0.0 <= manual_review_threshold <= 1.0:
            raise ValueError(
                f"manual_review_threshold must be between 0.0 and 1.0, "
                f"got {manual_review_threshold}"
            )
        if not 0.0 <= rejected_threshold <= 1.0:
            raise ValueError(
                f"rejected_threshold must be between 0.0 and 1.0, "
                f"got {rejected_threshold}"
            )
        # Warn if thresholds are out of expected ordering (but don't fail
        # to allow test/custom configurations with non-standard ordering)
        if not (rejected_threshold <= manual_review_threshold <= threshold):
            logger.warning(
                "Threshold ordering unusual: rejected=%.2f, manual_review=%.2f, "
                "accept=%.2f. Expected: rejected <= manual_review <= accept",
                rejected_threshold,
                manual_review_threshold,
                threshold,
            )
        self._threshold = threshold
        self._manual_review_threshold = manual_review_threshold
        self._rejected_threshold = rejected_threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def manual_review_threshold(self) -> float:
        return self._manual_review_threshold

    @property
    def rejected_threshold(self) -> float:
        return self._rejected_threshold

    def calculate(
        self,
        candidate: BookCandidate,
        ocr_confidence: float = 1.0,
        ocr_available: bool = True,
    ) -> float:
        """Calculate the final confidence score for a candidate.

        Uses adaptive scoring: confidence is normalized over available
        evidence fields so that missing metadata does not artificially cap
        the score. ISBN conflicts are penalized; missing ISBN is neutral.

        Args:
            candidate: The ranked book candidate.
            ocr_confidence: Confidence of the OCR text (0.0-1.0).
            ocr_available: Whether OCR text was successfully extracted.

        Returns:
            Confidence score between 0.0 and 1.0.
        """
        # --- Evidence components ---
        # ISBN: 0.50 weight. MATCH=1.0, CONFLICT=-0.30 penalty,
        # UNAVAILABLE=excluded from normalization (neutral).
        if candidate.isbn_state == "MATCH":
            isbn_component = 1.0 * self.ISBN_WEIGHT
            isbn_max = self.ISBN_WEIGHT * 1.0
        elif candidate.isbn_state == "CONFLICT":
            isbn_component = 0.0 * self.ISBN_WEIGHT - self.ISBN_CONFLICT_PENALTY
            isbn_max = self.ISBN_WEIGHT * 1.0  # max if it had matched
        else:  # UNAVAILABLE
            isbn_component = 0.0
            isbn_max = 0.0
        # Title
        if candidate.title_available:
            title_component = candidate.title_score * self.TITLE_WEIGHT
            title_max = self.TITLE_WEIGHT * 1.0
        else:
            title_component = 0.0
            title_max = 0.0

        # Author
        if candidate.author_available:
            author_component = candidate.author_score * self.AUTHOR_WEIGHT
            author_max = self.AUTHOR_WEIGHT * 1.0
        else:
            author_component = 0.0
            author_max = 0.0

        # Publisher
        if candidate.publisher_available:
            publisher_component = candidate.publisher_score * self.PUBLISHER_WEIGHT
            publisher_max = self.PUBLISHER_WEIGHT * 1.0
        else:
            publisher_component = 0.0
            publisher_max = 0.0

        # OCR quality (always available)
        if ocr_available and ocr_confidence > 0:
            ocr_factor = ocr_confidence
        elif not ocr_available:
            ocr_factor = 0.5
        else:
            ocr_factor = 0.3
        ocr_component = ocr_factor * self.OCR_QUALITY_WEIGHT

        # Source bonus
        source_bonus = self.SOURCE_BONUS.get(candidate.source, 0.0)
        source_component = source_bonus * self.OCR_QUALITY_WEIGHT

        # --- Normalize over available evidence ---
        # max_possible is the sum of all max component contributions
        # Only available evidence contributes to max_possible.
        # OCR and source bonus are always counted (they're quality signals).
        max_possible = (
            isbn_max
            + title_max
            + author_max
            + publisher_max
            + self.OCR_QUALITY_WEIGHT * 1.0  # perfect OCR
            + self.OCR_QUALITY_WEIGHT * source_bonus  # max source bonus
        )

        base_score = (
            isbn_component
            + title_component
            + author_component
            + publisher_component
            + ocr_component
            + source_component
        )

        if max_possible > 0:
            confidence = base_score / max_possible
        else:
            confidence = 0.0

        # --- Minimum evidence requirement ---
        # A book must have at least title AND author matching to be
        # auto-accepted. Title alone or author alone is insufficient.
        has_title_and_author = candidate.title_available and candidate.author_available
        if not has_title_and_author:
            # Cap confidence below the auto-accept threshold so that
            # title-only or author-only matches go to review/reject
            confidence = min(confidence, 0.60)

        # --- Extraction quality penalty ---
        # If the OCR-extracted title is suspicious (e.g. a contributor
        # phrase like "avec la collaboration de" was extracted as title),
        # cap confidence to prevent false high-confidence matches.
        if candidate.metadata.extracted_title:
            title_lower = candidate.metadata.extracted_title.lower().strip()
            from services.book_text_extractor import BookTextExtractor

            extractor = BookTextExtractor()
            if extractor._is_contributor_phrase(
                title_lower
            ) or extractor._is_publication_line(title_lower):
                logger.warning(
                    "Suspicious OCR title detected: '%s' - capping confidence",
                    candidate.metadata.extracted_title,
                )
                confidence = min(confidence, 0.40)

            # Penalize if there were multiple title candidates (primary title
            # was ambiguous/uncertain — means extraction was noisy)
            if (
                candidate.metadata.extracted_title_candidates
                and len(candidate.metadata.extracted_title_candidates) > 1
            ):
                logger.info(
                    "Multiple title candidates (%d) - applying quality penalty",
                    len(candidate.metadata.extracted_title_candidates),
                )
                confidence = min(confidence, 0.75)

            # Penalize if OCR text has many single-character lines (fragmentation)
            ocr_lines = candidate.metadata.ocr_text.strip().split("\n")
            single_char_lines = sum(1 for l in ocr_lines if len(l.strip()) == 1)
            if len(ocr_lines) > 0:
                fragmentation_ratio = single_char_lines / len(ocr_lines)
                if fragmentation_ratio > 0.15:
                    logger.info(
                        "OCR fragmentation detected: %.0f%% single-char lines",
                        fragmentation_ratio * 100,
                    )
                    confidence *= 1.0 - fragmentation_ratio * 0.5  # up to 50% reduction

        confidence = max(0.0, min(1.0, round(confidence, 4)))

        logger.info(
            "Confidence: %.4f (isbn_state=%s, isbn=%.4f, title=%.4f, "
            "author=%.4f, publisher=%.4f, ocr=%.4f, source=%.4f, "
            "max_possible=%.4f)",
            confidence,
            candidate.isbn_state,
            isbn_component,
            title_component,
            author_component,
            publisher_component,
            ocr_component,
            source_component,
            max_possible,
        )
        return confidence

    def get_best_candidate(
        self,
        candidates: List[BookCandidate],
        ocr_confidence: float = 1.0,
        ocr_available: bool = True,
    ) -> Optional[BookCandidate]:
        """Find the best candidate by confidence score.

        Args:
            candidates: Ranked list of candidates.
            ocr_confidence: OCR confidence score.
            ocr_available: Whether OCR text was available.

        Returns:
            The best-scoring BookCandidate, or None if no candidates.
        """
        if not candidates:
            return None

        scored = []
        for candidate in candidates:
            confidence = self.calculate(
                candidate,
                ocr_confidence=ocr_confidence,
                ocr_available=ocr_available,
            )
            candidate.metadata.confidence = confidence
            scored.append((confidence, candidate))

        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1] if scored else None

        if best:
            logger.info(
                "Best candidate: %s (confidence=%.4f)",
                best.metadata.title,
                scored[0][0],
            )
        return best

    def get_best_candidate_with_margin(
        self,
        candidates: List[BookCandidate],
        ocr_confidence: float = 1.0,
        ocr_available: bool = True,
        margin_threshold: float = 0.20,
    ) -> tuple:
        """Find the best candidate and assess ambiguity.

        Args:
            candidates: Ranked list of candidates.
            ocr_confidence: OCR confidence score.
            ocr_available: Whether OCR text was available.
            margin_threshold: Minimum confidence margin between 1st and 2nd
                place to be considered unambiguous.

        Returns:
            Tuple of (best_candidate, best_confidence, is_ambiguous).
            is_ambiguous is True when the margin between the best and
            second-best candidate is below margin_threshold.
        """
        if not candidates:
            return None, 0.0, True

        scored = []
        for candidate in candidates:
            confidence = self.calculate(
                candidate,
                ocr_confidence=ocr_confidence,
                ocr_available=ocr_available,
            )
            candidate.metadata.confidence = confidence
            scored.append((confidence, candidate))

        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1]
        best_confidence = scored[0][0]

        is_ambiguous = False
        if len(scored) >= 2:
            second_confidence = scored[1][0]
            margin = best_confidence - second_confidence
            if margin < margin_threshold:
                is_ambiguous = True

        if best:
            logger.info(
                "Best candidate: %s (confidence=%.4f, ambiguous=%s)",
                best.metadata.title,
                best_confidence,
                is_ambiguous,
            )
        return best, best_confidence, is_ambiguous

    def is_auto_accepted(self, confidence: float) -> bool:
        """Check if a confidence score meets the auto-accept threshold."""
        return confidence >= self._threshold

    def is_manual_review(self, confidence: float) -> bool:
        """Check if confidence falls in the manual review range."""
        return self._manual_review_threshold <= confidence < self._threshold

    def get_status(self, confidence: float) -> str:
        """Determine the identification status based on confidence.

        Returns:
            'identified' if confidence >= threshold (auto-accept)
            'needs_review' if confidence < threshold (manual review)

        Note: The 'rejected' status has been removed. Per the non-negotiable
        requirement, no discovered book may be rejected. All books with
        confidence below the auto-accept threshold go to needs_review.
        """
        if confidence >= self._threshold:
            return "identified"
        else:
            return "needs_review"
