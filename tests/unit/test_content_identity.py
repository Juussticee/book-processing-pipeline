"""Content identity: hashes, duplicate thresholds and same-book rules.

The image thresholds are calibrated here against real transforms of one
synthetic cover (resize, recompress, brightness, crop) and against
different covers, so the constants in ``services.content_identity`` are
justified by measurements rather than guesses.
"""

from __future__ import annotations

import os

import PIL.Image
import PIL.ImageDraw
import PIL.ImageEnhance
import pytest

from services import content_identity as ci


def _cover(color, lines) -> PIL.Image.Image:
    img = PIL.Image.new("RGB", (600, 900), color)
    draw = PIL.ImageDraw.Draw(img)
    y = 80
    for index, _text in enumerate(lines):
        draw.rectangle(
            [50, y, 550, y + 60],
            fill=(255, 255, 255) if index % 2 else (30, 30, 30),
        )
        y += 90
    return img


@pytest.fixture()
def covers(tmp_path):
    base = _cover((120, 30, 40), ["LE COEUR VERT", "JEAN-LOUIS KUFFER", "ROMAN"])
    other = _cover((20, 60, 140), ["DUNE", "FRANK HERBERT", "SCIENCE FICTION"])
    same_title = _cover((40, 120, 60), ["LE COEUR VERT", "OTHER AUTHOR", "ESSAI"])

    def save(img, name, **kwargs):
        path = os.path.join(tmp_path, name)
        img.save(path, **kwargs)
        return path

    return {
        "base": save(base, "base.jpg", quality=92),
        "resized": save(base.resize((300, 450)), "resized.jpg", quality=92),
        "recompressed": save(base, "recompressed.jpg", quality=35),
        "brighter": save(
            PIL.ImageEnhance.Brightness(base).enhance(1.2), "bright.jpg", quality=92
        ),
        "darker": save(
            PIL.ImageEnhance.Brightness(base).enhance(0.8), "dark.jpg", quality=92
        ),
        "cropped": save(
            base.crop((0, 30, 600, 870)).resize((600, 900)), "crop.jpg", quality=92
        ),
        "other_book": save(other, "other.jpg", quality=92),
        "same_title_other_author": save(same_title, "same_title.jpg", quality=92),
    }


class TestHashes:
    def test_sha256_is_stable_and_content_based(self, covers) -> None:
        first = ci.sha256_file(covers["base"])
        assert first == ci.sha256_file(covers["base"])
        assert first != ci.sha256_file(covers["resized"])

    def test_perceptual_hash_is_64_bits(self, covers) -> None:
        value = ci.perceptual_hash(covers["base"])
        assert len(value) == 16
        int(value, 16)  # valid hex

    def test_hamming_distance_of_identical_hashes_is_zero(self, covers) -> None:
        value = ci.perceptual_hash(covers["base"])
        assert ci.hamming_distance(value, value) == 0

    def test_hamming_distance_handles_missing_hashes(self) -> None:
        assert ci.hamming_distance("", "abcd") > 1000


class TestDuplicateClassification:
    @pytest.mark.parametrize(
        "variant", ["resized", "recompressed", "brighter", "darker", "cropped"]
    )
    def test_same_photo_transforms_are_near_duplicates(self, covers, variant) -> None:
        base = ci.ImageFingerprint.from_path(covers["base"])
        other = ci.ImageFingerprint.from_path(covers[variant])
        verdict = ci.classify_fingerprint(
            base,
            other.perceptual_hash,
            ci.encode_color(other.color_signature),
            other.aspect_ratio,
        )
        distance = ci.hamming_distance(base.perceptual_hash, other.perceptual_hash)
        assert (
            verdict == ci.DUPLICATE_NEAR
        ), f"{variant} should be a near duplicate (distance={distance})"

    @pytest.mark.parametrize("variant", ["other_book", "same_title_other_author"])
    def test_different_covers_are_not_duplicates(self, covers, variant) -> None:
        base = ci.ImageFingerprint.from_path(covers["base"])
        other = ci.ImageFingerprint.from_path(covers[variant])
        verdict = ci.classify_fingerprint(
            base,
            other.perceptual_hash,
            ci.encode_color(other.color_signature),
            other.aspect_ratio,
        )
        assert verdict == "not_duplicate"

    def test_distance_inside_possible_band_is_flagged_not_skipped(self) -> None:
        base = ci.ImageFingerprint(
            content_hash="a",
            perceptual_hash="0000000000000000",
            color_signature=(1.0,) * 27,
            aspect_ratio=1.0,
        )
        # 5 bits differ -> possible band (4..9)
        verdict = ci.classify_fingerprint(
            base,
            "000000000000001f",
            ci.encode_color((1.0,) * 27),
            1.0,
        )
        assert verdict == ci.DUPLICATE_POSSIBLE_NEAR

    def test_colour_mismatch_blocks_a_flat_cover_collision(self) -> None:
        base = ci.ImageFingerprint(
            content_hash="a",
            perceptual_hash="0000000000000000",
            color_signature=(1.0, 0.2, 0.2) * 9,
        )
        verdict = ci.classify_fingerprint(
            base,
            "0000000000000000",
            ci.encode_color((0.2, 0.2, 1.0) * 9),
            1.0,
        )
        assert verdict == "not_duplicate"

    def test_aspect_ratio_mismatch_blocks_a_match(self) -> None:
        base = ci.ImageFingerprint(
            content_hash="a",
            perceptual_hash="0000000000000000",
            color_signature=(1.0,) * 27,
            aspect_ratio=0.66,
        )
        verdict = ci.classify_fingerprint(
            base, "0000000000000000", ci.encode_color((1.0,) * 27), 1.5
        )
        assert verdict == "not_duplicate"


class TestIsbnNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "978-0-441-01359-3",
            "9780441013593",
            "978 0 441 01359 3",
            "0-441-01359-2",
        ],
    )
    def test_formatting_differences_normalise_to_one_isbn13(self, raw: str) -> None:
        assert ci.normalize_isbn(raw) == "9780441013593"

    def test_invalid_isbn_returns_empty(self) -> None:
        assert ci.normalize_isbn("not-an-isbn") == ""
        assert ci.normalize_isbn(None) == ""

    @pytest.mark.parametrize(
        "value",
        ["9780441013593", "978-0-441-01359-3", "0-441-01359-X", "0-441-01359-2"],
    )
    def test_valid_isbn13_is_recognised(self, value: str) -> None:
        assert ci.is_valid_isbn13(value)

    @pytest.mark.parametrize("value", ["9780441013594", "1234567890123", None, ""])
    def test_bad_checksum_is_not_a_valid_isbn13(self, value) -> None:
        assert not ci.is_valid_isbn13(value)


class TestSameBook:
    def test_same_isbn_with_formatting_differences_is_certain(self) -> None:
        verdict = ci.compare_books(
            "978-0-441-01359-3",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            "9780441013593",
            "Le Coeur Vert",
            ["J.-L. Kuffer"],
        )
        assert verdict.is_certain
        assert verdict.reason == "same_isbn"

    def test_equal_isbn_with_bad_checksum_is_not_auto_merged(self) -> None:
        verdict = ci.compare_books(
            "9780441013594",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            "9780441013594",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
        )
        assert verdict.is_possible
        assert not verdict.is_certain
        assert verdict.reason == "same_isbn_unverified"

    def test_same_title_and_full_author_is_review_only(self) -> None:
        verdict = ci.compare_books(
            None,
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            None,
            "Le Coeur Vert",
            ["Jean-Louis Kuffer"],
        )
        assert verdict.is_possible
        assert not verdict.is_certain
        assert verdict.reason == "same_title_full_author"

    def test_same_title_and_author_surname_only_is_review_only(self) -> None:
        verdict = ci.compare_books(
            None,
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            None,
            "Le cœur vert",
            ["J. Kuffer"],
        )
        assert verdict.is_possible
        assert not verdict.is_certain
        assert verdict.reason == "same_title_surname"

    def test_exact_title_and_author_with_one_missing_isbn_is_review_only(self) -> None:
        verdict = ci.compare_books(
            None,
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            "9780441013593",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
        )
        assert verdict.is_possible
        assert not verdict.is_certain

    def test_minor_title_spelling_difference_is_review_only(self) -> None:
        verdict = ci.compare_books(
            None,
            "Le coeur ver",
            ["Jean-Louis Kuffer"],
            None,
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
        )
        assert verdict.is_possible
        assert not verdict.is_certain

    def test_diff_isbn_wins_over_identical_title_and_author(self) -> None:
        verdict = ci.compare_books(
            "9780441013593",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            "9782070360024",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
        )
        assert verdict.kind == "none"
        assert verdict.reason == "different_isbn"

    def test_same_author_different_title_does_not_match(self) -> None:
        verdict = ci.compare_books(
            None,
            "Un autre livre",
            ["Jean-Louis Kuffer"],
            None,
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
        )
        assert not verdict.is_match

    def test_different_isbn_keeps_editions_separate(self) -> None:
        verdict = ci.compare_books(
            "9780441013593",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            "9782070360024",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
        )
        assert not verdict.is_match

    def test_same_title_different_author_is_not_a_match(self) -> None:
        verdict = ci.compare_books(
            None, "Le cœur vert", ["Jean-Louis Kuffer"], None, "Le cœur vert", ["Other"]
        )
        assert not verdict.is_match

    def test_similar_title_same_author_is_possible(self) -> None:
        verdict = ci.compare_books(
            None,
            "Le coeur vert",
            ["Jean-Louis Kuffer"],
            None,
            "Le coeur vert roman",
            ["Jean-Louis Kuffer"],
        )
        assert verdict.is_possible

    def test_unrelated_books_do_not_match(self) -> None:
        verdict = ci.compare_books(
            None, "Dune", ["Frank Herbert"], None, "Le cœur vert", ["Jean-Louis Kuffer"]
        )
        assert not verdict.is_match

    def test_best_match_prefers_certain_over_possible(self) -> None:
        verdict = ci.best_same_book_match(
            "9780441013593",
            "Le cœur vert",
            ["Jean-Louis Kuffer"],
            [
                ("f1", None, "Le coeur vert - illustre", ["Jean-Louis Kuffer"]),
                ("f2", "9780441013593", "Le cœur vert", ["Jean-Louis Kuffer"]),
            ],
        )
        assert verdict.is_certain
        assert verdict.matched_drive_file_id == "f2"
