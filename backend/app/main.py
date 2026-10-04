"""FastAPI app: local multipart and/or Yandex presigned upload + gallery."""

from __future__ import annotations

import asyncio
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
from app.filetype import SNIFF_BYTES, content_matches
from app.rate_limit import RateLimiter
from app.thumbnails import image_dimensions, sanitize_original
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
_admin_limiter: RateLimiter | None = None
_storage: Storage | None = None

logger = logging.getLogger("wedding")
# uvicorn configures only its own loggers; without a handler our INFO lines are
# dropped and errors print without context. Children (wedding.storage, …) inherit it.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


async def _backfill_derived(repo: PhotoRepository, storage: Storage) -> None:
    """Generate missing thumbnails + display JPEGs (and store dimensions).

    Runs once at startup so pre-existing photos get derived images too.
    Yandex downloads each missing image once (skipped in mock mode).
    """
    try:
        rows = await repo.list_all(kind="image")
    except Exception:
        logger.exception("Не удалось получить список фото для генерации производных изображений")
        return

    def _process_one(row: dict[str, Any]) -> tuple[bool, tuple[int, int] | None]:
        key = row["object_key"]
        try:
            if storage.exists(storage.thumb_key(key)) and storage.exists(
                storage.display_key(key)
            ):
                return False, None
        except ValueError:
            return False, None
        raw = None
        if isinstance(storage, LocalStorage):
            try:
                path = storage.absolute_path(key)
            except ValueError:
                return False, None
            if not path.is_file():
                return False, None
            raw = path.read_bytes()
        else:
            if storage.settings.s3_mock:
                return False, None
            raw = storage.get_bytes(key)
            if raw is None:
                return False, None
        if raw is None:
            return False, None
        storage.save_thumb(key, raw)
        storage.save_display(key, raw)
        return True, image_dimensions(raw)

    updated = 0
    for row in rows:
        try:
            done, dims = await asyncio.to_thread(_process_one, row)
            if done:
                updated += 1
                if dims:
                    w, h = dims
                    await repo.update_dimensions(
                        row["id"],
                        thumb_width=w,
                        thumb_height=h,
                        display_width=w,
                        display_height=h,
                    )
        except Exception:
            logger.exception("Не удалось обработать %s", row.get("object_key"))
            continue
    if updated:
        logger.info("Сгенерированы производные изображения для %s фото", updated)


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
    global _repo, _limiter, _admin_limiter, _storage
    settings = get_settings()
    _repo = PhotoRepository(resolve_db_path(settings.database_path))
    await _repo.init()
    _limiter = RateLimiter(settings.rate_limit_per_minute)
    # Separate, tight fixed limiter for the admin panel (brute-force protection).
    _admin_limiter = RateLimiter(limit_per_minute=10)
    _storage = create_storage(settings)
    _storage.ensure_ready()
    await _backfill_derived(_repo, _storage)
    yield


def get_repo() -> PhotoRepository:
    if _repo is None:
        raise RuntimeError("repository not initialized")
    return _repo


def get_limiter() -> RateLimiter:
    if _limiter is None:
        raise RuntimeError("limiter not initialized")
    return _limiter


def get_admin_limiter() -> RateLimiter:
    if _admin_limiter is None:
        raise RuntimeError("admin limiter not initialized")
    return _admin_limiter


def get_storage() -> Storage:
    if _storage is None:
        raise RuntimeError("storage not initialized")
    return _storage


def client_ip(request: Request, trusted_proxies: int = 1) -> str:
    """Real client IP, taking X-Forwarded-For from the right.

    Each trusted reverse proxy appends the peer IP it saw, so the rightmost
    ``trusted_proxies`` entries are real hops; the entry just left of them is
    the actual client. Leftmost entries are attacker-controlled and ignored.
    """
    if trusted_proxies > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            idx = len(parts) - trusted_proxies
            if idx >= 0:
                candidate = parts[idx]
                if candidate and candidate.lower() != "unknown":
                    return candidate
    if request.client:
        return request.client.host
    return "unknown"


_GUEST_NAME_MAX = 80


def _guest_name(request: Request) -> str:
    """Read the remembered guest name from the cookie, sanitized and length-capped."""
    raw = (request.cookies.get("guest_name") or "").strip()
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n\t")
    return cleaned[:_GUEST_NAME_MAX]


def enforce_rate_limit(request: Request, limiter: RateLimiter, trusted_proxies: int) -> None:
    allowed, _remaining = limiter.allow(client_ip(request, trusted_proxies))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                "Слишком много запросов с вашего IP. "
                "Подождите минуту и попробуйте снова."
            ),
        )


def _not_media_detail(name: str | None) -> str:
    subject = f"«{name}»" if name else "Файл"
    return (
        f"{subject} не похож на фото или видео. "
        "Нужны JPEG, PNG, WebP, HEIC, MP4, WebM или MOV."
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
    """Reject admin calls unless the correct password is presented as a bearer token.

    Also rate-limits per (real) IP so the password can't be brute-forced quickly.
    """
    password = settings.admin_password
    if not password:
        raise HTTPException(
            status_code=503,
            detail="Админка отключена. Задайте ADMIN_PASSWORD в .env.",
        )
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token, password):
        # Count failed attempts against the tight admin limiter too.
        enforce_admin_rate_limit(request, settings)
        raise HTTPException(status_code=401, detail="Неверный пароль администратора.")
    enforce_admin_rate_limit(request, settings)


def enforce_admin_rate_limit(request: Request, settings: Settings) -> None:
    limiter = get_admin_limiter()
    allowed, _remaining = limiter.allow(client_ip(request, settings.trusted_proxies))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток входа. Подождите минуту.",
        )


def _thumb_url_for(row: dict[str, Any], storage: Storage, base: str) -> str | None:
    if kind_of(row["content_type"]) == "image":
        return storage.thumb_url(row["object_key"], base)
    return None


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
    payload: dict[str, Any] = {
        "id": row["id"],
        "key": key,
        "content_type": content_type,
        "kind": kind,
        "size_bytes": row["size_bytes"],
        "uploaded_at": row["uploaded_at"],
        "url": storage.photo_url(key, base),
        "thumb_url": _thumb_url_for(row, storage, base),
        "thumb_width": row.get("thumb_width"),
        "thumb_height": row.get("thumb_height"),
        "display_url": (
            storage.display_url(key, base)
            if kind == "image"
            else None
        ),
    }
    if admin:
        payload["client_ip"] = row.get("client_ip", "")
        payload["guest_name"] = row.get("guest_name", "")
        payload["hidden"] = bool(row.get("hidden", 0))
    return payload


def _require_local(storage: Storage) -> LocalStorage:
    if not isinstance(storage, LocalStorage):
        raise HTTPException(
            status_code=400,
            detail="Этот эндпоинт доступен только при STORAGE_BACKEND=local.",
        )
    return storage


_EXIF_BEARING_IMAGE_TYPES = frozenset({"image/jpeg", "image/heic", "image/heif"})


def _process_local_image(
    local: LocalStorage,
    key: str,
    data: bytes,
    content_type: str,
    strip_exif: bool,
) -> tuple[bytes, tuple[int, int] | None]:
    """Persist original + thumb + display. Returns bytes, dims.

    EXIF/GPS is stripped only from formats that actually carry it (JPEG/HEIC);
    PNG/WebP originals are kept byte-for-byte.
    """
    if strip_exif and content_type in _EXIF_BEARING_IMAGE_TYPES:
        cleaned = sanitize_original(data, content_type)
        if cleaned:
            data = cleaned
    local.save_bytes(key, data)
    local.save_thumb(key, data)
    local.save_display(key, data)
    return data, image_dimensions(data)


def _process_yandex_upload(
    yandex: YandexStorage,
    key: str,
    content_type: str,
    strip_exif: bool,
    sniff_bytes: int,
) -> dict[str, Any]:
    """Sniff + derive images/videos for a confirmed bucket object (blocking, run in thread).

    Downloads each image exactly once; the original is sanitized (EXIF stripped)
    and re-uploaded. Video size is taken from the server, not the client.
    """
    is_img = kind_of(content_type) == "image"
    if not yandex.settings.s3_mock:
        head = yandex.get_head(key, sniff_bytes)
        if head is None:
            return {"ok": False, "reason": "notfound"}
        if not content_matches(content_type, head):
            yandex.delete(key)
            return {"ok": False, "reason": "mismatch"}

    if is_img:
        raw = yandex.get_bytes(key)
        if not raw and not yandex.settings.s3_mock:
            return {"ok": False, "reason": "unreadable"}
        if (
            raw
            and strip_exif
            and content_type in _EXIF_BEARING_IMAGE_TYPES
        ):
            cleaned = sanitize_original(raw, content_type)
            if cleaned and cleaned != raw:
                yandex.put_bytes(key, cleaned, content_type)
                raw = cleaned
        if raw:
            yandex.save_thumb(key, raw)
            yandex.save_display(key, raw)
        return {
            "ok": True,
            "kind": "image",
            "size": len(raw) if raw else None,
            "dims": image_dimensions(raw) if raw else None,
        }

    size = yandex.stat(key) if not yandex.settings.s3_mock else None
    return {"ok": True, "kind": "video", "size": size}


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
        enforce_rate_limit(request, limiter, settings.trusted_proxies)

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
        ip = client_ip(request, settings.trusted_proxies)
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
            if not content_matches(content_type, data[:SNIFF_BYTES]):
                logger.warning(
                    "Отклонён %r: содержимое не совпадает с типом %s",
                    upload.filename,
                    content_type,
                )
                raise HTTPException(
                    status_code=400,
                    detail=_not_media_detail(upload.filename),
                )

            key = build_object_key(content_type)
            dims: tuple[int, int] | None = None
            if kind_of(content_type) == "image":
                data, dims = await asyncio.to_thread(
                    _process_local_image,
                    local,
                    key,
                    data,
                    content_type,
                    settings.strip_exif,
                )
            else:
                local.save_bytes(key, data)
            row = await repo.add(
                object_key=key,
                content_type=content_type,
                size_bytes=len(data),
                uploaded_at=now,
                client_ip=ip,
                guest_name=_guest_name(request),
                thumb_width=dims[0] if dims else None,
                thumb_height=dims[1] if dims else None,
                display_width=dims[0] if dims else None,
                display_height=dims[1] if dims else None,
            )
            if row:
                saved.append(
                    UploadedPhoto(
                        key=key,
                        content_type=content_type,
                        size_bytes=len(data),
                        url=local.photo_url(key, base),
                        kind=kind_of(content_type),
                        thumb_url=(
                            local.thumb_url(key, base)
                            if kind_of(content_type) == "image"
                            else None
                        ),
                        display_url=(
                            local.display_url(key, base)
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
        enforce_rate_limit(request, limiter, settings.trusted_proxies)

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
        enforce_rate_limit(request, limiter, settings.trusted_proxies)

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
        ip = client_ip(request, settings.trusted_proxies)
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

            result = await asyncio.to_thread(
                _process_yandex_upload,
                yandex,
                file_in.key,
                content_type,
                settings.strip_exif,
                SNIFF_BYTES,
            )
            if not result["ok"]:
                reason = result["reason"]
                if reason == "notfound":
                    raise HTTPException(
                        status_code=400,
                        detail=f"Объект «{file_in.key}» не найден в бакете.",
                    )
                if reason == "mismatch":
                    raise HTTPException(status_code=400, detail=_not_media_detail(None))
                raise HTTPException(status_code=400, detail="Не удалось прочитать объект.")

            size_bytes = result.get("size") or file_in.size_bytes
            dims = result.get("dims")
            row = await repo.add(
                object_key=file_in.key,
                content_type=content_type,
                size_bytes=size_bytes,
                uploaded_at=now,
                client_ip=ip,
                guest_name=_guest_name(request),
                thumb_width=dims[0] if dims else None,
                thumb_height=dims[1] if dims else None,
                display_width=dims[0] if dims else None,
                display_height=dims[1] if dims else None,
            )
            if row:
                saved.append(
                    UploadedPhoto(
                        key=file_in.key,
                        content_type=content_type,
                        size_bytes=size_bytes,
                        url=yandex.photo_url(file_in.key, base),
                        kind=kind_of(content_type),
                        thumb_url=(
                            yandex.thumb_url(file_in.key, base)
                            if kind_of(content_type) == "image"
                            else None
                        ),
                        display_url=(
                            yandex.display_url(file_in.key, base)
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
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
        limit: int = 48,
        offset: int = 0,
        kind: str = "all",
    ) -> PhotoListResponse:
        # Public gallery is deliberately NOT rate-limited: guests on one network
        # shouldn't be blocked, and the endpoint is cheap (metadata only).
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
        is_img = kind_of(row["content_type"]) == "image"
        # Delete originals/derivatives; only drop the DB row if the object is gone.
        deleted = await asyncio.to_thread(storage.delete, key)
        if is_img:
            await asyncio.to_thread(storage.delete, storage.thumb_key(key))
            await asyncio.to_thread(storage.delete, storage.display_key(key))
        if not deleted and storage.is_ready():
            raise HTTPException(
                status_code=500,
                detail="Не удалось удалить файл из хранилища. Запись сохранена.",
            )
        await repo.delete(photo_id)
        return DeleteResponse(ok=True)

    @api.get("/api/files/{key:path}")
    async def serve_file(
        key: str,
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> FileResponse:
        local = _require_local(storage)
        prefix = key.split("/", 1)[0]
        if prefix not in ("uploads", "thumbs", "display"):
            raise HTTPException(status_code=400, detail="Недопустимый ключ.")
        try:
            path = local.absolute_path(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Файл не найден.")

        # Derived images are always public (EXIF-free). Originals are withheld
        # for hidden photos so "hide" actually removes direct-link access.
        if prefix in ("thumbs", "display"):
            return FileResponse(path, media_type="image/jpeg")

        meta = await repo.get_by_key(key)
        if not meta:
            raise HTTPException(status_code=404, detail="Файл не найден.")
        if meta.get("hidden"):
            raise HTTPException(status_code=404, detail="Файл скрыт.")
        content_type = meta.get("content_type") or "application/octet-stream"
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
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
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
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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
