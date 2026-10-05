"""Background maintenance: SQLite backup, orphan-object cleanup, bucket versioning.

These are best-effort safety nets, not critical paths — a failure logs and is
skipped rather than taking the service down.
"""

from __future__ import annotations

import asyncio
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


# Even the ledger can't justify a mass delete in one go; the rest waits for the
# next pass and the warning shows up in the log.
MAX_ORPHANS_PER_PASS = 200


async def cleanup_orphans(
    repo: PhotoRepository,
    storage,
    age_hours: int,
    max_per_pass: int = MAX_ORPHANS_PER_PASS,
) -> int:
    """Delete uploads the app handed out but that were never confirmed.

    Only keys from the ``unconfirmed_uploads`` ledger older than ``age_hours``
    are touched. Objects the app never issued, or whose rows exist, are never
    deleted — so a lost or rolled-back DB can't wipe the gallery.
    Returns the number of objects removed.
    """
    if age_hours <= 0:
        return 0
    cutoff = _cutoff(age_hours).isoformat()
    keys = await repo.stale_unconfirmed(cutoff, limit=max_per_pass)
    if not keys:
        return 0
    if len(keys) >= max_per_pass:
        logger.warning(
            "Неподтверждённых загрузок больше %s — удаляю первые, остальные в следующий проход",
            max_per_pass,
        )
    # Blocking I/O (filesystem or boto3) — keep it off the event loop.
    gone = await asyncio.to_thread(_delete_unconfirmed, storage, keys)
    await repo.untrack_unconfirmed(gone)
    if gone:
        logger.info("Удалено неподтверждённых загрузок: %s", len(gone))
    return len(gone)


def _delete_unconfirmed(storage, keys: list[str]) -> list[str]:
    """Delete each key if present; return the keys that are now gone."""
    gone: list[str] = []
    for key in keys:
        if isinstance(storage, LocalStorage):
            if storage.exists(key) and not storage.delete(key):
                continue
            # A crash between saving and the DB insert may have left derivatives.
            for derived in (
                storage.thumb_key(key),
                storage.display_key(key),
                storage.poster_key(key),
            ):
                if storage.exists(derived):
                    storage.delete(derived)
            gone.append(key)
        elif isinstance(storage, YandexStorage):
            # Never uploaded at all — nothing to delete (and with versioning a
            # delete of a missing key would only leave a stray delete marker).
            if storage.object_exists(key) and not storage.delete(key):
                continue  # delete failed and was logged; retry next pass
            gone.append(key)
    return gone


def ensure_bucket_versioning(storage) -> bool:
    """Enable S3 versioning once (yandex). Without it, delete is irreversible."""
    if not isinstance(storage, YandexStorage):
        return False
    if storage.settings.s3_mock:
        return False
    client = storage._get_client()
    bucket = storage.settings.s3_bucket
    try:
        # Usually enabled once in the console; reading it needs fewer rights
        # than PutBucketVersioning, so don't log AccessDenied on every pass.
        if client.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled":
            return True
        client.put_bucket_versioning(
            Bucket=bucket,
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
        await asyncio.to_thread(ensure_bucket_versioning, storage)
    await cleanup_orphans(repo, storage, settings.orphan_max_age_hours)
    if settings.backup_hours > 0:
        await asyncio.to_thread(backup_sqlite, settings)


async def maintenance_loop(
    repo: PhotoRepository, storage, settings: Settings
) -> None:
    """Background task: run periodically until the event loop is cancelled."""
    interval = settings.backup_hours * 3600
    if interval <= 0:
        interval = 3600  # still allow orphan cleanup / versioning on a sane cadence
    while True:
        try:
            await run_once(repo, storage, settings)
        except Exception:
            logger.exception("Сбой фонового обслуживания")
        await asyncio.sleep(interval)