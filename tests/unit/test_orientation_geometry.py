"""Regression coverage for the geometry orientation detector.

The detector reads ``cv2.HoughLinesP`` output, whose shape changed between
OpenCV releases: older versions return ``(N, 1, 4)``, the installed version
returns ``(N, 4)``. Unpacking ``line[0]`` therefore raised
``TypeError: cannot unpack non-iterable numpy.int32``, the broad handler logged
"Orientation detection failed" and every photo silently fell back to
"no rotation".

These tests assert the detector actually executes. The success path returns
``confidence == 0.5``; the exception path returns ``confidence == 0.0``, so a
regression that resurrects a swallowed exception fails here instead of merely
being logged.

Synthetic line images are used only to pin the heuristic's decisions; the
real-photo behaviour is asserted on the genuine fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import PIL.Image
import PIL.ImageDraw
import pytest

from services import image_preprocessor as image_preprocessor_module
from services.image_preprocessor import ImagePreprocessor
from tests.conftest import REAL_FIXTURE_DIR, require_real_photo_fixtures

require_real_photo_fixtures()

FIXTURE_DIR = REAL_FIXTURE_DIR
MANIFEST = json.loads((FIXTURE_DIR / "metadata.json").read_text(encoding="utf-8"))
BY_ROLE = {entry["role"]: entry for entry in MANIFEST["fixtures"]}

VERTICAL_SEGMENTS = np.array(
    [[[100, 10, 100, 210]], [[180, 40, 180, 260]], [[60, 30, 60, 240]]],
    dtype=np.int32,
)
HORIZONTAL_SEGMENTS = np.array(
    [[[10, 100, 210, 100]], [[40, 180, 260, 180]], [[30, 60, 240, 60]]],
    dtype=np.int32,
)


def _horizontal_lines_image() -> PIL.Image.Image:
    """Synthetic cover: dark horizontal lines on a light background."""
    img = PIL.Image.new("RGB", (300, 450), (235, 235, 235))
    draw = PIL.ImageDraw.Draw(img)
    for y in range(40, 420, 40):
        draw.rectangle([30, y, 270, y + 16], fill=(20, 20, 20))
    return img


def _vertical_lines_image() -> PIL.Image.Image:
    """Synthetic sideways cover: the same content rotated 90 degrees."""
    return _horizontal_lines_image().rotate(90, expand=True)


def _as_shape(lines: np.ndarray, legacy: bool) -> np.ndarray:
    return lines.reshape(-1, 1, 4) if legacy else lines.reshape(-1, 4)


def _patch_hough(monkeypatch, value) -> None:
    monkeypatch.setattr(
        image_preprocessor_module.cv2, "HoughLinesP", lambda *args, **kwargs: value
    )


class TestDetectorExecutes:
    def test_horizontal_lines_are_left_upright(self) -> None:
        orientation = ImagePreprocessor().detect_orientation(_horizontal_lines_image())

        assert orientation.was_corrected is False
        assert orientation.angle == 0
        assert orientation.confidence == 0.5, "success path, not the exception path"

    def test_vertical_lines_are_reported_as_sideways(self) -> None:
        orientation = ImagePreprocessor().detect_orientation(_vertical_lines_image())

        assert orientation.was_corrected is True
        assert orientation.angle == 90
        assert orientation.confidence == 0.7


class TestHoughOutputShapes:
    def test_installed_opencv_shape_is_handled(self, monkeypatch) -> None:
        _patch_hough(monkeypatch, _as_shape(VERTICAL_SEGMENTS, legacy=False))
        orientation = ImagePreprocessor().detect_orientation(_horizontal_lines_image())

        assert (orientation.angle, orientation.confidence) == (90, 0.7)

    def test_legacy_shape_is_handled(self, monkeypatch) -> None:
        _patch_hough(monkeypatch, _as_shape(VERTICAL_SEGMENTS, legacy=True))
        orientation = ImagePreprocessor().detect_orientation(_horizontal_lines_image())

        assert (orientation.angle, orientation.confidence) == (90, 0.7)

    def test_horizontal_segments_never_request_rotation(self, monkeypatch) -> None:
        _patch_hough(monkeypatch, _as_shape(HORIZONTAL_SEGMENTS, legacy=False))
        orientation = ImagePreprocessor().detect_orientation(_vertical_lines_image())

        assert orientation.was_corrected is False
        assert orientation.confidence == 0.5

    @pytest.mark.parametrize(
        "value",
        [
            None,
            np.empty((0, 1, 4), dtype=np.int32),
            np.empty((0, 4), dtype=np.int32),
            np.zeros((3, 2), dtype=np.int32),
            [[1, 2], [3, 4]],
        ],
        ids=["none", "empty-legacy", "empty-new", "bad-shape", "nested-list"],
    )
    def test_degenerate_output_is_safe(self, monkeypatch, value) -> None:
        _patch_hough(monkeypatch, value)
        orientation = ImagePreprocessor().detect_orientation(_horizontal_lines_image())

        assert orientation.was_corrected is False
        assert orientation.angle == 0
        assert orientation.confidence == 0.5, "safe fallback, not a swallowed error"


class TestRealPhotographs:
    def test_upright_real_photo_runs_the_detector(self) -> None:
        path = FIXTURE_DIR / BY_ROLE["upright"]["file"]
        with PIL.Image.open(path) as img:
            orientation = ImagePreprocessor().detect_orientation(img)

        assert orientation.was_corrected is False
        assert orientation.angle == 0
        assert orientation.confidence == 0.5, "detector executed on real pixels"

    def test_upside_down_real_photo_is_not_misidentified(self) -> None:
        """The line heuristic cannot see 180 degrees; the OCR fallback owns it."""
        path = FIXTURE_DIR / BY_ROLE["rot180"]["file"]
        with PIL.Image.open(path) as img:
            orientation = ImagePreprocessor().detect_orientation(img)

        assert orientation.was_corrected is False
        assert orientation.angle == 0
        assert orientation.confidence == 0.5
