"""Server-side image thumbnail generation (JPEG) and thumb key derivation."""

from __future__ import annotations

from io import BytesIO

THUMB_PREFIX = "thumbs"
THUMB_EXTENSION = "jpg"

# Enable HEIC/HEIF decoding for thumbnails when pillow-heif is installed.
try:
    import pillow_heif  # noqa: F401

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional dependency
    pass


def thumb_key_for(key: str) -> str:
    """Derive the thumbnail key for an upload key.

    uploads/YYYY-MM-DD/<uuid>.jpg  ->  thumbs/YYYY-MM-DD/<uuid>.jpg
    """
    if not key.startswith("uploads/"):
        raise ValueError("превью можно сгенерировать только для uploads/")
    parts = key.split("/")
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    return f"{THUMB_PREFIX}/{'/'.join(parts[1:-1])}/{stem}.{THUMB_EXTENSION}"


def generate_thumbnail(data: bytes, size: int = 400) -> bytes | None:
    """Return a small JPEG thumbnail (long edge <= size) or None when not decodable."""
    if not data:
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = BytesIO()
            img.save(out, "JPEG", quality=82, optimize=True)
            return out.getvalue()
    except Exception:
        return None


def thumbnail_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) of a JPEG thumbnail for stable layout, if readable."""
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            return img.size
    except Exception:
        return None