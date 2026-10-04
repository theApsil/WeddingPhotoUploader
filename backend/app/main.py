"""FastAPI app: local multipart and/or Yandex presigned upload + gallery."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Union

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import (
    ALLOWED_CONTENT_TYPES,
    Settings,
    get_settings,
    kind_of,
)
from app.db import PhotoRepository, resolve_db_path
from app.rate_limit import RateLimiter
from app.schemas import (
    AdminListResponse,
    AdminPhotoOut,
    ConfirmRequest,
    ConfirmResponse,
    DeleteResponse,
    HealthResponse,
    PhotoListResponse,
    PhotoPatchRequest,
    PresignRequest,
    PresignResponse,
    PresignItemOut,
    UploadedPhoto,
    UploadResponse,
)
from app.storage import LocalStorage, YandexStorage, build_object_key, create_storage

Storage = Union[LocalStorage, YandexStorage]

_repo: PhotoRepository | None = None
_limiter: RateLimiter | None = None
_storage: Storage | None = None

logger = logging.getLogger("wedding")


async def _backfill_thumbnails(
    repo: PhotoRepository,
    storage: Storage,
) -> None:
    """One-time: generate missing thumbnails for existing local images."""
    if not isinstance(storage, LocalStorage):
        return
    try:
        rows = await repo.list_all(kind="image")
    except Exception:
        return
    created = 0
    for row in rows:
        key = row["object_key"]
        try:
            if storage.exists(storage.thumb_key(key)):
                continue
            path = storage.absolute_path(key)
        except ValueError:
            continue
        if not path.is_file():
            continue
        try:
            if storage.save_thumb(key, path.read_bytes()):
                created += 1
        except Exception:
            continue
    if created:
        logger.info("Сгенерировано превью для %s существующих фото", created)


def _frontend_dir(settings: Settings) -> Path | None:
    if settings.frontend_dir:
        path = Path(settings.frontend_dir).expanduser().resolve()
    else:
        path = Path(__file__).resolve().parents[2] / "frontend"
    if path.is_dir():
        return path
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _repo, _limiter, _storage
    settings = get_settings()
    _repo = PhotoRepository(resolve_db_path(settings.database_path))
    await _repo.init()
    _limiter = RateLimiter(settings.rate_limit_per_minute)
    _storage = create_storage(settings)
    _storage.ensure_ready()
    await _backfill_thumbnails(_repo, _storage)
    yield


def get_repo() -> PhotoRepository:
    if _repo is None:
        raise RuntimeError("repository not initialized")
    return _repo


def get_limiter() -> RateLimiter:
    if _limiter is None:
        raise RuntimeError("limiter not initialized")
    return _limiter


def get_storage() -> Storage:
    if _storage is None:
        raise RuntimeError("storage not initialized")
    return _storage


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def enforce_rate_limit(request: Request, limiter: RateLimiter) -> None:
    allowed, _remaining = limiter.allow(client_ip(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                "Слишком много запросов с вашего IP. "
                "Подождите минуту и попробуйте снова."
            ),
        )


def validate_content_type(content_type: str) -> str:
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Тип «{content_type}» не поддерживается. "
                "Допустимы: jpeg, png, webp, heic, mp4, webm, mov, m4v."
            ),
        )
    return normalized


def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Reject admin calls unless the correct password is presented as a bearer token."""
    password = settings.admin_password
    if not password:
        raise HTTPException(
            status_code=503,
            detail="Админка отключена. Задайте ADMIN_PASSWORD в .env.",
        )
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token, password):
        raise HTTPException(status_code=401, detail="Неверный пароль администратора.")


def _thumb_size(storage: Storage, key: str) -> tuple[int | None, int | None]:
    """Read thumbnail pixel size for a stable masonry layout (local only)."""
    if not isinstance(storage, LocalStorage):
        return None, None
    try:
        path = storage.absolute_path(storage.thumb_key(key))
    except ValueError:
        return None, None
    if not path.is_file():
        return None, None
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return None, None


def _build_photo(
    row: dict[str, Any],
    storage: Storage,
    base: str,
    *,
    admin: bool = False,
) -> dict[str, Any]:
    key = row["object_key"]
    content_type = row["content_type"]
    kind = kind_of(content_type)
    thumb_url = storage.thumb_url(key, base) if kind == "image" else None
    thumb_width: int | None = None
    thumb_height: int | None = None
    if thumb_url is not None:
        thumb_width, thumb_height = _thumb_size(storage, key)
    payload: dict[str, Any] = {
        "id": row["id"],
        "key": key,
        "content_type": content_type,
        "kind": kind,
        "size_bytes": row["size_bytes"],
        "uploaded_at": row["uploaded_at"],
        "url": storage.photo_url(key, base),
        "thumb_url": thumb_url,
        "thumb_width": thumb_width,
        "thumb_height": thumb_height,
    }
    if admin:
        payload["client_ip"] = row.get("client_ip", "")
        payload["hidden"] = bool(row.get("hidden", 0))
    return payload


def _require_local(storage: Storage) -> LocalStorage:
    if not isinstance(storage, LocalStorage):
        raise HTTPException(
            status_code=400,
            detail="Этот эндпоинт доступен только при STORAGE_BACKEND=local.",
        )
    return storage


def _require_yandex(storage: Storage) -> YandexStorage:
    if not isinstance(storage, YandexStorage):
        raise HTTPException(
            status_code=400,
            detail="Этот эндпоинт доступен только при STORAGE_BACKEND=yandex.",
        )
    return storage


def _register_routes(api: FastAPI) -> None:
    """Attach API handlers and static UI to an app instance."""

    @api.get("/api/health", response_model=HealthResponse)
    async def health(
        settings: Annotated[Settings, Depends(get_settings)],
        storage: Annotated[Storage, Depends(get_storage)],
    ) -> HealthResponse:
        return HealthResponse(
            status="ok",
            storage_backend=settings.storage_backend,
            storage_configured=storage.is_ready(),
            storage_dir=str(storage.root) if isinstance(storage, LocalStorage) else None,
            s3_bucket=settings.s3_bucket if settings.is_yandex else None,
            max_file_size_mb=settings.max_file_size_mb,
            max_video_size_mb=settings.max_video_size_mb,
            max_files_per_request=settings.max_files_per_request,
            admin_enabled=bool(settings.admin_password),
        )

    @api.post("/api/uploads", response_model=UploadResponse)
    async def upload_photos(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        limiter: Annotated[RateLimiter, Depends(get_limiter)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
        files: list[UploadFile] = File(...),
    ) -> UploadResponse:
        """Multipart upload to local disk (STORAGE_BACKEND=local)."""
        local = _require_local(storage)
        enforce_rate_limit(request, limiter)

        if not files:
            raise HTTPException(status_code=400, detail="Нет файлов для загрузки.")

        if settings.max_files_per_request and len(files) > settings.max_files_per_request:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"За один заход можно загрузить не больше "
                    f"{settings.max_files_per_request} файлов."
                ),
            )

        if not local.is_ready():
            raise HTTPException(
                status_code=503,
                detail="Локальное хранилище недоступно для записи.",
            )

        saved: list[UploadedPhoto] = []
        ip = client_ip(request)
        now = datetime.now(timezone.utc).isoformat()
        base = settings.base_path

        for upload in files:
            content_type = validate_content_type(upload.content_type or "")
            data = await upload.read()
            size = len(data)
            if size < 1:
                raise HTTPException(status_code=400, detail="Пустой файл.")
            limit_bytes = settings.size_limit_for(content_type)
            if size > limit_bytes:
                limit_mb = (
                    settings.max_video_size_mb
                    if kind_of(content_type) == "video"
                    else settings.max_file_size_mb
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Файл слишком большой ({size} байт). "
                        f"Лимит для этого типа — {limit_mb} МБ."
                    ),
                )

            key = build_object_key(content_type)
            local.save_bytes(key, data)
            local.save_thumb(key, data)
            row = await repo.add(
                object_key=key,
                content_type=content_type,
                size_bytes=size,
                uploaded_at=now,
                client_ip=ip,
            )
            if row:
                saved.append(
                    UploadedPhoto(
                        key=key,
                        content_type=content_type,
                        size_bytes=size,
                        url=local.photo_url(key, base),
                        kind=kind_of(content_type),
                        thumb_url=(
                            local.thumb_url(key, base)
                            if kind_of(content_type) == "image"
                            else None
                        ),
                    )
                )

        return UploadResponse(ok=True, saved=saved)

    @api.post("/api/uploads/presign", response_model=PresignResponse)
    async def presign_uploads(
        body: PresignRequest,
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        limiter: Annotated[RateLimiter, Depends(get_limiter)],
        storage: Annotated[Storage, Depends(get_storage)],
    ) -> PresignResponse:
        """Issue browser POST policies for Yandex Object Storage."""
        yandex = _require_yandex(storage)
        enforce_rate_limit(request, limiter)

        if not body.files:
            raise HTTPException(status_code=400, detail="Нет файлов для загрузки.")
        if settings.max_files_per_request and len(body.files) > settings.max_files_per_request:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"За один заход можно загрузить не больше "
                    f"{settings.max_files_per_request} файлов."
                ),
            )
        if not yandex.is_ready():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Yandex Object Storage не настроен. "
                    "Проверьте ключи и S3_BUCKET или вернитесь на STORAGE_BACKEND=local."
                ),
            )

        items: list[PresignItemOut] = []
        for file_in in body.files:
            content_type = validate_content_type(file_in.content_type)
            limit_bytes = settings.size_limit_for(content_type)
            if file_in.size > limit_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Файл слишком большой ({file_in.size} байт). "
                        f"Лимит — {settings.max_file_size_mb} МБ для фото, "
                        f"{settings.max_video_size_mb} МБ для видео."
                    ),
                )
            key = build_object_key(content_type)
            try:
                signed = yandex.create_presigned_post(
                    key=key,
                    content_type=content_type,
                    size_bytes=file_in.size,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            items.append(
                PresignItemOut(
                    key=key,
                    content_type=content_type,
                    size=file_in.size,
                    upload_url=signed["url"],
                    fields=signed["fields"],
                )
            )
        return PresignResponse(items=items)

    @api.post("/api/uploads/confirm", response_model=ConfirmResponse)
    async def confirm_uploads(
        body: ConfirmRequest,
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        limiter: Annotated[RateLimiter, Depends(get_limiter)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> ConfirmResponse:
        """Record metadata after browser finished POSTing to the bucket."""
        yandex = _require_yandex(storage)
        enforce_rate_limit(request, limiter)

        if not body.files:
            raise HTTPException(status_code=400, detail="Нечего подтверждать.")
        if settings.max_files_per_request and len(body.files) > settings.max_files_per_request:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"За один заход можно подтвердить не больше "
                    f"{settings.max_files_per_request} файлов."
                ),
            )

        saved: list[UploadedPhoto] = []
        ip = client_ip(request)
        now = datetime.now(timezone.utc).isoformat()
        base = settings.base_path

        for file_in in body.files:
            content_type = validate_content_type(file_in.content_type)
            if not file_in.key.startswith("uploads/") or ".." in file_in.key:
                raise HTTPException(status_code=400, detail="Недопустимый ключ.")
            if file_in.size_bytes > settings.size_limit_for(content_type):
                raise HTTPException(
                    status_code=400,
                    detail=f"Файл слишком большой. Лимит превышен.",
                )
            if not yandex.object_exists(file_in.key):
                raise HTTPException(
                    status_code=400,
                    detail=f"Объект «{file_in.key}» не найден в бакете.",
                )
            if kind_of(content_type) == "image":
                raw = yandex.get_bytes(file_in.key)
                if raw:
                    yandex.save_thumb(file_in.key, raw)
            row = await repo.add(
                object_key=file_in.key,
                content_type=content_type,
                size_bytes=file_in.size_bytes,
                uploaded_at=now,
                client_ip=ip,
            )
            if row:
                saved.append(
                    UploadedPhoto(
                        key=file_in.key,
                        content_type=content_type,
                        size_bytes=file_in.size_bytes,
                        url=yandex.photo_url(file_in.key, base),
                        kind=kind_of(content_type),
                        thumb_url=(
                            yandex.thumb_url(file_in.key, base)
                            if kind_of(content_type) == "image"
                            else None
                        ),
                    )
                )
        return ConfirmResponse(ok=True, saved=saved)

    @api.get("/api/photos", response_model=PhotoListResponse)
    async def list_photos(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        limiter: Annotated[RateLimiter, Depends(get_limiter)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
        limit: int = 48,
        offset: int = 0,
        kind: str = "all",
    ) -> PhotoListResponse:
        enforce_rate_limit(request, limiter)
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if kind not in ("all", "image", "video"):
            kind = "all"
        rows = await repo.list_recent(
            limit=limit, offset=offset, hidden=False, kind=kind
        )
        total = await repo.count(hidden=False, kind=kind)
        base = settings.base_path
        items = [_build_photo(row, storage, base) for row in rows]
        return PhotoListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )

    @api.get("/api/admin/photos", response_model=AdminListResponse)
    async def admin_list_photos(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
        limit: int = 48,
        offset: int = 0,
        kind: str = "all",
        hidden: bool | None = None,
    ) -> AdminListResponse:
        require_admin(request, settings)
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if kind not in ("all", "image", "video"):
            kind = "all"
        rows = await repo.list_recent(limit=limit, offset=offset, hidden=hidden, kind=kind)
        total = await repo.count(hidden=hidden, kind=kind)
        base = settings.base_path
        items = [_build_photo(row, storage, base, admin=True) for row in rows]
        return AdminListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )

    @api.patch("/api/admin/photos/{photo_id}", response_model=AdminPhotoOut)
    async def admin_set_hidden(
        photo_id: int,
        body: PhotoPatchRequest,
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> AdminPhotoOut:
        require_admin(request, settings)
        row = await repo.set_hidden(photo_id, body.hidden)
        if row is None:
            raise HTTPException(status_code=404, detail="Фото не найдено.")
        return AdminPhotoOut(**_build_photo(row, storage, settings.base_path, admin=True))

    @api.delete("/api/admin/photos/{photo_id}", response_model=DeleteResponse)
    async def admin_delete_photo(
        photo_id: int,
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> DeleteResponse:
        require_admin(request, settings)
        row = await repo.get_by_id(photo_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Фото не найдено.")
        key = row["object_key"]
        storage.delete(key)
        if kind_of(row["content_type"]) == "image":
            storage.delete(storage.thumb_key(key))
        await repo.delete(photo_id)
        return DeleteResponse(ok=True)

    @api.get("/api/files/{key:path}")
    async def serve_file(
        key: str,
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> FileResponse:
        local = _require_local(storage)
        if not (key.startswith("uploads/") or key.startswith("thumbs/")):
            raise HTTPException(status_code=400, detail="Недопустимый ключ.")
        try:
            path = local.absolute_path(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Файл не найден.")

        if key.startswith("thumbs/"):
            return FileResponse(path, media_type="image/jpeg")

        meta = await repo.get_by_key(key)
        content_type = (meta or {}).get("content_type") or "application/octet-stream"
        return FileResponse(path, media_type=content_type)

    settings = get_settings()
    frontend = _frontend_dir(settings)
    if frontend is not None:
        api.mount(
            "/",
            StaticFiles(directory=str(frontend), html=True),
            name="frontend",
        )


def create_app() -> FastAPI:
    """ASGI entry: wrap with BASE_PATH mount when deployed under a subpath."""
    settings = get_settings()
    base = settings.base_path

    if not base:
        api = FastAPI(
            title="Wedding Photo Upload",
            version="2.0.0",
            docs_url="/api/docs",
            lifespan=lifespan,
        )
        api.add_middleware(
            CORSMiddleware,
            allow_origins=settings.resolved_cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )
        _register_routes(api)
        return api

    inner = FastAPI(
        title="Wedding Photo Upload",
        version="2.0.0",
        docs_url="/api/docs",
    )
    inner.add_middleware(
        CORSMiddleware,
        allow_origins=settings.resolved_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    _register_routes(inner)

    root = FastAPI(title="Wedding Photo Upload (root)", lifespan=lifespan)

    @root.get(base)
    async def redirect_to_slash() -> RedirectResponse:
        return RedirectResponse(url=f"{base}/", status_code=308)

    root.mount(base, inner)
    return root


app = create_app()
