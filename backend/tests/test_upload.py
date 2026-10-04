"""Multipart upload, object key shape, validation (local backend)."""

from __future__ import annotations

import re

from app.storage import build_object_key


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_build_object_key_uses_uuid_and_date():
    key = build_object_key("image/jpeg")
    assert key.startswith("uploads/")
    assert key.endswith(".jpg")
    assert re.match(r"uploads/\d{4}-\d{2}-\d{2}/[0-9a-f]{32}\.jpg$", key)


def test_upload_saves_file_and_returns_url(client, env_local):
    res = client.post(
        "/api/uploads",
        files=[("files", ("tiny.png", TINY_PNG, "image/png"))],
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["ok"] is True
    assert len(data["saved"]) == 1
    item = data["saved"][0]
    assert item["key"].startswith("uploads/")
    assert item["key"].endswith(".png")
    assert item["content_type"] == "image/png"
    assert item["size_bytes"] == len(TINY_PNG)
    assert item["url"].startswith("/api/files/")

    disk = env_local["storage"] / item["key"]
    assert disk.is_file()
    assert disk.read_bytes() == TINY_PNG


def test_upload_rejects_bad_type(client):
    res = client.post(
        "/api/uploads",
        files=[("files", ("x.pdf", b"%PDF-1.4", "application/pdf"))],
    )
    assert res.status_code == 400
    assert "не поддерживается" in res.json()["detail"]


def test_upload_rejects_disguised_file(client):
    """Declared type is not trusted: a PDF sent as image/jpeg is refused."""
    res = client.post(
        "/api/uploads",
        files=[("files", ("x.jpg", b"%PDF-1.4 not a photo", "image/jpeg"))],
    )
    assert res.status_code == 400
    assert "не похож на фото или видео" in res.json()["detail"]
    assert client.get("/api/photos").json()["total"] == 0


def test_upload_rejects_image_bytes_declared_as_video(client):
    res = client.post(
        "/api/uploads",
        files=[("files", ("clip.mp4", TINY_PNG, "video/mp4"))],
    )
    assert res.status_code == 400


def test_upload_rejects_oversize(client):
    big = b"x" * (15 * 1024 * 1024 + 1)
    res = client.post(
        "/api/uploads",
        files=[("files", ("big.jpg", big, "image/jpeg"))],
    )
    assert res.status_code == 400
    assert "слишком большой" in res.json()["detail"].lower() or "МБ" in res.json()["detail"]


def test_upload_rejects_too_many_files(client):
    files = [
        ("files", (f"a{i}.png", TINY_PNG, "image/png"))
        for i in range(11)
    ]
    res = client.post("/api/uploads", files=files)
    assert res.status_code == 400
    assert "не больше" in res.json()["detail"]


def test_local_rejects_presign(client):
    res = client.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": "image/jpeg", "size": 100}]},
    )
    assert res.status_code == 400
    assert "yandex" in res.json()["detail"].lower()
