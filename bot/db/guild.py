"""Repository: guild_config table."""

from __future__ import annotations

import aiosqlite


class GuildRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get_guild_config(self, guild_id: int, key: str) -> str | None:
        cur = await self._conn.execute(
            "SELECT value FROM guild_config WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        row = await cur.fetchone()
        return row["value"] if row else None

    async def get_setting(self, guild_id: int, key: str) -> str:
        from bot.config import DEFAULTS
        val = await self.get_guild_config(guild_id, key)
        return val if val is not None else DEFAULTS.get(key, "")

    async def get_setting_int(self, guild_id: int, key: str) -> int:
        return int(await self.get_setting(guild_id, key))

    async def get_setting_float(self, guild_id: int, key: str) -> float:
        return float(await self.get_setting(guild_id, key))

    async def set_guild_config(self, guild_id: int, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, value),
        )
        await self._conn.commit()
