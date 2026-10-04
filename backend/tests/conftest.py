"""Pytest fixtures — local disk and Yandex (moto) backends."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    db = tmp_path / "photos.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("CORS_ORIGINS", "http://testserver")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "100")
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "15")
    monkeypatch.setenv("MAX_FILES_PER_REQUEST", "10")
    # Host agent shells may export BASE_PATH=/pulse-demo — keep tests at root.
    monkeypatch.setenv("BASE_PATH", "")
    monkeypatch.setenv("FRONTEND_DIR", "")
    return {"db": db}


@pytest.fixture()
def env_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    info = _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_DIR", str(storage))
    from app import config

    config.get_settings.cache_clear()
    yield {"storage": storage, **info}
    config.get_settings.cache_clear()


@pytest.fixture()
def client(env_local):
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def env_yandex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yandex backend against moto S3 (no real cloud)."""
    info = _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("STORAGE_BACKEND", "yandex")
    monkeypatch.setenv("S3_MOCK", "false")
    monkeypatch.setenv("YANDEX_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("YANDEX_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("S3_BUCKET", "wedding-photos-test")
    monkeypatch.setenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
    monkeypatch.setenv("S3_REGION", "ru-central1")
    monkeypatch.setenv("PUBLIC_READ", "false")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "unused-local"))

    from app import config

    config.get_settings.cache_clear()

    import boto3
    from moto import mock_aws

    with mock_aws():
        # Empty endpoint → boto3 talks to moto (no real Yandex network).
        monkeypatch.setenv("S3_ENDPOINT", "")
        monkeypatch.setenv("S3_REGION", "us-east-1")
        config.get_settings.cache_clear()

        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket="wedding-photos-test")
        yield {"bucket": "wedding-photos-test", "s3": client, **info}

    config.get_settings.cache_clear()


@pytest.fixture()
def client_yandex(env_yandex):
    _clear_app_modules()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
