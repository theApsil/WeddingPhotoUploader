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
    client_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_photos_uploaded_at ON photos(uploaded_at DESC);
"""


class PhotoRepository:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    async def init(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def add(
        self,
        *,
        object_key: str,
        content_type: str,
        size_bytes: int,
        uploaded_at: str,
        client_ip: str,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT OR IGNORE INTO photos
                    (object_key, content_type, size_bytes, uploaded_at, client_ip)
                VALUES (?, ?, ?, ?, ?)
                """,
                (object_key, content_type, size_bytes, uploaded_at, client_ip),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM photos WHERE object_key = ?",
                (object_key,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def get_by_key(self, object_key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM photos WHERE object_key = ?",
                (object_key,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_recent(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, object_key, content_type, size_bytes, uploaded_at
                FROM photos
                ORDER BY uploaded_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def count(self) -> int:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("SELECT COUNT(*) AS c FROM photos")
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


def resolve_db_path(path: str) -> str:
    """Expand relative paths against process cwd."""
    if os.path.isabs(path):
        return path
    return str(Path.cwd() / path)
