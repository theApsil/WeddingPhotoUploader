"""Yandex Object Storage backend — presign, confirm, gallery (moto)."""

from __future__ import annotations

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
    assert data["storage_dir"] is None


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
