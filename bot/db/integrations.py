"""Repository: github_subscriptions, github_poll_state, gitlab_subscriptions, gitlab_poll_state tables."""

from __future__ import annotations

import aiosqlite


class IntegrationsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ── GitHub subscriptions ──────────────────────────────────────────

    async def add_github_subscription(
        self, guild_id: int, channel_id: int, repo: str, events: str, added_by: int
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO github_subscriptions (guild_id, channel_id, repo, events, added_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, repo, events, added_by),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_github_subscription_events(
        self, guild_id: int, channel_id: int, repo: str, events: str
    ) -> bool:
        cur = await self._conn.execute(
            "UPDATE github_subscriptions SET events = ? "
            "WHERE guild_id = ? AND channel_id = ? AND repo = ?",
            (events, guild_id, channel_id, repo),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def remove_github_subscription(
        self, guild_id: int, channel_id: int, repo: str
    ) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM github_subscriptions WHERE guild_id = ? AND channel_id = ? AND repo = ?",
            (guild_id, channel_id, repo),
        )
        if cur.rowcount > 0:
            remaining_cur = await self._conn.execute(
                "SELECT COUNT(*) AS c FROM github_subscriptions WHERE repo = ?",
                (repo,),
            )
            remaining = await remaining_cur.fetchone()
            if not remaining or remaining["c"] == 0:
                await self._conn.execute(
                    "DELETE FROM github_poll_state WHERE repo = ?", (repo,)
                )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_github_subscriptions(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM github_subscriptions WHERE guild_id = ? ORDER BY repo",
            (guild_id,),
        )
        return await cur.fetchall()

    async def get_all_github_subscriptions(self):
        cur = await self._conn.execute("SELECT * FROM github_subscriptions")
        return await cur.fetchall()

    # ── GitHub poll state ─────────────────────────────────────────────

    async def get_github_poll_state(self, repo: str, event_type: str):
        cur = await self._conn.execute(
            "SELECT * FROM github_poll_state WHERE repo = ? AND event_type = ?",
            (repo, event_type),
        )
        return await cur.fetchone()

    async def set_github_poll_state(
        self, repo: str, event_type: str, last_id: str | None, etag: str | None
    ) -> None:
        await self._conn.execute(
            "INSERT INTO github_poll_state (repo, event_type, last_id, etag, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(repo, event_type) DO UPDATE SET "
            "last_id = excluded.last_id, etag = excluded.etag, updated_at = excluded.updated_at",
            (repo, event_type, last_id, etag),
        )
        await self._conn.commit()

    # ── GitLab subscriptions ──────────────────────────────────────────

    async def add_gitlab_subscription(
        self, guild_id: int, channel_id: int, project: str, events: str, added_by: int
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO gitlab_subscriptions (guild_id, channel_id, project, events, added_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, project, events, added_by),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_gitlab_subscription_events(
        self, guild_id: int, channel_id: int, project: str, events: str
    ) -> bool:
        cur = await self._conn.execute(
            "UPDATE gitlab_subscriptions SET events = ? "
            "WHERE guild_id = ? AND channel_id = ? AND project = ?",
            (events, guild_id, channel_id, project),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def remove_gitlab_subscription(
        self, guild_id: int, channel_id: int, project: str
    ) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM gitlab_subscriptions WHERE guild_id = ? AND channel_id = ? AND project = ?",
            (guild_id, channel_id, project),
        )
        if cur.rowcount > 0:
            remaining_cur = await self._conn.execute(
                "SELECT COUNT(*) AS c FROM gitlab_subscriptions WHERE project = ?",
                (project,),
            )
            remaining = await remaining_cur.fetchone()
            if not remaining or remaining["c"] == 0:
                await self._conn.execute(
                    "DELETE FROM gitlab_poll_state WHERE project = ?", (project,)
                )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_gitlab_subscriptions(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM gitlab_subscriptions WHERE guild_id = ? ORDER BY project",
            (guild_id,),
        )
        return await cur.fetchall()

    async def get_all_gitlab_subscriptions(self):
        cur = await self._conn.execute("SELECT * FROM gitlab_subscriptions")
        return await cur.fetchall()

    # ── GitLab poll state ─────────────────────────────────────────────

    async def get_gitlab_poll_state(self, project: str, event_type: str):
        cur = await self._conn.execute(
            "SELECT * FROM gitlab_poll_state WHERE project = ? AND event_type = ?",
            (project, event_type),
        )
        return await cur.fetchone()

    async def set_gitlab_poll_state(
        self, project: str, event_type: str, last_id: str | None
    ) -> None:
        await self._conn.execute(
            "INSERT INTO gitlab_poll_state (project, event_type, last_id, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(project, event_type) DO UPDATE SET "
            "last_id = excluded.last_id, updated_at = excluded.updated_at",
            (project, event_type, last_id),
        )
        await self._conn.commit()
