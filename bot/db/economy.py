"""Repository: economy_accounts table."""

from __future__ import annotations

import aiosqlite


class EconomyRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def ensure_account(self, guild_id: int, user_id: int) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO economy_accounts (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        await self._conn.commit()

    async def get_balance(self, guild_id: int, user_id: int) -> int:
        await self.ensure_account(guild_id, user_id)
        cur = await self._conn.execute(
            "SELECT balance FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return row["balance"] if row else 0

    async def set_balance(self, guild_id: int, user_id: int, amount: int) -> None:
        await self.ensure_account(guild_id, user_id)
        await self._conn.execute(
            "UPDATE economy_accounts SET balance = ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, user_id),
        )
        await self._conn.commit()

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        await self.ensure_account(guild_id, user_id)
        await self._conn.execute(
            "UPDATE economy_accounts SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, user_id),
        )
        await self._conn.commit()
        return await self.get_balance(guild_id, user_id)

    async def transfer_balance(
        self, guild_id: int, from_id: int, to_id: int, amount: int
    ) -> bool:
        bal = await self.get_balance(guild_id, from_id)
        if bal < amount:
            return False
        await self.ensure_account(guild_id, to_id)
        await self._conn.execute(
            "UPDATE economy_accounts SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, from_id),
        )
        await self._conn.execute(
            "UPDATE economy_accounts SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, to_id),
        )
        await self._conn.commit()
        return True

    async def get_last_payday(self, guild_id: int, user_id: int) -> str | None:
        cur = await self._conn.execute(
            "SELECT last_payday FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return row["last_payday"] if row else None

    async def set_last_payday(self, guild_id: int, user_id: int, ts: str) -> None:
        await self._conn.execute(
            "UPDATE economy_accounts SET last_payday = ? WHERE guild_id = ? AND user_id = ?",
            (ts, guild_id, user_id),
        )
        await self._conn.commit()

    async def get_leaderboard(self, guild_id: int, limit: int = 10):
        cur = await self._conn.execute(
            "SELECT user_id, balance FROM economy_accounts WHERE guild_id = ? ORDER BY balance DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()
