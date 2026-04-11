"""Repository: custom_commands table."""

from __future__ import annotations

import aiosqlite


class CustomCommandsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def add_custom_command(
        self, guild_id: int, name: str, response: str, creator_id: int
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO custom_commands (guild_id, name, response, creator_id) VALUES (?, ?, ?, ?)",
                (guild_id, name.lower(), response, creator_id),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def edit_custom_command(self, guild_id: int, name: str, response: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE custom_commands SET response = ? WHERE guild_id = ? AND name = ?",
            (response, guild_id, name.lower()),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def delete_custom_command(self, guild_id: int, name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM custom_commands WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_custom_command(self, guild_id: int, name: str):
        cur = await self._conn.execute(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )
        return await cur.fetchone()

    async def list_custom_commands(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT name, creator_id, created_at FROM custom_commands WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()
