"""Repository: mcp_servers table."""

from __future__ import annotations

import aiosqlite


class MCPRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def add_mcp_server(
        self,
        guild_id: int,
        name: str,
        transport: str,
        command: str | None,
        args: str,
        env: str,
        url: str | None,
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO mcp_servers (guild_id, name, transport, command, args, env, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, name, transport, command, args, env, url),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_mcp_server(self, guild_id: int, name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM mcp_servers WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_mcp_servers(self, guild_id: int, enabled_only: bool = False):
        if enabled_only:
            cur = await self._conn.execute(
                "SELECT * FROM mcp_servers WHERE guild_id = ? AND enabled = 1 ORDER BY name",
                (guild_id,),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM mcp_servers WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            )
        return await cur.fetchall()

    async def get_mcp_server(self, guild_id: int, name: str):
        cur = await self._conn.execute(
            "SELECT * FROM mcp_servers WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def toggle_mcp_server(self, guild_id: int, name: str) -> bool | None:
        row = await self.get_mcp_server(guild_id, name)
        if row is None:
            return None
        new_val = 0 if row["enabled"] else 1
        await self._conn.execute(
            "UPDATE mcp_servers SET enabled = ? WHERE guild_id = ? AND name = ?",
            (new_val, guild_id, name),
        )
        await self._conn.commit()
        return bool(new_val)

    async def update_mcp_server(
        self,
        guild_id: int,
        name: str,
        *,
        transport: str | None = None,
        command: str | None = None,
        args: str | None = None,
        env: str | None = None,
        url: str | None = None,
    ) -> bool:
        fields: list[str] = []
        values: list[object] = []
        if transport is not None:
            fields.append("transport = ?")
            values.append(transport)
        if command is not None:
            fields.append("command = ?")
            values.append(command)
        if args is not None:
            fields.append("args = ?")
            values.append(args)
        if env is not None:
            fields.append("env = ?")
            values.append(env)
        if url is not None:
            fields.append("url = ?")
            values.append(url)
        if not fields:
            return False
        values.extend([guild_id, name])
        cur = await self._conn.execute(
            f"UPDATE mcp_servers SET {', '.join(fields)} WHERE guild_id = ? AND name = ?",
            values,
        )
        await self._conn.commit()
        return cur.rowcount > 0
