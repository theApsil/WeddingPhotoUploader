"""Pydantic request/response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.config import ALLOWED_CONTENT_TYPES


class UploadedPhoto(BaseModel):
    key: str
    content_type: str
    size_bytes: int
    url: str
    kind: str = "image"
    thumb_url: str | None = None


class UploadResponse(BaseModel):
    ok: bool = True
    saved: list[UploadedPhoto]


class PhotoOut(BaseModel):
    id: int
    key: str
    content_type: str
    kind: str
    size_bytes: int
    uploaded_at: str
    url: str
    thumb_url: str | None = None
    thumb_width: int | None = None
    thumb_height: int | None = None


class AdminPhotoOut(PhotoOut):
    client_ip: str = ""
    hidden: bool = False


class PhotoListResponse(BaseModel):
    items: list[PhotoOut]
    total: int
    limit: int
    offset: int
    has_more: bool


class AdminListResponse(BaseModel):
    items: list[AdminPhotoOut]
    total: int
    limit: int
    offset: int
    has_more: bool


class PhotoPatchRequest(BaseModel):
    hidden: bool


class DeleteResponse(BaseModel):
    ok: bool = True


class HealthResponse(BaseModel):
    status: str
    storage_backend: str
    storage_configured: bool
    storage_dir: str | None = None
    s3_bucket: str | None = None
    allowed_types: list[str] = Field(default_factory=lambda: sorted(ALLOWED_CONTENT_TYPES))
    max_file_size_mb: int
    max_video_size_mb: int
    max_files_per_request: int
    admin_enabled: bool


class PresignFileIn(BaseModel):
    content_type: str
    size: int = Field(ge=1)


class PresignRequest(BaseModel):
    files: list[PresignFileIn]


class PresignItemOut(BaseModel):
    key: str
    content_type: str
    size: int
    upload_url: str
    fields: dict[str, Any]


class PresignResponse(BaseModel):
    items: list[PresignItemOut]


class ConfirmFileIn(BaseModel):
    key: str
    content_type: str
    size_bytes: int = Field(ge=1)


class ConfirmRequest(BaseModel):
    files: list[ConfirmFileIn]


class ConfirmResponse(BaseModel):
    ok: bool = True
    saved: list[UploadedPhoto]
