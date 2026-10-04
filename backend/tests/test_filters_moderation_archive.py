"""Guest filter, pre-moderation, video posters, and archive (local backend)."""

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
def mod_client(env_local, monkeypatch):
    """Client with pre-moderation enabled and admin password set."""
    monkeypatch.setenv("PRE_MODERATION", "true")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-admin")
    from app import config

    config.get_settings.cache_clear()
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


def _auth() -> dict:
    return {"Authorization": "Bearer s3cret-admin"}


def _upload_image(client, name="a.jpg"):
    return client.post(
        "/api/uploads",
        files=[("files", (name, TINY_JPEG, "image/jpeg"))],
    ).json()["saved"][0]


# ---- Guest (uploader) filter ----


def test_gallery_filters_by_guest(client):
    client.cookies.set("guest_name", "Maria")
    _upload_image(client, "maria.jpg")
    client.cookies.set("guest_name", "Ivan")
    _upload_image(client, "ivan.jpg")

    all_photos = client.get("/api/photos").json()
    assert all_photos["total"] == 2

    ivan = client.get("/api/photos", params={"guest": "Ivan"}).json()
    assert ivan["total"] == 1
    maria = client.get("/api/photos", params={"guest": "Maria"}).json()
    assert maria["total"] == 1
    nobody = client.get("/api/photos", params={"guest": "Nobody"}).json()
    assert nobody["total"] == 0


def test_guests_endpoint_lists_distinct_uploaders(client):
    client.cookies.set("guest_name", "Maria")
    _upload_image(client)
    client.cookies.set("guest_name", "Maria")
    _upload_image(client, "second.jpg")
    client.cookies.set("guest_name", "Ivan")
    _upload_image(client, "third.jpg")
    # Anonymous upload should not appear.
    _upload_image(client, "anon.jpg")

    guests = client.get("/api/photos/guests").json()["guests"]
    assert set(guests) == {"Maria", "Ivan"}


# ---- Pre-moderation ----


def test_premoderation_keeps_uploads_hidden_until_approved(mod_client):
    saved = _upload_image(mod_client)
    photo_id = _admin_first_id(mod_client)

    # Not visible in the public gallery while pending.
    assert mod_client.get("/api/photos").json()["total"] == 0

    # Admin sees it as pending.
    listing = mod_client.get(
        "/api/admin/photos?pending=true", headers=_auth()
    ).json()
    assert listing["total"] == 1
    assert listing["items"][0]["pending"] is True

    # Approve it.
    res = mod_client.patch(
        f"/api/admin/photos/{photo_id}",
        json={"pending": False},
        headers=_auth(),
    )
    assert res.status_code == 200
    assert res.json()["pending"] is False

    assert mod_client.get("/api/photos").json()["total"] == 1


def _admin_first_id(client) -> int:
    return client.get("/api/admin/photos", headers=_auth()).json()["items"][0]["id"]


def test_premoderation_off_by_default_publishes_immediately(client):
    _upload_image(client)
    assert client.get("/api/photos").json()["total"] == 1


# ---- Video posters ----


def test_video_poster_generated_and_served(client, env_local, monkeypatch):
    from app import main

    fake_poster = TINY_JPEG
    monkeypatch.setattr(
        main, "generate_poster_from_path", lambda *a, **k: fake_poster
    )

    res = client.post(
        "/api/uploads",
        files=[("files", ("clip.mp4", TINY_VIDEO, "video/mp4"))],
    )
    assert res.status_code == 200, res.text
    saved = res.json()["saved"][0]
    assert saved["kind"] == "video"
    assert saved["poster_url"]
    assert saved["poster_url"].startswith("/api/files/posters/")

    assert client.get(saved["poster_url"]).status_code == 200

    key = saved["key"]
    poster_key = key.replace("uploads/", "posters/", 1).rsplit(".", 1)[0] + ".jpg"
    assert (env_local["storage"] / poster_key).is_file()


def test_video_without_ffmpeg_gets_no_poster(client, env_local, monkeypatch):
    """A failed/best-effort poster must never break the upload."""
    from app import main

    monkeypatch.setattr(main, "generate_poster_from_path", lambda *a, **k: None)
    res = client.post(
        "/api/uploads",
        files=[("files", ("clip.mp4", TINY_VIDEO, "video/mp4"))],
    )
    assert res.status_code == 200, res.text
    saved = res.json()["saved"][0]
    # URL still present; the tile falls back to the play icon if the file is absent.
    assert saved["poster_url"]


# ---- Archive ----


def test_archive_requires_auth(admin_local):
    assert admin_local.get("/api/admin/photos/archive.zip").status_code == 401


def test_archive_contains_only_visible_photos(admin_local, monkeypatch):
    from app import main

    monkeypatch.setattr(
        main, "generate_poster_from_path", lambda *a, **k: None
    )

    saved1 = _upload_image(admin_local, "one.jpg")
    saved2 = _upload_image(admin_local, "two.jpg")
    # Hide the second one — it must not appear in the archive.
    photo2_id = admin_local.get(
        "/api/admin/photos", headers=_auth()
    ).json()["items"][0]["id"]
    admin_local.patch(f"/api/admin/photos/{photo2_id}", json={"hidden": True}, headers=_auth())

    res = admin_local.get(
        "/api/admin/photos/archive.zip", headers=_auth()
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/zip")
    import io
    import zipfile

    zf = zipfile.ZipFile(io.BytesIO(res.content))
    names = zf.namelist()
    assert len(names) == 1
    assert saved1["key"].replace("uploads/", "", 1) in names


@pytest.fixture()
def admin_local(env_local, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-admin")
    from app import config

    config.get_settings.cache_clear()
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


# ---- Maintenance: backup + orphan cleanup ----


def test_backup_sqlite_creates_backup(env_local):
    import asyncio

    from app.config import get_settings
    from app.db import PhotoRepository, resolve_db_path
    from app.maintenance import backup_sqlite
    from app.storage import create_storage

    settings = get_settings()
    repo = PhotoRepository(resolve_db_path(settings.database_path))
    asyncio.run(repo.init())
    storage = create_storage(settings)
    storage.ensure_ready()

    key = "uploads/2026-01-01/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg"
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

    path = backup_sqlite(settings)
    assert path is not None
    backup = __import__("pathlib").Path(path)
    assert backup.is_file()
    assert backup.name.startswith("photos-")


def test_orphan_cleanup_removes_only_unreferenced(env_local):
    import asyncio

    from app.config import get_settings
    from app.db import PhotoRepository, resolve_db_path
    from app.maintenance import cleanup_orphans
    from app.storage import create_storage

    settings = get_settings()
    repo = PhotoRepository(resolve_db_path(settings.database_path))
    asyncio.run(repo.init())
    storage = create_storage(settings)
    storage.ensure_ready()

    # Referenced file — must be kept.
    keep_key = "uploads/2026-01-01/cccccccccccccccccccccccccccccccc.jpg"
    storage.save_bytes(keep_key, TINY_JPEG)
    asyncio.run(
        repo.add(
            object_key=keep_key,
            content_type="image/jpeg",
            size_bytes=len(TINY_JPEG),
            uploaded_at="2026-01-01T00:00:00Z",
            client_ip="x",
        )
    )
    # Orphan (presign-then-abort) — old mtime, no DB row — must be removed.
    orphan_key = "uploads/2026-01-01/dddddddddddddddddddddddddddddddd.jpg"
    orphan_path = storage.save_bytes(orphan_key, TINY_JPEG)
    old = __import__("datetime").datetime(2025, 1, 1)
    __import__("os").utime(orphan_path, (old.timestamp(), old.timestamp()))

    removed = asyncio.run(cleanup_orphans(repo, storage, age_hours=1))
    assert removed == 1
    assert storage.exists(keep_key)
    assert not storage.exists(orphan_key)