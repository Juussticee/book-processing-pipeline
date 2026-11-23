"""Content identity: cryptographic and perceptual fingerprints, same-book logic.

Two separate questions are answered here:

1. *Is this the same image?* — SHA-256 catches byte-identical uploads;
   a 64-bit perceptual (difference) hash catches resized, recompressed,
   brightened or slightly cropped copies of the same photograph.
2. *Is this the same book?* — ISBN equality, or a strong title+author match,
   decides whether two different photographs describe one book.

Both are conservative: uncertain evidence never causes a photo to be
dropped. Callers route uncertain results to manual review instead.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

from PIL import Image

# Thresholds are calibrated in tests/unit/test_content_identity.py against real
# transforms of the same image (resize, recompress, brightness, crop, rotation)
# and against different books. The dHash alone is not sufficient: flat covers
# can collide at distance 2-4, so a brightness-invariant colour signature and
# the aspect ratio must agree before a photo is treated as a duplicate.
NEAR_DUPLICATE_MAX_DISTANCE = 3  # high confidence: skip OCR
POSSIBLE_DUPLICATE_MAX_DISTANCE = 9  # record for review, still process
NEAR_DUPLICATE_MAX_COLOR_DISTANCE = 0.04  # chromaticity mean abs difference
POSSIBLE_DUPLICATE_MAX_COLOR_DISTANCE = 0.12
NEAR_DUPLICATE_MAX_ASPECT_DELTA = 0.06
POSSIBLE_DUPLICATE_MAX_ASPECT_DELTA = 0.12

DUPLICATE_EXACT = "exact"
DUPLICATE_NEAR = "near"
DUPLICATE_POSSIBLE_NEAR = "possible_near"
DUPLICATE_SAME_BOOK = "same_book"
DUPLICATE_POSSIBLE_SAME_BOOK = "possible_same_book"

HASH_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: str) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def perceptual_hash(path: str, hash_size: int = 8) -> str:
    """Return a ``hash_size**2``-bit difference hash as a hex string.

    The image is reduced to a 9x8 grayscale thumbnail; each bit records
    whether a pixel is brighter than its right neighbour. This is stable
    under resizing, JPEG recompression and modest brightness changes.
    """
    with Image.open(path) as img:
        small = img.convert("L").resize(
            (hash_size + 1, hash_size), Image.Resampling.LANCZOS
        )
        # tobytes() is row-major: row 0 left->right, then row 1, ...
        pixels = small.tobytes()

    bits = 0
    index = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            if left > right:
                bits |= 1 << index
            index += 1
    width = (hash_size * hash_size) // 4
    return f"{bits:0{width}x}"


def color_signature(path: str, grid: int = 3) -> Tuple[float, ...]:
    """Brightness-invariant 3x3 chromaticity signature of an image.

    Each cell's RGB is divided by its own luminance, so exposure and JPEG
    brightness changes do not move the signature while genuinely different
    cover colours do.
    """
    with Image.open(path) as img:
        small = img.convert("RGB").resize((grid, grid), Image.Resampling.LANCZOS)
        raw = small.tobytes()
    signature: list[float] = []
    for index in range(0, len(raw), 3):
        red, green, blue = (channel / 255.0 for channel in raw[index : index + 3])
        luminance = 0.299 * red + 0.587 * green + 0.114 * blue
        if luminance < 1e-3:
            signature.extend((0.0, 0.0, 0.0))
            continue
        signature.extend((red / luminance, green / luminance, blue / luminance))
    return tuple(signature)


def color_distance(sig_a: Sequence[float], sig_b: Sequence[float]) -> float:
    """Mean absolute chromaticity difference in the range 0..~1.5."""
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return 10**6
    return sum(abs(a - b) for a, b in zip(sig_a, sig_b)) / len(sig_a)


def aspect_ratio(path: str) -> float:
    """Width/height of an image, or 0.0 when it cannot be read."""
    with Image.open(path) as img:
        width, height = img.size
    return (width / height) if height else 0.0


def aspect_delta(ratio_a: float, ratio_b: float) -> float:
    """Relative aspect-ratio difference; 1.0 when either looks unreadable."""
    if ratio_a <= 0 or ratio_b <= 0:
        return 1.0
    return abs(ratio_a - ratio_b) / max(ratio_a, ratio_b)


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Hamming distance between two equal-length hex hashes."""
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return 10**6
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


@dataclass(frozen=True)
class ImageFingerprint:
    """Fingerprint of a normalized processing image."""

    content_hash: str
    perceptual_hash: str
    color_signature: Tuple[float, ...] = ()
    aspect_ratio: float = 0.0

    @classmethod
    def from_path(cls, path: str) -> "ImageFingerprint":
        return cls(
            content_hash=sha256_file(path),
            perceptual_hash=perceptual_hash(path),
            color_signature=color_signature(path),
            aspect_ratio=aspect_ratio(path),
        )

    @classmethod
    def from_images(
        cls, normalized_path: str, original_path: str
    ) -> "ImageFingerprint":
        """Fingerprint a submission.

        The content hash is taken over the *normalized* image (EXIF applied,
        deterministic), while the perceptual and colour hashes are taken over
        the *original* image: normalization converts to grayscale, which would
        destroy the colour signal that separates two different covers with a
        similar layout.
        """
        perceptual_source = normalized_path
        if original_path and os.path.exists(original_path):
            perceptual_source = original_path
        return cls(
            content_hash=sha256_file(normalized_path),
            perceptual_hash=perceptual_hash(perceptual_source),
            color_signature=color_signature(perceptual_source),
            aspect_ratio=aspect_ratio(perceptual_source),
        )


def classify_fingerprint(
    new: ImageFingerprint,
    existing_perceptual: str,
    existing_color: str = "",
    existing_aspect: float = 0.0,
) -> str:
    """Classify an image against one stored fingerprint.

    Three signals must agree before a photo is called a duplicate:
    perceptual distance, chromaticity distance and aspect ratio.
    """
    distance = hamming_distance(new.perceptual_hash, existing_perceptual)
    if distance > POSSIBLE_DUPLICATE_MAX_DISTANCE:
        return "not_duplicate"
    existing_signature = _decode_color(existing_color)
    chroma = color_distance(new.color_signature, existing_signature)
    aspect = aspect_delta(new.aspect_ratio, existing_aspect)
    if distance <= NEAR_DUPLICATE_MAX_DISTANCE:
        if (
            chroma <= NEAR_DUPLICATE_MAX_COLOR_DISTANCE
            and aspect <= NEAR_DUPLICATE_MAX_ASPECT_DELTA
        ):
            return DUPLICATE_NEAR
        return "not_duplicate"
    if (
        chroma <= POSSIBLE_DUPLICATE_MAX_COLOR_DISTANCE
        and aspect <= POSSIBLE_DUPLICATE_MAX_ASPECT_DELTA
    ):
        return DUPLICATE_POSSIBLE_NEAR
    return "not_duplicate"


def encode_color(signature: Sequence[float]) -> str:
    """Serialise a colour signature for storage."""
    return ",".join(f"{value:.4f}" for value in signature)


def _decode_color(encoded: str) -> Tuple[float, ...]:
    if not encoded:
        return ()
    try:
        return tuple(float(part) for part in encoded.split(","))
    except ValueError:
        return ()


# ---------------------------------------------------------------------------
# Same-book identity
# ---------------------------------------------------------------------------


# Typographic characters that NFKD does not decompose but that must compare
# equal to their ASCII spelling ("cœur" vs "coeur", "Müller" vs "Mueller").
_LIGATURES = {
    "\u0153": "oe",  # œ
    "\u0152": "OE",  # Œ
    "\u00e6": "ae",  # æ
    "\u00c6": "AE",  # Æ
    "\u00df": "ss",  # ß
    "\u00f8": "o",  # ø
    "\u00d8": "O",  # Ø
    "\u0142": "l",  # ł
    "\u0141": "L",  # Ł
    "\u0111": "d",  # đ
    "\u0110": "D",  # Đ
    "\u00f0": "d",  # ð
    "\u00fe": "th",  # þ
    "\u2019": "'",  # right single quote
    "\u2018": "'",
    "\u2013": "-",
    "\u2014": "-",
}


def _strip_accents(value: str) -> str:
    expanded = "".join(_LIGATURES.get(ch, ch) for ch in value)
    decomposed = unicodedata.normalize("NFKD", expanded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(value: Optional[str]) -> str:
    """Lowercase, accent- and punctuation-free form used for comparisons."""
    if not value:
        return ""
    text = _strip_accents(str(value)).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_isbn(value: Optional[str]) -> str:
    """Return an ISBN-13 string (converting ISBN-10), or ``""``."""
    if not value:
        return ""
    digits = re.sub(r"[^0-9Xx]", "", str(value)).upper()
    if len(digits) == 13 and digits.isdigit():
        return digits
    if len(digits) == 10:
        base = "978" + digits[:9]
        total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(base))
        check = (10 - total % 10) % 10
        return f"{base}{check}"
    return ""


def _author_key(authors: Sequence[str]) -> str:
    """Surname-ish key for the first author (last token, normalized)."""
    if not authors:
        return ""
    tokens = normalize_text(authors[0]).split()
    return tokens[-1] if tokens else ""


def _full_author_key(authors: Sequence[str]) -> str:
    """Full normalized author string (all names, all authors)."""
    names = [normalize_text(author) for author in authors if normalize_text(author)]
    return "|".join(names)


def is_valid_isbn13(value: Optional[str]) -> bool:
    """True when ``value`` is a checksum-valid ISBN-13.

    ``normalize_isbn`` only formats; OCR can turn a valid ISBN into a
    different but syntactically valid 13-digit string. The checksum is what
    makes ISBN equality a safe automatic duplicate signal.
    """
    isbn = normalize_isbn(value)
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    total = sum((1 if index % 2 == 0 else 3) * int(ch) for index, ch in enumerate(isbn))
    return total % 10 == 0


def title_similarity(a: str, b: str) -> float:
    """Similarity ratio in [0, 1] over normalized titles."""
    from difflib import SequenceMatcher

    left, right = normalize_text(a), normalize_text(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


@dataclass(frozen=True)
class SameBookVerdict:
    """Outcome of comparing a new identification with an existing row."""

    kind: str  # "certain" | "possible" | "none"
    matched_drive_file_id: Optional[str] = None
    reason: str = ""

    @property
    def is_certain(self) -> bool:
        return self.kind == "certain"

    @property
    def is_possible(self) -> bool:
        return self.kind == "possible"

    @property
    def is_match(self) -> bool:
        return self.kind in ("certain", "possible")


NO_MATCH = SameBookVerdict(kind="none")

# A fuzzy title match this strong, with the same author surname, is treated
# as a possible duplicate (flagged) but never auto-merged.
POSSIBLE_TITLE_SIMILARITY = 0.92


def _tokens_subset(a: str, b: str) -> bool:
    """True when one normalized title's word set is contained in the other.

    Catches subtitle/series suffixes ("le coeur vert" vs "le coeur vert roman")
    while requiring at least two shared words so single generic words do not
    match unrelated books.
    """
    left, right = set(a.split()), set(b.split())
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    return len(smaller) >= 2 and smaller <= larger


def compare_books(
    new_isbn: Optional[str],
    new_title: str,
    new_authors: Iterable[str],
    existing_isbn: Optional[str],
    existing_title: str,
    existing_authors: Iterable[str],
) -> SameBookVerdict:
    """Compare one identification against one existing row.

    Only a matching checksum-valid ISBN-13 auto-merges (``certain``).
    Everything else that looks alike is a review flag (``possible``): the
    record is still created, so a wrong merge can never lose a book.

    Rules, in order:

    1. Two different valid ISBN-13s => different editions/books, no match.
    2. Equal, checksum-valid ISBN-13 => certain duplicate.
    3. Equal but checksum-invalid ISBN-13 => possible (never auto-merged).
    4. One side without ISBN: exact title + full author match => possible.
    5. One side without ISBN: exact title + author surname only => possible.
    6. Very similar title (>= 0.92) + compatible author => possible.
    """
    new_authors = list(new_authors)
    existing_authors = list(existing_authors)

    new_isbn_norm = normalize_isbn(new_isbn)
    existing_isbn_norm = normalize_isbn(existing_isbn)

    if new_isbn_norm and existing_isbn_norm:
        if new_isbn_norm != existing_isbn_norm:
            return SameBookVerdict("none", reason="different_isbn")
        if is_valid_isbn13(new_isbn_norm) and is_valid_isbn13(existing_isbn_norm):
            return SameBookVerdict("certain", reason="same_isbn")
        return SameBookVerdict("possible", reason="same_isbn_unverified")

    new_title_norm = normalize_text(new_title)
    existing_title_norm = normalize_text(existing_title)
    if not new_title_norm or not existing_title_norm:
        return NO_MATCH

    new_author = _author_key(new_authors)
    existing_author = _author_key(existing_authors)
    if not new_author or not existing_author or new_author != existing_author:
        return NO_MATCH

    new_full_author = _full_author_key(new_authors)
    existing_full_author = _full_author_key(existing_authors)

    if new_title_norm == existing_title_norm:
        if new_full_author == existing_full_author:
            return SameBookVerdict("possible", reason="same_title_full_author")
        return SameBookVerdict("possible", reason="same_title_surname")

    similarity = title_similarity(new_title, existing_title)
    if similarity >= POSSIBLE_TITLE_SIMILARITY:
        return SameBookVerdict("possible", reason=f"similar_title={similarity:.2f}")
    if _tokens_subset(new_title_norm, existing_title_norm):
        return SameBookVerdict("possible", reason="title_suffix_or_subtitle")

    return NO_MATCH


def best_same_book_match(
    new_isbn: Optional[str],
    new_title: str,
    new_authors: Iterable[str],
    candidates: Sequence[Tuple[str, Optional[str], str, Sequence[str]]],
) -> SameBookVerdict:
    """Pick the strongest verdict across existing rows.

    ``candidates`` is a sequence of
    ``(drive_file_id, isbn, title, authors)`` tuples.
    """
    best = NO_MATCH
    for drive_file_id, isbn, title, authors in candidates:
        verdict = compare_books(
            new_isbn,
            new_title,
            new_authors,
            isbn,
            title,
            authors,
        )
        if verdict.is_certain:
            return SameBookVerdict(
                "certain",
                matched_drive_file_id=drive_file_id,
                reason=verdict.reason,
            )
        if verdict.is_possible and not best.is_possible:
            best = SameBookVerdict(
                "possible",
                matched_drive_file_id=drive_file_id,
                reason=verdict.reason,
            )
    return best


__all__ = [
    "DUPLICATE_EXACT",
    "DUPLICATE_NEAR",
    "DUPLICATE_POSSIBLE_NEAR",
    "DUPLICATE_POSSIBLE_SAME_BOOK",
    "DUPLICATE_SAME_BOOK",
    "NEAR_DUPLICATE_MAX_DISTANCE",
    "POSSIBLE_DUPLICATE_MAX_DISTANCE",
    "ImageFingerprint",
    "SameBookVerdict",
    "best_same_book_match",
    "classify_fingerprint",
    "color_distance",
    "color_signature",
    "compare_books",
    "encode_color",
    "aspect_delta",
    "aspect_ratio",
    "hamming_distance",
    "is_valid_isbn13",
    "normalize_isbn",
    "normalize_text",
    "perceptual_hash",
    "sha256_file",
    "title_similarity",
]
