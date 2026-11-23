"""Phase 43: Final PaddleOCR-VL extractor with correct title/author ordering."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from services.paddleocr_vl_service import LayoutBlock

# Keywords that indicate a block is NOT a title or author
NON_TITLE_KEYWORDS = {
    "l'age d'homme", "éditions", "gallimard", "seuil", "folio",
    "grasset", "flammarion", "collection", "éditeur", "imprint",
    "publisher", "published by", "printed by", "copyright",
    "isbn", "éd.", "rééd.", "tome", "vol.", "réimprimé", "série",
    "numéro", "delt", "points", "bibliothèque", "bibliotheque",
    "vent d'est", "classiques slaves",
}

# Genre/category words that indicate a subtitle, not the main title
SUBTITLE_KEYWORDS = {
    "roman", "poèmes", "nouvelles", "récit", "essai", "étude",
    "théâtre", "poésie", "conte", "chronique", "mémoires",
    "biographie", "autobiographie", "correspondance", "album",
    "guide", "manuel", "dictionnaire", "anthologie",
    "tout en couleurs", "tout en couleurs et en lambeaux",
}

# Common author name patterns
AUTHOR_PATTERNS = [
    re.compile(r'^[A-ZÀ-ÿ][a-zà-ÿ]+ [A-ZÀ-ÿ][a-zà-ÿ]+$'),  # First Last
    re.compile(r'^[A-ZÀ-ÿ][a-zà-ÿ]+ [A-ZÀ-ÿ]\. [A-ZÀ-ÿ][a-zà-ÿ]+$'),  # First M. Last
    re.compile(r'^[A-ZÀ-Ü][a-zà-ü]+-[A-ZÀ-ÿ][a-zà-ÿ]+$'),  # Hyphenated
]


def extract_title_author(layout_blocks: List[LayoutBlock]) -> Tuple[Optional[str], Optional[str]]:
    """Extract title and author from PaddleOCR-VL layout blocks.
    
    Handles multiple book cover layouts:
    1. Title on top, author below (most common)
    2. Author on top, title below (e.g., HISTOIRES D'AMOUR)
    3. Explicit labels (doc_title, paragraph_title)
    """
    if not layout_blocks:
        return None, None
    
    # Sort blocks by reading order (top to bottom, left to right)
    sorted_blocks = sorted(layout_blocks, key=lambda b: (b.bbox[1] if len(b.bbox) >= 4 else 0, b.bbox[0] if len(b.bbox) >= 4 else 0))
    
    # Get all valid text blocks (not publisher, not footer/header)
    text_blocks = []
    for block in sorted_blocks:
        text = _clean_text(block.text)
        if not text:
            continue
        if _is_publisher(text):
            continue
        if block.label in ("header", "footer", "header_image", "footer_image"):
            continue
        text_blocks.append((text, block))
    
    if not text_blocks:
        return None, None
    
    # Single block case
    if len(text_blocks) == 1:
        text, block = text_blocks[0]
        if _is_person_name(text):
            return None, text
        return text, None
    
    # Strategy 1: Find explicitly labeled title blocks
    for text, block in text_blocks:
        if block.label in ("doc_title", "paragraph_title", "title"):
            title = text
            # Find author from remaining blocks (before or after)
            author = _find_author(text_blocks, block)
            return title, author
    
    # Strategy 2: No explicit title label - use positional heuristics
    # Pattern 1: First block is person name, second is not → Author, Title
    # Pattern 2: First block is not person name, second is → Title, Author
    
    first_text, first_block = text_blocks[0]
    second_text, second_block = text_blocks[1]
    
    first_is_person = _is_person_name(first_text)
    second_is_person = _is_person_name(second_text)
    
    if first_is_person and not second_is_person:
        # Author, Title pattern
        author = first_text
        title = second_text
        # Check if there's more title text in subsequent blocks
        for text, block in text_blocks[2:]:
            if not _is_person_name(text) and text.lower() not in SUBTITLE_KEYWORDS:
                title += " " + text
            else:
                break
        return title, author
    
    elif not first_is_person and second_is_person:
        # Title, Author pattern
        title = first_text
        author = second_text
        return title, author
    
    elif not first_is_person and not second_is_person:
        # Both not person names - pick longer as title
        if len(first_text) >= len(second_text):
            title = first_text
            author = second_text
        else:
            title = second_text
            author = first_text
        return title, author
    
    return None, None


def _find_author(text_blocks: List[Tuple[str, LayoutBlock]], title_block: LayoutBlock) -> Optional[str]:
    """Find author from text blocks, looking both before and after the title."""
    # First look after the title
    found_title = False
    for text, block in text_blocks:
        if block == title_block:
            found_title = True
            continue
        if not found_title:
            continue
        if _is_person_name(text):
            return text
    
    # Then look before the title
    for text, block in reversed(text_blocks):
        if block == title_block:
            break
        if _is_person_name(text):
            return text
    
    # No person name found - return first non-title block
    for text, block in text_blocks:
        if block == title_block:
            continue
        if text.lower() not in SUBTITLE_KEYWORDS:
            return text
    
    return None


def _clean_text(text: str) -> Optional[str]:
    """Clean text by removing markdown headers and extra whitespace."""
    if not text:
        return None
    text = re.sub(r'^#+\s*', '', text)
    text = text.strip()
    if not text:
        return None
    return text


def _is_publisher(text: str) -> bool:
    """Check if text is a publisher/collection name."""
    text_lower = text.lower()
    for kw in NON_TITLE_KEYWORDS:
        if kw in text_lower:
            return True
    return False


def _is_person_name(text: str) -> bool:
    """Check if text looks like a person name."""
    words = text.split()
    if len(words) < 2 or len(words) > 4:
        return False
    
    if not all(w[0].isupper() for w in words if w):
        return False
    
    for pattern in AUTHOR_PATTERNS:
        if pattern.match(text):
            return True
    
    if re.match(r'^[A-ZÀ-ÿ][a-zà-ÿ]+[- ][A-ZÀ-ÿ]?[a-zà-ÿ]+$', text):
        return True
    if re.match(r'^[A-ZÀ-ÿ][a-zà-ÿ]+ (de|du|van|von|del) [A-ZÀ-ÿ][a-zà-ÿ]+$', text):
        return True
    
    return False
