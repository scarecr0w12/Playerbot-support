from __future__ import annotations

import unittest
from types import SimpleNamespace

import dashboard.app as dashboard_app


class DashboardAuthAccessTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _session(
        guild_ids: list[int] | None = None,
        user_id: int = 1001,
    ) -> dict:
        return {
            "authenticated": True,
            "discord_user_id": user_id,
            "guild_access_ids": guild_ids or [],
        }

    async def test_get_authorized_guilds_filters_non_master_user(self) -> None:
        original_get_all_guilds = dashboard_app.get_all_guilds

        async def fake_get_all_guilds() -> list[dict[str, int | str]]:
            return [
                {"guild_id": 1, "guild_name": "Guild One"},
                {"guild_id": 2, "guild_name": "Guild Two"},
            ]

        dashboard_app.get_all_guilds = fake_get_all_guilds
        try:
            request = SimpleNamespace(session=self._session([2]))
            guilds = await dashboard_app.get_authorized_guilds(request)
        finally:
            dashboard_app.get_all_guilds = original_get_all_guilds

        self.assertEqual(guilds, [{"guild_id": 2, "guild_name": "Guild Two"}])

    async def test_get_authorized_guilds_allows_master_user_to_see_every_guild(self) -> None:
        original_get_all_guilds = dashboard_app.get_all_guilds
        original_owner_id = dashboard_app.BOT_OWNER_DISCORD_ID

        async def fake_get_all_guilds() -> list[dict[str, int | str]]:
            return [
                {"guild_id": 1, "guild_name": "Guild One"},
                {"guild_id": 2, "guild_name": "Guild Two"},
            ]

        dashboard_app.get_all_guilds = fake_get_all_guilds
        dashboard_app.BOT_OWNER_DISCORD_ID = 9999
        try:
            request = SimpleNamespace(session=self._session([2], user_id=9999))
            guilds = await dashboard_app.get_authorized_guilds(request)
        finally:
            dashboard_app.get_all_guilds = original_get_all_guilds
            dashboard_app.BOT_OWNER_DISCORD_ID = original_owner_id

        self.assertEqual(len(guilds), 2)
        self.assertEqual([guild["guild_id"] for guild in guilds], [1, 2])

    async def test_get_authorized_guilds_rejects_unknown_guild_for_user(self) -> None:
        original_get_all_guilds = dashboard_app.get_all_guilds

        async def fake_get_all_guilds() -> list[dict[str, int | str]]:
            return [
                {"guild_id": 1, "guild_name": "Guild One"},
                {"guild_id": 2, "guild_name": "Guild Two"},
            ]

        dashboard_app.get_all_guilds = fake_get_all_guilds
        try:
            request = SimpleNamespace(session=self._session([2]))
            with self.assertRaises(dashboard_app.HTTPException) as exc:
                await dashboard_app.get_authorized_guilds(request, guild_id=1)
        finally:
            dashboard_app.get_all_guilds = original_get_all_guilds

        self.assertEqual(exc.exception.status_code, 403)

    async def test_crawl_status_rejects_different_authenticated_user(self) -> None:
        dashboard_app._crawl_jobs["job-1"] = {
            "status": "running",
            "pages": 1,
            "chunks": 4,
            "error": None,
            "guild_id": 1,
            "user_id": 1001,
        }
        try:
            request = SimpleNamespace(session=self._session([1], user_id=2002))
            with self.assertRaises(dashboard_app.HTTPException) as exc:
                await dashboard_app.api_crawl_status(request, "job-1")
        finally:
            dashboard_app._crawl_jobs.clear()

        self.assertEqual(exc.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
