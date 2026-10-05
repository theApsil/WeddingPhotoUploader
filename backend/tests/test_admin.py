"""Admin panel: auth, listing, hiding and deleting photos (local backend)."""

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

ADMIN_PASSWORD = "s3cret-admin"


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


@pytest.fixture()
def admin_client(env_local, monkeypatch):
    """Client with ADMIN_PASSWORD set (admin endpoints enabled)."""
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    from app import config

    config.get_settings.cache_clear()
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


def _auth() -> dict:
    return {"Authorization": f"Bearer {ADMIN_PASSWORD}"}


def _upload(client, name="a.jpg", headers=None):
    return client.post(
        "/api/uploads",
        files=[("files", (name, TINY_JPEG, "image/jpeg"))],
        headers=headers or {},
    ).json()["saved"][0]


def test_admin_disabled_without_password(client):
    res = client.get("/api/admin/photos")
    assert res.status_code == 503
    assert "ADMIN_PASSWORD" in res.json()["detail"]


def test_admin_requires_auth(admin_client):
    res = admin_client.get("/api/admin/photos")
    assert res.status_code == 401


def test_admin_wrong_password(admin_client):
    res = admin_client.get(
        "/api/admin/photos",
        headers={"Authorization": "Bearer nope"},
    )
    assert res.status_code == 401


def test_admin_list_and_hide(admin_client):
    _upload(admin_client, headers={"X-Forwarded-For": "9.9.9.9"})
    res = admin_client.get("/api/admin/photos", headers=_auth())
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["hidden"] is False
    assert item["client_ip"] == "9.9.9.9"

    photo_id = item["id"]
    patch = admin_client.patch(
        f"/api/admin/photos/{photo_id}",
        json={"hidden": True},
        headers=_auth(),
    )
    assert patch.status_code == 200
    assert patch.json()["hidden"] is True

    # Hidden photos must disappear from the public gallery.
    public = admin_client.get("/api/photos").json()
    assert public["total"] == 0

    # But still visible in admin (including hidden filter).
    admin_list = admin_client.get(
        "/api/admin/photos?hidden=true",
        headers=_auth(),
    ).json()
    assert admin_list["total"] == 1


def test_admin_delete_removes_file_and_db(admin_client, env_local):
    saved = _upload(admin_client)
    key = saved["key"]
    assert (env_local["storage"] / key).is_file()

    # Need the photo id: fetch from admin list.
    listing = admin_client.get("/api/admin/photos", headers=_auth()).json()
    photo_id = listing["items"][0]["id"]

    res = admin_client.delete(f"/api/admin/photos/{photo_id}", headers=_auth())
    assert res.status_code == 200
    assert res.json()["ok"] is True

    assert not (env_local["storage"] / key).exists()
    gallery = admin_client.get("/api/photos").json()
    assert gallery["total"] == 0
    # DB row gone.
    admin_list = admin_client.get("/api/admin/photos", headers=_auth()).json()
    assert admin_list["total"] == 0


def test_admin_delete_requires_auth(admin_client):
    res = admin_client.delete("/api/admin/photos/1")
    assert res.status_code == 401


def test_hidden_original_not_served_by_direct_link(admin_client):
    """Hiding must remove direct-link access to the local original."""
    saved = _upload(admin_client)
    url = saved["url"]
    assert admin_client.get(url).status_code == 200

    listing = admin_client.get("/api/admin/photos", headers=_auth()).json()
    photo_id = listing["items"][0]["id"]
    admin_client.patch(
        f"/api/admin/photos/{photo_id}", json={"hidden": True}, headers=_auth()
    )

    # Original is now withheld; the derived (EXIF-free) display still works.
    assert admin_client.get(url).status_code == 404
    assert admin_client.get(saved["display_url"]).status_code == 200


def test_trusted_proxy_ignores_spoofed_leftmost_ip(admin_client):
    """X-Forwarded-For spoofing must not override the real (rightmost) IP."""
    _upload(admin_client, headers={"X-Forwarded-For": "1.2.3.4, 9.9.9.9"})
    item = admin_client.get("/api/admin/photos", headers=_auth()).json()["items"][0]
    assert item["client_ip"] == "9.9.9.9"


def test_guest_name_attributed_from_cookie(admin_client):
    admin_client.cookies.set("guest_name", "Ivan Ivanov")
    _upload(admin_client)
    item = admin_client.get("/api/admin/photos", headers=_auth()).json()["items"][0]
    assert item["guest_name"] == "Ivan Ivanov"


def test_no_guest_name_when_cookie_absent(admin_client):
    _upload(admin_client)
    item = admin_client.get("/api/admin/photos", headers=_auth()).json()["items"][0]
    assert item["guest_name"] == ""


def test_admin_brute_force_rate_limited(admin_client):
    """Repeated wrong passwords should eventually hit 429."""
    bad = {"Authorization": "Bearer wrong"}
    statuses = {admin_client.get("/api/admin/photos", headers=bad).status_code for _ in range(15)}
    assert 401 in statuses
    assert 429 in statuses

def test_cyrillic_guest_name_cookie_is_decoded(admin_client):
    """The frontend writes the cookie with encodeURIComponent."""
    from urllib.parse import quote

    admin_client.cookies.set("guest_name", quote("Пупуня"))
    _upload(admin_client)
    item = admin_client.get("/api/admin/photos", headers=_auth()).json()["items"][0]
    assert item["guest_name"] == "Пупуня"
    guests = admin_client.get("/api/photos/guests").json()["guests"]
    assert guests == ["Пупуня"]


async def test_percent_encoded_names_fixed_on_startup(tmp_path):
    """Rows saved before the fix get their names decoded by the migration."""
    import aiosqlite

    from app.db import PhotoRepository

    repo = PhotoRepository(str(tmp_path / "photos.db"))
    await repo.init()
    for i, name in enumerate(["%D0%9F%D1%83%D0%BF%D1%83%D0%BD%D1%8F", "100%", "Ivan"]):
        await repo.add(
            object_key=f"uploads/2026-10-05/{i}.jpg",
            content_type="image/jpeg",
            size_bytes=1,
            uploaded_at="2026-10-05T00:00:00+00:00",
            client_ip="",
            guest_name=name,
        )
    await repo.init()  # restart runs the migration again
    async with aiosqlite.connect(repo.database_path) as db:
        rows = await (await db.execute("SELECT guest_name FROM photos ORDER BY id")).fetchall()
    assert [r[0] for r in rows] == ["Пупуня", "100%", "Ivan"]


def test_admin_successful_requests_not_rate_limited(admin_client):
    """Moderation makes many requests a minute — only failures may count."""
    statuses = {
        admin_client.get("/api/admin/photos", headers=_auth()).status_code for _ in range(25)
    }
    assert statuses == {200}


def test_blocked_ip_gets_429_even_with_correct_password(admin_client):
    """Once blocked, a right guess must not be distinguishable from a wrong one."""
    bad = {"Authorization": "Bearer wrong"}
    for _ in range(15):
        admin_client.get("/api/admin/photos", headers=bad)
    assert admin_client.get("/api/admin/photos", headers=_auth()).status_code == 429
