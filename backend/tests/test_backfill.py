"""Startup backfill of thumbnails/display JPEGs on the Yandex backend."""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture()
def yandex_setup(env_yandex):
    from app.config import get_settings
    from app.db import PhotoRepository
    from app.storage import YandexStorage

    repo = PhotoRepository(str(env_yandex["db"]))
    asyncio.run(repo.init())
    return repo, YandexStorage(get_settings()), env_yandex["s3"], env_yandex["bucket"]


def _add(repo, key: str, **dims) -> None:
    asyncio.run(
        repo.add(
            object_key=key,
            content_type="image/jpeg",
            size_bytes=1,
            uploaded_at="2026-10-05T00:00:00+00:00",
            client_ip="x",
            **dims,
        )
    )


def test_yandex_backfill_creates_missing_derivatives(yandex_setup):
    """Regression: YandexStorage had no exists(), so every row crashed."""
    from app import main

    repo, storage, s3, bucket = yandex_setup
    key = "uploads/2026-10-05/" + "a" * 32 + ".jpg"
    s3.put_object(Bucket=bucket, Key=key, Body=_jpeg())
    _add(repo, key)

    asyncio.run(main._backfill_derived(repo, storage))

    assert storage.exists(storage.thumb_key(key))
    assert storage.exists(storage.display_key(key))
    row = asyncio.run(repo.get_by_key(key))
    assert (row["thumb_width"], row["thumb_height"]) == (64, 48)


def test_backfill_skips_rows_with_dimensions_without_bucket_calls(yandex_setup, monkeypatch):
    from app import main

    repo, storage, _s3, _bucket = yandex_setup
    _add(repo, "uploads/2026-10-05/" + "b" * 32 + ".jpg", thumb_width=64, thumb_height=48)
    monkeypatch.setattr(storage, "object_exists", lambda key: pytest.fail(f"HEAD {key}"))
    monkeypatch.setattr(storage, "get_bytes", lambda key: pytest.fail(f"GET {key}"))

    asyncio.run(main._backfill_derived(repo, storage))
