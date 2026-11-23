"""Phase 26 — Book-cover metadata extraction from PaddleOCR-VL-1.6 layout blocks.

COMPLETELY FRESH implementation. Does NOT reuse Phase 25 heuristics.

Key insights from Phase 26 research:
1. PaddleOCR-VL API does NOT support custom prompts or disabling layout detection.
   The API always runs the full pipeline: orientation → layout → OCR.
2. The API returns `doc_title` blocks (from the layout model) — but these are UNRELIABLE:
   - Sometimes correct (e.g., "Le Mal Eté")
   - Sometimes merges title+author into one block (e.g., "Miguel de Unamuno\nAbel Sanchez")
   - Sometimes labels publisher text as doc_title (e.g., "ÉCRITS SUR L'ART L'AGE D'HOMME")
3. The API returns `text` blocks — these include author, publisher, genre, etc.
4. PaddleOCR-VL's orientation classification works well (detects 0/90/180/270).
   When orient=180, the API rotates the image internally before layout/OCR.
5. Each block has bbox=[x1,y1,x2,y2] in the (already-rotated) image coordinate system.

Extraction strategy:
1. Use `doc_title` blocks as the PRIMARY title candidate (but don't trust blindly).
2. If a doc_title block contains multiple lines that look like "author\ntitle",
   split them using positional analysis (author above title).
3. Identify author from `text` blocks positioned above the title.
4. Filter out publisher/collection text using keyword matching + position (bottom of page).
5. Use spatial position, font size, and text characteristics as evidence, not as hard rules.

This approach handles:
- Upside-down covers (API orientation detection + preprocessed image)
- Multiple visible books (spatial clustering for multi-book detection)
- Merged title+author blocks (splitting heuristic)
- Publisher text mislabeled as doc_title (keyword filtering)
"""
from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any


# ============================================================================
# Publisher / non-essential text keywords (for filtering)
# ============================================================================
PUBLISHER_PATTERNS = [
    r"\beditions?\b",
    r"\bl'age d'homme\b",
    r"\bcollection\b",
    r"\béditeur\b",
    r"\bédition\b",
    r"\bimprimé\b",
    r"\bpresses?\b",
    r"\bpublications?\b",
    r"\borgane\b",
    r"\breview\b",
    r"\btraduit par\b",
    r"\btraduction de\b",
    r"\bpréface\b",
    r"\bpostface\b",
]

ROMAN_NUMERAL_PATTERN = re.compile(r'^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$')


# ============================================================================
# Data structures
# ============================================================================
@dataclass
class LayoutBlock:
    """A text block from PaddleOCR-VL layout detection."""
    text: str
    bbox: List[float]
    label: str = "text"
    score: float = 1.0
    order: int = 0

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0] if len(self.bbox) >= 4 else 0

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1] if len(self.bbox) >= 4 else 0

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def x_center(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2 if len(self.bbox) >= 4 else 0

    @property
    def y_center(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2 if len(self.bbox) >= 4 else 0

    @property
    def y_top(self) -> float:
        return self.bbox[1] if len(self.bbox) >= 4 else 0

    @property
    def y_bottom(self) -> float:
        return self.bbox[3] if len(self.bbox) >= 4 else 0

    @property
    def x_left(self) -> float:
        return self.bbox[0] if len(self.bbox) >= 4 else 0

    @property
    def x_right(self) -> float:
        return self.bbox[2] if len(self.bbox) >= 4 else 0

    def lines(self) -> List[str]:
        return [line for line in self.text.strip().split("\n") if line.strip()]


@dataclass
class ExtractionOutput:
    """Output of the book-cover extraction."""
    title: str = ""
    author: str = ""
    publisher: str = ""
    confidence: float = 0.0
    multiple_books_detected: bool = False
    primary_book_reason: str = ""
    other_visible_text: List[str] = field(default_factory=list)
    debug: Dict[str, Any] = field(default_factory=dict)


class BookCoverExtractor:
    """Extract book title and author from PaddleOCR-VL-1.6 layout blocks.

    Fresh implementation that does NOT reuse Phase 25 heuristics.
    Uses spatial clustering + label-based extraction with publisher filtering.

    Key principles:
    - DO NOT blindly trust the `doc_title` label
    - Use spatial position (y-coordinate) as primary signal for title vs author
    - Use font size (bbox height) as secondary signal
    - Filter publisher text using keyword matching + position (bottom of page)
    - Handle merged author+title blocks by splitting on line position
    """

    REGION_X_TOLERANCE = 100
    REGION_Y_GAP = 400  # Increased — title/author on book covers can be far apart vertically
    AUTHOR_MAX_HEIGHT_RATIO = 0.8
    TITLE_MAX_LINES = 6

    def __init__(self, image_width: int = 900, image_height: int = 1600):
        self.img_w = image_width
        self.img_h = image_height

    def extract(
        self,
        blocks: List[LayoutBlock],
        raw_markdown: str = "",
        image_width: Optional[int] = None,
        image_height: Optional[int] = None,
        orientation: int = 0,
    ) -> ExtractionOutput:
        if image_width:
            self.img_w = image_width
        if image_height:
            self.img_h = image_height

        text_blocks = [b for b in blocks if b.text.strip()
                       and b.label not in ("image", "seal", "chart", "formula", "table", "figure")]

        if not text_blocks:
            return ExtractionOutput(
                title="", author="", confidence=0.0,
                primary_book_reason="no_text_blocks",
                debug={"n_blocks": 0},
            )

        # Step 1: Separate publisher/collection text
        filtered_blocks, publisher_blocks = self._separate_publisher_text(text_blocks)

        # Step 2: Spatial clustering into regions
        regions = self._cluster_into_regions(filtered_blocks)

        # Step 3: For each region, identify title/author
        region_candidates = []
        for region_blocks in regions:
            if not region_blocks:
                continue
            title_candidate, author_candidate, conf, reason = self._identify_title_author(region_blocks)
            region_candidates.append({
                "blocks": region_blocks,
                "title": title_candidate,
                "author": author_candidate,
                "confidence": conf,
                "reason": reason,
                "n_blocks": len(region_blocks),
                "max_font_size": max(b.height for b in region_blocks),
                "total_text_len": sum(len(b.text) for b in region_blocks),
            })

        # Step 4: Select primary region
        primary = self._select_primary_region(region_candidates)

        if primary is None:
            return ExtractionOutput(
                title="", author="", confidence=0.0,
                primary_book_reason="no_regions_after_filtering",
                debug={"n_blocks": len(text_blocks), "n_regions": len(regions),
                       "n_filtered_regions": len(region_candidates)},
            )

        # Step 5: Extract publisher
        publisher_text = self._extract_publisher(publisher_blocks, primary["blocks"])

        # Step 6: Other visible text
        other_visible = []
        for rc in region_candidates:
            if rc is not primary and rc["title"]:
                other_visible.append(rc["title"])

        multiple_books = len(regions) > 1 and not self._regions_overlap(regions)

        output = ExtractionOutput(
            title=primary["title"],
            author=primary["author"],
            publisher=publisher_text,
            confidence=round(primary["confidence"], 2),
            multiple_books_detected=multiple_books,
            primary_book_reason=primary["reason"],
            other_visible_text=other_visible[:5],
            debug={
                "n_blocks": len(text_blocks),
                "n_regions": len(regions),
                "n_filtered_regions": len(region_candidates),
                "orientation": orientation,
                "img_w": self.img_w,
                "img_h": self.img_h,
                "region_details": [
                    {"title": rc["title"][:50], "author": rc["author"][:50],
                     "confidence": rc["confidence"], "n_blocks": rc["n_blocks"],
                     "max_fs": rc["max_font_size"]}
                    for rc in region_candidates
                ],
            },
        )

        return output

    # ---------------------------------------------------------------------------
    # Step 1: Publisher separation
    # ---------------------------------------------------------------------------
    def _separate_publisher_text(
        self, blocks: List[LayoutBlock]
    ) -> Tuple[List[LayoutBlock], List[LayoutBlock]]:
        title_author = []
        publisher = []
        for block in blocks:
            if self._is_publisher_text(block):
                publisher.append(block)
            else:
                title_author.append(block)
        return title_author, publisher

    def _is_publisher_text(self, block: LayoutBlock) -> bool:
        text_lower = block.text.lower().strip()
        lines = block.lines()

        for pattern in PUBLISHER_PATTERNS:
            if re.search(pattern, text_lower):
                return True

        bottom_ratio = block.y_bottom / self.img_h if self.img_h > 0 else 0
        if bottom_ratio > 0.75 and block.y_top > 0.65 * self.img_h:
            if not self._looks_like_title(lines, block):
                if len(lines) <= 2 and len(text_lower.split()) <= 5:
                    return True
                if all(len(l.split()) <= 3 for l in lines):
                    return True

        if len(lines) == 1 and ROMAN_NUMERAL_PATTERN.match(lines[0].strip()):
            return True

        return False

    # ---------------------------------------------------------------------------
    # Step 2: Spatial clustering
    # ---------------------------------------------------------------------------
    def _cluster_into_regions(self, blocks: List[LayoutBlock]) -> List[List[LayoutBlock]]:
        if not blocks:
            return []

        sorted_blocks = sorted(blocks, key=lambda b: b.y_center)
        regions = []
        current_region = []

        for block in sorted_blocks:
            if not current_region:
                current_region = [block]
                continue

            is_close = False
            for existing in current_region:
                x_overlap = (block.x_left < existing.x_right and block.x_right > existing.x_left)
                x_center_dist = abs(block.x_center - existing.x_center)
                max_width = max(block.width, existing.width)
                x_close = x_center_dist < max_width * 0.5 if max_width > 0 else False
                y_gap = abs(block.y_top - existing.y_bottom)

                if (x_overlap or x_close) and y_gap < self.REGION_Y_GAP:
                    is_close = True
                    break

            if is_close:
                current_region.append(block)
            else:
                if current_region:
                    regions.append(current_region)
                current_region = [block]

        if current_region:
            regions.append(current_region)

        return regions

    def _regions_overlap(self, regions: List[List[LayoutBlock]]) -> bool:
        for i in range(len(regions)):
            for j in range(i + 1, len(regions)):
                r1, r2 = regions[i], regions[j]
                y1_min = min(b.y_top for b in r1)
                y1_max = max(b.y_bottom for b in r1)
                y2_min = min(b.y_top for b in r2)
                y2_max = max(b.y_bottom for b in r2)
                if y1_min < y2_max and y2_min < y1_max:
                    return True
        return False

    # ---------------------------------------------------------------------------
    # Step 3: Title/author identification
    # ---------------------------------------------------------------------------
    def _identify_title_author(
        self, region_blocks: List[LayoutBlock]
    ) -> Tuple[str, str, float, str]:
        if not region_blocks:
            return "", "", 0.0, "empty_region"

        sorted_blocks = sorted(region_blocks, key=lambda b: b.y_center)

        doc_title_blocks = [b for b in sorted_blocks if b.label in ("doc_title", "paragraph_title")]
        header_blocks = [b for b in sorted_blocks if b.label == "header"]
        text_blocks = [b for b in sorted_blocks if b.label in ("text", "paragraph_text", "footer", "seal")]

        title_block = None
        title_lines = []
        title_confidence = 0.0
        title_reason = ""
        split_result = None

        if doc_title_blocks:
            title_block = max(doc_title_blocks, key=lambda b: b.height)
            title_lines = title_block.lines()
            title_confidence += 0.3
            title_reason = "doc_title_label"
            split_result = self._try_split_title_author_block(title_block, sorted_blocks)
            if split_result is not None:
                actual_title, actual_author = split_result
                if actual_title:
                    title_lines = [actual_title]
                    title_confidence += 0.2
                    title_reason += ",split_block"
        elif header_blocks:
            title_block = max(header_blocks, key=lambda b: b.height)
            title_lines = title_block.lines()
            title_confidence += 0.2
            title_reason = "header_label"
        elif text_blocks:
            title_block = max(text_blocks, key=lambda b: b.area)
            title_lines = title_block.lines()
            title_confidence += 0.1
            title_reason = "spatial_largest"
        elif sorted_blocks:
            title_block = max(sorted_blocks, key=lambda b: b.area)
            title_lines = title_block.lines()
            title_confidence += 0.05
            title_reason = "fallback_any"

        title_lines = self._clean_title_lines(title_lines, sorted_blocks)

        author = ""
        author_confidence = 0.0
        author_reason = ""

        if title_block and split_result is not None and split_result[1]:
            author = split_result[1]
            author_confidence += 0.4
            author_reason = "split_from_title_block"
        else:
            author, author_confidence, author_reason = self._find_author(
                title_block, sorted_blocks, title_lines
            )

        title = " ".join(self._normalize_line(line) for line in title_lines)
        title = self._clean_title_string(title)
        title = self._normalize_title_case(title)

        # Normalize author case too (OCR may return all-caps)
        author = self._normalize_title_case(author)

        confidence = min(1.0, title_confidence + author_confidence)
        reason = f"title:{title_reason},author:{author_reason}"

        return title, author, confidence, reason

    def _try_split_title_author_block(
        self, block: LayoutBlock, all_blocks: List[LayoutBlock]
    ) -> Optional[Tuple[str, str]]:
        lines = block.lines()
        if len(lines) < 2:
            return None

        for other_block in all_blocks:
            if other_block is block:
                continue
            other_text = other_block.text.lower().strip()
            for line in lines:
                if line.lower().strip() in other_text and len(line.split()) > 1:
                    return None

        # For 2-line blocks: only split if one line looks like a person name
        # AND the other does NOT look like a title phrase
        if len(lines) == 2:
            name_lines = [i for i, line in enumerate(lines)
                          if self._looks_like_person_name(line)]
            if len(name_lines) == 2:
                # BOTH lines look like names (e.g., "Miguel de Unamuno\nAbel Sanchez")
                # Author typically appears FIRST (top) in the merged block
                return lines[1].strip(), lines[0].strip()

            if len(name_lines) == 1:
                idx = name_lines[0]
                other = lines[1-idx].strip()
                if not self._looks_like_title_phrase(other):
                    if idx == 0:
                        return lines[1].strip(), lines[0].strip()
                    else:
                        return lines[0].strip(), lines[1].strip()

        return None

    def _looks_like_title_phrase(self, text: str) -> bool:
        """Check if text looks like a book title phrase (not a person name).

        Titles often contain French articles/prepositions, multiple words,
        and/or known title vocabulary (guerre, amour, violence, etc.).
        """
        text_lower = text.lower().strip()
        # Strip leading apostrophes for checking
        text_check = text_lower.lstrip("'")
        words = text_check.split()
        if not words:
            return False

        # Common French title words/phrases that strongly indicate a title
        title_words = {
            "guerre", "amour", "violence", "construction", "villes", "ville",
            "monde", "temps", "vie", "mort", "paix", "pitié", "chrétien",
            "cité", "mal", "été", "histoire", "théorie", "philosophie",
            "création", "esprit", "art", "œuvre", "livre", "texte",
            "roman", "poésie", "théâtre", "nouvelle", "conte", "essai",
            "désastres", "debout", "harmonie", "création", "ordre",
        }
        for w in words:
            if w.rstrip("'").rstrip(",") in title_words:
                return True

        # Contains French articles/prepositions (strong title indicator)
        articles = {"de", "la", "le", "l", "d", "des", "du", "les", "un", "une",
                    "et", "dans", "sur", "par", "pour", "à", "au", "aux"}
        for w in words:
            w_clean = w.strip(",").lower()
            # Handle apostrophe words: d'amour, l'esprit, etc.
            if "'" in w_clean:
                prefix = w_clean.split("'")[0]
                if prefix in ("d", "l", "s", "qu", "n", "c", "j", "t"):
                    return True
            if w_clean in articles:
                return True

        # Multi-word text (4+ words) with any lowercase word → likely a title
        if len(words) >= 4:
            has_lowercase = any(w != w.upper() and len(w) > 1 for w in words)
            if has_lowercase:
                return True

        return False

    def _looks_like_person_name(self, text: str) -> bool:
        """Check if text looks like a person's name (for author identification).

        Author names typically have these characteristics:
        - 2-5 words (first + last name, or with middle/van/de prefix)
        - At least the first and last words are capitalized
        - Single all-caps words are unlikely to be names (could be genre, etc.)
        - Lowercase prepositions (de, van, di, du) are common in names
        """
        words = text.strip().split()
        if len(words) < 1 or len(words) > 5:
            return False

        # Single all-caps words are unlikely to be person names
        # (e.g., "ROMAN", "POEMES" — these are genre/collection words)
        if len(words) == 1 and words[0].isupper() and len(words[0]) > 2:
            return False

        title_case_words = sum(1 for w in words if len(w) > 0 and w[0].isupper())
        if title_case_words < len(words):
            # Some words are lowercase — common in "de", "van", "di", etc.
            if len(words) >= 2:
                if words[0][0].isupper() and words[-1][0].isupper():
                    # Check for French title words
                    french_title_words_lower = {
                        "désastres", "guerre", "amour", "villes", "monde",
                        "temps", "construction", "voix", "roi", "reine",
                        "ville", "livre", "texte", "théorie", "philosophie",
                        "politique", "économie", "sociologie", "roman", "novel",
                        "poésie", "poème", "théâtre", "nouvelle", "conte", "essai",
                        "histoire", "création", "pitié", "viols",
                    }
                    if any(w.lower() in french_title_words_lower for w in words):
                        return False
                    return True
            return False

        # All words capitalized (all-caps)
        # For single words, require it to be 2-4 chars (initials, short names)
        if len(words) == 1:
            word = words[0]
            if len(word) <= 3 and word.isupper():
                return True  # Short all-caps: could be an initial or short name
            return False  # Long single all-caps word is likely not a name

        # Multi-word all-caps: check for common French title words in the FIRST position
        # "LA CONSTRUCTION" → "LA" is a French article → title, not name
        # "LE CORBUSIER" → "LE" is a French article but "CORBUSIER" is a name
        # Heuristic: if first word is a French article, check if remaining words
        # look like they could be a surname (not common nouns)
        french_articles = {"la", "le", "l'", "les", "des", "du", "de", "d'",
                           "del'", "au", "aux", "par", "dans", "sur"}
        if words[0].lower().strip("'\"") in french_articles:
            # Check if remaining words look like a surname (not a title phrase)
            # Common title words that indicate the line is NOT a name
            french_title_words = {
                "construction", "voix", "roi", "reine", "ville", "villes",
                "monde", "temps", "vie", "mort", "guerre", "paix", "amour",
                "histoire", "recit", "roman", "poeme", "poemes", "essai",
                "essais", "carnet", "journal", "priere", "prieres",
                "meditations", "pensées", "oeuvre", "livre", "texte",
                "theorie", "theologie", "philosophie", "politique", "economie",
                "sociologie", "anthropologie", "psychologie", "histoire",
            }
            remaining = words[1:]
            if any(w.lower() in french_title_words for w in remaining):
                return False  # Likely a title phrase, not a name
            # If remaining words don't look like title words, treat as name
            # e.g., "LE CORBUSIER" → not a title word → could be a name
            if len(remaining) <= 3 and all(len(w) > 2 for w in remaining):
                return True  # Plausible surname(s)
            return False

        if len(words) <= 4 and len(text) <= 40:
            return True

        return False

    def _find_author(
        self,
        title_block: Optional[LayoutBlock],
        sorted_blocks: List[LayoutBlock],
        title_lines: List[str],
    ) -> Tuple[str, float, str]:
        if title_block is None:
            if sorted_blocks:
                return sorted_blocks[0].text, 0.3, "topmost_fallback"
            return "", 0.0, "no_blocks"

        title_y = title_block.y_center
        img_center_x = self.img_w / 2

        # Search for author blocks ABOVE the title (within 600px vertical gap)
        # This is the most common pattern: author above title on book covers
        blocks_above = [b for b in sorted_blocks
                        if b.y_bottom < title_y and b is not title_block
                        and (title_y - b.y_bottom) < 600]
        if not blocks_above:
            blocks_near = [b for b in sorted_blocks
                          if abs(b.y_center - title_y) < title_block.height * 2 and b is not title_block]
            if blocks_near:
                blocks_above = blocks_near

        if not blocks_above:
            return "", 0.0, "no_blocks_above"

        scored = []
        for block in blocks_above:
            score = 0.0
            reasons = []

            if block.y_bottom < title_y:
                score += 0.3
                reasons.append("above_title")
            else:
                score += 0.1
                reasons.append("near_title")

            center_dist = abs(block.x_center - img_center_x) / img_center_x if img_center_x > 0 else 0
            if center_dist < 0.15:
                score += 0.2
                reasons.append("centered")

            if title_block.height > 0:
                height_ratio = block.height / title_block.height
                if 0.3 <= height_ratio <= 0.9:
                    score += 0.3
                    reasons.append(f"size_ratio_{height_ratio:.2f}")
                elif height_ratio < 0.3:
                    score += 0.15
                    reasons.append("small_text")
                else:
                    score += 0.1
                    reasons.append("large_text")

            if self._looks_like_person_name(block.text):
                score += 0.3
                reasons.append("person_name_pattern")

            if not self._is_publisher_text(block):
                score += 0.1
                reasons.append("not_publisher")

            word_count = len(block.text.split())
            if 1 <= word_count <= 6:
                score += 0.1
                reasons.append(f"word_count_{word_count}")

            scored.append((block, score, reasons))

        scored.sort(key=lambda x: x[1], reverse=True)
        best_block, best_score, best_reasons = scored[0]

        return best_block.text, min(0.6, best_score), ",".join(best_reasons)

    # ---------------------------------------------------------------------------
    # Title line cleaning
    # ---------------------------------------------------------------------------
    def _clean_title_lines(
        self, lines: List[str], region_blocks: List[LayoutBlock]
    ) -> List[str]:
        if not lines:
            return []

        cleaned = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()

            if any(re.search(p, lower) for p in PUBLISHER_PATTERNS):
                continue

            if len(lines) > 1:
                genre_words = {"roman", "novel", "poésie", "poem", "poème",
                               "théâtre", "nouvelle", "conte", "essai"}
                if stripped.lower() in genre_words:
                    continue

            if self._looks_like_metadata(stripped):
                if len(cleaned) > 0:
                    continue

            cleaned.append(stripped)

        if not cleaned:
            cleaned = [l.strip() for l in lines if l.strip()]

        return cleaned

    def _looks_like_metadata(self, text: str) -> bool:
        text_lower = text.lower().strip()
        if any(re.search(p, text_lower) for p in PUBLISHER_PATTERNS):
            return True
        if ROMAN_NUMERAL_PATTERN.match(text.strip()):
            return True
        words = text_lower.split()
        if len(words) < 3 and text_lower != text_lower.upper():
            if any(w in text_lower for w in ["trad.", "traduit", "éd.", "édition", "collection"]):
                return True
        return False

    def _normalize_line(self, line: str) -> str:
        return line.strip()

    def _normalize_title_case(self, text: str) -> str:
        """Normalize all-caps titles to proper French title case.

        French title case: first word capitalized, articles/prepositions
        (le, la, de, etc.) lowercased. Apostrophe words (d', l') handled.
        Short all-caps symbols (I, V, L', D') preserved if <= 3 chars.
        """
        if not text:
            return text
        if not text.isupper():
            return text
        words = text.split()
        if not words:
            return text
        if len(words) == 1:
            w = words[0]
            if len(w) <= 3:
                return w
            return w[0] + w[1:].lower()

        french_lower = {
            "DE", "DU", "DES", "LA", "LE", "LES", "ET", "DANS", "SUR",
            "PAR", "POUR", "EN", "SOUS", "CONTRE", "AU", "AUX", "Y", "D", "C", "J", "T",
        }

        result = []
        for i, w in enumerate(words):
            # Handle apostrophe words
            if "'" in w:
                prefix = w.split("'")[0].upper()
                suffix = w.split("'")[1]
                if i == 0:
                    result.append(prefix + "'" + suffix.lower())
                else:
                    result.append(prefix.lower() + "'" + suffix.lower())
            elif w.upper() in french_lower:
                # Lowercase articles/prepositions everywhere EXCEPT first word
                if i == 0:
                    result.append(w[0] + w[1:].lower())
                else:
                    result.append(w.lower())
            elif len(w) <= 2:
                # Keep short all-caps (initials, Roman numerals)
                if i == 0:
                    result.append(w[0] + w[1:].lower())
                else:
                    result.append(w.lower())
            else:
                # Lowercase as per French title convention (only first word capitalized)
                if i == 0:
                    result.append(w[0] + w[1:].lower())
                else:
                    result.append(w[0] + w[1:].lower())
        return " ".join(result)


    def _clean_title_string(self, title: str) -> str:
        title = re.sub(r"\s+", " ", title).strip()
        title = re.sub(r"^[:–—\s]+", "", title)
        title = re.sub(r"[:–—\s]+$", "", title)
        return title.strip()

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------
    def _looks_like_title(self, lines: List[str], block: LayoutBlock) -> bool:
        if not lines:
            return False
        total_words = sum(len(l.split()) for l in lines)
        if total_words > 20:
            return False
        return True

    # ---------------------------------------------------------------------------
    # Step 4: Primary region selection
    # ---------------------------------------------------------------------------
    def _select_primary_region(
        self, candidates: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None

        scored = []
        for rc in candidates:
            score = 0.0
            factors = []
            blocks = rc["blocks"]

            if not blocks:
                score = 0.01
                scored.append((rc, score, factors))
                continue

            has_doc_title = any(b.label == "doc_title" for b in blocks)
            if has_doc_title:
                score += 0.3
                factors.append("doc_title")

            has_header = any(b.label == "header" for b in blocks)
            if has_header:
                score += 0.2
                factors.append("header")

            max_fs = rc["max_font_size"]
            if max_fs >= 100:
                score += 0.2
                factors.append(f"large_text({max_fs})")
            elif max_fs >= 50:
                score += 0.1
                factors.append(f"medium_text({max_fs})")

            center_x = self.img_w / 2
            region_center_x = sum(b.x_center for b in blocks) / len(blocks)
            center_dist = abs(region_center_x - center_x) / center_x if center_x > 0 else 0
            if center_dist < 0.1:
                score += 0.15
                factors.append("center_aligned")
            elif center_dist < 0.2:
                score += 0.1
                factors.append("near_center")

            region_y = sum(b.y_center for b in blocks) / len(blocks)
            if region_y < self.img_h * 0.6:
                score += 0.15
                factors.append("upper_cover")
            elif region_y < self.img_h * 0.8:
                score += 0.1
                factors.append("middle_cover")

            title_lines = rc["title"].count("\n") + 1 if rc["title"] else 0
            if title_lines >= 2:
                score += 0.1
                factors.append(f"multi_line({title_lines}L)")

            if rc["total_text_len"] < 500:
                score += 0.05
                factors.append("concise_text")

            scored.append((rc, score, factors))

        scored.sort(key=lambda x: x[1], reverse=True)
        best_rc, best_score, best_factors = scored[0]

        if best_score < 0.1:
            best_rc["reason"] = "fallback:low_score, factors=" + ",".join(best_factors)
        else:
            best_rc["reason"] = f"score={best_score:.2f}, factors=" + ",".join(best_factors)

        return best_rc

    # ---------------------------------------------------------------------------
    # Publisher extraction
    # ---------------------------------------------------------------------------
    def _extract_publisher(
        self,
        publisher_blocks: List[LayoutBlock],
        primary_blocks: List[LayoutBlock],
    ) -> str:
        if not publisher_blocks:
            return ""
        publisher_texts = []
        for block in sorted(publisher_blocks, key=lambda b: b.y_bottom, reverse=True):
            text = block.text.strip()
            if text:
                publisher_texts.append(text)
        if publisher_texts:
            return " | ".join(publisher_texts[:2])
        return ""
