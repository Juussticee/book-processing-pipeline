"""Book metadata model — the output of the identification pipeline.

Extended with identification evidence fields for debugging and validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class BookMetadata:
    """Normalized book metadata produced by the identification pipeline.

    Fields:
        title: Book title.
        subtitle: Optional subtitle.
        authors: List of author names.
        isbn_10: ISBN-10 identifier (if available).
        isbn_13: ISBN-13 identifier (if available).
        publisher: Publisher name.
        published_date: Date of publication (ISO string or year).
        language: Language code (e.g. 'en').
        page_count: Number of pages.
        categories: List of category/genre strings.
        description: Book description/summary.
        thumbnail_url: URL to thumbnail image.
        cover_url: URL to cover image.
        image_links: Dict of image URLs from source API (e.g. Google Books
            'smallThumbnail', 'thumbnail', 'small', 'medium', 'large',
            'extraLarge'). Supports fallback cover resolution.
        confidence: Confidence score 0.0–1.0.
        source: Where the metadata came from ('google_books',
            'open_library', 'combined').
        raw_ocr: Raw OCR text that was used for the lookup.
        created_at: When this metadata was produced.
        status: Current status of the book ('identified', 'manual_review',
            'needs_review'). Note: 'rejected' is deprecated — no book is
            ever rejected per non-negotiable requirement.
        drive_file_id: Google Drive file ID of the source image.
        job_id: Processing job ID.

        === Identification Evidence (new) ===
        ocr_text: Full OCR text extracted from the image.
        extracted_title: Title detected by BookTextExtractor.
        extracted_title_candidates: Multiple title candidates from BookTextExtractor.
        extracted_author: Author detected by BookTextExtractor.
        extracted_contributor: Contributor name extracted from collaboration phrases.
        extracted_isbn: ISBN detected by BookTextExtractor.
        extracted_publisher: Publisher detected by BookTextExtractor.
        match_score: Final match score (0.0–1.0) between OCR fields and API result.
        candidate_sources: List of search sources tried (e.g. ['isbn', 'title_author']).
        rejection_reason: If rejected, why (None if accepted).
    """

    title: str
    subtitle: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    isbn_10: Optional[str] = None
    isbn_13: Optional[str] = None
    publisher: Optional[str] = None
    published_date: Optional[str] = None
    language: Optional[str] = None
    page_count: Optional[int] = None
    categories: List[str] = field(default_factory=list)
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    cover_url: Optional[str] = None
    image_links: dict = field(default_factory=dict)
    confidence: float = 0.0
    source: str = ""
    raw_ocr: str = ""
    status: str = "identified"
    drive_file_id: str = ""
    job_id: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)

    # Identification evidence fields (Milestone 5 improvements)
    ocr_text: str = ""
    extracted_title: Optional[str] = None
    extracted_title_candidates: List[str] = field(default_factory=list)
    extracted_author: Optional[str] = None
    extracted_contributor: Optional[str] = None
    extracted_isbn: Optional[str] = None
    extracted_publisher: Optional[str] = None
    match_score: float = 0.0
    candidate_sources: List[str] = field(default_factory=list)
    rejection_reason: Optional[str] = None

    # Verification evidence fields (attached from CandidateVerificationService)
    verification_title_score: Optional[float] = None
    verification_author_score: Optional[float] = None
    verification_isbn_state: Optional[str] = None
    verification_isbn_score: Optional[float] = None
    verification_cover_similarity: Optional[float] = None

    # Vision model evidence fields (Phase 28 — vision-first integration)
    vision_title: Optional[str] = None
    vision_author: Optional[str] = None
    vision_publisher: Optional[str] = None
    vision_confidence: float = 0.0
    vision_used: bool = False

    # Open Library work key (e.g. '/works/OL12627957W') for deep lookup
    open_library_work_key: Optional[str] = None
    # Clean external cover tracking (Phase 35B)
    local_cover_path: Optional[str] = None
    cover_source: Optional[str] = None  # 'google_books', 'open_library', 'exa', 'none'
    cover_source_url: Optional[str] = None
    cover_status: Optional[str] = None  # 'found', 'not_found', 'rejected_mismatch'
    cover_source_type: Optional[str] = None  # 'external_bibliographic'

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("title cannot be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if not 0.0 <= self.match_score <= 1.0:
            raise ValueError("match_score must be between 0.0 and 1.0")
