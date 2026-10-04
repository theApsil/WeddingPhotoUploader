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


class UploadResponse(BaseModel):
    ok: bool = True
    saved: list[UploadedPhoto]


class PhotoOut(BaseModel):
    id: int
    key: str
    content_type: str
    size_bytes: int
    uploaded_at: str
    url: str


class PhotoListResponse(BaseModel):
    items: list[PhotoOut]
    total: int


class HealthResponse(BaseModel):
    status: str
    storage_backend: str
    storage_configured: bool
    storage_dir: str | None = None
    s3_bucket: str | None = None
    allowed_types: list[str] = Field(default_factory=lambda: sorted(ALLOWED_CONTENT_TYPES))
    max_file_size_mb: int
    max_files_per_request: int


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
