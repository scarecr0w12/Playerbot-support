from __future__ import annotations

import unittest
from types import SimpleNamespace

import dashboard.app as dashboard_app
import dashboard.helpers as dashboard_helpers
from fastapi import HTTPException

# Re-export symbols tests expect on the dashboard_app namespace
dashboard_app.get_all_guilds = dashboard_helpers.get_all_guilds
dashboard_app.get_authorized_guilds = dashboard_helpers.get_authorized_guilds
dashboard_app.BOT_OWNER_DISCORD_ID = dashboard_helpers.BOT_OWNER_DISCORD_ID
dashboard_app.HTTPException = HTTPException

# _crawl_jobs lives in dashboard.routes.knowledge
from dashboard.routes.knowledge import _crawl_jobs
dashboard_app._crawl_jobs = _crawl_jobs

# api_crawl_status handler
def _get_route_handler(path: str, method: str = "GET"):
    for route in dashboard_app.app.routes:
        if hasattr(route, "path") and route.path == path:
            if hasattr(route, "methods") and method.upper() in (route.methods or set()):
                return route.endpoint
    return None

dashboard_app.api_crawl_status = _get_route_handler("/api/crawl/status/{job_id}")


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
        original_get_all_guilds = dashboard_helpers.get_all_guilds

        async def fake_get_all_guilds() -> list[dict[str, int | str]]:
            return [
                {"guild_id": 1, "guild_name": "Guild One"},
                {"guild_id": 2, "guild_name": "Guild Two"},
            ]

        dashboard_helpers.get_all_guilds = fake_get_all_guilds
        try:
            request = SimpleNamespace(session=self._session([2]))
            guilds = await dashboard_helpers.get_authorized_guilds(request)
        finally:
            dashboard_helpers.get_all_guilds = original_get_all_guilds

        self.assertEqual(guilds, [{"guild_id": 2, "guild_name": "Guild Two"}])

    async def test_get_authorized_guilds_allows_master_user_to_see_every_guild(self) -> None:
        original_get_all_guilds = dashboard_helpers.get_all_guilds
        original_owner_id = dashboard_helpers.BOT_OWNER_DISCORD_ID

        async def fake_get_all_guilds() -> list[dict[str, int | str]]:
            return [
                {"guild_id": 1, "guild_name": "Guild One"},
                {"guild_id": 2, "guild_name": "Guild Two"},
            ]

        dashboard_helpers.get_all_guilds = fake_get_all_guilds
        dashboard_helpers.BOT_OWNER_DISCORD_ID = 9999
        try:
            request = SimpleNamespace(session=self._session([2], user_id=9999))
            guilds = await dashboard_helpers.get_authorized_guilds(request)
        finally:
            dashboard_helpers.get_all_guilds = original_get_all_guilds
            dashboard_helpers.BOT_OWNER_DISCORD_ID = original_owner_id

        self.assertEqual(len(guilds), 2)
        self.assertEqual([guild["guild_id"] for guild in guilds], [1, 2])

    async def test_get_authorized_guilds_rejects_unknown_guild_for_user(self) -> None:
        original_get_all_guilds = dashboard_helpers.get_all_guilds

        async def fake_get_all_guilds() -> list[dict[str, int | str]]:
            return [
                {"guild_id": 1, "guild_name": "Guild One"},
                {"guild_id": 2, "guild_name": "Guild Two"},
            ]

        dashboard_helpers.get_all_guilds = fake_get_all_guilds
        try:
            request = SimpleNamespace(session=self._session([2]))
            with self.assertRaises(HTTPException) as exc:
                await dashboard_helpers.get_authorized_guilds(request, guild_id=1)
        finally:
            dashboard_helpers.get_all_guilds = original_get_all_guilds

        self.assertEqual(exc.exception.status_code, 403)

    async def test_crawl_status_rejects_different_authenticated_user(self) -> None:
        _crawl_jobs["job-1"] = {
            "status": "running",
            "pages": 1,
            "chunks": 4,
            "error": None,
            "guild_id": 1,
            "user_id": 1001,
        }
        try:
            request = SimpleNamespace(session=self._session([1], user_id=2002))
            with self.assertRaises(HTTPException) as exc:
                await dashboard_app.api_crawl_status(request, "job-1")
        finally:
            _crawl_jobs.clear()

        self.assertEqual(exc.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
