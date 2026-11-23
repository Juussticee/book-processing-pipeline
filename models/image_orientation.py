"""Orientation detection result model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ImageOrientation:
    """Result of orientation detection for an image.

    Fields:
        angle: The rotation angle in degrees (0, 90, 180, or 270).
        confidence: Score in [0.0, 1.0] indicating confidence in the detection.
        was_corrected: Whether the image was rotated from its original orientation.
    """

    angle: int = 0
    confidence: float = 0.0
    was_corrected: bool = False
