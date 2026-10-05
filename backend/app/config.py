"""Application settings from environment / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
    }
)

VIDEO_CONTENT_TYPES = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",  # .mov
        "video/x-m4v",  # .m4v
    }
)

ALLOWED_CONTENT_TYPES = IMAGE_CONTENT_TYPES | VIDEO_CONTENT_TYPES

CONTENT_TYPE_EXTENSION = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
}


def is_image(content_type: str) -> bool:
    return content_type in IMAGE_CONTENT_TYPES


def is_video(content_type: str) -> bool:
    return content_type in VIDEO_CONTENT_TYPES


def kind_of(content_type: str) -> str:
    return "video" if is_video(content_type) else "image"

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
    # Signed bucket URLs. Gallery images go through /api/media redirects that the
    # browser caches for half of this, so keep it long (12 h).
    presign_expires_seconds: int = Field(default=43200, alias="PRESIGN_EXPIRES_SECONDS")

    # Generate a poster frame (ffmpeg) for uploaded videos so the gallery tile
    # shows a real preview instead of a blank play button.
    video_poster: bool = Field(default=True, alias="VIDEO_POSTER")
    ffmpeg_binary: str = Field(default="ffmpeg", alias="FFMPEG_BINARY")

    # Moderation: when true, new uploads are kept out of the gallery until an
    # admin approves them (approval clears the pending flag).
    pre_moderation: bool = Field(default=False, alias="PRE_MODERATION")

    # Background maintenance (0 disables the timer):
    #   BACKUP_HOURS         — interval between SQLite backups;
    #   BACKUP_KEEP          — how many old backups to keep;
    #   ORPHAN_MAX_AGE_HOURS — delete objects with no DB row older than this.
    backup_hours: int = Field(default=6, alias="BACKUP_HOURS")
    backup_keep: int = Field(default=28, alias="BACKUP_KEEP")
    orphan_max_age_hours: int = Field(default=24, alias="ORPHAN_MAX_AGE_HOURS")

    site_domain: str = Field(default="", alias="SITE_DOMAIN")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")

    max_file_size_mb: int = Field(default=40, alias="MAX_FILE_SIZE_MB")
    max_video_size_mb: int = Field(default=500, alias="MAX_VIDEO_SIZE_MB")
    # 0 = no per-request file count limit.
    max_files_per_request: int = Field(default=0, alias="MAX_FILES_PER_REQUEST")
    rate_limit_per_minute: int = Field(default=20, alias="RATE_LIMIT_PER_MINUTE")
    database_path: str = Field(default="./data/photos.db", alias="DATABASE_PATH")

    # Gallery thumbnails (images only; long edge in px).
    thumbnail_size: int = Field(default=400, alias="THUMBNAIL_SIZE")

    # Display-size JPEG (EXIF stripped) served in the lightbox instead of the
    # original — opens HEIC everywhere and keeps guests from downloading full
    # originals. Long edge in px.
    display_size: int = Field(default=1920, alias="DISPLAY_SIZE")

    # Number of trusted reverse proxies in front of the app. Used to pick the
    # real client IP from X-Forwarded-For (from the right), ignoring spoofed
    # leftmost entries. 1 = nginx/Caddy directly in front.
    trusted_proxies: int = Field(default=1, alias="TRUSTED_PROXIES")

    # Strip EXIF/GPS from stored originals (local) and display JPEGs.
    # Off by default: strip_exif_jpeg drops the Orientation tag and ICC profile,
    # so portrait phone photos end up sideways and the bucket original is
    # overwritten. Do not enable until that is fixed.
    strip_exif: bool = Field(default=False, alias="STRIP_EXIF")

    # Admin panel password. Empty = admin endpoints disabled.
    admin_password: str = Field(default="", alias="ADMIN_PASSWORD")

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
    def max_video_size_bytes(self) -> int:
        return self.max_video_size_mb * 1024 * 1024

    def size_limit_for(self, content_type: str) -> int:
        """Bytes allowed for a given content type (video is larger than photos)."""
        if is_video(content_type):
            return self.max_video_size_bytes
        return self.max_file_size_bytes

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
