"""Repository: tickets, ticket_messages tables."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


class TicketsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create_ticket(
        self, guild_id: int, user_id: int, channel_id: int, subject: str | None
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO tickets (guild_id, user_id, channel_id, subject) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, channel_id, subject),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_open_tickets(self, guild_id: int, user_id: int | None = None):
        if user_id:
            cur = await self._conn.execute(
                "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status != 'closed'",
                (guild_id, user_id),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM tickets WHERE guild_id = ? AND status != 'closed'",
                (guild_id,),
            )
        return await cur.fetchall()

    async def close_ticket(self, ticket_id: int) -> None:
        await self._conn.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), ticket_id),
        )
        await self._conn.commit()

    async def claim_ticket(self, ticket_id: int, moderator_id: int) -> None:
        await self._conn.execute(
            "UPDATE tickets SET status = 'claimed', claimed_by = ? WHERE id = ?",
            (moderator_id, ticket_id),
        )
        await self._conn.commit()

    async def add_ticket_message(self, ticket_id: int, user_id: int, content: str) -> None:
        await self._conn.execute(
            "INSERT INTO ticket_messages (ticket_id, user_id, content) VALUES (?, ?, ?)",
            (ticket_id, user_id, content),
        )
        await self._conn.commit()

    async def get_ticket_transcript(self, ticket_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
        return await cur.fetchall()

    async def get_ticket_by_channel(self, channel_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
        )
        return await cur.fetchone()
