"""Phase 27 — Vision-model book-cover extraction.

FRESH VISUAL ANALYSIS APPROACH:

The model receives the RAW image and performs visual reasoning to extract
book metadata. It does NOT rely on any previous OCR output, heuristics,
or candidate ranking. The image is the sole source of truth.

The model must:
1. Determine cover orientation (upright / 90 / 180 / 270)
2. Detect multiple books and select the target
3. Read the COMPLETE title (not just the first line)
4. Identify the author
5. Extract publisher if clearly visible
6. Return structured JSON

This is ONE vision-model request per image. No agent loops, no OCR pre-processing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# Prompt sent to the vision model. This is the ONLY instruction.
# No prior OCR results, no heuristics, no candidate data.
EXTRACTION_PROMPT = """
You are a rigorous visual book-cover metadata extraction specialist.

You receive ONE image and must extract book metadata by VISUAL INSPECTION ALONE.
Do NOT use OCR output, prior pipeline results, heuristics, or external knowledge.
The image is the ONLY source of truth.

==================================================
STRICT EXTRACTION PROTOCOL
==================================================

STEP 1 — FULL SCENE INSPECTION
Inspect the ENTIRE image before extracting anything.
Count ALL visible books, covers, boxes, papers, or other text-bearing objects.

STEP 2 — TARGET BOOK IDENTIFICATION
Determine which physical book is the TARGET BOOK.
The target book is the one the reader would hold — the one being reviewed or photographed as the main subject.
If you cannot determine which book is the target: set target_book_identified = false and return null for all fields.
Text from other books MUST NOT be assigned to the target.

STEP 3 — ORIENTATION CORRECTION
Determine the orientation of the TARGET BOOK (not the whole image):
- normal: text reads left-to-right, top-to-bottom as expected
- rotated_90_cw: book rotated 90° clockwise (read counter-clockwise)
- rotated_90_ccw: book rotated 90° counter-clockwise (read clockwise)
- rotated_180: book upside down
Mentally rotate the target book to its correct reading orientation before reading text.
DO NOT report "normal" if the text appears upside down or rotated.

STEP 4 — TEXT EXTRACTION
Read ONLY text that physically belongs to the TARGET BOOK.
A title can span multiple lines — reconstruct it completely.
DO NOT truncate. If "LOVE YOUR LIFE / SISTER, / LA PETITE" are on the same cover,
return "Love Your Life, Sister, La Petite" — not just "Love Your Life".
Preserve accents, apostrophes, hyphens, and punctuation as visually supported.

STEP 5 — FIELD ASSIGNMENT
title: COMPLETE title, multi-line reconstructed, null if unreadable
author: Comma-separated if multiple, null if not visible
publisher: ONLY if visibly printed on the target cover, null if uncertain
subtitle: Only if clearly distinguished from the title on the cover

STEP 6 — CONFIDENCE
Return field-level confidence based on VISUAL EVIDENCE only — not familiarity.
0.90–1.00: Clearly visible and unambiguous
0.50–0.89: Some visual uncertainty (blur, angle, occlusion)
Below 0.50: Prefer null over low-confidence guesses
NEVER assign high confidence to a guessed value.

STEP 7 — UNCERTAINTY
List every reason extraction is uncertain:
- target book partially covered/rotated
- title partially obscured
- author text too small/unclear
- publisher logo unclear
- text from another book nearby
- damaged/stained cover
- orientation required correction

==================================================
NO HALLUCINATION RULE (MANDATORY)
==================================================

The model MUST prefer null over guessing.

NEVER:
- invent text not visible on the cover
- complete partially visible text from memory
- infer an author from a known title
- infer a publisher from a known author
- use external knowledge to fill missing visual information
- assume that a plausible result is correct
- use information from another book in the image

If insufficient visual evidence: return null.

==================================================
FINAL CHECKLIST (before returning JSON)
==================================================

1. Did I inspect the entire image?
2. Did I identify the physical target book?
3. Are there other books in the image?
4. Could any extracted text belong to another book?
5. Is the target book rotated?
6. Did I mentally correct its orientation?
7. Did I reconstruct the COMPLETE multi-line title?
8. Did I accidentally truncate after the first line?
9. Is the author actually visible on the target cover?
10. Is the publisher actually visible on the target cover?
11. Did I use visual evidence rather than memory?
12. Did I infer anything not directly visible?
13. If uncertain, did I return null instead of guessing?
14. Do confidence scores reflect actual visual evidence?

==================================================
OUTPUT FORMAT (JSON only, no markdown)
==================================================

{
  "title": "..." or null,
  "title_confidence": 0.0,
  "author": "..." or null,
  "author_confidence": 0.0,
  "publisher": "..." or null,
  "publisher_confidence": 0.0,
  "subtitle": "..." or null,
  "orientation": "normal|rotated_90_cw|rotated_90_ccw|rotated_180|unknown",
  "multiple_books_detected": true or false,
  "target_book_identified": true or false,
  "other_text": ["..."],
  "uncertainties": ["..."]
}

Rules:
- Never invent missing information
- If something is not readable, use null
- confidence between 0 and 1
- uncertainties is a list of strings explaining what is uncertain
- other_text contains ONLY text from the target book (series, collection, edition, etc.)
"""

@dataclass
class VisionMetadata:
    """Structured book-cover metadata extracted via visual analysis."""
    title: Optional[str] = None
    title_confidence: float = 0.0
    author: Optional[str] = None
    author_confidence: float = 0.0
    publisher: Optional[str] = None
    publisher_confidence: float = 0.0
    subtitle: Optional[str] = None
    orientation: str = "normal"
    multiple_books_detected: bool = False
    target_book_identified: bool = True
    other_text: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "title_confidence": self.title_confidence,
            "author": self.author,
            "author_confidence": self.author_confidence,
            "publisher": self.publisher,
            "publisher_confidence": self.publisher_confidence,
            "subtitle": self.subtitle,
            "orientation": self.orientation,
            "multiple_books_detected": self.multiple_books_detected,
            "target_book_identified": self.target_book_identified,
            "other_text": self.other_text,
            "uncertainties": self.uncertainties,
        }


class VisionBookCoverExtractor:
    """Extract book-cover metadata via a single vision-model request per image.

    The vision model receives the raw image and reasons about orientation,
    multiple books, complete titles, author, and publisher directly from pixels.
    """

    def __init__(self):
        self.prompt = EXTRACTION_PROMPT

    def extract_from_image(
        self,
        image_path: str,
        # Optional: previous OCR results for debug comparison ONLY (never influences extraction)
        previous_title: Optional[str] = None,
        previous_author: Optional[str] = None,
        previous_confidence: Optional[float] = None,
    ) -> VisionMetadata:
        """Extract metadata from a single book-cover image.

        Sends ONE request to the vision model with the image and extraction prompt.
        Previous OCR results are accepted ONLY for debug logging — they do NOT
        influence the model's decision.

        Args:
            image_path: Absolute path to the book-cover image.
            previous_title: (debug only) Previous OCR title for comparison.
            previous_author: (debug only) Previous OCR author for comparison.
            previous_confidence: (debug only) Previous confidence score.

        Returns:
            VisionMetadata with extracted fields.
        """
        # The vision model call happens here. The image is read and sent
        # as base64 to the vision model with EXTRACTION_PROMPT.
        #
        # Implementation depends on the vision model backend:
        # - For local vision_analyze tool: the image is loaded into context
        # - For HF Inference API: image sent as data URI
        #
        # This method returns the parsed JSON from the model response.
        raise NotImplementedError(
            "Use extract_from_response() for testing with pre-cached responses, "
            "or override this method for the active vision backend."
        )

    @staticmethod
    def extract_from_response(model_response: str) -> VisionMetadata:
        """Parse a vision model JSON response into VisionMetadata.

        Handles responses that may be wrapped in markdown code blocks
        or contain explanatory text before/after the JSON.
        """
        """Parse a vision model JSON response into VisionMetadata.

        Handles responses that may be wrapped in markdown code blocks
        or contain explanatory text before/after the JSON.
        """
        # Strip markdown code fences if present
        cleaned = model_response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        # Find the JSON object
        json_start = cleaned.find("{")
        if json_start < 0:
            raise ValueError(f"No JSON object found in response: {model_response[:200]}")
        json_text = cleaned[json_start:]

        # Strip trailing text after the JSON object using bracket matching
        data = VisionBookCoverExtractor._extract_json_object(json_text)

        return VisionMetadata(
            title=data.get("title"),
            title_confidence=data.get("title_confidence", 0.0),
            author=data.get("author"),
            author_confidence=data.get("author_confidence", 0.0),
            publisher=data.get("publisher"),
            publisher_confidence=data.get("publisher_confidence", 0.0),
            subtitle=data.get("subtitle"),
            orientation=_normalize_orientation(data.get("orientation", "normal")),
            multiple_books_detected=data.get("multiple_books_detected", False),
            target_book_identified=data.get("target_book_identified", True),
            other_text=data.get("other_text", []),
            uncertainties=data.get("uncertainties", []),
        )


    @staticmethod
    def _extract_json_object(text: str) -> dict:
        """Extract a JSON object from text that may have trailing content.
        
        Uses bracket counting to find the complete JSON object,
        handling nested braces.
        """
        depth = 0
        start = text.index("{")
        for i, char in enumerate(text[start:], start=start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    json_str = text[start:i + 1]
                    return json.loads(json_str)
        # Fallback: try json.loads on the whole thing
        return json.loads(text)

    def extract_from_cache(self, cache_key: str, cache_path: str) -> VisionMetadata:
        """Load a cached vision model response from disk.

        Args:
            cache_key: Key in the JSON cache file.
            cache_path: Path to the JSON cache file.

        Returns:
            VisionMetadata parsed from the cached response.
        """
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)

        response = cache[cache_key]
        if isinstance(response, dict):
            response = json.dumps(response)

        return self.extract_from_response(response)



def _normalize_orientation(orientation: str) -> str:
    """Normalize orientation values to canonical form."""
    if not orientation:
        return "unknown"
    o = orientation.strip().lower()
    if o in ("normal", "upright", "normal"):
        return "normal"
    elif o in ("rotated_90_cw", "rotated_90", "90_cw", "90"):
        return "rotated_90_cw"
    elif o in ("rotated_90_ccw", "rotated_270", "90_ccw", "270"):
        return "rotated_90_ccw"
    elif o in ("rotated_180", "180", "upside_down", "upside-down"):
        return "rotated_180"
    elif o in ("unknown", "none"):
        return "unknown"
    return "unknown"


def normalize_for_comparison(text: Optional[str]) -> str:
    """Normalize text for fuzzy comparison.

    Lowercases, removes accents, strips punctuation.
    Used ONLY in the benchmark, NOT in extraction.
    """
    if not text:
        return ""
    import unicodedata
    normalized = unicodedata.normalize("NFKD", text).lower()
    no_accents = "".join(c for c in normalized if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9\s]", " ", no_accents)
    return " ".join(cleaned.split())


def fuzzy_match(text1: Optional[str], text2: Optional[str], threshold: float = 0.85) -> bool:
    """Compare two strings using token-based fuzzy matching.

    Uses difflib's SequenceMatcher on normalized text.
    """
    from difflib import SequenceMatcher
    n1 = normalize_for_comparison(text1)
    n2 = normalize_for_comparison(text2)
    if not n1 and not n2:
        return True
    if not n1 or not n2:
        return False
    ratio = SequenceMatcher(None, n1, n2).ratio()
    return ratio >= threshold


def compare_title(extracted: Optional[str], expected: str) -> bool:
    """Compare extracted title against expected ground truth.

    Uses fuzzy matching with accent and case insensitivity.
    Also handles cases where the vision model appends subtitle text
    to the title by checking if the expected title is a substring.
    """
    if not extracted and not expected:
        return True
    if not extracted or not expected:
        return False

    # Exact or near-exact match
    if fuzzy_match(extracted, expected, threshold=0.80):
        return True

    # Substring match: vision title may contain extra text
    norm_extracted = normalize_for_comparison(extracted)
    norm_expected = normalize_for_comparison(expected)
    if norm_expected and norm_expected in norm_extracted:
        return True

    return False


def compare_author(extracted: Optional[str], expected: str) -> bool:
    """Compare extracted author against expected ground truth.

    Handles comma-separated author lists: if the vision model returns
    multiple authors (e.g. "Pavle Ivić, Nikola Samardžić, Anne Yelen"),
    any single match against the expected author is sufficient.
    """
    if not extracted and not expected:
        return True
    if not extracted or not expected:
        return False

    # Split extracted author on commas; check each individually
    authors = [a.strip() for a in extracted.split(",") if a.strip()]
    if len(authors) <= 1:
        return fuzzy_match(extracted, expected, threshold=0.80)

    # Try matching any individual author against the expected
    normalized_expected = normalize_for_comparison(expected)
    for author in authors:
        if fuzzy_match(author, expected, threshold=0.80):
            return True

    # If no individual match, try the full string as well
    return fuzzy_match(extracted, expected, threshold=0.70)
