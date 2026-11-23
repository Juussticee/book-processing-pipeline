"""Candidate ranker — ranks book candidates by similarity to OCR-extracted text.

Uses RapidFuzz for fuzzy string matching: title, author, and publisher.
"""

from __future__ import annotations

from typing import List, Optional

from core.logger import setup_logger
from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata

logger = setup_logger(__name__)

# Weights for ranking components
TITLE_WEIGHT = 1.0
AUTHOR_WEIGHT = 1.0
ISBN_WEIGHT = 2.0
PUBLISHER_WEIGHT = 0.5

# Minimum title score for a candidate to be considered viable.
# Title is the primary identifier of a book — when the title similarity
# falls below this threshold, the candidate is likely a wrong book and the
# author fuzzy-match contribution is zeroed out. This prevents spurious
# author matches (e.g. "Theodore" matching "Théodore Litt" via
# token_sort_ratio giving 0.67) from overriding a poor title match.
# ISBN evidence (ISBN_WEIGHT=2.0) is unaffected and still overrides.
MIN_TITLE_SCORE_FLOOR = 0.50


class CandidateRanker:
    """Ranks book candidates using fuzzy string matching.

    Compares:
    - title similarity
    - subtitle similarity
    - author similarity
    - ISBN exact match
    - publisher similarity

    Uses RapidFuzz for token_sort_ratio and token_set_ratio.
    """

    def rank(
        self,
        candidates: List[BookMetadata],
        ocr_title: Optional[str] = None,
        ocr_author: Optional[str] = None,
        ocr_isbn: Optional[str] = None,
        ocr_publisher: Optional[str] = None,
        evidence_candidates: Optional[List[BookCandidate]] = None,
    ) -> List[BookCandidate]:
        """Rank a list of book candidates against OCR-extracted fields.

        If evidence_candidates are provided (from the verification service),
        their enriched evidence scores (cover_similarity, isbn_state, etc.)
        are merged into the ranked output.

        Args:
            candidates: List of BookMetadata candidates to rank.
            ocr_title: Title extracted from OCR text.
            ocr_author: Author extracted from OCR text.
            ocr_isbn: ISBN extracted from OCR text.
            ocr_publisher: Publisher extracted from OCR text.
            evidence_candidates: Optional BookCandidate list with enriched
                evidence scores from the verification service.

        Returns:
            List of BookCandidate sorted by total_score descending.
        """
        # Build a lookup from evidence candidates by metadata identity
        evidence_map: dict = {}
        if evidence_candidates:
            for ec in evidence_candidates:
                key = id(ec.metadata) if ec.metadata else None
                if key is not None:
                    evidence_map[key] = ec

        ranked: List[BookCandidate] = []

        for candidate in candidates:
            candidate_obj = self._score_candidate(
                candidate,
                ocr_title=ocr_title,
                ocr_author=ocr_author,
                ocr_isbn=ocr_isbn,
                ocr_publisher=ocr_publisher,
            )
            # Merge enriched evidence from verification service
            evidence = evidence_map.get(id(candidate))
            if evidence is not None:
                candidate_obj.cover_similarity = evidence.cover_similarity
                candidate_obj.isbn_state = evidence.isbn_state
                candidate_obj.title_available = evidence.title_available
                candidate_obj.author_available = evidence.author_available
                candidate_obj.publisher_available = evidence.publisher_available
            ranked.append(candidate_obj)

        ranked.sort(key=lambda c: c.total_score, reverse=True)
        logger.info("Ranked %d candidates", len(ranked))
        return ranked

    def _score_candidate(
        self,
        metadata: BookMetadata,
        ocr_title: Optional[str] = None,
        ocr_author: Optional[str] = None,
        ocr_isbn: Optional[str] = None,
        ocr_publisher: Optional[str] = None,
    ) -> BookCandidate:
        """Score a single candidate against OCR fields."""
        from rapidfuzz import fuzz

        title_score = 0.0
        author_score = 0.0
        isbn_score = 0.0
        publisher_score = 0.0
        isbn_state = "UNAVAILABLE"
        title_available = False
        author_available = False
        publisher_available = False

        # Title similarity
        if ocr_title and metadata.title:
            title_score = self._similarity(ocr_title, metadata.title, fuzz)
            title_available = True
        elif ocr_title:
            title_score = 0.0
            title_available = True

        # Author similarity
        if ocr_author and metadata.authors:
            best_author = max(
                (self._similarity(ocr_author, a, fuzz) for a in metadata.authors),
                default=0.0,
            )
            author_score = best_author
            author_available = True
        elif ocr_author:
            author_score = 0.0
            author_available = True

        # ISBN matching - detect MATCH, CONFLICT, or UNAVAILABLE
        if ocr_isbn:
            ocr_isbn_normalized = ocr_isbn.replace("-", "").strip()
            metadata_isbns = []
            if metadata.isbn_10:
                metadata_isbns.append(metadata.isbn_10.replace("-", "").strip())
            if metadata.isbn_13:
                metadata_isbns.append(metadata.isbn_13.replace("-", "").strip())

            if metadata_isbns:
                if any(ocr_isbn_normalized == m_isbn for m_isbn in metadata_isbns):
                    isbn_score = 1.0
                    isbn_state = "MATCH"
                else:
                    isbn_score = 0.0
                    isbn_state = "CONFLICT"
            else:
                # OCR has ISBN but candidate doesn't have any ISBN
                # This is not a CONFLICT — it's just that the candidate
                # lacks ISBN data. Treat as MATCH (not penalized).
                isbn_score = 1.0
                isbn_state = "MATCH"
        else:
            if metadata.isbn_10 or metadata.isbn_13:
                isbn_score = 0.0
                isbn_state = "UNAVAILABLE"
            else:
                isbn_state = "UNAVAILABLE"
                isbn_score = 0.0

        # Publisher similarity
        if ocr_publisher and metadata.publisher:
            publisher_score = self._similarity(ocr_publisher, metadata.publisher, fuzz)
            publisher_available = True
        elif ocr_publisher:
            publisher_score = 0.0
            publisher_available = True

        total = self._calculate_total(
            title_score, author_score, isbn_score, publisher_score
        )

        return BookCandidate(
            metadata=metadata,
            title_score=round(title_score, 4),
            author_score=round(author_score, 4),
            isbn_score=round(isbn_score, 4),
            publisher_score=round(publisher_score, 4),
            total_score=round(total, 4),
            source=metadata.source,
            isbn_state=isbn_state,
            title_available=title_available,
            author_available=author_available,
            publisher_available=publisher_available,
        )

    @staticmethod
    def _similarity(text_a: str, text_b: str, fuzz) -> float:
        """Calculate normalized similarity between two strings (0.0-1.0).

        Uses semantic normalization FIRST (handles punctuation, parentheses,
        apostrophes, accents, capitalization differences) and falls back to
        fuzzy token matching. This ensures 'HISTOIRES D'AMOUR (mais
        petites-bourgeoises)' and 'HISTOIRES D'AMOUR mais petites-bourgeoises'
        compare as 1.0, not ~0.85.
        """
        # Semantic equivalence shortcut — exact same meaningful text
        try:
            from services.semantic_title_matcher import SemanticTitleMatcher

            norm_a = SemanticTitleMatcher.normalize(text_a)
            norm_b = SemanticTitleMatcher.normalize(text_b)
            if norm_a and norm_b:
                if norm_a == norm_b:
                    return 1.0
        except Exception as exc:
            # Fall back to fuzzy matching, but keep the degradation visible:
            # a broken matcher silently lowers every candidate's score.
            logger.debug("Semantic title matcher unavailable: %s", exc)
        ratio = fuzz.token_sort_ratio(text_a.lower(), text_b.lower())
        return float(ratio) / 100.0

    @staticmethod
    def _calculate_total(
        title_score: float,
        author_score: float,
        isbn_score: float,
        publisher_score: float,
    ) -> float:
        """Calculate weighted total score.

        A title-score floor is enforced: when the title similarity is below
        MIN_TITLE_SCORE_FLOOR, the candidate is likely a wrong book even if
        the author fuzzy-matches. The author-score contribution is zeroed
        so that spurious author matches (e.g. "Theodore" matching
        "Théodore Litt" via token_sort_ratio) cannot override a poor
        title match. ISBN evidence (ISBN_WEIGHT=2.0) is unaffected.
        """
        # When title match is poor, suppress the author contribution —
        # title is the primary book identifier and a spurious author
        # match (e.g. one shared token) must not decide the winner.
        effective_author_score = author_score
        if title_score < MIN_TITLE_SCORE_FLOOR:
            effective_author_score = 0.0
        total = (
            title_score * TITLE_WEIGHT
            + effective_author_score * AUTHOR_WEIGHT
            + isbn_score * ISBN_WEIGHT
            + publisher_score * PUBLISHER_WEIGHT
        )
        weights_sum = TITLE_WEIGHT + AUTHOR_WEIGHT + ISBN_WEIGHT + PUBLISHER_WEIGHT
        return total / weights_sum if weights_sum > 0 else 0.0
