"""Yandex Object Storage backend — presign, confirm, gallery (moto)."""

from __future__ import annotations

import logging

import requests

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_health_reports_yandex(client_yandex):
    res = client_yandex.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["storage_backend"] == "yandex"
    assert data["storage_configured"] is True
    assert data["s3_bucket"] == "wedding-photos-test"
    assert "storage_dir" not in data


def test_yandex_rejects_multipart_upload(client_yandex):
    res = client_yandex.post(
        "/api/uploads",
        files=[("files", ("tiny.png", TINY_PNG, "image/png"))],
    )
    assert res.status_code == 400
    assert "local" in res.json()["detail"].lower()


def test_presign_confirm_gallery(client_yandex, env_yandex):
    size = len(TINY_PNG)
    presign = client_yandex.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": "image/png", "size": size}]},
    )
    assert presign.status_code == 200, presign.text
    item = presign.json()["items"][0]
    assert item["key"].startswith("uploads/")
    assert item["upload_url"]
    assert "key" in item["fields"]

    # Browser-like POST to moto bucket.
    form = dict(item["fields"])
    post = requests.post(
        item["upload_url"],
        data=form,
        files={"file": (item["key"], TINY_PNG, "image/png")},
        timeout=10,
    )
    assert post.status_code in (200, 204), post.text

    # Object must exist for confirm.
    head = env_yandex["s3"].head_object(Bucket=env_yandex["bucket"], Key=item["key"])
    assert head["ContentLength"] == size

    confirm = client_yandex.post(
        "/api/uploads/confirm",
        json={
            "files": [
                {
                    "key": item["key"],
                    "content_type": "image/png",
                    "size_bytes": size,
                }
            ]
        },
    )
    assert confirm.status_code == 200, confirm.text
    saved = confirm.json()["saved"]
    assert len(saved) == 1
    assert saved[0]["key"] == item["key"]

    photos = client_yandex.get("/api/photos")
    assert photos.status_code == 200
    body = photos.json()
    assert body["total"] == 1
    assert body["items"][0]["key"] == item["key"]
    assert body["items"][0]["url"]  # signed GET


def test_presign_rejects_bad_type(client_yandex):
    res = client_yandex.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": "application/pdf", "size": 10}]},
    )
    assert res.status_code == 400
    assert "не поддерживается" in res.json()["detail"]


def test_presign_rejects_oversize(client_yandex):
    res = client_yandex.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": "image/jpeg", "size": 15 * 1024 * 1024 + 1}]},
    )
    assert res.status_code == 400


def test_confirm_rejects_and_deletes_disguised_file(client_yandex, env_yandex):
    """Bytes in the bucket are checked on confirm; a PDF posing as PNG is removed."""
    payload = b"%PDF-1.4 not a photo"
    presign = client_yandex.post(
        "/api/uploads/presign",
        json={"files": [{"content_type": "image/png", "size": len(payload)}]},
    )
    key = presign.json()["items"][0]["key"]
    env_yandex["s3"].put_object(Bucket=env_yandex["bucket"], Key=key, Body=payload)

    confirm = client_yandex.post(
        "/api/uploads/confirm",
        json={"files": [{"key": key, "content_type": "image/png", "size_bytes": len(payload)}]},
    )
    assert confirm.status_code == 400
    assert "не похож на фото или видео" in confirm.json()["detail"]

    listed = env_yandex["s3"].list_objects_v2(Bucket=env_yandex["bucket"])
    assert listed.get("KeyCount", 0) == 0
    assert client_yandex.get("/api/photos").json()["total"] == 0


def test_storage_logs_s3_failures(env_yandex, caplog):
    from app.config import get_settings
    from app.storage import YandexStorage

    storage = YandexStorage(get_settings())
    with caplog.at_level(logging.WARNING, logger="wedding"):
        # Missing object is an expected answer, not an error.
        assert storage.object_exists("uploads/2026-01-01/missing.png") is False
        assert not caplog.records

        broken = YandexStorage(get_settings().model_copy(update={"s3_bucket": "no-such-bucket"}))
        assert broken.put_bytes("thumbs/2026-01-01/x.jpg", b"x") is False
        assert broken.get_bytes("uploads/2026-01-01/x.png") is None

    messages = [r.getMessage() for r in caplog.records]
    assert "Не удалось загрузить thumbs/2026-01-01/x.jpg в бакет" in messages
    assert "Не удалось скачать uploads/2026-01-01/x.png из бакета" in messages
    assert all(r.exc_info for r in caplog.records)


def test_thumbnail_failure_is_logged(env_yandex, caplog):
    from app.config import get_settings
    from app.storage import YandexStorage

    storage = YandexStorage(get_settings())
    with caplog.at_level(logging.WARNING, logger="wedding"):
        assert storage.save_thumb("uploads/2026-01-01/bad.png", b"not an image") is False

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Не удалось декодировать изображение") for m in messages)
    assert "Превью не создано для uploads/2026-01-01/bad.png" in messages
