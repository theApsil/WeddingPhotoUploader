"""SQLite metadata store for confirmed uploads (gallery listing)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_key TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    client_ip TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0,
    thumb_width INTEGER,
    thumb_height INTEGER,
    display_width INTEGER,
    display_height INTEGER
);
CREATE INDEX IF NOT EXISTS idx_photos_uploaded_at ON photos(uploaded_at DESC, id DESC);
"""


class PhotoRepository:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    async def init(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.executescript(SCHEMA)
            await self._migrate(db)
            await db.commit()

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        """Add columns introduced after the original schema (idempotent)."""
        cursor = await db.execute("PRAGMA table_info(photos)")
        existing = {row[1] for row in await cursor.fetchall()}
        additions = {
            "hidden": "INTEGER NOT NULL DEFAULT 0",
            "thumb_width": "INTEGER",
            "thumb_height": "INTEGER",
            "display_width": "INTEGER",
            "display_height": "INTEGER",
        }
        for name, decl in additions.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE photos ADD COLUMN {name} {decl}")

    async def add(
        self,
        *,
        object_key: str,
        content_type: str,
        size_bytes: int,
        uploaded_at: str,
        client_ip: str,
        thumb_width: int | None = None,
        thumb_height: int | None = None,
        display_width: int | None = None,
        display_height: int | None = None,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT OR IGNORE INTO photos
                    (object_key, content_type, size_bytes, uploaded_at, client_ip,
                     thumb_width, thumb_height, display_width, display_height)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    object_key,
                    content_type,
                    size_bytes,
                    uploaded_at,
                    client_ip,
                    thumb_width,
                    thumb_height,
                    display_width,
                    display_height,
                ),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM photos WHERE object_key = ?",
                (object_key,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def update_dimensions(
        self,
        photo_id: int,
        *,
        thumb_width: int | None,
        thumb_height: int | None,
        display_width: int | None,
        display_height: int | None,
    ) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                UPDATE photos
                SET thumb_width = ?, thumb_height = ?,
                    display_width = ?, display_height = ?
                WHERE id = ?
                """,
                (thumb_width, thumb_height, display_width, display_height, photo_id),
            )
            await db.commit()

    async def get_by_key(self, object_key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM photos WHERE object_key = ?",
                (object_key,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_by_id(self, photo_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM photos WHERE id = ?",
                (photo_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    def _filters(
        self,
        hidden: bool | None = None,
        kind: str = "all",
    ) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if hidden is not None:
            where.append("hidden = ?")
            params.append(1 if hidden else 0)
        if kind == "image":
            where.append("content_type LIKE 'image/%'")
        elif kind == "video":
            where.append("content_type LIKE 'video/%'")
        return where, params

    async def list_recent(
        self,
        limit: int = 100,
        offset: int = 0,
        hidden: bool | None = None,
        kind: str = "all",
    ) -> list[dict[str, Any]]:
        where, params = self._filters(hidden=hidden, kind=kind)
        sql = """
            SELECT id, object_key, content_type, size_bytes, uploaded_at,
                   client_ip, hidden, thumb_width, thumb_height,
                   display_width, display_height
            FROM photos
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY uploaded_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def count(
        self,
        hidden: bool | None = None,
        kind: str = "all",
    ) -> int:
        where, params = self._filters(hidden=hidden, kind=kind)
        sql = "SELECT COUNT(*) AS c FROM photos"
        if where:
            sql += " WHERE " + " AND ".join(where)
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def list_all(self, kind: str = "all") -> list[dict[str, Any]]:
        """All rows (no pagination) — used for one-time thumbnail backfill."""
        where, params = self._filters(kind=kind)
        sql = "SELECT id, object_key, content_type FROM photos"
        if where:
            sql += " WHERE " + " AND ".join(where)
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def set_hidden(self, photo_id: int, hidden: bool) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "UPDATE photos SET hidden = ? WHERE id = ?",
                (1 if hidden else 0, photo_id),
            )
            await db.commit()
        return await self.get_by_id(photo_id)

    async def delete(self, photo_id: int) -> dict[str, Any] | None:
        row = await self.get_by_id(photo_id)
        if row is None:
            return None
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
            await db.commit()
        return row


def resolve_db_path(path: str) -> str:
    """Expand relative paths against process cwd."""
    if os.path.isabs(path):
        return path
    return str(Path.cwd() / path)
