"""Repository: command_permissions table."""

from __future__ import annotations

import aiosqlite


class PermissionsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def set_command_permission(
        self, guild_id: int, command: str, target_type: str, target_id: int, allowed: bool
    ) -> None:
        await self._conn.execute(
            "INSERT INTO command_permissions (guild_id, command, target_type, target_id, allowed) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, command, target_type, target_id) DO UPDATE SET allowed = excluded.allowed",
            (guild_id, command, target_type, target_id, int(allowed)),
        )
        await self._conn.commit()

    async def remove_command_permission(
        self, guild_id: int, command: str, target_type: str, target_id: int
    ) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM command_permissions WHERE guild_id = ? AND command = ? AND target_type = ? AND target_id = ?",
            (guild_id, command, target_type, target_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_command_permissions(self, guild_id: int, command: str):
        cur = await self._conn.execute(
            "SELECT * FROM command_permissions WHERE guild_id = ? AND command = ?",
            (guild_id, command),
        )
        return await cur.fetchall()

    async def check_command_allowed(
        self, guild_id: int, command: str, user_id: int, channel_id: int, role_ids: list[int]
    ) -> bool | None:
        perms = await self.get_command_permissions(guild_id, command)
        if not perms:
            return None
        for p in perms:
            if p["target_type"] == "user" and p["target_id"] == user_id:
                return bool(p["allowed"])
            if p["target_type"] == "channel" and p["target_id"] == channel_id:
                return bool(p["allowed"])
            if p["target_type"] == "role" and p["target_id"] in role_ids:
                return bool(p["allowed"])
        return None
