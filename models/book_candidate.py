"""Candidate book model for ranking search results."""

from __future__ import annotations

from dataclasses import dataclass

from models.book_metadata import BookMetadata


@dataclass
class BookCandidate:
    """A search result candidate before final ranking.

    Fields:
        metadata: The candidate's book metadata.
        title_score: Similarity score for the title (0.0-1.0).
        author_score: Similarity score for the author (0.0-1.0).
        isbn_score: 1.0 if ISBN matches, else 0.0.
        publisher_score: Similarity score for the publisher (0.0-1.0).
        total_score: Weighted total score used for ranking.
        source: Where this candidate came from.
        isbn_state: State of ISBN matching - 'MATCH', 'CONFLICT', or 'UNAVAILABLE'.
            UNAVAILABLE means neither OCR nor candidate has an ISBN (neutral).
            CONFLICT means both have ISBNs but they differ (negative signal).
            MATCH means ISBNs are present and identical (positive signal).
        title_available: True when both OCR and candidate had a title
            (so title_score is meaningful even if 0.0).
        author_available: True when both OCR and candidate had an author.
        publisher_available: True when both OCR and candidate had a publisher.
    """

    metadata: BookMetadata
    title_score: float = 0.0
    author_score: float = 0.0
    isbn_score: float = 0.0
    publisher_score: float = 0.0
    total_score: float = 0.0
    source: str = ""
    isbn_state: str = "UNAVAILABLE"
    title_available: bool = False
    author_available: bool = False
    publisher_available: bool = False
    cover_similarity: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.title_score <= 1.0:
            raise ValueError("title_score must be between 0.0 and 1.0")
        if not 0.0 <= self.author_score <= 1.0:
            raise ValueError("author_score must be between 0.0 and 1.0")
        if not 0.0 <= self.isbn_score <= 1.0:
            raise ValueError("isbn_score must be between 0.0 and 1.0")
        if not 0.0 <= self.publisher_score <= 1.0:
            raise ValueError("publisher_score must be between 0.0 and 1.0")
        if not 0.0 <= self.total_score <= 1.0:
            raise ValueError("total_score must be between 0.0 and 1.0")
        if not 0.0 <= self.cover_similarity <= 1.0:
            raise ValueError("cover_similarity must be between 0.0 and 1.0")
        if self.isbn_state not in ("MATCH", "CONFLICT", "UNAVAILABLE"):
            raise ValueError("isbn_state must be MATCH, CONFLICT, or UNAVAILABLE")

    @classmethod
    def from_metadata(
        cls,
        metadata,
        title_available: bool = False,
        author_available: bool = False,
        publisher_available: bool = False,
        candidate_source: str = "",
    ):
        """Create a BookCandidate from BookMetadata with default scores."""
        return cls(
            metadata=metadata,
            title_score=0.0,
            author_score=0.0,
            isbn_score=0.0,
            publisher_score=0.0,
            total_score=0.0,
            source=metadata.source or candidate_source,
            isbn_state="UNAVAILABLE",
            title_available=title_available,
            author_available=author_available,
            publisher_available=publisher_available,
        )
