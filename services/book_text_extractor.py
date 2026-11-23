"""Structured text extractor — parses OCR output into book metadata fields.

Responsibilities:
- Clean OCR noise
- Detect: title, author, ISBN, publisher, edition
- Support multilingual (en/fr) patterns
- Validate ISBN checksums before returning
- Classify OCR lines by semantic role (title, author, contributor, publisher, etc.)
- Generate multiple title candidates for robust lookup
- Detect contributor/collaboration phrases and extract contributor names
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

from core.logger import setup_logger
from services.text_normalizer import TextNormalizer

logger = setup_logger(__name__)


class BookTextExtractor:
    """Extracts structured book information from OCR text.

    Uses pattern matching and heuristics to detect:
    - Title: Scored from multiple candidate lines, with multi-line title support
    - Author: Patterns like "by <name>", "par <name>", "de <name>",
      or comma-format names like "Kuffer, Jean-Louis"
    - ISBN: ISBN-10 and ISBN-13 with checksum validation
    - Publisher: Keywords like "Publisher:", "Éditeur:", "Published by"
    - Edition: "2nd edition", "3e édition", etc.
    - Contributors: "avec la collaboration de", "sous la direction de", etc.
    """

    # Common title-leading articles that indicate a title, not an author name.
    _TITLE_ARTICLES = frozenset(
        {
            "le",
            "la",
            "les",
            "l'",
            "un",
            "une",
            "des",
            "du",
            "the",
            "a",
            "an",
            "el",
            "los",
            "una",
        }
    )
    # Author attribution keywords that introduce an author name on a line.
    # Lines starting with these are attribution phrases (handled by
    # AUTHOR_PATTERNS), NOT standalone author names.
    _ATTRIBUTION_KEYWORDS = frozenset({"par", "by", "de", "published"})

    # Short function words that can appear as standalone OCR lines between
    # title fragments (e.g. "de" between "Analyse" and "VIE ET MORT").
    # These are "bridgeable" — they can connect two title candidates.
    _BRIDGE_WORDS = frozenset(
        {
            "de",
            "le",
            "la",
            "les",
            "et",
            "du",
            "des",
            "un",
            "une",
            "l'",
            "d'",
            "t'",
            "qu'",
        }
    )

    # Author attribution patterns (multilingual) — non-greedy, bounded
    # All patterns anchor at line start (^) to avoid matching translator
    # credits mid-line (e.g. "TRADUIT DE LALLEMAND PAR JAN DUSAY").
    AUTHOR_PATTERNS = [
        r"^par\s+(.+?)(?=\s+isbn|\s+éditeur|\s+édition|publish|$)",
        r"^published\s+by\s+(.+?)(?=\s+isbn|\s+éditeur|\s+édition|$)",
        # "de" attribution: match only at line start (attribution pattern),
        # not mid-line (e.g. "Miguel de Unamuno"). Exclude common French
        # title articles after "de" (de la, de l', de le, etc.).
        r"^de\s+(?!la|l'|le|les|du|des|blam|roman)(.+?)(?=\s+isbn|\s+éditeur|\s+édition|publish|$)",
        r"^by\s+(.+?)(?=\s+isbn|\s+éditeur|\s+édition|publish|$)",
    ]

    # Contributor/collaboration phrase patterns (multilingual).
    # These phrases indicate that the following text is a contributor name,
    # NOT a book title. The captured group is the contributor name.
    CONTRIBUTOR_PATTERNS = [
        # French patterns
        r"avec\s+la\s+collaboration\s+de\s+(.+)",
        r"sous\s+la\s+direction\s+de\s+(.+)",
        r"sous\s+la\s+responsabilité\s+de\s+(.+)",
        r"préface\s+de\s+(.+)",
        r"postface\s+de\s+(.+)",
        r"introduction\s+de\s+(.+)",
        r"traduit\s+par\s+(.+)",
        r"traduction\s+de\s+(.+)",
        r"éditorial\s+de\s+(.+)",
        # English patterns
        r"with\s+contributions\s+from\s+(.+)",
        r"edited\s+by\s+(.+)",
        r"translated\s+by\s+(.+)",
        r"foreword\s+by\s+(.+)",
        r"introduction\s+by\s+(.+)",
        r"with\s+the\s+collaboration\s+of\s+(.+)",
        # Spanish patterns
        r"con\s+la\s+colaboración\s+de\s+(.+)",
        r"editado\s+por\s+(.+)",
    ]

    # Full contributor phrase text (lowercase) for line classification
    CONTRIBUTOR_PHRASES = [
        "avec la collaboration de",
        "sous la direction de",
        "sous la responsabilité de",
        "préface de",
        "postface de",
        "introduction de",
        "traduit par",
        "traduction de",
        "éditorial de",
        "with contributions from",
        "edited by",
        "translated by",
        "foreword by",
        "introduction by",
        "with the collaboration of",
        "con la colaboración de",
        "editado por",
    ]

    # Publisher keywords (matched as whole words)
    PUBLISHER_KEYWORDS = {
        "publisher",
        "éditeur",
        "éditions",
        "édition",
        "publishing",
        "imprint",
    }

    # Known publisher names that should never be treated as authors
    _KNOWN_PUBLISHER_NAMES = frozenset(
        {
            "l'age d'homme",
            "l'âge d'homme",
            "lage d'homme",
            "lage d'homme",
            "éditions l'âge d'homme",
            "editions l'age d'homme",
            "bibliothèque l'âge d'homme",
            "bibliotheque l'age d'homme",
        }
    )

    # Publication boilerplate keywords — these lines are typically NOT titles
    PUBLICATION_KEYWORDS = {
        "collection",
        "série",
        "volume",
        "tome",
        "numéro",
        "edition",
        "édition",
        "réimpression",
        "isbn",
        "issn",
        "©",
        "copyright",
        "droits réservés",
        "price",
        "prix",
        "tarif",
        "www.",
        "http",
        "email",
        "@",
    }

    # Edition patterns
    EDITION_PATTERNS = [
        r"(\d+)(?:st|nd|rd|th|er|ère|eme|ème|ième|i?e)?\s*[ée]dition",
        r"(\d+)(?:st|nd|rd|th)?\s*edition",
        r"(\d+)\s*edition",
        r"(First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth)\s+edition",
    ]

    # Lines shorter than this are not considered for title candidates
    _MIN_TITLE_LENGTH = 3
    # Lines longer than this are suspicious for title (likely multiple lines merged)
    _MAX_TITLE_LINE_LENGTH = 80

    def __init__(self, languages: Optional[List[str]] = None) -> None:
        self._languages = languages or ["en"]
        self._normalizer = TextNormalizer()

    def extract(self, ocr_text: str) -> Dict:
        """Extract structured fields from OCR text.

        Returns dict with keys: title, title_candidates, author, contributor,
        isbn, publisher, edition, language.
        Values are strings or None (except title_candidates which is a list).
        """
        if not ocr_text:
            return self._empty_result()

        # Split original text into lines (before normalization, which
        # merges newlines). Handle both actual newlines (from test text)
        # and literal backslash-n (from OCR normalization that may escape newlines).
        raw_lines = (
            ocr_text.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\\n", "\n")
            .split("\n")
        )
        # Recombine fragmented multi-line titles — OCR frequently splits
        # short fragments like "L'" + "individu et la communauté" across
        # adjacent lines. We attempt to merge adjacent short lines that
        # form a coherent title.
        raw_lines = self._recombine_fragmented_lines(raw_lines)
        cleaned = self._clean_text(ocr_text)

        result: Dict = {
            "title": None,
            "title_candidates": [],
            "author": None,
            "contributor": None,
            "isbn": None,
            "publisher": None,
            "edition": None,
            "language": None,
        }

        # 1. Extract ISBN (most reliable field)
        isbns = self._normalizer.extract_isbn(ocr_text)
        if isbns:
            result["isbn"] = isbns[0]
            logger.info("Extracted ISBN: %s", result["isbn"])

        # 2. Extract contributor/collaboration names (before author extraction
        #    so contributor phrases don't get confused with author patterns)
        result["contributor"] = self._extract_contributor(raw_lines)

        # 3. Extract author
        result["author"] = self._extract_author(raw_lines)

        # 4. Extract publisher
        result["publisher"] = self._extract_publisher(raw_lines)

        # 5. Extract edition
        result["edition"] = self._extract_edition(cleaned)

        # 6. Detect language
        result["language"] = self._detect_language(cleaned)

        # 7. Extract title and title candidates
        title_candidates_result = self._extract_title_candidates(raw_lines, result)
        title, title_candidates = title_candidates_result
        if title is None:
            # Fallback: legacy single-line approach (for backward compatibility
            # with OCR noise that doesn't pass the new scoring)
            title = self._extract_title_legacy(raw_lines, result)
        result["title"] = title
        result["title_candidates"] = title_candidates

        logger.info(
            "Extraction complete: title=%s, author=%s, contributor=%s, isbn=%s, "
            "publisher=%s, candidates=%s",
            result["title"],
            result["author"],
            result["contributor"],
            result["isbn"],
            result["publisher"],
            title_candidates,
        )
        return result

    def _clean_text(self, text: str) -> str:
        """Remove OCR noise: extra whitespace, broken chars, etc."""
        text = self._normalizer.normalize(text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def _recombine_fragmented_lines(self, lines: List[str]) -> List[str]:
        """Attempt to recombine OCR-fragmented lines into coherent entries.

        OCR (especially PaddleOCR-VL with paragraph=False) sometimes splits
        a single text element across multiple lines.  We detect and merge:

        1. Very short fragments ending with an apostrophe (e.g. "L'", "d'",
           "l'", "n'") whose next line continues the same word
           (e.g. "L'" + "individu" → "L'individu").

        Non-title lines (author names, ISBN, publisher) are left untouched.
        """
        if len(lines) < 2:
            return lines

        result: List[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Only attempt recombination for very short fragments
            # (1-4 chars) that end with an apostrophe — these are clearly
            # broken French elisions like "L'", "d'", "l'", "n'"
            if len(stripped) <= 5 and stripped.endswith("'") and i + 1 < len(lines):
                next_line = lines[i + 1]
                next_stripped = next_line.strip()

                # Skip if next line is a known non-continuation
                if self._is_contributor_phrase(next_stripped):
                    result.append(line)
                    i += 1
                    continue
                if self._is_publication_line(next_stripped):
                    result.append(line)
                    i += 1
                    continue

                # Only merge if the next line starts with a lowercase
                # letter — indicates a continuation of the same word/phrase,
                # not a new title line or author name
                if next_stripped and next_stripped[0].islower():
                    # Check the next line doesn't start with
                    # author/publisher keywords
                    next_lower = next_stripped.lower()
                    skip_merge = any(
                        next_lower.startswith(kw)
                        for kw in (
                            "par ",
                            "by ",
                            "du ",
                            "avec ",
                            "isbn",
                            "éditeur",
                            "publisher",
                            "collection",
                            "volume",
                            "tome",
                            "©",
                            "droits",
                        )
                    )
                    # "de" is a title article, not a continuation marker
                    # — don't skip on "de" alone, but do skip on "de "
                    # followed by a capital (e.g. "de la" in a title)
                    if "de " in next_lower[:4]:
                        skip_merge = True

                    if not skip_merge:
                        merged = line + next_line
                        result.append(merged)
                        i += 2
                        continue

            result.append(line)
            i += 1

        return result

    def _is_contributor_phrase(self, line: str) -> bool:
        """Check if a line is a contributor/collaboration phrase.

        Returns True if the line matches a known contributor phrase pattern
        (e.g. "avec la collaboration de", "sous la direction de").
        Handles OCR character confusion (e.g. 'I' for 'l').
        """
        lower = line.lower().strip()
        # First try exact matching
        for phrase in self.CONTRIBUTOR_PHRASES:
            if lower.startswith(phrase) or lower == phrase:
                return True
            if re.match(re.escape(phrase), lower):
                return True
        # Then try with OCR character confusion tolerance
        # (e.g. "avec Ia collaboration de" where I is misread as l)
        for phrase in self.CONTRIBUTOR_PHRASES:
            if self._fuzzy_match_phrase(lower, phrase):
                return True
        return False

    def _fuzzy_match_phrase(self, text: str, phrase: str, max_errors: int = 1) -> bool:
        """Match a phrase at the start of text with OCR character tolerance.

        Handles common OCR confusions: I<->l, 0<->O, 1<->l, etc.
        Only applies to the phrase portion (not the full line).
        """
        phrase_lower = phrase.lower()
        if not text.startswith(phrase_lower[:3]):
            return False  # Quick filter: first 3 chars must match roughly
        text_start = text[: len(phrase_lower)]
        if len(text_start) < len(phrase_lower):
            text_start = text_start.ljust(len(phrase_lower))
        # Count character-level mismatches, treating common OCR confusions
        # as non-mismatches
        ocr_confusions = {
            ("i", "l"),
            ("l", "i"),
            ("o", "0"),
            ("0", "o"),
            ("1", "l"),
            ("!", "i"),
            ("n", "u"),
            ("u", "n"),
        }
        mismatches = 0
        for text_char, phrase_char in zip(text_start, phrase_lower):
            if text_char == phrase_char:
                continue
            if (text_char, phrase_char) in ocr_confusions:
                continue
            mismatches += 1
        return mismatches <= max_errors

    def _get_contributor_line_indices(self, lines: List[str]) -> Set[int]:
        """Identify line indices that are contributor phrases or names.

        When a line matches a contributor phrase (e.g. "avec la collaboration de"),
        that line and subsequent name-like lines are marked as contributor-related
        so they are excluded from title candidates.

        Also handles the case where a contributor phrase is split across
        lines (e.g. "postface" on one line and "de Maurice Gahail" on the
        next), by checking if a line is a prefix of a known contributor phrase
        and the following line continues it.

        Args:
            lines: Raw OCR lines.

        Returns:
            Set of indices (0-based) that should be excluded from title candidates.
        """
        contributor_lines: Set[int] = set()
        for i, line in enumerate(lines):
            if self._is_contributor_phrase(line):
                contributor_lines.add(i)
                # Also mark subsequent name-like lines as contributor lines
                # Limit to 3 lines max to avoid pulling in title text
                j = i + 1
                parts_collected = 0
                while j < len(lines) and parts_collected < 3:
                    next_line = lines[j].strip()
                    if not next_line:
                        break
                    # Stop if the next line is clearly not a name fragment
                    if not (
                        self._looks_like_author_name(next_line)
                        or (
                            next_line.isupper()
                            and len(next_line.split()) <= 3
                            and not self._is_title_like_line(next_line)
                        )
                    ):
                        break
                    contributor_lines.add(j)
                    j += 1
                    parts_collected += 1
            else:
                # Check if this line is a prefix of a contributor phrase
                # and the next line continues it (split across OCR lines).
                # e.g. "postface" + "de Maurice Gahail" -> "postface de Maurice Gahail"
                lower = line.lower().strip()
                for phrase in self.CONTRIBUTOR_PHRASES:
                    if lower == phrase.split()[0] or lower.startswith(
                        phrase.split()[0]
                    ):
                        # Combining with next line(s) forms a phrase?
                        combined = lower
                        for j in range(i + 1, min(i + 3, len(lines))):
                            next_lower = lines[j].strip().lower()
                            if not next_lower:
                                break
                            combined += " " + next_lower
                            combined = re.sub(r"\s+", " ", combined).strip()
                            if self._is_contributor_phrase(combined):
                                # Mark both the prefix line and continuation lines
                                contributor_lines.add(i)
                                for k in range(i + 1, j + 1):
                                    contributor_lines.add(k)
                                break
                        break
        return contributor_lines

    def _extract_contributor(self, lines: List[str]) -> Optional[str]:
        """Extract contributor/collaborator names from contributor phrases.

        Looks for lines like "avec la collaboration de <NAME>" and extracts
        the contributor name. Handles cases where the name appears on the
        lines after the phrase (common OCR artifact with wrapped text).

        This prevents the collaboration phrase from being misidentified
        as the book title.
        """
        for i, line in enumerate(lines):
            lower = line.lower().strip()
            for pattern in self.CONTRIBUTOR_PATTERNS:
                match = re.search(pattern, lower, re.IGNORECASE)
                if match:
                    contributor = match.group(1).strip()
                    contributor = re.sub(r"\s+", " ", contributor).rstrip(".,:;")
                    if contributor and len(contributor) > 1 and len(contributor) < 80:
                        if not self._is_contributor_phrase(contributor):
                            if contributor.lower() not in {
                                "de",
                                "et",
                                "la",
                                "le",
                                "des",
                                "du",
                                "par",
                                "pour",
                                "d'",
                                "l'",
                                "un",
                                "une",
                            }:
                                logger.info("Contributor found: %s", contributor)
                                return contributor
            # Cross-line contributor: phrase on one line, name on next lines
            if self._is_contributor_phrase(line):
                # Collect subsequent lines that look like name fragments
                # Limit to 3 lines max — a contributor name shouldn't span
                # more than 3 lines, and this prevents pulling in title text
                contributor_parts: list[str] = []
                j = i + 1
                while j < len(lines) and len(contributor_parts) < 3:
                    next_line = lines[j].strip()
                    if not next_line:
                        break
                    # Check if this looks like a continuation of the name
                    if self._looks_like_author_name(next_line) or (
                        next_line.isupper()
                        and len(next_line.split()) <= 3
                        and not self._is_title_like_line(next_line)
                    ):
                        contributor_parts.append(next_line)
                        j += 1
                    else:
                        break
                if contributor_parts:
                    contributor = " ".join(contributor_parts)
                    contributor = re.sub(r"\s+", " ", contributor).rstrip(".,:;")
                    if len(contributor) > 1 and len(contributor) < 80:
                        logger.info("Contributor found (cross-line): %s", contributor)
                        return contributor
        return None

    def _extract_author(self, lines: List[str]) -> Optional[str]:
        """Extract author name from text using multilingual patterns.

        Tries pattern matching on each line first, then falls back to
        author-name heuristics (preferring comma-format names).
        Skips contributor/collaboration lines so they're not mistaken for authors.
        """
        # Pre-check: if the first line is a clear multi-word author name
        # (3+ words, at least 2 capitalized, not all-caps), return it
        # immediately. This handles book covers where the author appears
        # first (e.g. "Miguel de Unamuno"), preventing the AUTHOR_PATTERNS
        # below from matching false attributions like "de Maurice Gahail"
        # on subsequent lines. Two-word lines are NOT pre-checked here.
        # Also handle ALL-CAPS author names (e.g. "HANNS HEINZ EWERS") which
        # are common on book covers — BUT reject all-caps lines that start
        # with a translator/contributor marker (e.g. "TRADUIT DU SERBE").
        if lines and not self._is_contributor_phrase(lines[0]):
            first_line = lines[0].strip()
            words = first_line.split()
            if len(words) >= 3 and sum(1 for w in words if w and w[0].isupper()) >= 2:
                first_word = words[0].lower().rstrip(".")
                starts_with_contributor = first_word in {
                    "traduit",
                    "traduction",
                    "traducteur",
                    "préface",
                    "postface",
                    "introduction",
                    "illustrations",
                    "edited",
                    "translated",
                    "foreword",
                }
                # Reject lines containing publisher names — series/publisher
                # text (e.g. "ÉCRITS SUR L'ART L'AGE D'HOMME") is not an author
                contains_publisher = any(
                    kw in first_line.lower()
                    for kw in ("l'age d'homme", "l'âge d'homme", "éditions", "édition")
                )
                if (
                    first_word not in self._TITLE_ARTICLES
                    and first_word not in self._ATTRIBUTION_KEYWORDS
                    and not starts_with_contributor
                    and not contains_publisher
                ):
                    logger.info("Author found via heuristic (line 0): %s", first_line)
                    return first_line

        # Try author attribution patterns first (skip contributor phrase lines).
        # These patterns catch explicit attributions like "par Victor Hugo",
        # "by John Doe", "de Victor Hugo". The "de" pattern anchors to line
        # start (^de) and excludes French title articles to avoid matching
        # title fragments like "DE LESPRIT ET" or "de la CREATION".
        # Also try author-name heuristics for multi-word first lines
        # (handles cases where author is split across lines like "Paul" + "Garde")
        # This handles the pattern where OCR splits a 2-word name across lines.
        # The combined name must look like a real person name (2 capitalized
        # words) — all-caps title fragments like "HISTOIRES" + "DAMOUR" fail
        # the _looks_like_author_name check (all-caps 2-word lines are
        # penalized), so this loop won't hijack titles.
        if len(lines) >= 2:
            for start_idx in range(len(lines) - 1):
                line1 = lines[start_idx].strip()
                line2 = lines[start_idx + 1].strip()
                if (
                    line1
                    and line2
                    and len(line1.split()) == 1
                    and len(line2.split()) == 1
                    and line1[0].isupper()
                    and line2[0].isupper()
                    and not line1.isupper()
                    and not line2.isupper()
                    and not self._is_contributor_phrase(line1)
                    and not self._is_contributor_phrase(line2)
                    and not self._preceded_by_contributor_marker(lines, line1)
                ):
                    combined = f"{line1} {line2}"
                    if self._looks_like_author_name(combined):
                        logger.info(
                            "Author found via multi-line heuristic: %s", combined
                        )
                        return combined

        for line in lines:
            if self._is_contributor_phrase(line):
                continue
            # Skip lines that are contributor markers (par, by) at start
            # These indicate the following text is a contributor, not author
            lower = line.lower().strip()
            if lower in {"par", "by", "de", "traduit par", "traduction de"}:
                continue
            for pattern in self.AUTHOR_PATTERNS:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    author = match.group(1).strip()
                    author = re.sub(r"\s+", " ", author).rstrip(".,:;")
                    if len(author) > 1 and len(author) < 100:
                        # Reject if the extracted author is a known publisher name
                        if author.lower().strip() in self._KNOWN_PUBLISHER_NAMES:
                            logger.info(
                                "Rejecting publisher name as author: %s", author
                            )
                            continue
                        # Reject if it looks like a contributor fragment
                        # (e.g. "Anne Yelev. Pierre" after "par")
                        # Heuristic: if the line starts with a contributor marker,
                        # the extracted text is likely a contributor, not the author
                        if self._is_contributor_fragment(author, lines, line):
                            logger.info(
                                "Rejecting contributor fragment as author: %s", author
                            )
                            continue
                        logger.info("Author found via pattern: %s", author)
                        return author

        # Try author-name heuristics — prefer comma-format names
        # on ALL lines (including first line, which is typically the
        # author on book covers). Skip lines starting with attribution
        # keywords (handled by AUTHOR_PATTERNS above).
        for line in lines:
            if self._is_contributor_phrase(line):
                continue
            if "," in line and self._looks_like_author_name(line):
                # Check if preceding line was a contributor/translator marker
                if self._preceded_by_contributor_marker(lines, line):
                    logger.info(
                        "Skipping comma-format author after contributor marker: %s",
                        line,
                    )
                    continue
                return line.strip()
        # Then try other lines for author-name heuristics
        for line in lines:
            if self._is_contributor_phrase(line):
                continue
            if self._looks_like_author_name(line):
                # Reject if the extracted author is a known publisher name
                if line.strip().lower() in self._KNOWN_PUBLISHER_NAMES:
                    logger.info("Rejecting publisher name as author: %s", line)
                    continue
                # Check if preceding line was a contributor/translator marker
                if self._preceded_by_contributor_marker(lines, line):
                    logger.info("Skipping author after contributor marker: %s", line)
                    continue
                return line.strip()

        return None

    def _preceded_by_contributor_marker(
        self, lines: List[str], current_line: str
    ) -> bool:
        """Check if the current line is preceded by a contributor/translator marker.

        This prevents translator credits like 'JAN DUSAY' from being picked up
        as the author when they follow 'TRADUIT DE LALLEMAND PAR'.

        Looks back up to 4 lines to handle multi-line contributor blocks.
        """
        try:
            idx = lines.index(current_line)
        except ValueError:
            return False

        # Check the preceding 4 lines for contributor markers
        # (contributor blocks can span multiple lines: "par" + 2-3 names)
        for i in range(max(0, idx - 4), idx):
            prev = lines[i].lower().strip()
            # Exact match - "par" and "by" are clear contributor markers
            # "de" is NOT included because it's ambiguous (can be title article)
            if prev in {
                "par",
                "by",
                "traduit par",
                "traduction de",
                "traduit de lallemand par",
                "traduit de l'anglais par",
                "traduit de l'espagnol par",
                "traduit du polonais par",
                "traduit du",
                "avec la collaboration de",
                "translated by",
                "edited by",
            }:
                return True
            # Prefix match (handles "traduit de lallemand par",
            # "traduit du serbe", "traduit du polonais par", etc.)
            for marker in {
                "par ",
                "by ",
                "traduit par",
                "traduction de",
                "traduit de",
                "traduit du",
            }:
                if prev.startswith(marker):
                    return True

        return False

    def _is_contributor_fragment(
        self, extracted: str, lines: List[str], current_line: str
    ) -> bool:
        """Check if extracted text is a contributor fragment, not the real author."""
        return self._preceded_by_contributor_marker(lines, current_line)

    def _looks_like_author_name(self, line: str) -> bool:
        """Check if a line looks like an author name (not title, ISBN, etc)."""
        if not line or len(line) > 60 or len(line) < 3:
            return False
        lower = line.lower()
        if any(re.search(rf"\b{kw}\b", lower) for kw in self.PUBLISHER_KEYWORDS):
            return False
        if "isbn" in lower:
            return False
        if any(c.isdigit() for c in line) and not re.search(r"^[A-Z][a-z]", line):
            return False
        # Skip contributor phrase lines
        if self._is_contributor_phrase(line):
            return False
        words = line.split()
        if len(words) < 2:
            return False
        # Lines starting with common title articles (Le, La, The, etc.)
        # are likely titles, not author names
        first_word_lower = words[0].lower().rstrip(".")
        if first_word_lower in self._TITLE_ARTICLES:
            return False
        # Lines starting with attribution keywords (par, by, de, published)
        # are attribution phrases, not standalone author names.
        # The AUTHOR_PATTERNS loop handles these separately.
        if first_word_lower in self._ATTRIBUTION_KEYWORDS:
            return False
        has_comma = "," in line
        capitalized_words = sum(1 for w in words if w and w[0].isupper())
        if has_comma:
            # Format: "Surname, First" or "Kuffer, Jean-Louis"
            return capitalized_words >= 2
        elif len(words) >= 3:
            # Format: "Mary Anne von Smith" (3+ words, all capitalized)
            return capitalized_words >= 2 and not line.isupper()
        else:
            # 2-word lines: could be author ("Jean-Louis Kuffer")
            # or title ("The Hobbit"). Match if both words are substantial
            # (2+ chars) and capitalized — "The Hobbit" excluded by article check
            return (
                capitalized_words >= 2
                and "." in line
                or all(len(w) >= 2 and w[0].isupper() for w in words)
            )

    def _extract_publisher(self, lines: List[str]) -> Optional[str]:
        """Extract publisher from lines."""
        for line in lines:
            lower = line.lower()
            for kw in self.PUBLISHER_KEYWORDS:
                if re.search(rf"\b{kw}\b", lower):
                    # Remove the keyword and keep the rest
                    cleaned_line = re.sub(rf"(?i)\b{kw}\b[\s:]*", "", line).strip()
                    if cleaned_line and len(cleaned_line) > 2:
                        return cleaned_line
        return None

    def _extract_edition(self, text: str) -> Optional[str]:
        """Extract edition information."""
        # First try numeric patterns
        for pattern in self.EDITION_PATTERNS[:3]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return f"{match.group(1)} edition"
        # Then try word-based ordinals
        match = re.search(
            r"(First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth)\s+edition",
            text,
            re.IGNORECASE,
        )
        if match:
            return match.group(1) + " edition"
        return None

    def _detect_language(self, text: str) -> str:
        """Detect language from text (en or fr)."""
        french_words = {
            "le",
            "la",
            "les",
            "un",
            "une",
            "des",
            "du",
            "de",
            "par",
            "édition",
            "français",
            "être",
            "et",
        }
        words = set(re.findall(r"\b\w+\b", text.lower()))
        french_ratio = len(words & french_words) / max(len(words), 1)
        if french_ratio > 0.15:
            return "fr"
        return "en"

    def _is_publication_line(self, line: str) -> bool:
        """Check if a line is publication/boilerplate text, not a title.

        Publication lines include: collection names, edition numbers,
        ISBN labels, prices, URLs, copyright notices.
        """
        lower = line.lower().strip()
        for kw in self.PUBLICATION_KEYWORDS:
            if kw in lower:
                return True
        # Pure numbers (edition/volume numbers, prices)
        if re.match(r"^[0-9\s.,$€£]+$", lower):
            return True
        # Single digit or short numeric fragment (OCR split artifact)
        if re.match(r"^[0-9]{1,2}$", lower):
            return True
        # URLs, email, www
        if re.search(r"(www\.|http|@|mailto)", lower):
            return True
        return False

    def _is_title_like_line(self, line: str) -> bool:
        """Check if a line looks like a title (not a contributor name).

        Used to prevent cross-line contributor extraction from pulling
        in book title text.
        """
        lower = line.lower().strip()
        first_word = lower.split()[0].rstrip("'.'")
        if first_word in self._TITLE_ARTICLES:
            return True
        # Lines with title-like structure (e.g. "LE CHRÉTIEN DANS LA CITÉ")
        words = line.split()
        if len(words) >= 2:
            return False  # Multi-word lines are unlikely to be names
        return False

    def _looks_like_publisher_fragment(self, line: str) -> bool:
        """Check if a line looks like a fragmented publisher name from OCR.

        OCR often splits publisher names across lines, producing fragments
        like 'MESSA', 'LAGE', 'HOMME' from 'MESSAGES L'AGE D'HOMME'.
        These should not be considered as title candidates.
        """
        lower = line.lower().strip()
        # Common publisher-name fragments that appear on book covers
        publisher_fragments = {
            "mess",
            "messag",
            "lage",
            "homme",
            "édition",
            "éditeur",
            "collection",
            "press",
            "publish",
        }
        words = lower.split()
        matched = sum(
            1 for w in words if any(frag in w for frag in publisher_fragments)
        )
        return matched > 0

    def _score_title_candidate(
        self, line: str, extracted_author: Optional[str] = None
    ) -> float:
        """Score how likely a line is to be a book title (0.0 to 1.0).

        Higher = more title-like. Lines with 0.0 are excluded from consideration.
        """
        if not line or len(line) < self._MIN_TITLE_LENGTH:
            return 0.0
        if len(line) > self._MAX_TITLE_LINE_LENGTH:
            return 0.0

        lower = line.lower().strip()

        # Exclude contributor phrases
        if self._is_contributor_phrase(line):
            return 0.0

        # Exclude publication/boilerplate lines
        if self._is_publication_line(line):
            return 0.0

        # Exclude ISBN lines
        if "isbn" in lower:
            return 0.0

        # Exclude edition lines
        if any(re.search(p, lower) for p in self.EDITION_PATTERNS):
            return 0.0

        # Exclude publisher keyword lines
        if any(re.search(rf"\b{kw}\b", lower) for kw in self.PUBLISHER_KEYWORDS):
            return 0.0

        # Exclude contributor phrase lines (e.g. "postface de", "traduction de")
        # These describe collaborators, not titles
        if self._is_contributor_phrase(line):
            return 0.0
        # Also exclude lines that are a prefix of a contributor phrase
        # (e.g. "postface" when OCR split "postface de" across lines)
        lower_for_phrase = lower.rstrip(".")
        for phrase in self.CONTRIBUTOR_PHRASES:
            phrase_parts = phrase.split()
            if len(phrase_parts) >= 2 and lower_for_phrase == phrase_parts[0]:
                return 0.0

        # Exclude lines starting with author attribution keywords
        # (par, by) — these are attribution lines, not titles.
        # Note: "de" is EXCLUDED here because it's also a French title
        # article (e.g. "DE L'ESPRIT ET" is part of the title
        # "la violence DE L'ESPRIT ET..."). The "de" attribution pattern
        # in AUTHOR_PATTERNS handles real de-attributions via ^de with
        # article exclusion.
        first_word_lower = lower.split()[0].rstrip(".") if lower.split() else ""
        if first_word_lower in {"par", "by", "published"}:
            return 0.0

        score = 0.0

        # All-caps lines are often titles on book covers
        if line.isupper() and len(line) > 3:
            score += 0.3
        # Title article prefix (Le, La, The) is common for titles
        first_word = line.split()[0].lower().rstrip(".")
        if first_word in self._TITLE_ARTICLES:
            score += 0.2
        # Longer bonus: longer lines are more likely to be real titles
        # (short all-caps fragments like "GES" are OCR garbage)
        word_count = len(line.split())
        if word_count >= 2:
            score += 0.15
        if word_count >= 3:
            score += 0.10
        if word_count >= 4:
            score += 0.05

        # Lines that are names (author-like) should not be titles.
        # However, if the author is already extracted and this line does
        # NOT match the author, it may be a title fragment (e.g. "DANS LORDRE"
        # on a cover is part of the title, not an author name).
        # Only exclude if: (a) extracted_author is None (unknown author —
        # conservative, exclude name-like lines), OR (b) the line matches
        # the extracted author (it IS the author).
        if self._looks_like_author_name(line):
            if extracted_author is None or self._normalizer.normalize(
                line.lower()
            ) == self._normalizer.normalize(extracted_author.lower().strip(".,:;'\"")):
                return 0.0
            # Looks like an author name but doesn't match the extracted author.
            # Penalize: some title fragments look name-like (e.g. "DANS LORDRE",
            # "HISTOIRES DAMOUR"). But 2-word all-caps lines with apostrophes in
            # the first word (e.g. "LE' CORBUSIER") are almost certainly names,
            # not titles. Return 0.0 for those; apply a moderate penalty for
            # others to deprioritize them without fully excluding them.
            first_word = line.split()[0].lower()
            if "'" in first_word:
                return 0.0
            # Only penalize all-caps name-like lines (common on covers as
            # authors/publishers). Mixed-case 2-word lines (e.g. "Abel Sanchez")
            # can be legitimate titles and should not be penalized.
            if line.isupper():
                score -= 0.30

        # Exclude cover-art description lines (e.g. "ÉCRITS SUR L'ART",
        # "TEXT ON COVER", "ILLUSTRATION BY") — these describe artwork/printing
        # on the cover, NOT the book title. Only applies to all-caps lines
        # to avoid excluding legitimate title text containing "art" or "sur".
        if line.isupper():
            # Strip accents for matching (é -> e) so "ÉCRITS SUR" -> "ecrits sur"
            _normalized = unicodedata.normalize("NFKD", line.lower())
            _no_accent = "".join(c for c in _normalized if not unicodedata.combining(c))
            _cleaned = re.sub(r"[\s']+", " ", _no_accent).strip()
            line_lower = self._normalizer.normalize(line.lower())
            cover_art_indicators = {
                "ecrits sur",
                "text on",
                "written on",
                "illustration",
                "illustre par",
                "illustrated by",
                "photographie",
                "photo by",
                "graphisme",
                "design by",
            }
            if any(ind in line_lower for ind in cover_art_indicators) or any(
                ind in _cleaned for ind in cover_art_indicators
            ):
                return 0.0

        # Mixed case (sentence style) often indicates titles
        if any(c.islower() for c in line[1:]):
            score += 0.05

        # Penalize lines with very short words (OCR fragmentation artifacts)
        # e.g. "GES", "D", "MASSA" — single letters or 2-3 char fragments
        # IMPORTANT: exempt common French/English title function words ("de",
        # "la", "le", "et", "du", "des", etc.) — these are legitimate parts of
        # titles that OCR may have split into separate lines (e.g. "de la" from
        # "Les Désastres de la guerre"). Penalizing them destroys multi-line
        # title reconstruction by scoring them below the 0.10 minimum threshold.
        _COMMON_TITLE_WORDS = frozenset(
            {
                "de",
                "le",
                "la",
                "les",
                "et",
                "du",
                "des",
                "un",
                "une",
                "l'",
                "d'",
                "t'",
                "qu'",
                "d",
                "l",
                "s",
                "m",
                "n",
                "the",
                "a",
                "an",
                "of",
                "and",
                "or",
                "in",
                "on",
                "to",
            }
        )
        short_word_count = sum(
            1
            for w in line.split()
            if len(w) <= 2 and w.lower().rstrip(".") not in _COMMON_TITLE_WORDS
        )
        if short_word_count > 0:
            score -= 0.15 * short_word_count

        # Penalize lines ending with common OCR-fragmented publisher keywords
        # e.g. "MESSA LAGE HOMME" is a fragment of "MESSAGES L'AGE D'HOMME"
        # which is a publisher name, not a title
        publisher_fragment_lower = lower
        for kw in [
            "homme",
            "mess",
            "lage",
            "messag",
            "édition",
            "éditeur",
            "vent",
            "d'ou",
            "ouest",
            "est",
        ]:
            if kw in publisher_fragment_lower and self._looks_like_publisher_fragment(
                line
            ):
                score -= 0.30
                break

        # Penalize lines that look like publisher logo/graphic text.
        # Publisher logos often repeat words (e.g. "VENT DEST VENT D'OU OUEST")
        # or contain directional terms (vent = wind, d'ou = from where).
        # These are NOT book titles.
        words_lower = lower.split()
        has_publisher_logo = len(words_lower) >= 2 and any(
            kw in words_lower
            for kw in (
                "vent",
                "dest",
                "ouest",
                "d'ou",
                "sud",
                "nord",
                "est",
            )
        )
        if has_publisher_logo:
            score -= 0.20

        # Penalize single-word titles that are very short (OCR fragments
        # like "DEBOUT," — note the trailing comma is an OCR artifact)

        # Single-word titles: only reject very short fragments or function words
        if word_count == 1:
            single_word_titles = {
                "et",
                "de",
                "la",
                "le",
                "les",
                "du",
                "des",
                "par",
                "pour",
            }
            if len(line) < 4 or lower in single_word_titles:
                return 0.0
            # Single-word title (e.g. "Dune"): valid but lower base score
            score += 0.10
            # Penalize single-word all-caps fragments (e.g. "INIVS", "LAGE").
            # Real single-word titles are uncommon on book covers; a 5-letter
            # all-caps word is more likely an OCR fragment of a publisher name
            # or title split across lines. The multi-line bonus in
            # get_candidate_score then ensures the real title outranks it.
            if line.isupper():
                score -= 0.20

        # Minimum score threshold: lines scoring below 0.1 are not real titles
        if score < 0.10:
            return 0.0

        return min(score, 0.7)

    def _extract_title_candidates(
        self, lines: List[str], extracted: Dict
    ) -> Tuple[Optional[str], List[str]]:
        """Extract title candidates from OCR text with evidence-based scoring.

        Instead of blindly picking the first non-excluded line as the title,
        this method:
        1. Scores each line as a potential title
        2. Groups consecutive high-scoring lines (for multi-line titles)
        3. Returns the best-scoring candidate as the title and all candidates

        Args:
            lines: Raw OCR lines (stripped, non-empty).
            extracted: Already-extracted fields (author, publisher, etc.)

        Returns:
            Tuple of (best_title, title_candidates) where title_candidates
            is a list of all plausible title candidates (best first).
        """
        if not lines:
            return None, []

        # Build excluded phrases (author, publisher) to avoid matching them as titles
        excluded_phrases_lower: List[str] = []
        if extracted.get("author"):
            excluded_phrases_lower.append(extracted["author"].lower().strip(".,:;"))
        if extracted.get("publisher"):
            excluded_phrases_lower.append(
                extracted["publisher"].lower().strip('.,:;"').strip()
            )

        # Identify contributor-related line indices to exclude from title candidates
        contributor_lines = self._get_contributor_line_indices(lines)

        # Score each line as a title candidate
        line_scores = []
        for idx, line in enumerate(lines):
            score = self._score_title_candidate(line, extracted.get("author"))

            # Skip lines that are exactly the extracted author/publisher
            line_stripped = line.lower().strip('.,:;"')
            if any(ep in line_stripped for ep in excluded_phrases_lower):
                score = 0.0

            # Exclude lines that are contributor phrases or part of a
            # contributor name (prevents "AARON R KAYAYAN" from being a title)
            if idx in contributor_lines:
                score = 0.0

            line_scores.append((line, score))

        # Collect candidate lines (score > 0)
        candidate_entries = [
            (line, score) for line, score in line_scores if score > 0.0
        ]
        line_indices = [i for i, (line, score) in enumerate(line_scores) if score > 0.0]

        # Group consecutive candidate lines into multi-line title candidates
        grouped_candidates: List[str] = []
        i = 0
        while i < len(candidate_entries):
            group = [candidate_entries[i][0]]
            group_score = candidate_entries[i][1]
            first_orig_idx = line_indices[i]

            # Try to merge with next consecutive candidate lines
            j = i + 1
            while j < len(candidate_entries):
                next_line, next_score = candidate_entries[j]
                next_orig_idx = line_indices[j]
                # Check if lines are adjacent in original OCR text
                if next_orig_idx == first_orig_idx + len(group):
                    # Only merge if the next line scores above 0.0 (i.e. it's
                    # a potential title fragment, not an excluded/garbage line).
                    # Lower threshold (0.1) allows single-word title fragments
                    # like "communauté", "Raison" to merge with the preceding
                    # title line, since these score 0.10 due to word-count bonus.
                    first_word = group[0].split()[0].lower().rstrip(".")
                    if next_score >= 0.1:
                        # If first line starts with a title article, be
                        # conservative about merging — only merge if the
                        # next line also looks title-like
                        if first_word in self._TITLE_ARTICLES:
                            # Only merge multi-word lines or lines with title articles
                            next_word_count = len(next_line.split())
                            next_first = next_line.split()[0].lower().rstrip(".")
                            if (
                                next_word_count >= 2
                                or next_first in self._TITLE_ARTICLES
                            ):
                                group.append(next_line)
                                group_score += next_score * 0.5
                                j += 1
                                continue
                            # Single-word fragment after a title-article-starting
                            # title — do not merge (e.g. "Romar" into "Le Mal Été").
                            break
                        group.append(next_line)
                        group_score += next_score * 0.5
                        j += 1
                    else:
                        break
                else:
                    # Check if the gap between current group and next
                    # candidate is bridgeable (short function words like
                    # "de", "et" that connect title fragments).
                    gap_start = first_orig_idx + len(group)
                    gap_end = next_orig_idx
                    gap_lines = lines[gap_start:gap_end]
                    # All gap lines must be short function words (score 0.0 but
                    # legitimate title fragments like "de", "et", "la")
                    if gap_lines and all(
                        gl.strip().lower().rstrip(".") in self._BRIDGE_WORDS
                        and not gl.strip().isupper()
                        for gl in gap_lines
                        if gl.strip()
                    ):
                        # Include the bridge words
                        for gl in gap_lines:
                            gl_stripped = gl.strip()
                            if gl_stripped:
                                group.append(gl_stripped)
                        # Apply the same conservative merge check as adjacent
                        # lines: if the title starts with a title article and
                        # the next candidate is a single word that doesn't
                        # start with a title article, don't merge (e.g.
                        # "Roman" after "Les Désastres" + bridge "de la").
                        first_word = group[0].split()[0].lower().rstrip(".")
                        if first_word in self._TITLE_ARTICLES:
                            next_word_count = len(next_line.split())
                            next_first = next_line.split()[0].lower().rstrip(".")
                            if not (
                                next_word_count >= 2
                                or next_first in self._TITLE_ARTICLES
                            ):
                                break
                        group.append(next_line)
                        group_score += next_score * 0.5
                        j += 1
                        continue
                    break

            combined = " ".join(group)
            combined = re.sub(r"\s+", " ", combined).strip()
            if combined and len(combined) >= self._MIN_TITLE_LENGTH:
                grouped_candidates.append(combined)
            i += len(group)

        if not grouped_candidates:
            return None, []

        # Sort candidates by their combined score (approximate)
        # Higher-scoring lines come first
        # For grouped candidates, use the average line score as proxy
        def get_candidate_score(candidate_text: str) -> float:
            """Get a score for a (possibly multi-word) title candidate."""
            # Match candidate text against line_scores by position
            # to handle duplicate lines correctly
            total_score = 0.0
            matched_lines = 0
            scores = []
            for orig_line, score in line_scores:
                if score <= 0.0:
                    # Skip lines that are not title candidates (score 0.0).
                    # This prevents false substring matches — e.g. "LA" (score 0.0)
                    # matching inside "LA VIOLENCE", dragging down the average
                    # and causing a garbled last line to outrank the correct title.
                    continue
                # For multi-line candidates, a line may be a substring of the
                # combined candidate text (e.g. "HISTOIRES" in "HISTOIRES DAMOUR").
                # Use word-boundary matching to avoid partial matches.
                if re.search(
                    r"(?<!\w)" + re.escape(orig_line) + r"(?!\w)", candidate_text
                ):
                    total_score += score
                    scores.append(score)
                    matched_lines += 1
            avg_score = total_score / max(matched_lines, 1)
            # Use the best line score as a floor, so a multi-line title
            # candidate containing at least one strong title line (e.g.
            # "LA VIOLENCE" at 0.50) is not outranked by a single garbled
            # line (e.g. "VIULEIYCE" at 0.40).
            best_score = max(scores) if scores else 0.0
            final_score = max(avg_score, best_score * 0.90)
            # Apply a small per-line penalty for multi-line candidates
            if matched_lines > 1:
                final_score *= 0.95 ** (matched_lines - 1)
                # Bonus for multi-line candidates: real titles are typically
                # multi-line on book covers, while garbled OCR fragments are
                # often single words. This bonus helps multi-line candidates
                # outrank single-word all-caps fragments like "INIVS" or "LAGE".
                final_score *= 1.10
            return final_score

        grouped_candidates.sort(key=get_candidate_score, reverse=True)

        # Return best candidate as title, all as candidates
        best_title = grouped_candidates[0]
        best_title = re.sub(r"\s+", " ", best_title).strip()

        return best_title, grouped_candidates

    def _extract_title(self, lines: List[str], extracted: Dict) -> Optional[str]:
        """Extract the title from OCR text.

        Uses the new multi-candidate approach. Returns the best-scoring
        title candidate, or falls back to the old method if no candidates
        score above the threshold.
        """
        title, _ = self._extract_title_candidates(lines, extracted)

        if title:
            return title

        # Fallback 1: try all-caps fragments (for covers where every line
        # is a short all-caps name-like fragment and author extraction failed)
        title = self._try_all_caps_fragments(lines, extracted)
        if title:
            return title

        # Fallback 2: old single-line approach (for backward compatibility)
        return self._extract_title_legacy(lines, extracted)

    def _try_all_caps_fragments(
        self, lines: List[str], extracted: Dict
    ) -> Optional[str]:
        """Handle all-caps fragments when all lines score 0.0 as title candidates.

        On some book covers, every OCR line is a short all-caps 2-word fragment
        (e.g. "LE' CORBUSIER", "LA CONSTRUCTION", "DES VILLES"). The scoring
        pipeline classifies these as author names and zeros them all out,
        leaving no candidates. When this happens:

        1. Try to identify the first line as the author (if not already extracted)
        2. Join the remaining lines as the title

        The author-like line is the one that does NOT contain a title article
        as its first word (e.g. "LE' CORBUSIER" has no title article, while
        "LA CONSTRUCTION" starts with "LA", "DES VILLES" starts with "DES").
        """
        if not lines or len(lines) < 2:
            return None

        # Only trigger when no other candidates were found
        title_candidates_result = self._extract_title_candidates(lines, extracted)
        if title_candidates_result[0] is not None:
            return None

        # Check if all lines are 2-word all-caps fragments (the failure pattern)
        name_like_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 3:
                continue
            words = stripped.split()
            if len(words) >= 2 and stripped.isupper():
                name_like_lines.append(stripped)

        if len(name_like_lines) < 2:
            return None

        author = extracted.get("author")
        excluded_author_lower = (
            self._normalizer.normalize(author.lower().strip(".,:;'\""))
            if author
            else None
        )
        # Find lines that look like author (no title article first word,
        # not matching extracted author, looks like a name)
        # and lines that look like title (title article first word)
        author_candidates = []
        title_line_indices = []
        title_lines = []

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or len(stripped) < 3:
                continue
            if not stripped.isupper():
                continue
            words = stripped.split()
            if len(words) < 2:
                continue
            first_word_lower = words[0].lower().rstrip(".")
            if first_word_lower in self._TITLE_ARTICLES:
                title_line_indices.append(i)
                title_lines.append(stripped)
            elif (
                author is None
                or self._normalizer.normalize(stripped.lower()) != excluded_author_lower
            ):
                author_candidates.append(stripped)

        # If we have title-like lines (starting with title articles),
        # join them into the title and use the first author-like line as author
        if title_lines:
            title = " ".join(title_lines)
            title = re.sub(r"\s+", " ", title).strip()
            # Don't include publisher-like fragments in the title
            title = re.sub(
                r"\b(mess|messag|l'age|d'homme|édition|éditeur|collection|press|publish)\b.*$",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()
            if len(title) >= 3:
                # Also set the author if we identified one and it wasn't already
                if author_candidates and author is None:
                    extracted["author"] = author_candidates[0]
                return title

        return None

    def _extract_title_legacy(self, lines: List[str], extracted: Dict) -> Optional[str]:
        """Legacy title extraction: first non-excluded line as title.

        Fallback when the new scoring approach finds no candidates.
        """
        if not lines:
            return None

        excluded_phrases_lower: List[str] = []
        if extracted.get("author"):
            excluded_phrases_lower.append(extracted["author"].lower().strip(".,:;"))
        if extracted.get("publisher"):
            excluded_phrases_lower.append(
                extracted["publisher"].lower().strip('.,:;"').strip()
            )

        candidate_lines: List[str] = []
        for line in lines:
            lower = line.lower()
            if "isbn" in lower:
                continue
            if any(re.search(p, lower) for p in self.EDITION_PATTERNS):
                continue
            if len(line) < 3:
                continue
            # Skip lines matching author attribution patterns
            if re.search(r"\b(?:published\s+by|par|de|by)\s+\S", lower):
                continue
            # Skip lines matching publisher keywords
            if any(re.search(rf"\b{kw}\b", lower) for kw in self.PUBLISHER_KEYWORDS):
                continue
            # Skip contributor phrase lines
            if self._is_contributor_phrase(line):
                continue
            # Skip publication/boilerplate lines
            if self._is_publication_line(line):
                continue
            # Skip lines that are exactly the extracted author/publisher
            line_stripped = lower.strip('.,:;"')
            if any(ep in line_stripped for ep in excluded_phrases_lower):
                continue
            candidate_lines.append(line)

        if candidate_lines:
            title = candidate_lines[0]
            title = re.sub(r"\s+", " ", title).strip()
            if len(title) >= 3:
                return title

        # Single-line fallback: strip all identified fields
        full_text = " ".join(lines)
        title = full_text

        # Remove ISBN patterns
        title = re.sub(
            r"\bisbn[-\s:]*(?:97[89][-\s\d]+|\d[-\s\dXx]*)+",
            "",
            title,
            flags=re.IGNORECASE,
        )

        # Remove author attribution patterns
        for pattern in self.AUTHOR_PATTERNS:
            title = re.sub(pattern, "", title, flags=re.IGNORECASE)

        # Remove contributor phrases and their content
        for phrase in self.CONTRIBUTOR_PHRASES:
            title = re.sub(rf"(?i){re.escape(phrase)}\s*.+", "", title)

        # Remove publisher keywords and surrounding text
        for kw in self.PUBLISHER_KEYWORDS:
            title = re.sub(rf"(?i)\b{kw}\b[\s:]*", "", title)

        # Remove "published by ..."
        title = re.sub(r"published\s+by\s+\S+", "", title, flags=re.IGNORECASE)

        # Remove extracted author/publisher phrases
        for phrase in excluded_phrases_lower:
            if phrase:
                title = re.sub(re.escape(phrase), "", title, flags=re.IGNORECASE)

        # Clean up
        title = re.sub(r"\s+", " ", title).strip(" ,.:;-")
        if len(title) >= 3:
            return title

        return None

    @staticmethod
    def _empty_result() -> Dict:
        return {
            "title": None,
            "title_candidates": [],
            "author": None,
            "contributor": None,
            "isbn": None,
            "publisher": None,
            "edition": None,
            "language": None,
        }
