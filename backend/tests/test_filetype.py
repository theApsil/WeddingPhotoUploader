"""Magic-byte sniffing of uploaded files."""

from __future__ import annotations

import pytest

from app.filetype import content_matches, detect_format


@pytest.mark.parametrize(
    ("head", "fmt"),
    [
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "jpeg"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "png"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "webp"),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81", "webm"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "isobmff"),  # iPhone photo
        (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", "isobmff"),
        (b"\x00\x00\x00\x08wide\x00\x00\x00\x00", "isobmff"),  # old QuickTime .mov
        (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3", None),
        (b"MZ\x90\x00\x03\x00\x00\x00", None),
        (b"", None),
    ],
)
def test_detect_format(head, fmt):
    assert detect_format(head) == fmt


def test_content_matches_by_kind():
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
    assert content_matches("image/png", png)
    assert content_matches("image/jpeg", png)  # misnamed photo is still a photo
    assert not content_matches("video/mp4", png)
    assert content_matches("video/quicktime", mp4)
    assert content_matches("image/heic", mp4)  # ISO BMFF brands are mixed up by phones
    assert not content_matches("image/jpeg", b"%PDF-1.4\n")
