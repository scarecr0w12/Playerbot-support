from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

import dashboard.app as dashboard_app


class DashboardGitHubStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_db_path = dashboard_app.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        dashboard_app.DB_PATH = f"{self._tmpdir.name}/test.db"

        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS github_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                repo TEXT NOT NULL,
                events TEXT NOT NULL,
                added_by INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, channel_id, repo)
            )
            """
        )
        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS github_poll_state (
                repo TEXT NOT NULL,
                event_type TEXT NOT NULL,
                last_id TEXT,
                etag TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (repo, event_type)
            )
            """
        )

    async def asyncTearDown(self) -> None:
        dashboard_app.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    async def test_reset_route_clears_poll_state_for_subscribed_repo(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO github_subscriptions (guild_id, channel_id, repo, events, added_by) VALUES (?, ?, ?, ?, ?)",
            (1, 10, "owner/repo", "push", 0),
        )
        await dashboard_app.db_execute(
            "INSERT INTO github_poll_state (repo, event_type, last_id, etag) VALUES (?, ?, ?, ?)",
            ("owner/repo", "events", "evt_1", 'W/"etag"'),
        )

        request = SimpleNamespace(session={"authenticated": True})
        response = await dashboard_app.integrations_github_reset_state(request, guild_id=1, repo="owner/repo")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/integrations?guild_id=1")
        row = await dashboard_app.db_fetchone(
            "SELECT * FROM github_poll_state WHERE repo = ?",
            ("owner/repo",),
        )
        self.assertIsNone(row)

    async def test_reset_route_rejects_repo_not_in_guild(self) -> None:
        request = SimpleNamespace(session={"authenticated": True})

        with self.assertRaises(dashboard_app.HTTPException) as exc:
            await dashboard_app.integrations_github_reset_state(request, guild_id=1, repo="owner/repo")

        self.assertEqual(exc.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()