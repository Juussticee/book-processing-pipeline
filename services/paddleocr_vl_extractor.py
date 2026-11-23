"""PaddleOCR-VL title/author extraction from layout blocks.

Uses spatial layout, block labels, and text characteristics to identify
the actual book title and author from PaddleOCR-VL output — NOT just the
first OCR line or a naive doc_title field.

Key distinction: On a book cover like:

    LA VIOLENCE
    ÉCRITS SUR L'ART
    L'AGE D'HOMME
    LA VIOLENCE
    DE L'ESPRIT ET
    LA PITIE DU CŒUR
    MARCEL PONCET

The title is "LA VIOLENCE DE L'ESPRIT ET LA PITIE DU CŒUR" (multi-line, centered,
large text), NOT "LA VIOLENCE" (appears twice — once as a header, once as part
of the actual title). Publisher/collection names ("L'AGE D'HOMME", "ÉCRITS SUR L'ART")
must be excluded.

This module is isolated — it does NOT touch the existing book_text_extractor.py
or book ranking logic.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from core.logger import setup_logger

from services.paddleocr_vl_service import LayoutBlock

logger = setup_logger(__name__)

# --- Patterns for filtering out non-title text ---

# Publisher, collection, and imprint keywords (case-insensitive)
PUBLISHER_KEYWORDS = {
    "l'age d'homme", "éditions", "édition", "gallimard", "seuil", "folio",
    "delt", "points", "grasset", " flammarion", " éditions", " le seuil",
    " éditions", " collection", " collection:", " éditeur", " impr",
    "publisher", "imprint", "published by", "printed by", "©", "copyright",
    "isbn", "éd.", "rééd.", "tome", "vol.", "édition", "réimprimé",
    "collection", "série", "serie", "numéro", "numero",
}

# Phrases that indicate non-title text
EXCLUDE_PATTERNS = [
    re.compile(r"^\d+$"),  # standalone numbers
    re.compile(r"^[IVXLCDM]+\s*$"),  # roman numerals only
    re.compile(r"^[A-Z]{1,3}$"),  # very short all-caps (unlikely to be a title)
    re.compile(r"^\d{4}$"),  # year only
    re.compile(r"^[\d\s\-/,.]+$"),  # only numbers and punctuation
    re.compile(r"^[\d\/\s\-.,]+$"),  # ISBN-like strings
]

# Lines that look like publisher/collection names
PUBLISHER_LINE_PATTERNS = [
    re.compile(r"^l'?age d'?homme$", re.I),
    re.compile(r"^écrits sur$", re.I),
    re.compile(r"^sur l'art$", re.I),
    re.compile(r"^collection\s", re.I),
    re.compile(r"^éditions?\s", re.I),
    re.compile(r"^tome\s", re.I),
    re.compile(r"^\d+e\s", re.I),
    re.compile(r"^réimpr", re.I),
    re.compile(r"^©", re.I),
    re.compile(r"^\d{4}$"),  # year
    re.compile(r"^isbn", re.I),
    re.compile(r"^par\s+", re.I),  # "par <author>" attribution
    re.compile(r"^by\s+", re.I),
    re.compile(r"^traduction de\s", re.I),
    re.compile(r"^postface de\s", re.I),
    re.compile(r"^sous la direction de\s", re.I),
    re.compile(r"^avec la collaboration de\s", re.I),
    re.compile(r"^préface de\s", re.I),
    re.compile(r"^introduction de\s", re.I),
]


@dataclass
class TitleCandidate:
    """A candidate title extracted from layout blocks."""
    text: str
    block_label: str
    bbox: List[int]
    avg_font_size: float  # estimated from bbox height / line count
    area: int
    score: float  # combined confidence score
    line_count: int
    confidence: float  # OCR confidence


@dataclass
class AuthorCandidate:
    """A candidate author extracted from layout blocks."""
    text: str
    block_label: str
    bbox: List[int]
    avg_font_size: float
    area: int
    score: float
    confidence: float


@dataclass
class ExtractionResult:
    """Final extraction result for one image."""
    title_candidates: List[TitleCandidate] = field(default_factory=list)
    author_candidates: List[AuthorCandidate] = field(default_factory=list)
    layout_blocks: List[LayoutBlock] = field(default_factory=list)
    raw_markdown: str = ""
    orientation: int = 0
    selected_title: Optional[str] = None
    selected_author: Optional[str] = None
    processing_time: float = 0.0
    error: Optional[str] = None


class PaddleOCRVlExtractor:
    """Extracts title and author from PaddleOCR-VL layout blocks.

    Uses spatial layout, block labels, text size, and reading order
    to distinguish the actual book title from publisher names,
    collection names, and other decorative text.
    """

    def __init__(self, image_width: int = 900, image_height: int = 1600) -> None:
        self._image_width = image_width
        self._image_height = image_height
        self._image_center_x = image_width / 2
        self._image_center_y = image_height / 2

    def extract(
        self,
        layout_blocks: List[LayoutBlock],
        raw_markdown: str = "",
        image_width: int = 0,
        image_height: int = 0,
    ) -> ExtractionResult:
        """Extract title and author from PaddleOCR-VL layout blocks."""
        start = _time()
        if image_width:
            self._image_width = image_width
            self._image_center_x = image_width / 2
        if image_height:
            self._image_height = image_height
            self._image_center_y = image_height / 2

        result = ExtractionResult(
            layout_blocks=layout_blocks,
            raw_markdown=raw_markdown,
        )

        if not layout_blocks:
            result.processing_time = _time() - start
            result.error = "No layout blocks to extract from"
            return result

        # Filter out non-content blocks
        content_blocks = self._filter_publisher_blocks(layout_blocks)

        # Score each block for title likelihood
        title_candidates = self._score_title_candidates(content_blocks)
        author_candidates = self._score_author_candidates(content_blocks)

        # Select best title and author
        selected_title = self._select_title(title_candidates)
        selected_author = self._select_author(author_candidates)

        result.title_candidates = title_candidates
        result.author_candidates = author_candidates
        result.selected_title = selected_title
        result.selected_author = selected_author
        result.processing_time = _time() - start

        logger.info(
            "Extraction: %d blocks → %d title candidates, %d author candidates | "
            "title='%s' author='%s'",
            len(layout_blocks),
            len(title_candidates),
            len(author_candidates),
            selected_title or "(none)",
            selected_author or "(none)",
        )
        return result

    def _filter_publisher_blocks(self, blocks: List[LayoutBlock]) -> List[LayoutBlock]:
        """Remove blocks that are clearly publisher/collection/imprint text."""
        filtered = []
        for b in blocks:
            text = b.text.strip().upper() if b.text else ""
            if not text:
                continue
            # Skip if matches any publisher pattern
            skip = False
            for pat in PUBLISHER_LINE_PATTERNS:
                if pat.search(text):
                    skip = True
                    break
            # Skip very short all-caps text that's likely publisher/collection
            if not skip and len(text) <= 4 and text.isupper() and b.score < 0.7:
                skip = True
            # Skip if text matches known publisher keywords
            if not skip:
                text_lower = text.lower()
                for kw in PUBLISHER_KEYWORDS:
                    if kw in text_lower:
                        skip = True
                        break
            if not skip:
                filtered.append(b)
        return filtered

    def _estimate_font_size(self, block: LayoutBlock) -> float:
        """Estimate visual font size from bbox height and line count."""
        if not block.bbox or len(block.bbox) < 4:
            return 0.0
        h = block.height()
        if h <= 0:
            return 0.0
        # If text has lines, divide by line count for avg line height
        line_count = max(len(block.text_lines), 1)
        return h / line_count

    def _is_centered(self, block: LayoutBlock, threshold: float = 0.15) -> bool:
        """Check if block is horizontally centered (common for titles)."""
        if not block.bbox or len(block.bbox) < 4:
            return False
        center_x = (block.bbox[0] + block.bbox[2]) / 2
        tolerance = self._image_width * threshold
        return abs(center_x - self._image_center_x) < tolerance

    def _is_top_or_center_vertical(self, block: LayoutBlock) -> bool:
        """Title is typically in the upper-third or middle of the cover."""
        if not block.bbox or len(block.bbox) < 4:
            return False
        center_y = (block.bbox[1] + block.bbox[3]) / 2
        # Upper half of the image
        return center_y < self._image_height * 0.7

    def _block_score_for_title(self, block: LayoutBlock) -> float:
        """Score a block for title-likelihood (0.0–1.0)."""
        score = 0.5  # base

        # Factor 1: Font size (larger = more likely title)
        fs = self._estimate_font_size(block)
        if fs > 0:
            # Normalize: larger text gets higher score
            # Typical font sizes range 20–200 on a 1600px image
            size_score = min(fs / 100.0, 1.0)
            score += 0.25 * size_score

        # Factor 2: Horizontal centering (titles are often centered)
        if self._is_centered(block, threshold=0.20):
            score += 0.15

        # Factor 3: Vertical position (upper/center of cover)
        if self._is_top_or_center_vertical(block):
            score += 0.10

        # Factor 4: Block label (PaddleOCR-VL may label as "title" or "doc_title")
        label_lower = (block.label or "").lower()
        if "title" in label_lower:
            score += 0.3
        elif "subtitle" in label_lower:
            score += 0.1
        elif "author" in label_lower:
            score -= 0.3  # author block, not title
        elif "footer" in label_lower or "header" in label_lower:
            score -= 0.2  # likely page number or header, not title

        # Factor 5: Text length (titles are typically medium-length)
        text_len = len(block.text.strip())
        if 4 <= text_len <= 200:
            score += 0.1
        elif text_len > 200:
            score += 0.05  # might be multi-line title
        elif text_len < 4:
            score -= 0.1

        # Factor 6: Multi-line blocks are more likely to be real titles
        # (A title often spans multiple lines. A single "LA VIOLENCE" header
        # is less likely to be the full title.)
        line_count = len(block.text_lines) if block.text_lines else (1 if block.text.strip() else 0)
        if line_count > 1:
            score += 0.3  # significant bonus for multi-line (real title)
        elif "\n" in block.text:
            score += 0.3

        # Factor 7: Reading order (first block is more likely title)
        if block.order == 0:
            score += 0.05
        elif block.order <= 2:
            score += 0.03

        # Factor 8: Exclude if it looks like a publisher/collection
        text_upper = block.text.strip().upper() if block.text else ""
        for pat in PUBLISHER_LINE_PATTERNS:
            if pat.search(text_upper):
                score -= 0.4
                break

        # Factor 9: Single-word blocks in all caps are less likely to be titles
        # (e.g. "LA VIOLENCE" appears as a decorative header, not the full title)
        if text_len < 20 and block.text.strip().isupper() and line_count == 1:
            score -= 0.3

        return max(0.0, min(1.0, score))

    def _block_score_for_author(self, block: LayoutBlock) -> float:
        """Score a block for author-likelihood (0.0–1.0)."""
        score = 0.3  # base

        text = block.text.strip() if block.text else ""
        words = text.split()

        # Factor 1: Text looks like a person's name (2-4 words, title case)
        if 1 <= len(words) <= 5:
            # Check for name-like capitalization
            if len(words) >= 2 and all(w[0].isupper() if w else True for w in words):
                # Exclude if all caps (likely title, not name)
                if not text.isupper():
                    score += 0.3

        # Factor 2: Lower on cover (authors often appear lower)
        if not block.bbox or len(block.bbox) < 4:
            pass
        else:
            center_y = (block.bbox[1] + block.bbox[3]) / 2
            if center_y > self._image_height * 0.3:
                score += 0.1

        # Factor 3: Block label — author-labeled blocks get strong bonus
        label_lower = (block.label or "").lower()
        if "author" in label_lower:
            score += 0.3
        elif "title" in label_lower:
            score -= 0.3

        # Factor 4: Non-author labels penalized (text, paragraph, etc.)
        # Author blocks should ideally be labeled "author"
        if label_lower and "author" not in label_lower and "title" not in label_lower:
            if label_lower in ("text", "paragraph", "footer"):
                score -= 0.15

        # Factor 5: Exclude common non-author text
        for pat in PUBLISHER_LINE_PATTERNS:
            if pat.search(text.upper()):
                score -= 0.5
                break
        for pat in EXCLUDE_PATTERNS:
            if pat.match(text.strip()):
                score -= 0.5
                break

        # Factor 6: Font size (authors are usually smaller than titles)
        fs = self._estimate_font_size(block)
        if fs > 0:
            size_score = min(fs / 80.0, 1.0)
            score += 0.15 * (1.0 - size_score)  # smaller = more likely author

        # Factor 7: Single-word or standalone text is less likely to be author
        if len(words) == 1:
            score -= 0.1

        return max(0.0, min(1.0, score))

    def _score_title_candidates(self, blocks: List[LayoutBlock]) -> List[TitleCandidate]:
        candidates = []
        for b in blocks:
            score = self._block_score_for_title(b)
            if score < 0.2:
                continue
            fs = self._estimate_font_size(b)
            area = b.area()
            line_count = len(b.text_lines) if b.text_lines else (1 if b.text.strip() else 0)
            # Multi-line and title-labeled blocks get preference in sorting
            is_multiline = line_count > 1 or "\n" in b.text
            has_title_label = "title" in (b.label or "").lower()
            candidates.append(TitleCandidate(
                text=b.text.strip(),
                block_label=b.label,
                bbox=b.bbox,
                avg_font_size=fs,
                area=area,
                score=score,
                line_count=max(line_count, 1),
                confidence=b.score,
            ))
        # Sort by score, then prefer multi-line + title-labeled blocks as tiebreaker
        candidates.sort(key=lambda c: (
            c.score,
            c.line_count > 1 or "\n" in c.text,
            "title" in (c.block_label or "").lower(),
            c.area,
        ), reverse=True)
        return candidates

    def _score_author_candidates(self, blocks: List[LayoutBlock]) -> List[AuthorCandidate]:
        candidates = []
        for b in blocks:
            score = self._block_score_for_author(b)
            if score < 0.2:
                continue
            fs = self._estimate_font_size(b)
            candidates.append(AuthorCandidate(
                text=b.text.strip(),
                block_label=b.label,
                bbox=b.bbox,
                avg_font_size=fs,
                area=b.area(),
                score=score,
                confidence=b.score,
            ))
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _select_title(self, candidates: List[TitleCandidate]) -> Optional[str]:
        """Select the best title candidate.

        Heuristic: the best-scoring single-line title, or the best multi-line
        group that forms a coherent title.
        """
        if not candidates:
            return None

        # Strategy 1: Find multi-line title groups (consecutive lines that form a title)
        # A title may span multiple blocks/lines at similar font size
        best = candidates[0]  # Already sorted by score

        # If the top candidate is multi-line (contains newlines or multiple text_lines),
        # it's likely the real title
        if best.line_count > 1 or "\n" in best.text:
            return _normalize_text(best.text)

        # Strategy 2: Check if top candidates form a vertical group (title continuation)
        # Look for adjacent blocks at similar y-coordinates and font size
        grouped = _group_title_lines(candidates)
        if grouped:
            return _normalize_text(grouped)

        return _normalize_text(best.text)

    def _select_author(self, candidates: List[AuthorCandidate]) -> Optional[str]:
        """Select the best author candidate.

        Prefers author-labeled blocks, then highest score.
        """
        if not candidates:
            return None
        # Prefer author-labeled blocks first, then highest score
        author_labeled = [c for c in candidates if "author" in (c.block_label or "").lower()]
        if author_labeled:
            return _normalize_text(author_labeled[0].text)
        return _normalize_text(candidates[0].text)


def _normalize_text(text: str) -> str:
    """Normalize extracted text: strip extra whitespace, fix encoding."""
    if not text:
        return ""
    # Remove excessive whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Normalize apostrophes
    text = text.replace("'", "'").replace("ʼ", "'")
    return text


def _time() -> float:
    return __import__("time").time()


def _group_title_lines(candidates: List[TitleCandidate]) -> Optional[str]:
    """Group consecutive title lines into a multi-line title.

    If multiple high-scoring candidates are vertically adjacent and have
    similar font sizes, they likely form one title.
    """
    if len(candidates) < 2:
        return None

    # Sort by vertical position
    by_y = sorted(candidates, key=lambda c: c.bbox[1] if len(c.bbox) >= 2 else 0)

    # Find candidates that are vertically close and have similar scores/sizes
    groups = []
    current_group = [by_y[0]]

    for i in range(1, len(by_y)):
        prev = current_group[-1]
        curr = by_y[i]
        if not prev.bbox or not curr.bbox:
            continue
        # Check vertical proximity (within 2x the font size)
        gap = curr.bbox[1] - prev.bbox[3]
        max_font = max(prev.avg_font_size, curr.avg_font_size, 1.0)
        if gap < max_font * 2.5 and abs(prev.avg_font_size - curr.avg_font_size) < max_font * 0.5:
            current_group.append(curr)
        else:
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = [curr]

    if len(current_group) >= 2:
        groups.append(current_group)

    if groups:
        # Return the group with highest combined score
        best_group = max(groups, key=lambda g: sum(c.score for c in g))
        # Sort by reading order (top to bottom)
        best_group.sort(key=lambda c: c.bbox[1] if len(c.bbox) >= 2 else 0)
        return " ".join(c.text for c in best_group)

    return None