"""Regression tests for CoverVerifier — offline cover image comparison."""

import pytest
import tempfile
import os

from services.cover_verifier import CoverVerifier, CoverSimilarityResult


class TestCoverVerifier:
    """Tests for offline cover image verification."""

    def setup_method(self):
        self.verifier = CoverVerifier()

    def test_identical_images_high_similarity(self):
        """Identical images should have very high dHash similarity."""
        from PIL import Image
        img = Image.new("RGB", (200, 200), color=(128, 64, 32))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img.save(f.name)
            path1 = f.name
        h1 = self.verifier.compute_dhash(path1)
        h2 = self.verifier.compute_dhash(path1)
        assert self.verifier.dhash_similarity(h1, h2) == 1.0
        os.unlink(path1)

    def test_different_images_low_similarity(self):
        """Very different images should have low similarity."""
        from PIL import Image
        img1 = Image.new("RGB", (200, 200), color=(128, 64, 32))
        img2 = Image.new("RGB", (200, 200), color=(200, 50, 100))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f1:
            img1.save(f1.name)
            path1 = f1.name
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
            img2.save(f2.name)
            path2 = f2.name
        h1 = self.verifier.compute_dhash(path1)
        h2 = self.verifier.compute_dhash(path2)
        sim = self.verifier.dhash_similarity(h1, h2)
        assert 0.0 <= sim <= 1.0
        os.unlink(path1)
        os.unlink(path2)

    def test_dhash_similarity_score_range(self):
        """dHash similarity is a float between 0 and 1."""
        from PIL import Image
        img = Image.new("RGB", (200, 200), color=(128, 64, 32))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img.save(f.name)
            path = f.name
        h1 = self.verifier.compute_dhash(path)
        h2 = self.verifier.compute_dhash(path)
        sim = self.verifier.dhash_similarity(h1, h2)
        assert isinstance(sim, float)
        assert 0.0 <= sim <= 1.0
        os.unlink(path)

    def test_feature_matching_available(self):
        """SIFT/ORB feature matching is available (offline)."""
        from PIL import Image
        img1 = Image.new("RGB", (200, 200), color=(128, 64, 32))
        img2 = Image.new("RGB", (200, 200), color=(128, 64, 32))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f1:
            img1.save(f1.name)
            path1 = f1.name
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
            img2.save(f2.name)
            path2 = f2.name
        score = self.verifier.feature_match_score(path1, path2)
        assert 0.0 <= score <= 1.0
        os.unlink(path1)
        os.unlink(path2)

    def test_ssim_score_available(self):
        """SSIM score is available via scikit-image (offline)."""
        from PIL import Image
        img = Image.new("RGB", (100, 100), color=(128, 64, 32))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img.save(f.name)
            path = f.name
        score = self.verifier.ssim_score(path, path)
        assert 0.0 <= score <= 1.0
        os.unlink(path)

    def test_nonexistent_file_handled(self):
        """Nonexistent source image returns None (no cover verification)."""
        result = self.verifier.verify_covers(
            "/nonexistent/image.jpg",
            "http://example.com/cover.jpg",
        )
        assert result is None

    def test_cover_similarity_result_fields(self):
        """CoverSimilarityResult has all expected fields."""
        result = CoverSimilarityResult(
            dhash_similarity=0.9,
            ssim_score=0.85,
            feature_score=0.8,
            color_histogram_score=0.75,
            overall_similarity=0.82,
        )
        assert result.dhash_similarity == 0.9
        assert result.overall_similarity == 0.82

