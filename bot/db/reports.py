"""Repository: reports table."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


class ReportsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create_report(
        self, guild_id: int, reporter_id: int, reported_user_id: int, reason: str
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO reports (guild_id, reporter_id, reported_user_id, reason) VALUES (?, ?, ?, ?)",
            (guild_id, reporter_id, reported_user_id, reason),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_open_reports(self, guild_id: int, limit: int = 25):
        cur = await self._conn.execute(
            "SELECT * FROM reports WHERE guild_id = ? AND status = 'open' ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def resolve_report(
        self, report_id: int, resolved_by: int, note: str | None, status: str = "resolved"
    ) -> bool:
        cur = await self._conn.execute(
            "UPDATE reports SET status = ?, resolved_by = ?, resolution_note = ?, resolved_at = ? WHERE id = ?",
            (status, resolved_by, note, datetime.now(timezone.utc).isoformat(), report_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_report(self, report_id: int):
        cur = await self._conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
        return await cur.fetchone()
