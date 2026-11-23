"""Unit tests for MetadataService.

Updated for strict confidence thresholds and adaptive scoring (Milestone 5):
- confidence >= 0.85 -> identified (auto-accept)
- 0.50 - 0.85 -> needs_review (manual review)
- < 0.50 -> rejected
"""

from unittest.mock import MagicMock, patch

import pytest

from models.book_candidate import BookCandidate
from models.book_metadata import BookMetadata
from models.ocr_result import OCRResult
from services.metadata_service import MetadataService


class TestOrientationCorrection:
    """Tests for _try_orientation_correction: OCR-based fallback for rotated images."""

    def test_skips_orientation_when_ocr_quality_sufficient(self):
        """When OCR has enough words and confidence, no rotation attempt is made."""
        pre = MagicMock()
        ocr_svc = MagicMock()
        service = MetadataService(
            image_preprocessor=pre,
            ocr_service=ocr_svc,
        )
        # OCR with 5 words and good confidence — should skip completely
        good_ocr = OCRResult(
            text="Good Book Title\nAuthor Name",
            confidence=0.90,
            words=[MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()],
            language="en",
            image_path="/fake/image.jpg",
        )
        result = service._try_orientation_correction("/fake/image.jpg", good_ocr)
        # No rotation attempt, no extra OCR calls
        assert result is good_ocr
        ocr_svc.extract_text.assert_not_called()

    def test_tries_all_rotations_when_ocr_quality_poor(self):
        """When OCR quality is poor, all 4 rotations should be tried with OCR
        and the one producing the most words is selected.

        This is necessary because the visual heuristic may pick the wrong angle.
        We prove correctness via OCR word count — the rotation with the most
        readable text wins.
        """
        pre = MagicMock()
        ocr_svc = MagicMock()

        pre._temp_dir = "/tmp/test"

        service = MetadataService(
            image_preprocessor=pre,
            ocr_service=ocr_svc,
        )

        # Poor OCR: 1 word, low confidence (typical of upside-down image)
        poor_ocr = OCRResult(
            text="Jed",
            confidence=0.10,
            words=[MagicMock(text="Jed", confidence=0.1, bounding_box=[])],
            language="en",
            image_path="/fake/image.jpg",
        )

        # OCR results per rotation angle
        # 0° (original): 1 word
        # 90°: 3 words
        # 180°: 10 words (the correct rotation)
        # 270°: 2 words
        def make_ocr(text, n_words):
            return OCRResult(
                text=text,
                confidence=0.87,
                words=[
                    MagicMock(text=f"w{i}", confidence=0.87, bounding_box=[])
                    for i in range(n_words)
                ],
                language="en",
                image_path=f"/tmp/test/rotated_{n_words}.jpg",
            )

        ocr_by_angle = {
            0: poor_ocr,
            90: make_ocr("a b c", 3),
            180: make_ocr("Analyse\nde\nVIE ET MORT\nde la Yougoslavaie", 10),
            270: make_ocr("8 5 5 1", 2),
        }

        # Track call sequence: first call returns poor OCR (original),
        # subsequent calls return angle results by order (90, 180, 270)
        call_count = [0]

        def side_effect(path):
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                return poor_ocr  # original OCR (already done)
            elif idx == 1:
                return ocr_by_angle[90]
            elif idx == 2:
                return ocr_by_angle[180]
            elif idx == 3:
                return ocr_by_angle[270]
            return poor_ocr

        ocr_svc.extract_text.side_effect = side_effect

        # Mock PIL image and rotation
        with (
            patch("PIL.Image.open") as mock_open,
            patch.object(pre, "correct_orientation") as mock_correct,
            patch.object(pre, "_rotate_image") as mock_rotate,
        ):
            mock_img = MagicMock()
            mock_img.convert.return_value = mock_img
            mock_img.save = MagicMock()
            mock_open.return_value = mock_img

            # correct_orientation should rotate by the specified angle
            def correct_fn(img, orient):
                return pre._rotate_image(img, orient.angle)

            mock_correct.side_effect = correct_fn
            mock_rotate.side_effect = lambda img, angle: img

            result = service._try_orientation_correction("/fake/image.jpg", poor_ocr)

        # The method should have tried all 4 rotations (at least 2 additional OCR calls after the original)
        total_ocr_calls = ocr_svc.extract_text.call_count
        assert (
            total_ocr_calls >= 3
        ), f"Expected at least 3 OCR calls (original + corrections), got {total_ocr_calls}"

        # Should return the best OCR result (180° with 10 words)
        assert len(result.words) == 10

    def test_orientation_fallback_finds_correct_rotation(self):
        """When the heuristic picks the wrong angle, OCR comparison should find the best one."""
        pre = MagicMock()
        ocr_svc = MagicMock()

        service = MetadataService(
            image_preprocessor=pre,
            ocr_service=ocr_svc,
        )

        pre._temp_dir = "/tmp/test"

        # Poor OCR: 2 words
        poor_ocr = OCRResult(
            text="Jed LMOW",
            confidence=0.30,
            words=[MagicMock(), MagicMock()],
            language="en",
            image_path="/fake/image.jpg",
        )

        # The correct rotation (180°) produces 10 words
        # Other rotations produce fewer
        good_ocr = OCRResult(
            text="Analyse\nde\nVIE ET MORT\nde la Yougoslavaie\nde\nPaul\nGarde\npar\nAnne\nYelev",
            confidence=0.87,
            words=[MagicMock() for _ in range(10)],
            language="en",
            image_path="/fake/corrected.jpg",
        )

        ocr_svc.extract_text.return_value = good_ocr

        # Mock PIL
        with patch("PIL.Image.open") as mock_open:
            mock_img = MagicMock()
            mock_img.convert.return_value = mock_img
            mock_img.save = MagicMock()
            mock_open.return_value = mock_img

            pre.detect_orientation = MagicMock()
            from models.image_orientation import ImageOrientation

            pre.detect_orientation.return_value = ImageOrientation(
                angle=180, confidence=0.9, was_corrected=True
            )
            pre.correct_orientation = MagicMock(side_effect=lambda img, orient: img)

            result = service._try_orientation_correction("/fake/image.jpg", poor_ocr)

        # Should return the corrected OCR (more words than original)
        assert result.text != "Jed LMOW"
        assert "Yougoslavaie" in result.text or len(result.words) > len(poor_ocr.words)

    def test_combined_score_not_word_count_only(self):
        """Word count alone is unreliable — a wrong rotation with many low-conf
        garbage words should not beat the correct rotation with fewer high-conf words.
        We use combined score (words * avg_confidence) to weight readability quality.
        """
        pre = MagicMock()
        ocr_svc = MagicMock()

        pre._temp_dir = "/tmp/test"

        service = MetadataService(
            image_preprocessor=pre,
            ocr_service=ocr_svc,
        )

        # Poor OCR: 1 word, very low confidence
        poor_ocr = OCRResult(
            text="Jed",
            confidence=0.05,
            words=[MagicMock(text="Jed", confidence=0.05, bounding_box=[])],
            language="en",
            image_path="/fake/image.jpg",
        )

        # 180° (correct): 5 words, high confidence each
        correct_ocr = OCRResult(
            text="Analyse de VIE ET MORT",
            confidence=0.85,
            words=[
                MagicMock(text=f"w{i}", confidence=0.85, bounding_box=[])
                for i in range(5)
            ],
            language="en",
            image_path="/tmp/test/correct.jpg",
        )

        # 90° (wrong): 10 words but very low confidence (garbage)
        garbage_ocr = OCRResult(
            text="a b c d e f g h i j",
            confidence=0.10,
            words=[
                MagicMock(text=f"g{i}", confidence=0.10, bounding_box=[])
                for i in range(10)
            ],
            language="en",
            image_path="/tmp/test/garbage.jpg",
        )

        # Correct rotation combined score = 5 * 0.85 = 4.25
        # Garbage rotation combined score = 10 * 0.10 = 1.00
        # Correct should win despite fewer words

        call_count = [0]

        def side_effect(path):
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                return poor_ocr
            elif idx == 1:
                return garbage_ocr  # 90° tried first (heuristic)
            elif idx == 2:
                return correct_ocr  # 180°
            elif idx == 3:
                return garbage_ocr  # 270°
            return poor_ocr

        ocr_svc.extract_text.side_effect = side_effect

        with (
            patch("PIL.Image.open") as mock_open,
            patch.object(pre, "correct_orientation") as mock_correct,
            patch.object(pre, "_rotate_image") as mock_rotate,
        ):
            mock_img = MagicMock()
            mock_img.convert.return_value = mock_img
            mock_img.save = MagicMock()
            mock_open.return_value = mock_img

            mock_correct.side_effect = lambda img, orient: pre._rotate_image(
                img, orient.angle
            )
            mock_rotate.side_effect = lambda img, angle: img

            result = service._try_orientation_correction("/fake/image.jpg", poor_ocr)

        # Should return the correct rotation (180°) despite fewer words,
        # because combined score (words * confidence) is higher.
        assert result.confidence == 0.85
        assert len(result.words) == 5
        assert result.text == "Analyse de VIE ET MORT"


class TestMetadataServiceNameErrorFix:
    """Tests that NameError does not occur when preprocess() fails."""

    def test_nameerror_not_raised_on_preprocess_failure(self):
        """If preprocess() raises, the finally block must not raise NameError."""
        preprocessor = MagicMock()
        preprocessor.preprocess.side_effect = ValueError("corrupt image")
        preprocessor.cleanup = MagicMock()

        service = MetadataService(
            image_preprocessor=preprocessor,
            ocr_service=MagicMock(),
            text_normalizer=MagicMock(),
            book_lookup=MagicMock(),
            candidate_ranker=MagicMock(),
            confidence_service=MagicMock(),
        )

        # Should raise ValueError (the original), NOT NameError
        with pytest.raises(ValueError, match="corrupt image"):
            service.identify_book("/fake/image.jpg")

        # Cleanup should NOT have been called (preprocessed_path was None)
        preprocessor.cleanup.assert_not_called()


class TestConfidenceThresholdFix:
    """Tests that the confidence threshold uses ConfidenceService output,
    not the ranker's total_score."""

    def test_confidence_uses_confidence_service_not_total_score(self):
        """The threshold check must use ConfidenceService.calculate(),
        not the CandidateRanker.total_score."""
        metadata = BookMetadata(
            title="Test Book",
            confidence=0.85,  # What ConfidenceService sets
            source="google_books",
        )
        candidate = BookCandidate(
            metadata=metadata,
            title_score=0.80,
            author_score=0.70,
            isbn_score=0.90,
            publisher_score=0.50,
            total_score=0.75,  # Different from confidence!
        )

        confidence_svc = MagicMock()
        confidence_svc.get_best_candidate_with_margin.return_value = (
            candidate,
            0.85,
            False,
        )
        confidence_svc.is_auto_accepted.return_value = True
        confidence_svc.get_status.return_value = "identified"

        # Mock other services
        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()
        ocr_service = MagicMock()
        ocr_service.extract_text.return_value = OCRResult(
            text="Test Book by Author ISBN 9780306406157",
            confidence=0.9,
            words=[],
            language="en",
            image_path="/fake/image.jpg",
        )
        text_normalizer = MagicMock()
        text_normalizer.normalize.return_value = "Test Book"
        text_normalizer.extract_isbn.return_value = ["9780306406157"]
        book_lookup = MagicMock()
        book_lookup.search_by_isbn.return_value = [metadata]

        service = MetadataService(
            image_preprocessor=preprocessor,
            ocr_service=ocr_service,
            text_normalizer=text_normalizer,
            book_lookup=book_lookup,
            candidate_ranker=MagicMock(),
            confidence_service=confidence_svc,
        )

        result, candidates = service.identify_book(
            "/fake/image.jpg",
            drive_file_id="drive_test",
            job_id="job_test",
        )

        # Confidence=0.85 >= 0.85 threshold -> should return the metadata
        assert result is not None, "Confidence 0.85 should be auto-accepted (>= 0.85)"
        assert result.title == "Test Book"


class TestMetadataPersistenceIntegration:
    """Tests that MetadataService persists metadata via the repository."""

    def test_persist_called_on_success(self):
        """_persist_metadata is called after successful identification."""
        metadata = BookMetadata(
            title="Persisted Book",
            authors=["Persisted Author"],
            confidence=0.95,
            source="google_books",
        )
        candidate = BookCandidate(
            metadata=metadata,
            title_score=0.9,
            author_score=0.8,
            isbn_score=0.95,
            publisher_score=0.6,
            total_score=0.85,
            title_available=True,
            author_available=True,
        )

        confidence_svc = MagicMock()
        confidence_svc.get_best_candidate_with_margin.return_value = (
            candidate,
            0.95,
            False,
        )
        confidence_svc.is_auto_accepted.return_value = True
        confidence_svc.get_status.return_value = "identified"

        preprocessor = MagicMock()
        preprocessor.preprocess.return_value = "/fake/preprocessed.jpg"
        preprocessor.cleanup = MagicMock()
        ocr_service = MagicMock()
        ocr_service.extract_text.return_value = OCRResult(
            text="Persisted Book by Persisted Author",
            confidence=0.9,
            words=[],
            language="en",
            image_path="/fake/image.jpg",
        )
        text_normalizer = MagicMock()
        text_normalizer.normalize.return_value = "Persisted Book"
        text_normalizer.extract_isbn.return_value = []
        book_lookup = MagicMock()
        book_lookup.search_by_isbn.return_value = []
        book_lookup.search_by_title_author.return_value = []
        book_lookup.search_by_title.return_value = [metadata]
        text_extractor = MagicMock()
        text_extractor.extract.return_value = {
            "title": "Persisted Book",
            "author": "Persisted Author",
        }

        meta_repo = MagicMock()
        service = MetadataService(
            image_preprocessor=preprocessor,
            ocr_service=ocr_service,
            text_normalizer=text_normalizer,
            book_lookup=book_lookup,
            candidate_ranker=MagicMock(),
            confidence_service=confidence_svc,
            metadata_repo=meta_repo,
            text_extractor=text_extractor,
        )

        result, _ = service.identify_book(
            "/fake/image.jpg",
            drive_file_id="drive_persist",
            job_id="job_persist",
        )

        assert result is not None
        # upsert_metadata should have been called
        meta_repo.upsert_metadata.assert_called_once()
        persisted = meta_repo.upsert_metadata.call_args[0][0]
        assert persisted.drive_file_id == "drive_persist"
        assert persisted.job_id == "job_persist"
        assert persisted.status == "identified"
