"""FastAPI app: local multipart and/or Yandex presigned upload + gallery."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Iterator, Union
from urllib.parse import unquote
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import (
    ALLOWED_CONTENT_TYPES,
    Settings,
    get_settings,
    kind_of,
)
from app.db import PhotoRepository, resolve_db_path
from app.filetype import SNIFF_BYTES, content_matches
from app.maintenance import maintenance_loop
from app.poster import generate_poster_from_path, generate_poster_from_url
from app.rate_limit import RateLimiter
from app.thumbnails import image_dimensions, sanitize_original
from app.schemas import (
    AdminListResponse,
    ArchiveTokenResponse,
    AdminPhotoOut,
    ConfirmRequest,
    ConfirmResponse,
    DeleteResponse,
    GuestsResponse,
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
_maintenance_task: asyncio.Task | None = None

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
    global _repo, _limiter, _admin_limiter, _storage, _maintenance_task
    settings = get_settings()
    _repo = PhotoRepository(resolve_db_path(settings.database_path))
    await _repo.init()
    _limiter = RateLimiter(settings.rate_limit_per_minute)
    # Separate, tight fixed limiter for the admin panel (brute-force protection).
    _admin_limiter = RateLimiter(limit_per_minute=10)
    _storage = create_storage(settings)
    _storage.ensure_ready()
    await _backfill_derived(_repo, _storage)
    _maintenance_task = asyncio.create_task(maintenance_loop(_repo, _storage, settings))
    try:
        yield
    finally:
        if _maintenance_task is not None:
            _maintenance_task.cancel()
            try:
                await _maintenance_task
            except asyncio.CancelledError:
                pass
            _maintenance_task = None


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
    # The frontend stores the name with encodeURIComponent (Cyrillic -> %D0%9F...).
    raw = unquote((request.cookies.get("guest_name") or "").strip())
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

    Only failed attempts count against the per-IP admin limiter, so normal
    moderation (many requests a minute) is never throttled.
    """
    password = settings.admin_password
    if not password:
        raise HTTPException(
            status_code=503,
            detail="Админка отключена. Задайте ADMIN_PASSWORD в .env.",
        )
    limiter = get_admin_limiter()
    ip = client_ip(request, settings.trusted_proxies)
    # Checked before the password: once an IP is blocked even a correct guess
    # gets 429, so the response can't confirm it.
    if limiter.blocked(ip):
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток входа. Подождите минуту.",
        )
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token, password):
        limiter.allow(ip)
        raise HTTPException(status_code=401, detail="Неверный пароль администратора.")


def _thumb_url_for(row: dict[str, Any], storage: Storage, base: str) -> str | None:
    if kind_of(row["content_type"]) == "image":
        return storage.thumb_url(row["object_key"], base)
    return None


def _uses_media_redirect(storage: Storage) -> bool:
    """Signed bucket URLs change on every request, which defeats the browser
    cache and expires open pages. Such files go through /api/media instead."""
    return (
        isinstance(storage, YandexStorage)
        and not storage.settings.public_read
        and not storage.settings.s3_mock
    )


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
    if not admin and _uses_media_redirect(storage):
        media = f"{base}/api/media/{row['id']}"
        return {
            "id": row["id"],
            "key": key,
            "content_type": content_type,
            "kind": kind,
            "size_bytes": row["size_bytes"],
            "uploaded_at": row["uploaded_at"],
            "url": f"{media}/original",
            "thumb_url": f"{media}/thumb" if kind == "image" else None,
            "thumb_width": row.get("thumb_width"),
            "thumb_height": row.get("thumb_height"),
            "display_url": f"{media}/display" if kind == "image" else None,
            "poster_url": f"{media}/poster" if kind == "video" else None,
        }
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
        "poster_url": (
            storage.poster_url(key, base)
            if kind == "video"
            else None
        ),
    }
    if admin:
        payload["client_ip"] = row.get("client_ip", "")
        payload["guest_name"] = row.get("guest_name", "")
        payload["hidden"] = bool(row.get("hidden", 0))
        payload["pending"] = bool(row.get("pending", 0))
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


def _make_local_poster(
    local: LocalStorage, key: str, ffmpeg: str
) -> Path | None:
    """Extract a poster frame for a just-saved local video (best-effort)."""
    try:
        poster = generate_poster_from_path(local.absolute_path(key), ffmpeg)
    except Exception:
        return None
    if not poster:
        return None
    return local.save_poster(key, poster)


def _make_yandex_poster(
    yandex: YandexStorage, key: str, ffmpeg: str
) -> bool:
    """Extract a poster frame from a bucket video (best-effort).

    ffmpeg reads the object over a signed URL with HTTP range requests, so only
    the bytes it needs are fetched — the video is never downloaded whole.
    """
    try:
        poster = generate_poster_from_url(yandex.photo_url(key), ffmpeg)
        if not poster:
            return False
        return yandex.save_poster(key, poster)
    except Exception:
        logger.warning("Не удалось создать постер видео %s", key)
        return False


def _encode_cursor(row: dict[str, Any]) -> str:
    raw = f"{row['uploaded_at']}|{row['id']}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        uploaded_at, photo_id = raw.rsplit("|", 1)
        return uploaded_at, int(photo_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Неверный курсор страницы.") from exc


async def _page(
    repo: PhotoRepository,
    *,
    limit: int,
    offset: int,
    cursor: str | None,
    **filters: Any,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """One page plus has_more/next_cursor. A cursor wins over offset."""
    before = _decode_cursor(cursor) if cursor else None
    rows = await repo.list_recent(
        limit=limit + 1, offset=0 if before else offset, before=before, **filters
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1]) if has_more and rows else None
    return rows, has_more, next_cursor


# Redirects to signed URLs are cached by the browser for at most this long
# (and never longer than half the signature lifetime).
_MEDIA_REDIRECT_MAX_AGE = 6 * 3600

_ARCHIVE_TOKEN_TTL = 300
_ARCHIVE_CHUNK = 1024 * 1024
# One-time download tokens: a plain link can't carry the Authorization header.
_archive_tokens: dict[str, float] = {}


def _issue_archive_token() -> str:
    now = time.monotonic()
    for token, expires in list(_archive_tokens.items()):
        if expires < now:
            _archive_tokens.pop(token, None)
    token = secrets.token_urlsafe(32)
    _archive_tokens[token] = now + _ARCHIVE_TOKEN_TTL
    return token


def _consume_archive_token(token: str) -> bool:
    expires = _archive_tokens.pop(token, None)
    return expires is not None and expires >= time.monotonic()


class _ZipSink(io.RawIOBase):
    """Unseekable sink: ZipFile writes into it, the response drains it."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:  # noqa: ANN001 — bytes-like from zipfile
        self._chunks.append(bytes(data))
        return len(data)

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _zip_time(uploaded_at: str) -> tuple[int, int, int, int, int, int]:
    try:
        moment = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
    except ValueError:
        return (1980, 1, 1, 0, 0, 0)
    return moment.timetuple()[:6] if moment.year >= 1980 else (1980, 1, 1, 0, 0, 0)


def _iter_archive(storage: Storage, rows: list[dict[str, Any]]) -> Iterator[bytes]:
    """Build the ZIP on the fly (stored, no compression) while it is being sent.

    Nothing is buffered beyond one chunk: no temp file, no whole archive in RAM.
    Runs in Starlette's threadpool (sync generator), so blocking reads are fine.
    """
    sink = _ZipSink()
    with ZipFile(sink, "w", compression=ZIP_STORED, allowZip64=True) as zf:
        for row in rows:
            key = row["object_key"]
            try:
                chunks = storage.iter_bytes(key, _ARCHIVE_CHUNK)
            except Exception:
                logger.exception("Не удалось добавить %s в архив", key)
                continue
            info = ZipInfo(key.replace("uploads/", "", 1), _zip_time(row["uploaded_at"]))
            info.compress_type = ZIP_STORED
            big = (row.get("size_bytes") or 0) > 1024 ** 3
            try:
                with zf.open(info, "w", force_zip64=big) as entry:
                    for chunk in chunks:
                        entry.write(chunk)
                        out = sink.drain()
                        if out:
                            yield out
            except Exception:
                # A broken read can't be undone mid-stream; the entry ends short.
                logger.exception("Архив: обрыв чтения %s", key)
            out = sink.drain()
            if out:
                yield out
    yield sink.drain()


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
            # Ledger entry covers a crash between writing the file and the DB row.
            await repo.track_unconfirmed([key], now)
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
                if settings.video_poster:
                    await asyncio.to_thread(_make_local_poster, local, key, settings.ffmpeg_binary)
            row = await repo.add(
                object_key=key,
                content_type=content_type,
                size_bytes=len(data),
                uploaded_at=now,
                client_ip=ip,
                guest_name=_guest_name(request),
                pending=settings.pre_moderation,
                thumb_width=dims[0] if dims else None,
                thumb_height=dims[1] if dims else None,
                display_width=dims[0] if dims else None,
                display_height=dims[1] if dims else None,
            )
            await repo.untrack_unconfirmed([key])
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
                        poster_url=(
                            local.poster_url(key, base)
                            if kind_of(content_type) == "video"
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
        repo: Annotated[PhotoRepository, Depends(get_repo)],
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
        # Only these keys may ever be removed by orphan cleanup (if never confirmed).
        await repo.track_unconfirmed(
            [item.key for item in items], datetime.now(timezone.utc).isoformat()
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
                    # The object was deleted right away; nothing left to clean up.
                    await repo.untrack_unconfirmed([file_in.key])
                    raise HTTPException(status_code=400, detail=_not_media_detail(None))
                raise HTTPException(status_code=400, detail="Не удалось прочитать объект.")

            size_bytes = result.get("size") or file_in.size_bytes
            dims = result.get("dims")
            if (
                result["kind"] == "video"
                and settings.video_poster
                and not yandex.settings.s3_mock
            ):
                await asyncio.to_thread(
                    _make_yandex_poster, yandex, file_in.key, settings.ffmpeg_binary
                )
            row = await repo.add(
                object_key=file_in.key,
                content_type=content_type,
                size_bytes=size_bytes,
                uploaded_at=now,
                client_ip=ip,
                guest_name=_guest_name(request),
                pending=settings.pre_moderation,
                thumb_width=dims[0] if dims else None,
                thumb_height=dims[1] if dims else None,
                display_width=dims[0] if dims else None,
                display_height=dims[1] if dims else None,
            )
            await repo.untrack_unconfirmed([file_in.key])
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
                        poster_url=(
                            yandex.poster_url(file_in.key, base)
                            if kind_of(content_type) == "video"
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
        guest: str | None = None,
        cursor: str | None = None,
    ) -> PhotoListResponse:
        # Public gallery is deliberately NOT rate-limited: guests on one network
        # shouldn't be blocked, and the endpoint is cheap (metadata only).
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if kind not in ("all", "image", "video"):
            kind = "all"
        filters = {"hidden": False, "pending": False, "kind": kind, "guest": guest or None}
        rows, has_more, next_cursor = await _page(
            repo, limit=limit, offset=offset, cursor=cursor, **filters
        )
        total = await repo.count(**filters)
        base = settings.base_path
        items = [_build_photo(row, storage, base) for row in rows]
        return PhotoListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    @api.get("/api/media/{photo_id}/{variant}")
    async def media_redirect(
        photo_id: int,
        variant: str,
        settings: Annotated[Settings, Depends(get_settings)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> RedirectResponse:
        """Stable gallery URL: 302 to a fresh signed URL for the file.

        The redirect itself is cacheable, so the browser keeps reusing one signed
        URL (and the image cached under it) instead of a new one per page load,
        and an open page never ends up with expired links.
        """
        row = await repo.get_by_id(photo_id)
        if row is None or row.get("hidden") or row.get("pending"):
            raise HTTPException(status_code=404, detail="Файл не найден.")
        key = row["object_key"]
        kind = kind_of(row["content_type"])
        if variant == "original":
            target = key
        elif variant == "thumb" and kind == "image":
            target = storage.thumb_key(key)
        elif variant == "display" and kind == "image":
            target = storage.display_key(key)
        elif variant == "poster" and kind == "video":
            target = storage.poster_key(key)
        else:
            raise HTTPException(status_code=404, detail="Файл не найден.")
        max_age = max(60, min(_MEDIA_REDIRECT_MAX_AGE, settings.presign_expires_seconds // 2))
        return RedirectResponse(
            storage.photo_url(target, settings.base_path),
            status_code=302,
            headers={"Cache-Control": f"private, max-age={max_age}"},
        )

    @api.get("/api/photos/guests", response_model=GuestsResponse)
    async def list_guests(
        request: Request,
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> GuestsResponse:
        # Names are public (they already appear in the gallery) and cheap; not
        # rate-limited like the gallery listing.
        return GuestsResponse(guests=await repo.list_guests(approved_only=True))

    @api.get("/api/admin/photos/guests", response_model=GuestsResponse)
    async def admin_list_guests(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> GuestsResponse:
        # Admin sees every uploader's name, including pending / hidden items.
        require_admin(request, settings)
        return GuestsResponse(guests=await repo.list_guests(approved_only=False))

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
        pending: bool | None = None,
        guest: str | None = None,
        cursor: str | None = None,
    ) -> AdminListResponse:
        require_admin(request, settings)
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if kind not in ("all", "image", "video"):
            kind = "all"
        filters = {"hidden": hidden, "pending": pending, "kind": kind, "guest": guest or None}
        rows, has_more, next_cursor = await _page(
            repo, limit=limit, offset=offset, cursor=cursor, **filters
        )
        total = await repo.count(**filters)
        base = settings.base_path
        items = [_build_photo(row, storage, base, admin=True) for row in rows]
        return AdminListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    @api.patch("/api/admin/photos/{photo_id}", response_model=AdminPhotoOut)
    async def admin_update_photo(
        photo_id: int,
        body: PhotoPatchRequest,
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> AdminPhotoOut:
        require_admin(request, settings)
        row = await repo.get_by_id(photo_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Фото не найдено.")
        if body.hidden is not None:
            row = await repo.set_hidden(photo_id, body.hidden)
        if body.pending is not None:
            row = await repo.set_pending(photo_id, body.pending)
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
        kind = kind_of(row["content_type"])
        # Delete originals/derivatives; only drop the DB row if the object is gone.
        deleted = await asyncio.to_thread(storage.delete, key)
        if kind == "image":
            await asyncio.to_thread(storage.delete, storage.thumb_key(key))
            await asyncio.to_thread(storage.delete, storage.display_key(key))
        else:
            await asyncio.to_thread(storage.delete, storage.poster_key(key))
        if not deleted and storage.is_ready():
            raise HTTPException(
                status_code=500,
                detail="Не удалось удалить файл из хранилища. Запись сохранена.",
            )
        await repo.delete(photo_id)
        return DeleteResponse(ok=True)

    @api.post("/api/admin/photos/archive-token", response_model=ArchiveTokenResponse)
    async def admin_archive_token(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> ArchiveTokenResponse:
        """One-time link token, so the browser can download the archive natively."""
        require_admin(request, settings)
        return ArchiveTokenResponse(token=_issue_archive_token(), expires_in=_ARCHIVE_TOKEN_TTL)

    @api.get("/api/admin/photos/archive.zip")
    async def admin_download_archive(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
        token: str | None = None,
    ) -> StreamingResponse:
        """Zip of every visible original — the couple's "all photos" backup.

        Streamed while it is built, so size and proxy timeouts don't matter.
        Auth: one-time ``?token=`` (plain link) or the admin bearer header.
        """
        if token is not None:
            if not _consume_archive_token(token):
                raise HTTPException(status_code=401, detail="Ссылка на архив устарела.")
        else:
            require_admin(request, settings)
        rows = await repo.list_all(hidden=False, pending=False)
        if not rows:
            raise HTTPException(status_code=404, detail="Нет опубликованных фото.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return StreamingResponse(
            _iter_archive(storage, rows),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="wedding-photos-{stamp}.zip"',
                "Cache-Control": "no-store",
                # nginx/NPM: pass bytes through instead of buffering the archive.
                "X-Accel-Buffering": "no",
            },
        )

    @api.get("/api/files/{key:path}")
    async def serve_file(
        key: str,
        storage: Annotated[Storage, Depends(get_storage)],
        repo: Annotated[PhotoRepository, Depends(get_repo)],
    ) -> FileResponse:
        local = _require_local(storage)
        prefix = key.split("/", 1)[0]
        if prefix not in ("uploads", "thumbs", "display", "posters"):
            raise HTTPException(status_code=400, detail="Недопустимый ключ.")
        try:
            path = local.absolute_path(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Файл не найден.")

        # Derived images (thumbs, display, video posters) are always public
        # (EXIF-free, single frame). Originals are withheld for hidden photos so
        # "hide" actually removes direct-link access.
        if prefix in ("thumbs", "display", "posters"):
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
