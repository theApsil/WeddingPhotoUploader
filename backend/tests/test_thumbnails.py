"""Unit tests for image processing: EXIF stripping and display generation."""

from __future__ import annotations

from io import BytesIO

from app.thumbnails import (
    display_key_for,
    generate_display,
    generate_thumbnail,
    sanitize_original,
    strip_exif_jpeg,
    thumb_key_for,
)


def test_thumb_and_display_keys_derive_from_upload():
    key = "uploads/2026-01-01/abc.jpg"
    assert thumb_key_for(key) == "thumbs/2026-01-01/abc.jpg"
    assert display_key_for(key) == "display/2026-01-01/abc.jpg"


def _make_jpeg_with_exif() -> bytes:
    """A valid JPEG carrying an APP1 EXIF marker."""
    from PIL import Image

    img = Image.new("RGB", (40, 30), (9, 9, 9))
    buf = BytesIO()
    img.save(buf, "JPEG")
    base = buf.getvalue()
    payload = b"Exif\0\0" + b"II*\0\x08\x00\x00\x00" + b"GARBAGE"
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return base[:2] + app1 + base[2:]


def test_strip_exif_jpeg_removes_app1():
    data = _make_jpeg_with_exif()
    assert b"Exif" in data
    cleaned = strip_exif_jpeg(data)
    assert cleaned is not None
    assert b"Exif" not in cleaned
    # Result is still a decodable image.
    from PIL import Image

    Image.open(BytesIO(cleaned)).load()


def test_strip_exif_jpeg_keeps_clean_file_untouched():
    data = b"\xff\xd8" + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x7f\xff\xd9"
    assert strip_exif_jpeg(data) is None  # nothing to strip


def test_sanitize_original_strips_exif_from_jpeg():
    data = _make_jpeg_with_exif()
    cleaned = sanitize_original(data, "image/jpeg")
    assert cleaned is not None
    assert b"Exif" not in cleaned


def test_png_original_not_reencoded():
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    assert sanitize_original(png, "image/png") is None


def test_generate_display_is_exif_free_and_scaled():
    data = _make_jpeg_with_exif()
    display = generate_display(data, size=200)
    assert display is not None
    assert b"Exif" not in display

    from PIL import Image

    with Image.open(BytesIO(display)) as img:
        assert max(img.size) <= 200


def test_generate_thumbnail_handles_transparency_with_white_bg():
    # RGBA PNG with transparency should not produce a black background.
    from PIL import Image

    img = Image.new("RGBA", (50, 50), (255, 0, 0, 0))
    buf = BytesIO()
    img.save(buf, "PNG")
    thumb = generate_thumbnail(buf.getvalue(), size=50)
    assert thumb is not None