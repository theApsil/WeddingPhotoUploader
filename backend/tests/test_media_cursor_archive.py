"""Cursor pagination, stable /api/media redirects, streamed archive with one-time token."""

from __future__ import annotations

import io
import sys
import zipfile

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
PASSWORD = "s3cret-admin"
AUTH = {"Authorization": f"Bearer {PASSWORD}"}


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


@pytest.fixture()
def admin_local(env_local, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    from app import config

    config.get_settings.cache_clear()
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


@pytest.fixture()
def admin_yandex(env_yandex, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("VIDEO_POSTER", "false")
    from app import config

    config.get_settings.cache_clear()
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


def _upload_batch(client, count: int) -> list[str]:
    """One request -> every row shares the same uploaded_at (worst case for paging)."""
    files = [("files", (f"{i}.jpg", TINY_JPEG, "image/jpeg")) for i in range(count)]
    res = client.post("/api/uploads", files=files)
    assert res.status_code == 200, res.text
    return [s["key"] for s in res.json()["saved"]]


def _all_pages(client, path: str, limit: int, **headers) -> list[dict]:
    items, cursor = [], None
    while True:
        url = f"{path}?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url, headers=headers).json()
        items += page["items"]
        if not page["has_more"]:
            assert page["next_cursor"] is None
            return items
        cursor = page["next_cursor"]


# ---- Cursor pagination ----------------------------------------------------


def test_cursor_walks_every_row_once_even_with_equal_timestamps(admin_local):
    keys = _upload_batch(admin_local, 5)
    items = _all_pages(admin_local, "/api/photos", limit=2)
    assert sorted(i["key"] for i in items) == sorted(keys)
    assert len({i["id"] for i in items}) == 5


def test_new_upload_between_pages_does_not_duplicate(admin_local):
    _upload_batch(admin_local, 4)
    first = admin_local.get("/api/photos?limit=2").json()
    _upload_batch(admin_local, 1)  # lands on top while the guest is scrolling
    second = admin_local.get(f"/api/photos?limit=2&cursor={first['next_cursor']}").json()
    ids = [i["id"] for i in first["items"] + second["items"]]
    assert len(ids) == len(set(ids)) == 4


def test_hiding_shown_row_does_not_skip_next_page(admin_local):
    _upload_batch(admin_local, 4)
    first = admin_local.get("/api/admin/photos?limit=2&hidden=false", headers=AUTH).json()
    admin_local.patch(
        f"/api/admin/photos/{first['items'][0]['id']}", json={"hidden": True}, headers=AUTH
    )
    rest = admin_local.get(
        f"/api/admin/photos?limit=10&hidden=false&cursor={first['next_cursor']}", headers=AUTH
    ).json()
    assert len(rest["items"]) == 2  # with offset=2 one row would have been skipped


def test_bad_cursor_is_400(admin_local):
    assert admin_local.get("/api/photos?cursor=%%%").status_code == 400


# ---- Stable media URLs (yandex) -------------------------------------------


def _confirm_image(client, s3, bucket) -> dict:
    presign = client.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": "image/jpeg", "size": len(TINY_JPEG)}]},
    ).json()["items"][0]
    s3.put_object(Bucket=bucket, Key=presign["key"], Body=TINY_JPEG)
    res = client.post(
        "/api/uploads/confirm",
        json={"files": [{"key": presign["key"], "content_type": "image/jpeg",
                         "size_bytes": len(TINY_JPEG)}]},
    )
    assert res.status_code == 200, res.text
    return client.get("/api/photos").json()["items"][0]


def test_gallery_urls_are_stable_media_redirects(admin_yandex, env_yandex):
    item = _confirm_image(admin_yandex, env_yandex["s3"], env_yandex["bucket"])
    pid = item["id"]
    assert item["url"] == f"/api/media/{pid}/original"
    assert item["thumb_url"] == f"/api/media/{pid}/thumb"
    assert item["display_url"] == f"/api/media/{pid}/display"
    # Same URLs on the next request -> the browser cache works.
    again = admin_yandex.get("/api/photos").json()["items"][0]
    assert (again["url"], again["thumb_url"]) == (item["url"], item["thumb_url"])

    res = admin_yandex.get(item["thumb_url"], follow_redirects=False)
    assert res.status_code == 302
    assert "thumbs/" in res.headers["location"]
    assert "Signature" in res.headers["location"]
    assert res.headers["cache-control"] == "private, max-age=21600"


def test_derived_images_are_stored_cacheable(admin_yandex, env_yandex):
    item = _confirm_image(admin_yandex, env_yandex["s3"], env_yandex["bucket"])
    thumb_key = item["key"].replace("uploads/", "thumbs/", 1)
    head = env_yandex["s3"].head_object(Bucket=env_yandex["bucket"], Key=thumb_key)
    assert head["CacheControl"] == "private, max-age=31536000, immutable"


def test_media_hides_hidden_and_wrong_variants(admin_yandex, env_yandex):
    item = _confirm_image(admin_yandex, env_yandex["s3"], env_yandex["bucket"])
    pid = item["id"]
    assert admin_yandex.get(f"/api/media/{pid}/poster", follow_redirects=False).status_code == 404
    assert admin_yandex.get(f"/api/media/{pid}/nope", follow_redirects=False).status_code == 404
    admin_yandex.patch(f"/api/admin/photos/{pid}", json={"hidden": True}, headers=AUTH)
    assert admin_yandex.get(f"/api/media/{pid}/original", follow_redirects=False).status_code == 404


def test_admin_list_keeps_direct_signed_urls(admin_yandex, env_yandex):
    _confirm_image(admin_yandex, env_yandex["s3"], env_yandex["bucket"])
    item = admin_yandex.get("/api/admin/photos", headers=AUTH).json()["items"][0]
    assert "Signature" in item["thumb_url"]  # admin sees hidden items too


# ---- Streamed archive -------------------------------------------------------


def _names(content: bytes) -> list[str]:
    zf = zipfile.ZipFile(io.BytesIO(content))
    assert zf.testzip() is None  # every entry's CRC checks out
    return zf.namelist()


def test_archive_one_time_token(admin_local):
    keys = _upload_batch(admin_local, 3)
    assert admin_local.post("/api/admin/photos/archive-token").status_code == 401
    token = admin_local.post("/api/admin/photos/archive-token", headers=AUTH).json()["token"]

    res = admin_local.get(f"/api/admin/photos/archive.zip?token={token}")  # no header
    assert res.status_code == 200
    assert res.headers["x-accel-buffering"] == "no"
    assert sorted(_names(res.content)) == sorted(k.replace("uploads/", "", 1) for k in keys)
    # One-time: the same link can't be reused.
    assert admin_local.get(f"/api/admin/photos/archive.zip?token={token}").status_code == 401


def test_archive_expired_token_rejected(admin_local, monkeypatch):
    from app import main

    _upload_batch(admin_local, 1)
    token = admin_local.post("/api/admin/photos/archive-token", headers=AUTH).json()["token"]
    main._archive_tokens[token] = 0.0  # already expired
    assert admin_local.get(f"/api/admin/photos/archive.zip?token={token}").status_code == 401


def test_archive_streams_bucket_objects_intact(admin_yandex, env_yandex):
    s3, bucket = env_yandex["s3"], env_yandex["bucket"]
    big = TINY_JPEG + b"\x00" * (3 * 1024 * 1024)  # spans several 1 MB chunks
    presign = admin_yandex.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": "image/jpeg", "size": len(big)}]},
    ).json()["items"][0]
    s3.put_object(Bucket=bucket, Key=presign["key"], Body=big)
    admin_yandex.post(
        "/api/uploads/confirm",
        json={"files": [{"key": presign["key"], "content_type": "image/jpeg",
                         "size_bytes": len(big)}]},
    )
    res = admin_yandex.get("/api/admin/photos/archive.zip", headers=AUTH)
    assert res.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    assert zf.read(presign["key"].replace("uploads/", "", 1)) == big
