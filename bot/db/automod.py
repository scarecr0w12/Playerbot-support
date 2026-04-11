"""Repository: automod_filters table."""

from __future__ import annotations

import aiosqlite


class AutomodRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def add_filter(self, guild_id: int, filter_type: str, pattern: str) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO automod_filters (guild_id, filter_type, pattern) VALUES (?, ?, ?)",
                (guild_id, filter_type, pattern),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_filter(self, guild_id: int, filter_type: str, pattern: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM automod_filters WHERE guild_id = ? AND filter_type = ? AND pattern = ?",
            (guild_id, filter_type, pattern),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_filters(self, guild_id: int, filter_type: str | None = None):
        if filter_type:
            cur = await self._conn.execute(
                "SELECT * FROM automod_filters WHERE guild_id = ? AND filter_type = ?",
                (guild_id, filter_type),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM automod_filters WHERE guild_id = ?", (guild_id,)
            )
        return await cur.fetchall()
