"""Repository: mod_cases, warnings, case_notes tables."""

from __future__ import annotations

import aiosqlite


class ModerationRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ── Mod cases ─────────────────────────────────────────────────────

    async def add_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str | None = None,
        duration: int | None = None,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO mod_cases (guild_id, user_id, moderator_id, action, reason, duration) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, action, reason, duration),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_cases(self, guild_id: int, user_id: int | None = None, limit: int = 25):
        if user_id:
            cur = await self._conn.execute(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, user_id, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM mod_cases WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, limit),
            )
        return await cur.fetchall()

    async def get_case_by_id(self, guild_id: int, case_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM mod_cases WHERE id = ? AND guild_id = ?",
            (case_id, guild_id),
        )
        return await cur.fetchone()

    async def update_case_reason(self, guild_id: int, case_id: int, reason: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE mod_cases SET reason = ? WHERE id = ? AND guild_id = ?",
            (reason, case_id, guild_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def count_cases(self, guild_id: int, user_id: int | None = None) -> int:
        if user_id:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        else:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM mod_cases WHERE guild_id = ?",
                (guild_id,),
            )
        row = await cur.fetchone()
        return row[0] if row else 0

    # ── Warnings ──────────────────────────────────────────────────────

    async def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str | None
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, reason),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_active_warnings(self, guild_id: int, user_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY id",
            (guild_id, user_id),
        )
        return await cur.fetchall()

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        cur = await self._conn.execute(
            "UPDATE warnings SET active = 0 WHERE guild_id = ? AND user_id = ? AND active = 1",
            (guild_id, user_id),
        )
        await self._conn.commit()
        return cur.rowcount

    async def delete_warning(self, guild_id: int, warning_id: int) -> bool:
        cur = await self._conn.execute(
            "UPDATE warnings SET active = 0 WHERE id = ? AND guild_id = ? AND active = 1",
            (warning_id, guild_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    # ── Case notes ────────────────────────────────────────────────────

    async def add_note(self, guild_id: int, user_id: int, moderator_id: int, note: str) -> int:
        cur = await self._conn.execute(
            "INSERT INTO case_notes (guild_id, user_id, moderator_id, note) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, note),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_notes(self, guild_id: int, user_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM case_notes WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (guild_id, user_id),
        )
        return await cur.fetchall()

    async def delete_note(self, guild_id: int, note_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM case_notes WHERE id = ? AND guild_id = ?",
            (note_id, guild_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0
