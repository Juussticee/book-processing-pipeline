"""EXIF orientation 1-8 and geometry-driven rotation.

Real JPEGs are written with each EXIF orientation tag; the normalization
copy must match the equivalent plain transform, must be idempotent, and
must never modify the source file.
"""

from __future__ import annotations

import PIL.Image
import PIL.ImageDraw
import PIL.ImageOps
import pytest

from models.image_orientation import ImageOrientation
from models.ocr_result import OCRResult
from services.image_preprocessor import ImagePreprocessor
from services.metadata_service import MetadataService

# Pillow's EXIF orientation -> transpose operation mapping (tags 2-8).
EXIF_TRANSPOSE = {
    1: None,
    2: PIL.Image.Transpose.FLIP_LEFT_RIGHT,
    3: PIL.Image.Transpose.ROTATE_180,
    4: PIL.Image.Transpose.FLIP_TOP_BOTTOM,
    5: PIL.Image.Transpose.TRANSPOSE,
    6: PIL.Image.Transpose.ROTATE_270,
    7: PIL.Image.Transpose.TRANSVERSE,
    8: PIL.Image.Transpose.ROTATE_90,
}


def _marker_image() -> PIL.Image.Image:
    """Asymmetric image so every orientation produces a distinct result."""
    img = PIL.Image.new("RGB", (60, 40), (40, 40, 40))
    draw = PIL.ImageDraw.Draw(img)
    draw.rectangle([0, 0, 14, 14], fill=(255, 255, 255))
    draw.rectangle([45, 25, 58, 38], fill=(200, 30, 30))
    draw.text((20, 18), "TITLE", fill=(220, 220, 220))
    return img


def _write_with_orientation(path: str, orientation: int) -> None:
    exif = PIL.Image.Exif()
    exif[0x0112] = orientation
    _marker_image().save(path, format="JPEG", quality=95, exif=exif)


def _luma(path: str) -> dict:
    """Size plus two position fingerprints used to verify orientation.

    JPEG re-encoding makes pixel-exact comparison unreliable, so orientation
    is checked positionally: the brightest pixel and the centroid of the
    brightest 1% of pixels must land in the same place.
    """
    with PIL.Image.open(path) as img:
        gray = img.convert("L")
        width, height = gray.size
        data = gray.tobytes()
    brightest_index = max(range(len(data)), key=data.__getitem__)
    threshold = sorted(data)[int(len(data) * 0.99)]
    xs, ys, total = 0, 0, 0
    for index, value in enumerate(data):
        if value >= threshold:
            xs += index % width
            ys += index // width
            total += 1
    return {
        "size": (width, height),
        "brightest": (brightest_index % width, brightest_index // width),
        "centroid": (xs / total, ys / total),
    }


def _assert_same_orientation(actual: str, expected: str) -> None:
    left, right = _luma(actual), _luma(expected)
    assert left["size"] == right["size"]
    assert abs(left["brightest"][0] - right["brightest"][0]) <= 8
    assert abs(left["brightest"][1] - right["brightest"][1]) <= 8
    assert abs(left["centroid"][0] - right["centroid"][0]) <= 6
    assert abs(left["centroid"][1] - right["centroid"][1]) <= 6


class TestExifOrientation:
    @pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_each_exif_orientation_is_applied(self, orientation: int, tmp_path) -> None:
        tagged = str(tmp_path / f"tag{orientation}.jpg")
        _write_with_orientation(tagged, orientation)

        preprocessor = ImagePreprocessor(temp_dir=str(tmp_path))
        result = preprocessor.preprocess(tagged)

        method = EXIF_TRANSPOSE[orientation]
        plain = _marker_image()
        if method is not None:
            plain = plain.transpose(method)
        reference_path = str(tmp_path / f"reference{orientation}.png")
        plain.save(reference_path, format="PNG")
        reference_result = preprocessor.preprocess(reference_path)

        _assert_same_orientation(result, reference_result)

    def test_source_file_is_never_modified(self, tmp_path) -> None:
        tagged = str(tmp_path / "tag6.jpg")
        _write_with_orientation(tagged, 6)
        before = open(tagged, "rb").read()

        ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(tagged)

        assert open(tagged, "rb").read() == before
        with PIL.Image.open(tagged) as img:
            assert img.getexif().get(0x0112) == 6

    @pytest.mark.parametrize("orientation", [2, 3, 5, 6, 7, 8])
    def test_normalization_is_idempotent(self, orientation: int, tmp_path) -> None:
        tagged = str(tmp_path / f"twice{orientation}.jpg")
        _write_with_orientation(tagged, orientation)
        preprocessor = ImagePreprocessor(temp_dir=str(tmp_path))

        first = preprocessor.preprocess(tagged)
        second = preprocessor.preprocess(first)

        assert _luma(first) == _luma(second)

    def test_missing_exif_is_untouched(self, tmp_path) -> None:
        path = str(tmp_path / "plain.jpg")
        _marker_image().save(path, format="JPEG", quality=95)
        preprocessor = ImagePreprocessor(temp_dir=str(tmp_path))

        result = preprocessor.preprocess(path)
        reference = str(tmp_path / "plain_copy.jpg")
        _marker_image().save(reference, format="JPEG", quality=95)
        reference_result = preprocessor.preprocess(reference)

        assert _luma(result)["size"] == _luma(reference_result)["size"]
        assert _luma(result)["centroid"] == _luma(reference_result)["centroid"][:2]

    @pytest.mark.parametrize("orientation", [1, 2, 3, 4])
    def test_non_transposing_tags_keep_dimensions(self, orientation, tmp_path) -> None:
        tagged = str(tmp_path / f"dim{orientation}.jpg")
        _write_with_orientation(tagged, orientation)
        result = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(tagged)
        assert _luma(result)["size"] == (60, 40)

    @pytest.mark.parametrize("orientation", [5, 6, 7, 8])
    def test_transposing_tags_swap_dimensions(self, orientation, tmp_path) -> None:
        tagged = str(tmp_path / f"dim{orientation}.jpg")
        _write_with_orientation(tagged, orientation)
        result = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(tagged)
        assert _luma(result)["size"] == (40, 60)

    @pytest.mark.parametrize(
        "orientation,expected_corner",
        [
            (1, "top-left"),
            (2, "top-right"),
            (3, "bottom-right"),
            (4, "bottom-left"),
            (5, "top-left"),
            (6, "top-right"),
            (7, "bottom-right"),
            (8, "bottom-left"),
        ],
    )
    def test_orientation_marker_lands_in_expected_corner(
        self, orientation: int, expected_corner: str, tmp_path
    ) -> None:
        """The white marker starts top-left in stored pixels; after applying
        the EXIF tag it must sit where the spec says the viewer sees it."""
        tagged = str(tmp_path / f"corner{orientation}.jpg")
        _write_with_orientation(tagged, orientation)
        result = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(tagged)

        with PIL.Image.open(result) as img:
            gray = img.convert("L")
            width, height = gray.size
            data = gray.tobytes()
        brightest = max(range(len(data)), key=data.__getitem__)
        x, y = brightest % width, brightest // width
        horizontal = "left" if x < width / 2 else "right"
        vertical = "top" if y < height / 2 else "bottom"

        assert f"{vertical}-{horizontal}" == expected_corner


class _SizeAwareOcr:
    """Fake OCR: reads text only from portrait images (like a real cover)."""

    def __init__(self) -> None:
        self.calls = 0

    def extract_text(self, path: str) -> OCRResult:
        self.calls += 1
        with PIL.Image.open(path) as img:
            width, height = img.size
        if height > width:
            words = ["LE", "COEUR", "VERT", "JEAN-LOUIS", "KUFFER", "ROMAN"]
            return OCRResult(
                text="LE COEUR VERT\nJEAN-LOUIS KUFFER",
                confidence=0.9,
                words=words,
                language="fr",
                image_path=path,
            )
        return OCRResult(
            text="?? ??",
            confidence=0.4,
            words=["??", "??"],
            language="fr",
            image_path=path,
        )


class TestGeometryOrientation:
    def test_geometry_gate_does_not_rotate_an_upside_down_photo(self, tmp_path) -> None:
        """The aspect-ratio gate cannot tell 180 degrees from upright.

        An upside-down portrait stays portrait, so this quality-independent gate
        leaves it alone. 180 degrees is only recovered by the OCR-quality
        fallback (`_try_orientation_correction`), which tries all four rotations
        when the first pass yields <3 words or confidence <0.30. When OCR still
        returns plausible text at the wrong angle, nothing corrects it.
        """
        portrait = PIL.Image.new("RGB", (300, 450), (200, 200, 200))
        draw = PIL.ImageDraw.Draw(portrait)
        for y in range(40, 420, 40):
            draw.rectangle([30, y, 270, y + 18], fill=(20, 20, 20))
        upside_down = str(tmp_path / "upside.jpg")
        portrait.rotate(180).save(upside_down, "JPEG", quality=95)
        preprocessed = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(upside_down)

        ocr = _SizeAwareOcr()
        service = MetadataService(
            image_preprocessor=ImagePreprocessor(temp_dir=str(tmp_path)),
            ocr_service=ocr,
        )
        initial = ocr.extract_text(preprocessed)
        corrected = service._apply_geometry_orientation_correction(  # noqa: SLF001
            preprocessed, initial
        )

        assert ocr.calls == 1  # no extra OCR pass was needed
        assert corrected is initial

    def test_landscape_page_is_rotated_before_trusting_ocr(self, tmp_path) -> None:
        portrait = PIL.Image.new("RGB", (300, 450), (200, 200, 200))
        draw = PIL.ImageDraw.Draw(portrait)
        for y in range(40, 420, 40):
            draw.rectangle([30, y, 270, y + 18], fill=(20, 20, 20))
        sideways_path = str(tmp_path / "sideways.jpg")
        portrait.rotate(90, expand=True).save(sideways_path, "JPEG", quality=95)
        preprocessed = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(
            sideways_path
        )

        ocr = _SizeAwareOcr()
        preprocessor = ImagePreprocessor(temp_dir=str(tmp_path))
        preprocessor.detect_orientation = lambda _img: ImageOrientation(  # type: ignore[assignment]
            angle=90, confidence=0.7, was_corrected=True
        )
        service = MetadataService(
            image_preprocessor=preprocessor,
            ocr_service=ocr,
        )

        initial = ocr.extract_text(preprocessed)
        assert len(initial.words) == 2

        corrected = service._apply_geometry_orientation_correction(  # noqa: SLF001
            preprocessed, initial
        )

        assert len(corrected.words) == 6
        assert corrected.confidence == 0.9
        assert ocr.calls == 2, "at most one extra OCR pass is allowed"

    def test_portrait_page_costs_no_extra_ocr(self, tmp_path) -> None:
        portrait = PIL.Image.new("RGB", (300, 450), (200, 200, 200))
        portrait.save(str(tmp_path / "upright.jpg"), "JPEG", quality=95)
        preprocessed = ImagePreprocessor(temp_dir=str(tmp_path)).preprocess(
            str(tmp_path / "upright.jpg")
        )

        ocr = _SizeAwareOcr()
        service = MetadataService(
            image_preprocessor=ImagePreprocessor(temp_dir=str(tmp_path)),
            ocr_service=ocr,
        )
        initial = ocr.extract_text(preprocessed)
        corrected = service._apply_geometry_orientation_correction(  # noqa: SLF001
            preprocessed, initial
        )

        assert corrected is initial
        assert ocr.calls == 1

    def test_geometry_failure_does_not_break_processing(self, tmp_path) -> None:
        service = MetadataService(
            image_preprocessor=ImagePreprocessor(temp_dir=str(tmp_path)),
            ocr_service=_SizeAwareOcr(),
        )
        initial = OCRResult(
            text="x", confidence=0.5, words=["x"], language="fr", image_path="missing"
        )
        corrected = service._apply_geometry_orientation_correction(  # noqa: SLF001
            "does-not-exist.jpg", initial
        )
        assert corrected is initial


def test_heic_is_not_decodable_in_this_environment() -> None:
    """Documents the HEIC policy: no decoder is installed, so HEIC is rejected."""
    import importlib.util

    assert importlib.util.find_spec("pillow_heif") is None
