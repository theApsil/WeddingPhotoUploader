"""Application settings from environment / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
    }
)

CONTENT_TYPE_EXTENSION = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}

StorageBackendName = Literal["local", "yandex"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # local (default) | yandex — without Yandex keys the app still runs on local.
    storage_backend: str = Field(default="local", alias="STORAGE_BACKEND")

    # Local disk (keys are relative: uploads/…).
    storage_dir: str = Field(default="./data", alias="STORAGE_DIR")

    # Yandex Object Storage (optional; required only when STORAGE_BACKEND=yandex).
    yandex_access_key_id: str = Field(default="", alias="YANDEX_ACCESS_KEY_ID")
    yandex_secret_access_key: str = Field(default="", alias="YANDEX_SECRET_ACCESS_KEY")
    s3_bucket: str = Field(default="", alias="S3_BUCKET")
    s3_endpoint: str = Field(
        default="https://storage.yandexcloud.net",
        alias="S3_ENDPOINT",
    )
    s3_region: str = Field(default="ru-central1", alias="S3_REGION")
    storage_domain: str = Field(default="", alias="STORAGE_DOMAIN")
    public_read: bool = Field(default=False, alias="PUBLIC_READ")
    s3_mock: bool = Field(default=False, alias="S3_MOCK")
    presign_expires_seconds: int = Field(default=3600, alias="PRESIGN_EXPIRES_SECONDS")

    site_domain: str = Field(default="", alias="SITE_DOMAIN")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")

    max_file_size_mb: int = Field(default=15, alias="MAX_FILE_SIZE_MB")
    max_files_per_request: int = Field(default=10, alias="MAX_FILES_PER_REQUEST")
    rate_limit_per_minute: int = Field(default=20, alias="RATE_LIMIT_PER_MINUTE")
    database_path: str = Field(default="./data/photos.db", alias="DATABASE_PATH")

    # Public URL prefix when served behind reverse-proxy (e.g. /wedding).
    base_path: str = Field(default="", alias="BASE_PATH")

    # Optional override for static frontend directory (defaults next to backend/).
    frontend_dir: str = Field(default="", alias="FRONTEND_DIR")

    @field_validator("storage_backend", mode="before")
    @classmethod
    def normalize_backend(cls, value: str | None) -> str:
        text = (str(value) if value is not None else "local").strip().lower()
        if text not in ("local", "yandex"):
            raise ValueError("STORAGE_BACKEND must be 'local' or 'yandex'")
        return text

    @field_validator("site_domain", "storage_domain", mode="before")
    @classmethod
    def strip_domain(cls, value: str | None) -> str:
        if not value:
            return ""
        text = str(value).strip().lower()
        for prefix in ("https://", "http://"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
        return text.rstrip("/")

    @field_validator("base_path", mode="before")
    @classmethod
    def normalize_base_path(cls, value: str | None) -> str:
        if not value:
            return ""
        text = str(value).strip()
        if not text.startswith("/"):
            text = f"/{text}"
        return text.rstrip("/")

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def resolved_cors_origins(self) -> list[str]:
        if self.cors_origins.strip():
            return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        if self.site_domain:
            return [f"https://{self.site_domain}"]
        return ["http://localhost:8080", "http://127.0.0.1:8080"]

    @property
    def is_yandex(self) -> bool:
        return self.storage_backend == "yandex"

    @property
    def yandex_keys_present(self) -> bool:
        return bool(
            self.yandex_access_key_id
            and self.yandex_secret_access_key
            and self.s3_bucket
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
