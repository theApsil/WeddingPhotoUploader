"""Orphan cleanup via the unconfirmed-uploads ledger, video posters, HEIC originals."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

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

OLD = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
NOW = datetime.now(timezone.utc).isoformat()


def _key(ch: str) -> str:
    return f"uploads/2026-01-01/{ch * 32}.jpg"


@pytest.fixture()
def local_setup(env_local):
    from app.config import get_settings
    from app.db import PhotoRepository, resolve_db_path
    from app.storage import create_storage

    settings = get_settings()
    repo = PhotoRepository(resolve_db_path(settings.database_path))
    asyncio.run(repo.init())
    storage = create_storage(settings)
    storage.ensure_ready()
    return repo, storage


def _add_row(repo, key: str) -> None:
    asyncio.run(
        repo.add(
            object_key=key,
            content_type="image/jpeg",
            size_bytes=len(TINY_JPEG),
            uploaded_at=NOW,
            client_ip="x",
        )
    )


def _ledger(repo) -> set[str]:
    import sqlite3

    with sqlite3.connect(repo.database_path) as db:
        return {r[0] for r in db.execute("SELECT object_key FROM unconfirmed_uploads")}


def test_cleanup_never_touches_objects_the_app_did_not_issue(local_setup):
    """A lost/restored DB must not turn real photos into "orphans"."""
    from app.maintenance import cleanup_orphans

    repo, storage = local_setup
    stray = _key("a")
    path = storage.save_bytes(stray, TINY_JPEG)
    old = datetime(2025, 1, 1).timestamp()
    __import__("os").utime(path, (old, old))

    assert asyncio.run(cleanup_orphans(repo, storage, age_hours=1)) == 0
    assert storage.exists(stray)


def test_cleanup_removes_only_stale_unconfirmed_keys(local_setup):
    from app.maintenance import cleanup_orphans

    repo, storage = local_setup
    stale, fresh, confirmed = _key("b"), _key("c"), _key("d")
    for key in (stale, fresh, confirmed):
        storage.save_bytes(key, TINY_JPEG)
    storage.save_thumb(stale, TINY_JPEG)
    asyncio.run(repo.track_unconfirmed([stale, confirmed], OLD))
    asyncio.run(repo.track_unconfirmed([fresh], NOW))
    _add_row(repo, confirmed)  # confirmed but the ledger entry was left behind

    assert asyncio.run(cleanup_orphans(repo, storage, age_hours=1)) == 1
    assert not storage.exists(stale)
    assert not storage.exists(storage.thumb_key(stale))  # derivatives go too
    assert storage.exists(fresh)
    assert storage.exists(confirmed)
    assert _ledger(repo) == {fresh}


def test_cleanup_is_capped_per_pass(local_setup):
    from app.maintenance import cleanup_orphans

    repo, storage = local_setup
    keys = [_key(ch) for ch in "efg"]
    for key in keys:
        storage.save_bytes(key, TINY_JPEG)
    asyncio.run(repo.track_unconfirmed(keys, OLD))

    assert asyncio.run(cleanup_orphans(repo, storage, age_hours=1, max_per_pass=2)) == 2
    assert asyncio.run(cleanup_orphans(repo, storage, age_hours=1, max_per_pass=2)) == 1
    assert _ledger(repo) == set()


def test_local_upload_leaves_no_ledger_entry(client, env_local):
    res = client.post("/api/uploads", files=[("files", ("a.jpg", TINY_JPEG, "image/jpeg"))])
    assert res.status_code == 200

    from app.db import PhotoRepository

    assert _ledger(PhotoRepository(str(env_local["db"]))) == set()


def _presign(client, content_type="image/jpeg", size=len(TINY_JPEG)) -> str:
    res = client.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": content_type, "size": size}]},
    )
    assert res.status_code == 200, res.text
    return res.json()["items"][0]["key"]


def test_presign_tracks_key_and_confirm_clears_it(client_yandex, env_yandex):
    from app.db import PhotoRepository

    repo = PhotoRepository(str(env_yandex["db"]))
    key = _presign(client_yandex)
    assert _ledger(repo) == {key}

    env_yandex["s3"].put_object(Bucket=env_yandex["bucket"], Key=key, Body=TINY_JPEG)
    res = client_yandex.post(
        "/api/uploads/confirm",
        json={"files": [{"key": key, "content_type": "image/jpeg", "size_bytes": len(TINY_JPEG)}]},
    )
    assert res.status_code == 200, res.text
    assert _ledger(repo) == set()


def test_yandex_cleanup_deletes_abandoned_upload_only(client_yandex, env_yandex):
    from app.config import get_settings
    from app.db import PhotoRepository
    from app.maintenance import cleanup_orphans
    from app.storage import YandexStorage

    s3, bucket = env_yandex["s3"], env_yandex["bucket"]
    repo = PhotoRepository(str(env_yandex["db"]))
    abandoned = _presign(client_yandex)   # uploaded, never confirmed
    never_sent = _presign(client_yandex)  # presigned, browser gave up
    s3.put_object(Bucket=bucket, Key=abandoned, Body=TINY_JPEG)
    stray = "uploads/2025-01-01/" + "f" * 32 + ".jpg"  # not issued by the app
    s3.put_object(Bucket=bucket, Key=stray, Body=TINY_JPEG)
    asyncio.run(repo.untrack_unconfirmed([abandoned, never_sent]))
    asyncio.run(repo.track_unconfirmed([abandoned, never_sent], OLD))

    storage = YandexStorage(get_settings())
    assert asyncio.run(cleanup_orphans(repo, storage, age_hours=1)) == 2

    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])}
    assert keys == {stray}
    assert _ledger(repo) == set()


# ---- Video posters -------------------------------------------------------


def test_missing_ffmpeg_skips_poster_without_running_anything(monkeypatch):
    from app import poster

    poster.ffmpeg_available.cache_clear()
    monkeypatch.setattr(poster.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        poster.subprocess, "run", lambda *a, **k: pytest.fail("ffmpeg must not run")
    )
    assert poster.generate_poster_from_url("https://example/v.mp4", "ffmpeg") is None
    poster.ffmpeg_available.cache_clear()


def test_poster_falls_back_to_first_frame_for_short_clips(monkeypatch):
    from app import poster

    seeks = []

    def fake_run(args, **_kwargs):
        seek = args[args.index("-ss") + 1]
        seeks.append(seek)
        out = TINY_JPEG if seek == "0" else b""
        return type("P", (), {"returncode": 0, "stdout": out})()

    poster.ffmpeg_available.cache_clear()
    monkeypatch.setattr(poster.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(poster.subprocess, "run", fake_run)
    assert poster.generate_poster_from_url("https://example/v.mp4", "ffmpeg") == TINY_JPEG
    assert seeks == ["1", "0"]
    poster.ffmpeg_available.cache_clear()


def test_yandex_poster_reads_by_url_and_never_downloads_video(client_yandex, env_yandex, monkeypatch):
    from app import main

    seen = {}

    def fake_from_url(url, _ffmpeg):
        seen["url"] = url
        return TINY_JPEG

    monkeypatch.setattr(main, "generate_poster_from_url", fake_from_url)
    storage = main.get_storage()
    monkeypatch.setattr(
        storage, "get_bytes", lambda key: pytest.fail(f"video {key} must not be downloaded")
    )

    key = _presign(client_yandex, "video/mp4", len(TINY_VIDEO))
    env_yandex["s3"].put_object(Bucket=env_yandex["bucket"], Key=key, Body=TINY_VIDEO)
    res = client_yandex.post(
        "/api/uploads/confirm",
        json={"files": [{"key": key, "content_type": "video/mp4", "size_bytes": len(TINY_VIDEO)}]},
    )
    assert res.status_code == 200, res.text
    assert key in seen["url"]
    poster_key = key.replace("uploads/", "posters/", 1).rsplit(".", 1)[0] + ".jpg"
    env_yandex["s3"].head_object(Bucket=env_yandex["bucket"], Key=poster_key)


# ---- HEIC originals ------------------------------------------------------


def test_heic_original_is_never_rewritten():
    from app.thumbnails import sanitize_original

    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    assert sanitize_original(heic, "image/heic") is None
    assert sanitize_original(heic, "image/heif") is None
