"""Server-side image processing: thumbnails, display JPEGs, EXIF stripping.

All outputs are JPEG. EXIF (including GPS) is intentionally dropped — guests
should never receive location or camera metadata.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image as _PILImage

try:  # Pillow is required for derived images; fail gracefully when missing.
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - optional dependency
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

THUMB_PREFIX = "thumbs"
DISPLAY_PREFIX = "display"
JPEG_EXTENSION = "jpg"

logger = logging.getLogger("wedding.thumbnails")

# Enable HEIC/HEIF decoding when pillow-heif is installed.
try:
    import pillow_heif  # noqa: F401

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional dependency
    logger.warning(
        "pillow-heif недоступен — HEIC/HEIF обрабатываться не будут",
        exc_info=True,
    )


def _open_image(data: bytes) -> "_PILImage.Image":
    """Open and transpose (apply EXIF orientation), dropping EXIF in the process."""
    img = Image.open(BytesIO(data))
    img.draft("RGB", (2048, 2048))
    return ImageOps.exif_transpose(img)


def _to_rgb(img: "_PILImage.Image", background: tuple[int, int, int] = (255, 255, 255)) -> "_PILImage.Image":
    """Flatten alpha onto a solid background so transparency is not black."""
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, background + (255,))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _save_jpeg(img: "_PILImage.Image", quality: int = 82) -> bytes:
    out = BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def derive_key(original_key: str, prefix: str) -> str:
    """uploads/YYYY-MM-DD/<uuid>.ext -> <prefix>/YYYY-MM-DD/<uuid>.jpg"""
    if not original_key.startswith("uploads/"):
        raise ValueError("производные изображения можно создать только для uploads/")
    parts = original_key.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    return f"{prefix}/{'/'.join(parts[1:-1])}/{stem}.{JPEG_EXTENSION}"


def thumb_key_for(key: str) -> str:
    return derive_key(key, THUMB_PREFIX)


def display_key_for(key: str) -> str:
    return derive_key(key, DISPLAY_PREFIX)


def generate_thumbnail(data: bytes, size: int = 400) -> bytes | None:
    """Small JPEG thumbnail (long edge <= size) or None when not decodable."""
    if not data:
        return None
    try:
        img = _open_image(data)
        img.thumbnail((size, size))
        return _save_jpeg(_to_rgb(img), quality=82)
    except Exception:
        logger.warning("Не удалось декодировать изображение (%s байт)", len(data), exc_info=True)
        return None


def generate_display(data: bytes, size: int = 1920) -> bytes | None:
    """Full-view JPEG (long edge <= size), EXIF-free. None when not decodable."""
    if not data:
        return None
    try:
        img = _open_image(data)
        img.thumbnail((size, size))
        return _save_jpeg(_to_rgb(img), quality=86)
    except Exception:
        logger.warning("Не удалось подготовить display-версию (%s байт)", len(data), exc_info=True)
        return None


def sanitize_image(data: bytes) -> bytes | None:
    """Re-encode an original to EXIF-free JPEG (keeps size) or None when undecodable."""
    if not data:
        return None
    try:
        img = _open_image(data)
        return _save_jpeg(_to_rgb(img), quality=90)
    except Exception:
        logger.warning("Не удалось очистить EXIF (%s байт)", len(data), exc_info=True)
        return None


# JPEG APP markers that carry metadata worth dropping: EXIF, ICC, Photoshop/IPTX.
_DROP_MARKERS = {0xE1, 0xE2, 0xED}


def strip_exif_jpeg(data: bytes) -> bytes | None:
    """Losslessly drop EXIF/ICC/IPTC segments from a JPEG.

    Returns None when there is nothing to strip (or the stream is unparseable),
    so the caller can keep the original untouched.
    """
    if not data or not data.startswith(b"\xff\xd8"):
        return None
    out = bytearray(b"\xff\xd8")
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xD8 or (0xD0 <= marker <= 0xD7) or marker == 0x01:
            out.append(0xFF)
            out.append(marker)
            i += 2
            continue
        if marker == 0xDA:  # SOS — copy the remaining entropy data verbatim
            out.extend(data[i:])
            break
        if i + 2 > n:
            return None
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if seg_len < 2 or i + 2 + seg_len > n:
            return None
        if marker not in _DROP_MARKERS:
            out.extend(data[i : i + 2 + seg_len])
        i += 2 + seg_len
    if bytes(out) == data:
        return None
    return bytes(out)


def sanitize_original(data: bytes, content_type: str) -> bytes | None:
    """Remove EXIF/GPS from an original while keeping quality.

    JPEG is stripped losslessly; HEIC/HEIF are re-encoded to EXIF-free JPEG.
    None means "nothing to change" (caller keeps the original).
    """
    if content_type == "image/jpeg":
        return strip_exif_jpeg(data)
    return sanitize_image(data)


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) of encoded image bytes, if readable."""
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            return img.size
    except Exception:
        return None