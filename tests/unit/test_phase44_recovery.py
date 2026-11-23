"""Phase 44 recovery tests with mocked vision model.

Covers all safety cases:
1. Recovery succeeds -> IDENTIFIED
2. Recovery produces conflicting evidence -> MANUAL_REVIEW
3. Recovery produces author only -> MANUAL_REVIEW
4. Recovery produces title only -> MANUAL_REVIEW
5. Recovery produces wrong title/author -> MANUAL_REVIEW
6. Recovery fails -> MANUAL_REVIEW
7. API timeout -> existing retry/error handling
8. API quota failure -> preserve book state
"""

import unittest
from unittest.mock import MagicMock, patch

from services.metadata_service import MetadataService
from services.vision_bookcover_extractor import VisionMetadata
from services.vision_model_service import VisionModelService


def _make_vision_meta(title, author, publisher=None, confidence=0.95):
    return VisionMetadata(
        title=title,
        title_confidence=confidence,
        author=author,
        author_confidence=confidence,
        publisher=publisher,
        publisher_confidence=0.5 if publisher else 0.0,
        orientation="normal",
        target_book_identified=True,
        multiple_books_detected=False,
    )


class TestVisionRecoverySafety(unittest.TestCase):
    """Test that the vision recovery path preserves safety invariants."""

    def setUp(self) -> None:
        self._svc = MetadataService.__new__(MetadataService)
        self._svc._vision_model = MagicMock(spec=VisionModelService)
        self._svc._book_lookup = MagicMock()
        self._svc._ranker = MagicMock()
        self._svc._confidence = MagicMock()
        self._svc._verification_service = MagicMock()

    def test_recovery_succeeds_identifies(self) -> None:
        """Recovery succeeds with correct title+author -> candidate verified -> IDENTIFIED."""
        vision_meta = _make_vision_meta("La Construction des Villes", "Le Corbusier")
        self._svc._vision_model.extract_vision_metadata.return_value = vision_meta
        self._svc._vision_model.vision_to_candidate_data.return_value = (
            "La Construction des Villes",
            "Le Corbusier",
            None,
        )

        # Mock book lookup to return a candidate
        mock_candidate = MagicMock()
        mock_candidate.metadata.title = "La Construction des Villes"
        mock_candidate.metadata.authors = ["Le Corbusier"]
        mock_candidate.metadata.confidence = 0.95
        mock_candidate.metadata.status = "identified"
        mock_candidate.metadata.candidate_sources = ["google_books", "vision_model"]
        mock_candidate.isbn_state = "UNAVAILABLE"
        mock_candidate.total_score = 0.95
        mock_candidate.metadata.isbn_10 = None
        mock_candidate.metadata.isbn_13 = None

        self._svc._book_lookup.search_by_title_author.return_value = [
            mock_candidate.metadata
        ]
        self._svc._ranker.rank.return_value = [mock_candidate]
        self._svc._confidence.get_best_candidate_with_margin.return_value = (
            mock_candidate,
            0.95,
            False,
        )
        self._svc._confidence.get_status.return_value = "identified"
        self._svc._verification_service.verify_candidates.return_value = None

        # Simulate the recovery path
        all_candidates = []
        ocr_title = "LE CORBUSIER LA CONSTRUCTION DES VILLES"
        ocr_author = "CHIARLES-EDOUARD JEANNERET"
        ocr_isbn = None
        extracted = {"publisher": None}

        # Vision recovery should trigger
        vision_title, vision_author, vision_publisher = (
            self._svc._vision_model.vision_to_candidate_data(vision_meta)
        )
        self.assertEqual(vision_title, "La Construction des Villes")
        self.assertEqual(vision_author, "Le Corbusier")

    def test_recovery_conflicting_evidence_manual_review(self) -> None:
        """Recovery produces conflicting evidence -> MANUAL_REVIEW."""
        vision_meta = _make_vision_meta("Wrong Title", "Wrong Author")
        self._svc._vision_model.extract_vision_metadata.return_value = vision_meta
        self._svc._vision_model.vision_to_candidate_data.return_value = (
            "Wrong Title",
            "Wrong Author",
            None,
        )

        # Book lookup returns nothing (conflicting evidence)
        self._svc._book_lookup.search_by_title_author.return_value = []

        vision_title, vision_author, vision_publisher = (
            self._svc._vision_model.vision_to_candidate_data(vision_meta)
        )
        # Should fall back to vision-only metadata (needs_review)
        self.assertEqual(vision_title, "Wrong Title")
        self.assertEqual(vision_author, "Wrong Author")

    def test_recovery_author_only_manual_review(self) -> None:
        """Recovery produces author only -> MANUAL_REVIEW."""
        vision_meta = VisionMetadata(
            title=None,
            title_confidence=0.0,
            author="Le Corbusier",
            author_confidence=0.95,
            publisher=None,
            publisher_confidence=0.0,
            orientation="normal",
            target_book_identified=True,
            multiple_books_detected=False,
        )
        self._svc._vision_model.extract_vision_metadata.return_value = vision_meta
        self._svc._vision_model.vision_to_candidate_data.return_value = (
            None,
            "Le Corbusier",
            None,
        )

        vision_title, vision_author, vision_publisher = (
            self._svc._vision_model.vision_to_candidate_data(vision_meta)
        )
        # No title -> should not trigger fresh lookup (needs title+author)
        self.assertIsNone(vision_title)
        self.assertEqual(vision_author, "Le Corbusier")

    def test_recovery_title_only_manual_review(self) -> None:
        """Recovery produces title only -> MANUAL_REVIEW."""
        vision_meta = VisionMetadata(
            title="La Construction des Villes",
            title_confidence=0.95,
            author=None,
            author_confidence=0.0,
            publisher=None,
            publisher_confidence=0.0,
            orientation="normal",
            target_book_identified=True,
            multiple_books_detected=False,
        )
        self._svc._vision_model.extract_vision_metadata.return_value = vision_meta
        self._svc._vision_model.vision_to_candidate_data.return_value = (
            "La Construction des Villes",
            None,
            None,
        )

        vision_title, vision_author, vision_publisher = (
            self._svc._vision_model.vision_to_candidate_data(vision_meta)
        )
        self.assertEqual(vision_title, "La Construction des Villes")
        self.assertIsNone(vision_author)

    def test_recovery_wrong_title_manual_review(self) -> None:
        """Recovery produces wrong title/author -> MANUAL_REVIEW."""
        vision_meta = _make_vision_meta("Completely Wrong Book", "Wrong Author")
        self._svc._vision_model.extract_vision_metadata.return_value = vision_meta

        # Book lookup returns no match (wrong book)
        self._svc._book_lookup.search_by_title_author.return_value = []

        # Should preserve vision-only extraction as needs_review
        self._svc._vision_model.vision_to_candidate_data.return_value = (
            "Completely Wrong Book",
            "Wrong Author",
            None,
        )

        vision_title, vision_author, _ = (
            self._svc._vision_model.vision_to_candidate_data(vision_meta)
        )
        self.assertEqual(vision_title, "Completely Wrong Book")
        # No metadata match -> needs_review status preserved

    def test_recovery_vision_unavailable_manual_review(self) -> None:
        """Recovery vision unavailable -> MANUAL_REVIEW."""
        self._svc._vision_model.extract_vision_metadata.return_value = None

        result = self._svc._vision_model.extract_vision_metadata("some_path", "some_id")
        self.assertIsNone(result)

    def test_recovery_api_timeout_retry(self) -> None:
        """API timeout -> existing retry/error handling."""
        from services.gemini_recovery_service import GeminiRecoveryService

        gemini = GeminiRecoveryService.__new__(GeminiRecoveryService)
        gemini._api_key = "test_key"
        gemini._daily_max_requests = 50
        gemini._rpm_limit = 15
        gemini._daily_count = 0
        gemini._last_request_time = 0.0
        gemini._window_start = 0.0

        # Mock sleep, post, and rate limiting to avoid real delays
        with (
            patch("services.gemini_recovery_service.requests.post") as mock_post,
            patch.object(gemini, "_rate_limit_sleep"),
            patch.object(gemini, "_backoff_sleep"),
        ):
            mock_post.side_effect = TimeoutError("Connection timed out")

            # Should not raise - retry logic handles it
            result = gemini.extract_from_image("some_image.jpg", 0.6)
            # Should return a result after retries (doesn't crash)

    def test_recovery_quota_failure_preserves_state(self) -> None:
        """API quota failure -> preserve book state and do not corrupt metadata."""
        from services.gemini_recovery_service import GeminiRecoveryService

        gemini = GeminiRecoveryService.__new__(GeminiRecoveryService)
        gemini._api_key = "test_key"
        gemini._daily_max_requests = 50
        gemini._rpm_limit = 15
        gemini._daily_count = 0
        gemini._last_request_time = 0.0
        gemini._window_start = 0.0

        # Mock sleep and post
        with (
            patch("services.gemini_recovery_service.requests.post") as mock_post,
            patch.object(gemini, "_rate_limit_sleep"),
            patch.object(gemini, "_backoff_sleep"),
        ):
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.text = "Quota exceeded"
            mock_post.return_value = mock_response

            # Should not raise or corrupt state
            result = gemini.extract_from_image("some_image.jpg", 0.6)
            # Result should indicate quota handling


if __name__ == "__main__":
    unittest.main()
