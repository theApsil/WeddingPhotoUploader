"""Gallery: thumbnails, pagination, video support (local backend)."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.\' \",#\x1c\x1c(7),01444\x1f\'9=82<.342"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x7f\xff\xd9"
)

TINY_VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 96


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


@pytest.fixture()
def video_limit_client(env_local, monkeypatch):
    """Client with a small MAX_VIDEO_SIZE_MB to test the video cap."""
    monkeypatch.setenv("MAX_VIDEO_SIZE_MB", "1")
    from app import config

    config.get_settings.cache_clear()
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


def test_thumbnail_generated_and_served(client, env_local):
    upload = client.post(
        "/api/uploads",
        files=[("files", ("shot.jpg", TINY_JPEG, "image/jpeg"))],
    )
    assert upload.status_code == 200, upload.text
    saved = upload.json()["saved"][0]
    assert saved["kind"] == "image"
    assert saved["thumb_url"]
    assert saved["thumb_url"].startswith("/api/files/thumbs/")

    thumb_res = client.get(saved["thumb_url"])
    assert thumb_res.status_code == 200
    assert "image/jpeg" in thumb_res.headers.get("content-type", "")

    key = saved["key"]
    thumb_key = key.replace("uploads/", "thumbs/", 1).rsplit(".", 1)[0] + ".jpg"
    assert (env_local["storage"] / thumb_key).is_file()


def test_gallery_returns_thumb_and_metadata(client):
    client.post(
        "/api/uploads",
        files=[("files", ("a.jpg", TINY_JPEG, "image/jpeg"))],
    )
    photos = client.get("/api/photos")
    assert photos.status_code == 200
    item = photos.json()["items"][0]
    assert item["kind"] == "image"
    assert item["thumb_url"]
    assert item["thumb_width"] and item["thumb_height"]
    assert "has_more" in photos.json()


def test_pagination(client):
    for i in range(5):
        client.post(
            "/api/uploads",
            files=[("files", (f"{i}.jpg", TINY_JPEG, "image/jpeg"))],
        )

    page1 = client.get("/api/photos?limit=2&offset=0").json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert page1["has_more"] is True

    page3 = client.get("/api/photos?limit=2&offset=4").json()
    assert len(page3["items"]) == 1
    assert page3["has_more"] is False


def test_display_version_served(client):
    saved = client.post(
        "/api/uploads",
        files=[("files", ("a.jpg", TINY_JPEG, "image/jpeg"))],
    ).json()["saved"][0]
    assert saved["display_url"]
    assert saved["display_url"].startswith("/api/files/display/")

    res = client.get(saved["display_url"])
    assert res.status_code == 200
    assert "image/jpeg" in res.headers.get("content-type", "")


def test_gallery_excludes_hidden_and_includes_dimensions(client):
    """Dimensions are stored in DB so Yandex tiles are not square/cropped."""
    client.post(
        "/api/uploads",
        files=[("files", ("a.jpg", TINY_JPEG, "image/jpeg"))],
    )
    item = client.get("/api/photos").json()["items"][0]
    assert item["thumb_width"] == 1
    assert item["thumb_height"] == 1
    assert item["display_url"]


def test_video_upload_kind_and_filter(client):
    upload = client.post(
        "/api/uploads",
        files=[("files", ("clip.mp4", TINY_VIDEO, "video/mp4"))],
    )
    assert upload.status_code == 200, upload.text
    saved = upload.json()["saved"][0]
    assert saved["kind"] == "video"
    assert saved["thumb_url"] is None
    assert saved["url"].endswith(".mp4")

    photos = client.get("/api/photos?kind=video").json()
    assert photos["total"] == 1
    assert photos["items"][0]["kind"] == "video"

    all_photos = client.get("/api/photos").json()
    assert all_photos["total"] == 1


def test_video_allowed_above_photo_limit(client):
    """Videos use their own (larger) size limit; 16 MB must be accepted."""
    big = TINY_VIDEO + b"x" * (16 * 1024 * 1024 - len(TINY_VIDEO))
    res = client.post(
        "/api/uploads",
        files=[("files", ("clip.mp4", big, "video/mp4"))],
    )
    assert res.status_code == 200, res.text


def test_video_rejects_oversize(video_limit_client):
    big = b"x" * (2 * 1024 * 1024)
    res = video_limit_client.post(
        "/api/uploads",
        files=[("files", ("clip.mp4", big, "video/mp4"))],
    )
    assert res.status_code == 400
    assert "слишком большой" in res.json()["detail"].lower()


def test_startup_backfill_generates_missing_thumbs(env_local):
    """Pre-existing images without a thumbnail get one at startup."""
    import asyncio

    from app import main
    from app.config import get_settings
    from app.db import PhotoRepository, resolve_db_path
    from app.storage import create_storage

    settings = get_settings()
    repo = PhotoRepository(resolve_db_path(settings.database_path))
    asyncio.run(repo.init())
    storage = create_storage(settings)
    storage.ensure_ready()

    key = "uploads/2026-01-01/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
    storage.save_bytes(key, TINY_JPEG)
    asyncio.run(
        repo.add(
            object_key=key,
            content_type="image/jpeg",
            size_bytes=len(TINY_JPEG),
            uploaded_at="2026-01-01T00:00:00Z",
            client_ip="x",
        )
    )
    assert not storage.exists(storage.thumb_key(key))

    asyncio.run(main._backfill_derived(repo, storage))

    assert storage.exists(storage.thumb_key(key))
    assert storage.exists(storage.display_key(key))