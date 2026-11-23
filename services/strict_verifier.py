"""Phase 43: Strict candidate verification with multi-evidence scoring."""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


class StrictCandidateVerifier:
    """Verifies book candidates using multiple independent evidence signals.

    Core principle: author similarity alone must NEVER identify a book.
    There must be meaningful title evidence or another strong independent identifier.
    """

    # Patterns indicating non-author attribution
    SUBJECT_PATTERNS = [
        re.compile(r"\bde\s+[A-Z][a-z]+\b"),  # "de Paul Garde" - subject
        re.compile(r"\bpar\s+[A-Z][a-z]+\b"),  # "par X" - sometimes author
        re.compile(r"\banalyse\s+de\b", re.I),  # "analyse de" - subject
        re.compile(r"\bsur\s+[A-Z][a-z]+\b"),  # "sur X" - about/subject
        re.compile(r"\bécrit\s+par\b", re.I),  # "écrit par" - author
        re.compile(r"\btraduit\s+par\b", re.I),  # "traduit par" - translator
        re.compile(r"\bpréface\s+de\b", re.I),  # "préface de" - not author
        re.compile(r"\bédité\s+par\b", re.I),  # "édité par" - editor
    ]

    @staticmethod
    def normalize_for_comparison(text: str) -> str:
        """Normalize text for comparison while preserving display form."""
        import unicodedata

        # Normalize unicode but keep accents
        text = unicodedata.normalize("NFC", text)
        # Lowercase
        text = text.lower()
        # Remove punctuation except hyphens and apostrophes
        text = re.sub(r"[^\w\s\-\'\u00C0-\u017F]", "", text)
        # Normalize whitespace
        text = " ".join(text.split())
        return text

    @staticmethod
    def titles_match(ocr_title: str, candidate_title: str) -> bool:
        """Check if titles match semantically (accent/punctuation tolerant).

        Uses SemanticTitleMatcher so that punctuation, parentheses,
        apostrophe style, capitalization, and accent differences do not
        reject a correct title. Meaningful-word differences still fail.
        """
        if not ocr_title or not candidate_title:
            return False

        from services.semantic_title_matcher import SemanticTitleMatcher

        if SemanticTitleMatcher.titles_equivalent(ocr_title, candidate_title):
            return True

        # Fall back to accent-insensitive fuzzy match (original behavior)
        def normalize_title(t: str) -> str:
            import unicodedata

            t = unicodedata.normalize("NFD", t)
            t = "".join(c for c in t if unicodedata.category(c) != "Mn")
            t = t.lower()
            t = re.sub(r"[^\w\s]", "", t)
            t = " ".join(t.split())
            return t

        norm_ocr = normalize_title(ocr_title)
        norm_cand = normalize_title(candidate_title)

        if norm_ocr == norm_cand:
            return True
        if norm_ocr in norm_cand or norm_cand in norm_ocr:
            return True

        score = fuzz.ratio(norm_ocr, norm_cand)
        return score >= 70

    @staticmethod
    def authors_match(ocr_author: str, candidate_authors: List[str]) -> bool:
        """Check if authors match with normalization.

        Requires meaningful match - surname-only substring matches are rejected.
        A hyphen in an author's name is NOT evidence of an invalid match.
        """
        if not ocr_author or not candidate_authors:
            return False

        def normalize_author(a: str) -> str:
            import unicodedata

            a = unicodedata.normalize("NFD", a)
            a = "".join(c for c in a if unicodedata.category(c) != "Mn")
            a = a.lower().strip()
            a = re.sub(r"[^\w\s,]", "", a)
            return " ".join(a.split())

        norm_ocr = normalize_author(ocr_author)
        ocr_words = set(norm_ocr.split())

        for cand in candidate_authors:
            norm_cand = normalize_author(cand)

            # Exact match
            if norm_ocr == norm_cand:
                return True

            cand_words = set(norm_cand.split())

            # If OCR has 1 word and candidate has 2+, surname-only match is insufficient
            # "Agier" should NOT match "Jean-Marie Agier"
            # "Jean" should NOT match "Jean-Louis Kuffer"
            if len(ocr_words) == 1 and len(cand_words) >= 2:
                continue

            # If candidate has 1 word and OCR has 2+, require exact word match
            if len(cand_words) == 1 and len(ocr_words) >= 2:
                if cand_words.issubset(ocr_words):
                    return True
                continue

            # Both have 2+ words - require significant overlap
            if len(ocr_words) >= 2 and len(cand_words) >= 2:
                # At least 50% of candidate words must be in OCR
                overlap = len(ocr_words & cand_words)
                if overlap >= len(cand_words) * 0.5:
                    return True

            # Fuzzy match with high threshold
            score = fuzz.ratio(norm_ocr, norm_cand)
            if score >= 85:
                return True

        return False

    def verify_candidate(
        self,
        ocr_title: str,
        ocr_author: str,
        ocr_isbn: str,
        candidate_title: str,
        candidate_authors: List[str],
        candidate_isbn: str,
    ) -> Tuple[bool, str, float]:
        """Verify a candidate against OCR evidence.

        Returns:
            Tuple of (is_valid, reason, confidence)
        """
        evidence_signals = []

        # Signal 1: ISBN exact match (strongest)
        if ocr_isbn and candidate_isbn:
            ocr_isbn_clean = re.sub(r"[^\dX]", "", ocr_isbn.upper())
            cand_isbn_clean = re.sub(r"[^\dX]", "", candidate_isbn.upper())
            if ocr_isbn_clean == cand_isbn_clean:
                evidence_signals.append(("isbn_exact", 1.0))
            else:
                # ISBN conflict - strong negative signal
                evidence_signals.append(("isbn_conflict", -0.5))

        # Signal 2: Title match
        title_match = self.titles_match(ocr_title, candidate_title)
        if title_match:
            evidence_signals.append(("title_match", 0.8))
        else:
            # Check partial title match
            if ocr_title and candidate_title:
                score = fuzz.ratio(
                    self.normalize_for_comparison(ocr_title),
                    self.normalize_for_comparison(candidate_title),
                )
                if score >= 50:
                    evidence_signals.append(("title_partial", score / 100.0 * 0.5))
                else:
                    evidence_signals.append(("title_mismatch", -0.3))

        # Signal 3: Author match (weakest alone)
        author_match = self.authors_match(ocr_author, candidate_authors)
        if author_match:
            evidence_signals.append(("author_match", 0.4))
        else:
            evidence_signals.append(("author_mismatch", -0.1))

        # Calculate confidence
        confidence = 0.0
        for signal, value in evidence_signals:
            confidence += value

        # Normalize to 0-1 range
        confidence = max(0.0, min(1.0, confidence))

        # Decision logic
        has_isbn_match = any(s[0] == "isbn_exact" for s in evidence_signals)
        has_title_match = any(
            s[0] in ("title_match", "title_partial") for s in evidence_signals
        )
        has_author_match = any(s[0] == "author_match" for s in evidence_signals)
        has_isbn_conflict = any(s[0] == "isbn_conflict" for s in evidence_signals)

        # ISBN conflict always triggers manual review
        if has_isbn_conflict:
            return False, "ISBN conflict", confidence

        # ISBN exact match is sufficient alone
        if has_isbn_match:
            return True, "ISBN exact match", confidence

        # Title match + author match = strong evidence
        if has_title_match and has_author_match:
            return True, "Title + author match", confidence

        # Title match alone = medium evidence (need more)
        if has_title_match and not has_author_match:
            return False, "Title match but author mismatch", confidence

        # Author match alone = insufficient
        if has_author_match and not has_title_match:
            return False, "Author match only (insufficient)", confidence

        # No evidence
        return False, "No evidence", confidence
