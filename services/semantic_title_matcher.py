"""Semantic title comparison for title-correctness evaluation.

Distinguishes SEMANTIC correctness from exact visual transcription:
- Accepts: punctuation, parentheses, apostrophe style, capitalization,
  Unicode accent, hyphen/space, whitespace, line-break differences
- Rejects: missing/changed meaningful words, wrong title, author name
  as title, title fragments missing an available subtitle

Normalization rules (conservative — never removes meaningful words):
1. Unicode NFKC + casefold
2. Strip accents for comparison only
3. Remove purely decorative punctuation: ( ) [ ] { } " " ' ' « » — - – _
4. Normalize apostrophes (' ' ` ´) to nothing (d'amour == damour)
5. Normalize hyphens to spaces
6. Collapse whitespace
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

from core.logger import setup_logger

logger = setup_logger(__name__)

# Decorative punctuation removed entirely (never word-bearing)
_DECORATIVE = re.compile(
    r"[()\[\]{}\"«»„“”\u2014\u2013\u2012\u2010\-–—_/\\|.,;:!?*#~^°]"
)

# Apostrophe-like chars -> removed (French elision: d'amour -> damour)
_APOSTROPHES = re.compile(r"['`´‘’\u2019\u02BC]")

# Stopwords NOT counted as meaningful title words when comparing
# word sets (they carry structure, not identity)
_FUNCTION_WORDS = {
    "le",
    "la",
    "les",
    "l",
    "un",
    "une",
    "des",
    "du",
    "de",
    "d",
    "et",
    "ou",
    "the",
    "a",
    "an",
    "of",
    "and",
    "or",
    "in",
    "on",
    "sur",
    "dans",
    "pour",
    "par",
    "avec",
    "mais",
}


class SemanticTitleMatcher:
    """Compares titles semantically (meaningful words), not visually."""

    @staticmethod
    def normalize(title: str) -> str:
        """Normalize a title for semantic comparison.

        Removes decorative punctuation, normalizes apostrophes/hyphens,
        strips accents, lowercases, collapses whitespace. NEVER removes
        alphanumeric words.
        """
        if not title:
            return ""
        # 1. Unicode normalize + casefold
        t = unicodedata.normalize("NFKC", title).casefold()
        # 1b. Decompose ligatures (œ/Œ -> oe, æ/Æ -> ae) — NFD does NOT
        # split these; without this "cœur" != "coeur"
        t = t.replace("œ", "oe").replace("æ", "ae")
        # 2. Strip accents (combining marks)
        t = unicodedata.normalize("NFD", t)
        t = "".join(c for c in t if not unicodedata.combining(c))
        # 3. Remove apostrophes (elision joins: d'amour -> damour)
        t = _APOSTROPHES.sub("", t)
        # 4. Decorative punctuation -> space (hyphens become word separators)
        t = _DECORATIVE.sub(" ", t)
        # 5. Keep only word chars and spaces
        t = re.sub(r"[^\w\s]", " ", t)
        # 6. Collapse whitespace
        t = " ".join(t.split())
        return t

    @staticmethod
    def meaningful_words(title: str) -> List[str]:
        """Return the meaningful (non-function) words of a title."""
        normalized = SemanticTitleMatcher.normalize(title)
        words = [w for w in normalized.split() if w]
        return [w for w in words if w not in _FUNCTION_WORDS]

    @staticmethod
    def titles_equivalent(ocr_title: str, candidate_title: str) -> bool:
        """True when both titles carry the same meaningful text.

        Semantic equivalence: normalized forms are equal, OR one contains
        the other with all meaningful words present.
        """
        if not ocr_title or not candidate_title:
            return False
        norm_a = SemanticTitleMatcher.normalize(ocr_title)
        norm_b = SemanticTitleMatcher.normalize(candidate_title)
        if not norm_a or not norm_b:
            return False
        if norm_a == norm_b:
            return True
        # Containment with meaningful-word completeness
        if norm_a in norm_b or norm_b in norm_a:
            return True
        return False

    @staticmethod
    def title_complete(extracted: str, reference: str) -> bool:
        """Check the extracted title does not DROP meaningful words that
        the reference (fuller) title contains and that were OCR-available.

        Used to reject incomplete titles like 'HISTOIRES D'AMOUR' when the
        cover's full title 'HISTOIRES D'AMOUR (mais petites-bourgeoises)'
        was readable. Function words are ignored.
        """
        if not extracted or not reference:
            return False
        ref_words = SemanticTitleMatcher.meaningful_words(reference)
        ext_words = set(SemanticTitleMatcher.meaningful_words(extracted))
        if not ref_words:
            return True
        missing = [w for w in ref_words if w not in ext_words]
        # Complete when no meaningful reference word is missing.
        # NOTE: "petites-bourgeoises" normalizes to two words
        # (petites, bourgeoises); a hyphen-only difference still matches.
        return len(missing) == 0

    @staticmethod
    def title_correct(
        extracted: str, ground_truth: str, ocr_available_text: str = ""
    ) -> Tuple[bool, str]:
        """Full semantic correctness check for evaluation.

        Returns (is_correct, reason).

        A title is CORRECT when:
        1. It is semantically equivalent to the ground truth, OR
        2. It contains the ground-truth meaningful words and adds nothing
           that contradicts it.
        It is INCORRECT when meaningful words are missing (if they were
        available in OCR text), changed, or when it's a different title.
        """
        if not extracted:
            return False, "no_title_extracted"

        if SemanticTitleMatcher.titles_equivalent(extracted, ground_truth):
            # Semantic equivalence must ALSO be complete: a fragment like
            # "HISTOIRES" is a substring of the full title but drops
            # meaningful words. Check completeness before accepting.
            if SemanticTitleMatcher.title_complete(extracted, ground_truth):
                return True, "semantically_equivalent"
            # Incomplete fragment achieved "equivalence" only via
            # substring containment — reject it.
            return False, "incomplete_fragment_of_title"

        gt_words = SemanticTitleMatcher.meaningful_words(ground_truth)
        ext_words = set(SemanticTitleMatcher.meaningful_words(extracted))

        if not gt_words:
            return True, "ground_truth_has_no_meaningful_words"

        # Missing meaningful words?
        missing = [w for w in gt_words if w not in ext_words]
        if missing:
            # Incomplete only if the missing words were readable in OCR
            if ocr_available_text:
                ocr_norm = SemanticTitleMatcher.normalize(ocr_available_text)
                all_missing_readable = all(w in ocr_norm.split() for w in missing)
                if all_missing_readable:
                    return False, f"missing_meaningful_words: {missing}"
            # Missing words not demonstrably available — partial credit
            return False, f"missing_words: {missing}"

        # All ground-truth words present; check for additions
        extra = [w for w in ext_words if w not in gt_words]
        if extra:
            # Added words that are NOT part of the title (e.g. author name)
            # Only fail if additions exceed 30% of word count
            if len(extra) > max(1, len(gt_words) * 0.3):
                return False, f"extra_meaningful_words: {extra}"
            return True, "contains_title_with_minor_additions"

        return True, "all_meaningful_words_match"
