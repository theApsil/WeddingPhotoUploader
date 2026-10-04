"""Background maintenance: SQLite backup, orphan-object cleanup, bucket versioning.

These are best-effort safety nets, not critical paths — a failure logs and is
skipped rather than taking the service down.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.db import PhotoRepository
from app.storage import LocalStorage, YandexStorage

logger = logging.getLogger("wedding.maintenance")


def backup_sqlite(settings: Settings) -> str | None:
    """Copy the SQLite DB to <db>/backups/photos-<ts>.db, pruning old copies.

    Uses the sqlite3 online backup API so the copy is consistent even while the
    app writes. Returns the backup path, or None when the DB file is absent.
    """
    db_path = Path(settings.database_path).expanduser().resolve()
    if not db_path.is_file():
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"photos-{ts}.db"
    try:
        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception:
        logger.exception("Не удалось создать бэкап SQLite %s", dest)
        return None

    _prune_backups(backup_dir, settings.backup_keep)
    logger.info("SQLite бэкап создан: %s", dest.name)
    return str(dest)


def _prune_backups(backup_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    backups = sorted(backup_dir.glob("photos-*.db"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Не удалось удалить старый бэкап %s", old.name)


def _cutoff(age_hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=age_hours)


async def cleanup_orphans(repo: PhotoRepository, storage, age_hours: int) -> int:
    """Delete stored objects that have no DB row and are older than age_hours.

    This removes the "presign but never confirm" junk: browsers that requested a
    signed upload URL but aborted, leaving orphaned objects in the bucket.
    Returns the number of objects removed.
    """
    if age_hours <= 0:
        return 0
    rows = await repo.list_all()
    known = {r["object_key"] for r in rows}
    cutoff = _cutoff(age_hours)

    if isinstance(storage, LocalStorage):
        return _cleanup_local_orphans(storage, known, cutoff)
    if isinstance(storage, YandexStorage):
        return _cleanup_yandex_orphans(storage, known, cutoff)
    return 0


def _cleanup_local_orphans(
    storage: LocalStorage, known: set[str], cutoff: datetime
) -> int:
    uploads = storage.root / "uploads"
    if not uploads.is_dir():
        return 0
    removed = 0
    for path in uploads.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(storage.root).as_posix()
        if rel in known:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("Не удалось удалить осиротевший объект %s", rel)
    if removed:
        logger.info("Удалено осиротевших локальных объектов: %s", removed)
    return removed


def _cleanup_yandex_orphans(
    storage: YandexStorage, known: set[str], cutoff: datetime
) -> int:
    if storage.settings.s3_mock:
        return 0
    client = storage._get_client()
    bucket = storage.settings.s3_bucket
    paginator = client.get_paginator("list_objects_v2")
    removed = 0
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix="uploads/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key in known:
                    continue
                last = obj.get("LastModified")
                if last is not None and last.replace(tzinfo=timezone.utc) < cutoff:
                    client.delete_object(Bucket=bucket, Key=key)
                    removed += 1
    except Exception:
        logger.exception("Не удалось вычистить осиротевшие объекты в бакете")
        return removed
    if removed:
        logger.info("Удалено осиротевших объектов в бакете: %s", removed)
    return removed


def ensure_bucket_versioning(storage) -> bool:
    """Enable S3 versioning once (yandex). Without it, delete is irreversible."""
    if not isinstance(storage, YandexStorage):
        return False
    if storage.settings.s3_mock:
        return False
    try:
        storage._get_client().put_bucket_versioning(
            Bucket=storage.settings.s3_bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        logger.info("Версионирование бакета включено")
        return True
    except Exception:
        logger.exception("Не удалось включить версионирование бакета")
        return False


async def run_once(repo: PhotoRepository, storage, settings: Settings) -> None:
    """Run a full maintenance pass."""
    if isinstance(storage, YandexStorage) and not storage.settings.s3_mock:
        ensure_bucket_versioning(storage)
    await cleanup_orphans(repo, storage, settings.orphan_max_age_hours)
    if settings.backup_hours > 0:
        backup_sqlite(settings)


async def maintenance_loop(
    repo: PhotoRepository, storage, settings: Settings
) -> None:
    """Background task: run periodically until the event loop is cancelled."""
    interval = settings.backup_hours * 3600
    if interval <= 0:
        interval = 3600  # still allow orphan cleanup / versioning on a sane cadence
    await run_once(repo, storage, settings)
    while True:
        await asyncio_sleep(interval)
        try:
            await run_once(repo, storage, settings)
        except Exception:
            logger.exception("Сбой фонового обслуживания")


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)