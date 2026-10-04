"""Storage backends: local filesystem and Yandex Object Storage (S3 API)."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from app.config import CONTENT_TYPE_EXTENSION, Settings, is_image, is_video
from app.thumbnails import (
    DISPLAY_PREFIX,
    THUMB_PREFIX,
    display_key_for,
    generate_display,
    generate_thumbnail,
    thumb_key_for,
)

logger = logging.getLogger("wedding.storage")

# S3 error codes that just mean "no such object" — expected, not worth logging.
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def build_object_key(content_type: str, today: date | None = None) -> str:
    """Random UUID key under a date prefix; never use the original filename."""
    day = today or date.today()
    ext = CONTENT_TYPE_EXTENSION.get(content_type, "bin")
    return f"uploads/{day.isoformat()}/{uuid.uuid4().hex}.{ext}"


class StorageBackend(Protocol):
    """Common surface used by the API layer."""

    def ensure_ready(self) -> None: ...

    def is_ready(self) -> bool: ...

    def photo_url(self, key: str, base_path: str = "") -> str: ...


class LocalStorage:
    """Files live under STORAGE_DIR with keys like uploads/YYYY-MM-DD/<uuid>.ext."""

    name = "local"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.storage_dir).expanduser().resolve()

    def ensure_ready(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def is_ready(self) -> bool:
        try:
            self.ensure_ready()
            return self.root.is_dir() and os.access(self.root, os.W_OK)
        except OSError:
            return False

    def absolute_path(self, key: str) -> Path:
        """Resolve key under root; reject traversal outside STORAGE_DIR."""
        prefix = key.split("/", 1)[0] if "/" in key else key
        allowed = ("uploads", THUMB_PREFIX, DISPLAY_PREFIX)
        if prefix not in allowed or ".." in key or key.count("/") < 2:
            raise ValueError("недопустимый ключ объекта")
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("недопустимый путь") from exc
        return path

    def save_bytes(self, key: str, data: bytes) -> Path:
        path = self.absolute_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def exists(self, key: str) -> bool:
        try:
            return self.absolute_path(key).is_file()
        except ValueError:
            return False

    def delete(self, key: str) -> bool:
        """Remove the object. Missing files are not an error."""
        try:
            path = self.absolute_path(key)
        except ValueError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def thumb_key(self, key: str) -> str:
        return thumb_key_for(key)

    def save_thumb(self, key: str, data: bytes) -> Path | None:
        """Generate and persist a JPEG thumbnail for an image key."""
        if not is_image(self._content_type(key)):
            return None
        thumb = generate_thumbnail(data, size=self.settings.thumbnail_size)
        if not thumb:
            logger.warning("Превью не создано для %s", key)
            return None
        return self.save_bytes(self.thumb_key(key), thumb)

    def display_key(self, key: str) -> str:
        return display_key_for(key)

    def save_display(self, key: str, data: bytes) -> Path | None:
        """Generate and persist a display-size (EXIF-free) JPEG for an image key."""
        if not is_image(self._content_type(key)):
            return None
        display = generate_display(data, size=self.settings.display_size)
        if not display:
            logger.warning("Display-версия не создана для %s", key)
            return None
        return self.save_bytes(self.display_key(key), display)

    def _content_type(self, key: str) -> str:
        ext = key.rsplit(".", 1)[-1].lower()
        for ct, cext in CONTENT_TYPE_EXTENSION.items():
            if cext == ext:
                return ct
        return "application/octet-stream"

    def photo_url(self, key: str, base_path: str = "") -> str:
        prefix = base_path.rstrip("/") if base_path else ""
        return f"{prefix}/api/files/{key}"

    def thumb_url(self, key: str, base_path: str = "") -> str:
        return self.photo_url(self.thumb_key(key), base_path)

    def display_url(self, key: str, base_path: str = "") -> str:
        return self.photo_url(self.display_key(key), base_path)

    # Back-compat alias used by older call sites.
    def file_url(self, key: str, base_path: str = "") -> str:
        return self.photo_url(key, base_path)


class YandexStorage:
    """Presigned POST into Yandex Object Storage; gallery via signed GET or public URL."""

    name = "yandex"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    def ensure_ready(self) -> None:
        # Client is created lazily; mock mode needs no network.
        if self.settings.s3_mock:
            return
        if not self.settings.yandex_keys_present:
            return
        self._get_client()

    def is_ready(self) -> bool:
        if self.settings.s3_mock:
            return True
        return self.settings.yandex_keys_present

    def _get_client(self):  # noqa: ANN202 — boto3 client type is dynamic
        if self._client is not None:
            return self._client
        import boto3
        from botocore.client import Config

        key_id = self.settings.yandex_access_key_id or "mock-access-key"
        secret = self.settings.yandex_secret_access_key or "mock-secret-key"
        endpoint = self.settings.s3_endpoint.strip() or None
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=self.settings.s3_region,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            config=Config(signature_version="s3v4"),
        )
        return self._client

    def create_presigned_post(
        self,
        *,
        key: str,
        content_type: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        """Return {url, fields} for browser multipart POST directly to the bucket."""
        settings = self.settings
        max_bytes = settings.size_limit_for(content_type)
        if size_bytes < 1 or size_bytes > max_bytes:
            limit_mb = (
                settings.max_video_size_mb
                if is_video(content_type)
                else settings.max_file_size_mb
            )
            raise ValueError(
                f"размер файла вне диапазона 1…{limit_mb} МБ"
            )

        client = self._get_client()
        conditions: list[Any] = [
            {"bucket": settings.s3_bucket},
            {"key": key},
            {"Content-Type": content_type},
            ["content-length-range", 1, max_bytes],
        ]
        fields = {
            "key": key,
            "Content-Type": content_type,
        }
        result = client.generate_presigned_post(
            Bucket=settings.s3_bucket,
            Key=key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=settings.presign_expires_seconds,
        )
        return {"url": result["url"], "fields": result["fields"]}

    def object_exists(self, key: str) -> bool:
        if self.settings.s3_mock:
            # In mock mode confirm trusts the client after a successful "upload" stub.
            return True
        client = self._get_client()
        try:
            client.head_object(Bucket=self.settings.s3_bucket, Key=key)
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in _NOT_FOUND_CODES:
                logger.exception("Не удалось проверить объект %s в бакете", key)
            return False

    def stat(self, key: str) -> int | None:
        """Server-reported object size in bytes (head_object), or None on failure."""
        if self.settings.s3_mock:
            return None
        client = self._get_client()
        try:
            resp = client.head_object(Bucket=self.settings.s3_bucket, Key=key)
            return int(resp.get("ContentLength", 0))
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in _NOT_FOUND_CODES:
                logger.exception("Не удалось получить размер %s из бакета", key)
            return None

    def photo_url(self, key: str, base_path: str = "") -> str:
        settings = self.settings
        if settings.public_read:
            host = settings.storage_domain or f"{settings.s3_bucket}.storage.yandexcloud.net"
            return f"https://{host}/{key}"
        if settings.s3_mock:
            # Stable placeholder for tests / local UI without cloud.
            prefix = base_path.rstrip("/") if base_path else ""
            return f"{prefix}/api/files/{key}"
        client = self._get_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=settings.presign_expires_seconds,
        )

    def thumb_key(self, key: str) -> str:
        return thumb_key_for(key)

    def thumb_url(self, key: str, base_path: str = "") -> str:
        return self.photo_url(self.thumb_key(key), base_path)

    def get_bytes(self, key: str) -> bytes | None:
        """Download an object's bytes (None in mock mode or on failure)."""
        if self.settings.s3_mock:
            return None
        client = self._get_client()
        try:
            resp = client.get_object(Bucket=self.settings.s3_bucket, Key=key)
            return resp["Body"].read()
        except Exception:
            logger.exception("Не удалось скачать %s из бакета", key)
            return None

    def get_head(self, key: str, size: int) -> bytes | None:
        """Download the first `size` bytes of an object (None in mock mode or on failure)."""
        if self.settings.s3_mock:
            return None
        client = self._get_client()
        try:
            resp = client.get_object(
                Bucket=self.settings.s3_bucket,
                Key=key,
                Range=f"bytes=0-{size - 1}",
            )
            return resp["Body"].read()
        except Exception:
            logger.exception("Не удалось прочитать начало %s из бакета", key)
            return None

    def put_bytes(self, key: str, data: bytes, content_type: str = "image/jpeg") -> bool:
        if self.settings.s3_mock:
            return True
        client = self._get_client()
        try:
            client.put_object(
                Bucket=self.settings.s3_bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            return True
        except Exception:
            logger.exception("Не удалось загрузить %s в бакет", key)
            return False

    def save_thumb(self, key: str, data: bytes) -> bool:
        """Generate and store a thumbnail for an image object."""
        if not is_image(self._content_type(key)):
            return False
        thumb = generate_thumbnail(data, size=self.settings.thumbnail_size)
        if not thumb:
            logger.warning("Превью не создано для %s", key)
            return False
        return self.put_bytes(self.thumb_key(key), thumb)

    def display_key(self, key: str) -> str:
        return display_key_for(key)

    def display_url(self, key: str, base_path: str = "") -> str:
        return self.photo_url(self.display_key(key), base_path)

    def save_display(self, key: str, data: bytes) -> bool:
        """Generate and store a display-size (EXIF-free) JPEG for an image object."""
        if not is_image(self._content_type(key)):
            return False
        display = generate_display(data, size=self.settings.display_size)
        if not display:
            logger.warning("Display-версия не создана для %s", key)
            return False
        return self.put_bytes(self.display_key(key), display)

    def delete(self, key: str) -> bool:
        if self.settings.s3_mock:
            return True
        client = self._get_client()
        try:
            client.delete_object(Bucket=self.settings.s3_bucket, Key=key)
            return True
        except Exception:
            logger.exception("Не удалось удалить %s из бакета", key)
            return False

    def _content_type(self, key: str) -> str:
        ext = key.rsplit(".", 1)[-1].lower()
        for ct, cext in CONTENT_TYPE_EXTENSION.items():
            if cext == ext:
                return ct
        return "application/octet-stream"


def create_storage(settings: Settings) -> LocalStorage | YandexStorage:
    if settings.is_yandex:
        return YandexStorage(settings)
    return LocalStorage(settings)
