"""Check that file bytes really are a supported photo/video (magic-byte sniffing).

The browser-declared Content-Type is just a string the client chose, so it is
never trusted on its own.
"""

from __future__ import annotations

from app.config import kind_of

# Enough bytes to recognise every supported container.
SNIFF_BYTES = 16

# ISO BMFF boxes a file may start with. Phones mix up HEIC/MP4/MOV brands, so
# any ISO BMFF file is accepted for heic/heif/mp4/mov/m4v alike.
_ISO_BMFF_BOXES = {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}


def detect_format(head: bytes) -> str | None:
    """Return the container family of the first bytes, or None if unknown."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head[4:8] in _ISO_BMFF_BOXES:
        return "isobmff"
    return None


# Which kinds (image/video) each container family may carry.
_FORMAT_KINDS = {
    "jpeg": {"image"},
    "png": {"image"},
    "webp": {"image"},
    "webm": {"video"},
    "isobmff": {"image", "video"},  # HEIC/HEIF photos and MP4/MOV videos
}


def content_matches(content_type: str, head: bytes) -> bool:
    """True when the bytes are a supported format of the declared kind.

    Kind-level match (not exact type) on purpose: a PNG saved as .jpg is still
    a photo, but a PDF declared as image/jpeg is rejected.
    """
    fmt = detect_format(head)
    if fmt is None:
        return False
    return kind_of(content_type) in _FORMAT_KINDS[fmt]
