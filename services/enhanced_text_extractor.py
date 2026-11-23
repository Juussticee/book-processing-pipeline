"""Enhanced book text extractor with layout-aware extraction.

Extends the base BookTextExtractor with:
- PaddleOCR-VL markdown/HTML artifact normalization (headings, img tags)
- Contributor role parsing
- Better title/author block selection using position signals
- Script detection for multilingual support
- Genre-word title penalties
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from core.logger import setup_logger
from services.book_text_extractor import BookTextExtractor
from services.script_detector import ScriptDetector

logger = setup_logger(__name__)

# --- PaddleOCR-VL output normalization ---

# Markdown heading prefixes (1-6 '#')
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")

# HTML tags emitted by PaddleOCR-VL markdown (img, div, br, p, span...)
_HTML_TAG_RE = re.compile(
    r"</?(?:div|img|br|p|span|a|b|i|em|strong)[^>]*>", re.IGNORECASE
)

# Whole-line HTML/image blocks (e.g. <div style=...><img .../></div>)
_HTML_LINE_RE = re.compile(r"^\s*<.*>\s*$")

# Genre words that are weak title candidates when STANDALONE
# (a single-word title that is just a genre). Multi-word titles
# containing these words are NOT penalized.
_GENRE_WORDS = frozenset(
    {
        "roman",
        "romans",
        "nouvelle",
        "nouvelles",
        "noüvelles",
        "poésie",
        "poemes",
        "poèmes",
        "poeme",
        "poème",
        "essai",
        "essais",
        "théâtre",
        "theatre",
        "conte",
        "contes",
        "récit",
        "récits",
        "recit",
        "recits",
        "chronique",
        "chroniques",
    }
)


def normalize_paddleocr_markdown(text: str) -> str:
    """Normalize PaddleOCR-VL markdown output to plain text lines.

    Removes OCR formatting artifacts WITHOUT touching book content:
    1. Markdown heading prefixes ('#', '##', ...) — PaddleOCR-VL emits
       these for large text blocks; they are not book content.
    2. HTML tags (<div>, <img ...>, <br>, <p>, ...) — emitted for
       embedded images/decorations.
    3. Lines that are ONLY an HTML tag (image placeholders) are dropped.

    '# PAWEL HUELLE WEISER DAVID'  ->  'PAWEL HUELLE WEISER DAVID'
    '<div ...><img .../></div>'    ->  (line removed)
    """
    if not text:
        return ""

    out_lines: List[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line

        # Drop lines that are purely an HTML tag (image placeholders)
        if _HTML_LINE_RE.match(line) and not re.search(r">\s*\w", line):
            continue

        # Strip inline HTML tags
        line = _HTML_TAG_RE.sub(" ", line)

        # Strip markdown heading prefixes
        line = _HEADING_RE.sub("", line)

        # Collapse whitespace from removals
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            out_lines.append(line)

    return "\n".join(out_lines)


class EnhancedBookTextExtractor(BookTextExtractor):
    """Enhanced extractor with layout-aware and multilingual support.

    Improvements over base BookTextExtractor:
    1. PaddleOCR-VL markdown/HTML normalization before extraction
    2. Explicit contributor role parsing before author extraction
    3. Better title block selection using line position and typography
    4. Script detection for Cyrillic/Arabic text
    5. Genre-word standalone title penalties
    6. Contributor markers terminate author extraction
    """

    # Additional contributor/role markers (French + English)
    _CONTRIBUTOR_MARKERS = frozenset(
        {
            "par",
            "by",
            "traduit",
            "traduction",
            "avec",
            "sous",
            "préface",
            "postface",
            "introduction",
            "édition",
            "éditorial",
            "edited",
            "translated",
            "foreword",
            "with",
            "contributions",
        }
    )

    # Common publisher names to exclude from title/author
    _KNOWN_PUBLISHERS = frozenset(
        {
            "l'age d'homme",
            "l'âge d'homme",
            "lage d'homme",
            "éditions",
            "editions",
            "collection",
            "série",
            "serie",
            "bibliothèque",
            "bibliotheque",
        }
    )

    def extract(self, ocr_text: str) -> Dict:
        """Enhanced extraction with normalization and role parsing."""
        if not ocr_text:
            return self._empty_result()

        # 1. Normalize PaddleOCR-VL markdown artifacts FIRST — before
        # script detection, author heuristics, or title scoring, so
        # '# PAWEL HUELLE' is evaluated exactly like 'PAWEL HUELLE'.
        ocr_text = normalize_paddleocr_markdown(ocr_text)

        # 2. Detect script
        scripts = ScriptDetector.detect_scripts(ocr_text)
        if "cyrillic" in scripts and "latin" not in scripts:
            # Pure Cyrillic text - flag for special handling
            logger.info("Cyrillic text detected, applying Cyrillic extraction")
            return self._extract_cyrillic(ocr_text)

        # 3. PaddleOCR-VL markdown uses blank lines between blocks; blank
        # lines break the adjacency check in multi-line title grouping
        # (next_orig_idx == first_orig_idx + len(group)). Strip them so
        # 'HISTOIRES D'AMOUR' + 'mais' + 'petites-bourgeoises' merge into
        # one title candidate.
        ocr_text = "\n".join(line for line in ocr_text.split("\n") if line.strip())

        result = super().extract(ocr_text)

        # 4. Post-extraction: split merged 'AUTHOR TITLE' blocks when the
        # author is missing (PaddleOCR-VL block-merge artifact).
        raw_lines = [
            line
            for line in ocr_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if line.strip()
        ]
        self._try_split_merged_author_title(raw_lines, result)

        return result

    def _extract_cyrillic(self, ocr_text: str) -> Dict:
        """Extract metadata from Cyrillic text.

        For Cyrillic books, we rely on the base extraction but with
        relaxed constraints since PaddleOCR may not handle Cyrillic optimally.
        """
        result = self._empty_result()

        # Split into lines
        lines = [
            line.strip()
            for line in ocr_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if line.strip()
        ]

        if not lines:
            return result

        # For Cyrillic: typically author at top, title below
        # Try first substantial line as author, next as title
        for line in lines:
            if len(line) >= 3 and not self._is_contributor_phrase(line):
                if result["author"] is None:
                    result["author"] = line
                elif result["title"] is None:
                    result["title"] = line
                    break

        return result

    def _extract_author(self, lines: List[str]) -> Optional[str]:
        """Enhanced author extraction with contributor termination."""
        # First, check if this is a multi-book image (too many books visible)
        if self._is_multi_book_image(lines):
            logger.info("Multi-book image detected, skipping author extraction")
            return None

        # Line-0 author guard: the base 3-word pre-check wrongly grabs
        # long title lines like 'Analyses de : VIE ET MORT DE LA
        # YOUGOSLAVIE de Paul Garde' (12 words, 57 chars). A real author
        # line is short. Guard: skip line-0 pre-check (both 2-word and
        # base 3-word) when the line is too long to be a name.
        if lines and not self._is_contributor_phrase(lines[0]):
            first_line = lines[0].strip()
            words = first_line.split()

            # Two-word mixed-case line-0 author (e.g. 'Angela Ghelber',
            # 'Jean Vuilleumier', 'Alexandre Tišma'). Requirements:
            # - exactly 2 words, both capitalized, at least 4 chars each
            # - NOT all-caps (all-caps 2-word lines are title fragments)
            # - not a title article / attribution start, not a publisher
            if len(words) == 2:
                w1, w2 = words
                if (
                    len(w1) >= 4
                    and len(w2) >= 4
                    and w1[0].isupper()
                    and w2[0].isupper()
                    and not first_line.isupper()
                    and w1.lower().rstrip(".") not in self._TITLE_ARTICLES
                    and w1.lower().rstrip(".") not in self._ATTRIBUTION_KEYWORDS
                    and first_line.lower() not in self._KNOWN_PUBLISHERS
                ):
                    logger.info(
                        "Author found via 2-word heuristic (line 0): %s", first_line
                    )
                    return first_line

            # Suppress the base 3-word pre-check for title-like lines:
            # long lines (> 40 chars or > 5 words) are titles, not names.
            if len(first_line) > 40 or len(words) > 5:
                lines = lines[1:] if len(lines) > 1 else lines

        return super()._extract_author(lines)

    def _try_split_merged_author_title(self, lines: List[str], extracted: Dict) -> None:
        """Split a merged 'AUTHOR TITLE' block when author is missing
        OR when the extracted author is actually a merged block.

        PaddleOCR-VL sometimes merges the author line and the title line
        into one block:

            'PAWEL HUELLE WEISER DAVID'      (author + title, 4 words)
            'LE CORBUSIER LA CONSTRUCTION DES VILLES'
            'Martine Magnaridè Entrez dans la danse'

        This happens in two ways:
        1. No author extracted, title is the merged block.
        2. The line-0 pre-check grabs the whole 4-7 word block as author
           (because it has 3+ capitalized words), leaving no title.
        """
        for source_key in ("title", "author"):
            block = extracted.get(source_key)
            if not block:
                continue
            # Only trigger for the right case:
            # - title block: when author is empty
            # - author block: when title is empty
            other_key = "author" if source_key == "title" else "title"
            if extracted.get(other_key):
                continue
            words = block.split()
            if not (4 <= len(words) <= 7):
                continue
            w1, w2, rest = words[0], words[1], words[2:]
            first_lower = w1.lower().rstrip(".,:;")
            second_lower = w2.lower().rstrip(".,:;")
            # Accept as name when:
            # - both capitalized, 3+ chars, AND
            # - first word is NOT a title article, OR
            # - first word IS a short article (le/la/de) but second word
            #   is ALL-CAPS proper noun (surname) — e.g. 'LE CORBUSIER',
            #   'DE GAULLE'. Title words like 'Désastres' (mixed case)
            #   must NOT trigger this.
            is_article_name = (
                first_lower in {"le", "la", "les", "de", "du"}
                and w2.isupper()
                and 3 <= len(w2) <= 12
            )
            if (
                w1[:1].isupper()
                and w2[:1].isupper()
                and (len(w1) >= 3 or is_article_name)
                and len(w2) >= 3
                and (first_lower not in self._TITLE_ARTICLES or is_article_name)
                and first_lower not in self._ATTRIBUTION_KEYWORDS
                and (second_lower not in self._TITLE_ARTICLES or is_article_name)
                and first_lower
                not in {
                    "classiques",
                    "collection",
                    "bibliothèque",
                    "bibliotheque",
                    "série",
                }
                and first_lower not in _GENRE_WORDS
                and second_lower not in _GENRE_WORDS
                and not self._is_contributor_or_credit_line(f"{w1} {w2}")
                and len(rest) >= 2
            ):
                # The first two words are the AUTHOR, the rest is the TITLE
                extracted["author"] = f"{w1} {w2}"
                extracted["title"] = " ".join(rest)
                logger.info(
                    "Split merged block: author=%r title=%r",
                    f"{w1} {w2}",
                    " ".join(rest),
                )
                return

        # Case 3: author is a 4-7 word merged block grabbed by the base
        # pre-check, and title is also set (to something weak). If the
        # author block looks like "AUTHOR TITLE", re-split it.
        author = extracted.get("author")
        title = extracted.get("title")
        if author and title:
            a_words = author.split()
            if 4 <= len(a_words) <= 7:
                w1, w2, rest = a_words[0], a_words[1], a_words[2:]
                first_lower = w1.lower().rstrip(".,:;")
                second_lower = w2.lower().rstrip(".,:;")
                is_article_name = (
                    first_lower in {"le", "la", "les", "de", "du"}
                    and w2.isupper()
                    and 3 <= len(w2) <= 12
                )
                if (
                    w1[:1].isupper()
                    and w2[:1].isupper()
                    and (len(w1) >= 3 or is_article_name)
                    and len(w2) >= 3
                    and (first_lower not in self._TITLE_ARTICLES or is_article_name)
                    and first_lower not in self._ATTRIBUTION_KEYWORDS
                    and (second_lower not in self._TITLE_ARTICLES or is_article_name)
                    and first_lower
                    not in {
                        "classiques",
                        "collection",
                        "bibliothèque",
                        "bibliotheque",
                        "série",
                    }
                    and first_lower not in _GENRE_WORDS
                    and second_lower not in _GENRE_WORDS
                    and not self._is_contributor_or_credit_line(f"{w1} {w2}")
                    and len(rest) >= 2
                ):
                    extracted["author"] = f"{w1} {w2}"
                    # Keep the better of the existing title and the split remainder
                    remainder = " ".join(rest)
                    if len(remainder) > len(title):
                        extracted["title"] = remainder
                    logger.info(
                        "Re-split merged author block: author=%r title=%r",
                        f"{w1} {w2}",
                        extracted["title"],
                    )

    def _is_multi_book_image(self, lines: List[str]) -> bool:
        """Detect if image contains multiple books.

        Heuristic: the SAME publisher name appearing twice (e.g.
        'L'Age d'Homme' on two separate books in a stack photo).
        A series name + publisher on the same book ('La Merveilleuse
        Collection' + 'Editions l'Age d'Homme') is ONE book — counting
        series+publisher as two publishers caused false multi-book
        detection on single-book covers.
        """
        publisher_hits: List[str] = []
        for line in lines:
            lower = line.lower()
            for pub in ["l'age d'homme", "l'âge d'homme", "éditions", "editions"]:
                if pub in lower:
                    publisher_hits.append(pub)
                    break

        # Multi-book only when the same publisher token appears 2+ times
        if not publisher_hits:
            return False
        counts: Dict[str, int] = {}
        for p in publisher_hits:
            counts[p] = counts.get(p, 0) + 1
        return any(c >= 2 for c in counts.values())

    def _is_likely_translator(self, line: str) -> bool:
        """Check if a line is a translator credit, not an author."""
        lower = line.lower().strip()

        translator_markers = [
            "traduit par",
            "traduction de",
            "translated by",
            "traduit de l",
            "traduit de l'anglais",
            "traduit de l'allemand",
            "traduit du",
            "traducteur",
        ]

        for marker in translator_markers:
            if lower.startswith(marker):
                return True

        return False

    def _get_contributor_line_indices(self, lines: List[str]) -> set:
        """Enhanced contributor-line detection.

        Base behavior: marks contributor-phrase lines + following
        name-like lines.

        Addition: once a standalone 'par' marker line appears, ALL
        subsequent name-like lines belong to the contributor section
        (e.g. 'par' / 'Anne Yelen, Pierre Maurer, Slobodan Despot')
        and must not become title candidates.
        """
        base = super()._get_contributor_line_indices(lines)

        # Find standalone 'par' marker lines; everything name-like after
        # them is a contributor name, not title material.
        par_seen = False
        for i, line in enumerate(lines):
            lower = line.lower().strip().strip(".,:;")

            # A standalone 'par' line, a translator / contributor credit,
            # or an OCR-mangled contributor fragment starts a section.
            if (
                lower == "par"
                or self._is_likely_translator(line)
                or self._is_contributor_or_credit_line(line)
            ):
                par_seen = True
                base.add(i)
                continue

            if par_seen:
                stripped = line.strip()
                if not stripped:
                    continue
                # Name-like lines after a credit line are contributor
                # names — including 'de <Name>' continuation fragments
                # (e.g. 'de Maurice Gahail' after ': et postface').
                if (
                    self._looks_like_author_name(stripped)
                    or stripped.lower().startswith("de ")
                    or (stripped[0].isupper() and len(stripped.split()) <= 6)
                ):
                    base.add(i)
                # A clearly title-like line (starts with title article,
                # long multi-word) may end the contributor section
                first_word = stripped.split()[0].lower().rstrip(".")
                if first_word in self._TITLE_ARTICLES or len(stripped.split()) >= 5:
                    par_seen = False

        return base

    def _extract_title_candidates(
        self, lines: List[str], extracted: Dict
    ) -> Tuple[Optional[str], List[str]]:
        """Enhanced title candidate extraction.

        Improvements:
        - Skip translator credits
        - Skip series headers
        - Better multi-line title grouping
        """
        if not lines:
            return None, []

        # Pre-filter: mark translator/contributor lines
        contributor_indices = self._get_contributor_line_indices(lines)

        # Also mark translator-only lines
        translator_indices = set()
        for i, line in enumerate(lines):
            if self._is_likely_translator(line):
                translator_indices.add(i)

        all_excluded = contributor_indices | translator_indices

        # Build excluded phrases
        excluded_phrases_lower: List[str] = []
        if extracted.get("author"):
            excluded_phrases_lower.append(extracted["author"].lower().strip(",:;"))
        if extracted.get("publisher"):
            excluded_phrases_lower.append(
                extracted["publisher"].lower().strip('.,:;"').strip()
            )

        # Score each line
        line_scores = []
        for idx, line in enumerate(lines):
            score = self._score_title_candidate(line, extracted.get("author"))

            # Additional exclusions
            if idx in all_excluded:
                score = 0.0

            line_stripped = line.lower().strip('.,:;"')
            if any(ep in line_stripped for ep in excluded_phrases_lower):
                score = 0.0

            # Skip series headers
            if self._is_series_header(line):
                score = 0.0

            # Skip contributor/translator credit lines (e.g. 'par',
            # 'PAR MADELEINE STEVANOV', 'TRADUIT DU SERBE', ': et postface')
            if score > 0 and self._is_contributor_or_credit_line(line):
                score = 0.0

            # Standalone genre word as title (e.g. 'Roman', 'Noüvelles'):
            # strong penalty — a single word that names a genre is almost
            # never the book's title. Multi-word titles containing genre
            # words ('Le Roman de Renart') are NOT penalized.
            if score > 0 and self._is_standalone_genre_word(line):
                score = 0.0
                logger.debug("Genre word rejected as title: %s", line)

            line_scores.append((line, score))

        # Filter and group candidates
        candidate_entries = [
            (line, score) for line, score in line_scores if score > 0.0
        ]

        if not candidate_entries:
            return None, []

        # Use base class grouping logic
        return self._group_title_candidates(candidate_entries, lines, line_scores)

    def _is_standalone_genre_word(self, line: str) -> bool:
        """True when the line is a single genre word (not a real title).

        'Roman' / 'Noüvelles' alone -> True (rejected as title)
        'Le Roman de Renart'        -> False (multi-word, evaluated normally)
        """
        stripped = line.strip().strip('.,:;"')
        if not stripped or len(stripped.split()) != 1:
            return False
        # Accent-tolerant lookup
        return stripped.lower() in _GENRE_WORDS or stripped.lower() in {
            "roman",
            "nouvelles",
            "nouvelle",
        }

    def _score_title_candidate(
        self, line: str, extracted_author: Optional[str] = None
    ) -> float:
        """Enhanced title scoring.

        Base behavior preserved, with one relaxation: a name-LIKE line
        with many words (7+) is too long to be a person's name — it is
        a title (e.g. 'Analyses de : VIE ET MORT DE LA YOUGOSLAVIE de
        Paul Garde', 12 words). The base class zeroes these out when no
        author is extracted yet, leaving no title candidates at all.
        """
        base_score = super()._score_title_candidate(line, extracted_author)

        if base_score == 0.0 and line and len(line) >= self._MIN_TITLE_LENGTH:
            # Recover over-long name-like lines as title candidates
            words = line.split()
            if (
                len(words) >= 7
                and len(line) <= self._MAX_TITLE_LINE_LENGTH
                and not self._is_contributor_or_credit_line(line)
                and not self._is_series_header(line)
                and not self._is_standalone_genre_word(line)
                and not self._is_publication_line(line)
                and "isbn" not in line.lower()
            ):
                logger.debug(
                    "Recovered long name-like line as title candidate: %s", line
                )
                return 0.30  # moderate: long titles are plausible but not top

        return base_score

    def _is_contributor_or_credit_line(self, line: str) -> bool:
        """True when a line is a contributor credit, not title material.

        Covers lines the base contributor-phrase check misses:
        - standalone 'par' marker lines
        - lines starting with 'par ' (e.g. 'PAR MADELEINE STEVANOV')
        - translator credits ('TRADUIT DU SERBE', 'ROMAN TRADUIT DU
          POLONAISE PAR FRANÇOIS ROSSET')
        - OCR-mangled contributor fragments (': et postface',
          'de Maurice Gahail')
        """
        lower = line.lower().strip()
        if lower == "par":
            return True
        if lower.startswith(("par ", "traduit ", "traduction ")):
            return True
        if " traduit " in f" {lower} " or " traduction " in f" {lower} ":
            return True
        # OCR-mangled contributor fragments: leading ':' or 'de <Name>'
        # after a postface/introduction marker on the previous line is
        # handled by _get_contributor_line_indices; here we catch lines
        # that themselves contain the marker words
        if lower.startswith(("postface", "préface", "introduction", ": et")):
            return True
        return False

    def _extract_title_legacy(self, lines: List[str], extracted: Dict) -> Optional[str]:
        """Enhanced legacy fallback: skip contributor/credit lines.

        The base legacy fallback picks the first non-excluded line, which
        can be a translator credit ('TRADUIT DU SERBE') when all real
        title lines were consumed. Filter credit lines here too.
        """
        filtered = [
            line for line in lines if not self._is_contributor_or_credit_line(line)
        ]
        if not filtered:
            filtered = lines
        return super()._extract_title_legacy(filtered, extracted)

    def _is_series_header(self, line: str) -> bool:
        """Check if a line is a series header (not a title)."""
        lower = line.lower().strip()

        series_markers = [
            "classiques slaves",
            "classiques",
            "collection",
            "série",
            "bibliothèque",
            "bibliotheque",
            "questions d'architecture",
        ]

        for marker in series_markers:
            if lower.startswith(marker) or lower == marker:
                return True

        return False

    def _group_title_candidates(
        self,
        candidate_entries: List[Tuple[str, float]],
        lines: List[str],
        line_scores: List[Tuple[str, float]],
    ) -> Tuple[Optional[str], List[str]]:
        """Group consecutive candidate lines into title candidates."""
        if not candidate_entries:
            return None, []

        # Simple grouping: merge adjacent candidates
        groups = []
        current_group = [candidate_entries[0][0]]

        for i in range(1, len(candidate_entries)):
            curr_line = candidate_entries[i][0]
            prev_line = candidate_entries[i - 1][0]

            # Check if adjacent in original lines
            curr_idx = lines.index(curr_line) if curr_line in lines else -1
            prev_idx = lines.index(prev_line) if prev_line in lines else -1

            if curr_idx == prev_idx + 1:
                current_group.append(curr_line)
            else:
                groups.append(current_group)
                current_group = [curr_line]

        groups.append(current_group)

        # Join groups and return
        candidates = [" ".join(g).strip() for g in groups if " ".join(g).strip()]

        if not candidates:
            return None, []

        return candidates[0], candidates
