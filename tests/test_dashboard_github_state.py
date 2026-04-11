from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

import dashboard.app as dashboard_app
import dashboard.helpers as dashboard_helpers
from fastapi import HTTPException
from dashboard.routes.github_integrations import FORM_STATE_CACHE, _take_form_state, build_review_preview, build_triage_preview

# Re-export symbols the tests expect on dashboard_app
dashboard_app.HTTPException = HTTPException
dashboard_app.db_execute = dashboard_helpers.db_execute
dashboard_app.db_fetchone = dashboard_helpers.db_fetchone
dashboard_app.db_fetchall = dashboard_helpers.db_fetchall


class DashboardGitHubStateTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _session(guild_ids: list[int] | None = None, user_id: int = 1001) -> dict:
        return {
            "authenticated": True,
            "discord_user_id": user_id,
            "guild_access_ids": guild_ids or [],
        }

    async def asyncSetUp(self) -> None:
        self._original_db_path = dashboard_helpers.DB_PATH
        self._original_github_get = dashboard_app.github_integrations._github_get
        self._tmpdir = tempfile.TemporaryDirectory()
        dashboard_helpers.DB_PATH = f"{self._tmpdir.name}/test.db"
        dashboard_app.DB_PATH = dashboard_helpers.DB_PATH
        FORM_STATE_CACHE.clear()

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
        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (guild_id, key)
            )
            """
        )

    async def asyncTearDown(self) -> None:
        dashboard_helpers.DB_PATH = self._original_db_path
        dashboard_app.DB_PATH = self._original_db_path
        dashboard_app.github_integrations._github_get = self._original_github_get
        FORM_STATE_CACHE.clear()
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

        request = SimpleNamespace(session=self._session([1]))
        response = await dashboard_app.integrations_github_reset_state(request, guild_id=1, repo="owner/repo")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/integrations?guild_id=1", response.headers["location"])
        self.assertIn("flash=success", response.headers["location"])
        self.assertTrue(response.headers["location"].endswith("#poll-state"))
        row = await dashboard_app.db_fetchone(
            "SELECT * FROM github_poll_state WHERE repo = ?",
            ("owner/repo",),
        )
        self.assertIsNone(row)

    async def test_reset_route_rejects_repo_not_in_guild(self) -> None:
        request = SimpleNamespace(session=self._session([1]))

        response = await dashboard_app.integrations_github_reset_state(request, guild_id=1, repo="owner/repo")

        self.assertEqual(response.status_code, 302)
        self.assertIn("flash=error", response.headers["location"])
        self.assertIn("poll-state", response.headers["location"])

    async def test_reset_route_rejects_user_without_guild_access(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO github_subscriptions (guild_id, channel_id, repo, events, added_by) VALUES (?, ?, ?, ?, ?)",
            (1, 10, "owner/repo", "push", 0),
        )

        request = SimpleNamespace(session=self._session([2]))

        with self.assertRaises(HTTPException) as exc:
            await dashboard_app.integrations_github_reset_state(request, guild_id=1, repo="owner/repo")

        self.assertEqual(exc.exception.status_code, 403)

    async def test_workflow_save_persists_default_repo_digest_and_templates(self) -> None:
        request = SimpleNamespace(session=self._session([1]))
        dashboard_app.github_integrations._github_get = AsyncMock(return_value=(200, {"number": 7}))

        response = await dashboard_app.integrations_github_workflow_save(
            request,
            guild_id=1,
            default_repo="owner/repo",
            review_digest_channel="12345",
            review_digest_hour_utc="9",
            review_digest_repo="owner/repo",
            review_digest_stale_hours="48",
            issue_default_template="bug",
            issue_template_bug="Bug summary\n\nExpected\n\nActual",
            issue_template_feature="Feature summary",
            issue_template_docs="Docs summary",
            issue_template_labels_bug="bug, backend",
            issue_template_labels_feature="enhancement",
            issue_template_labels_docs="docs",
            issue_template_assignees_bug="octocat, maintainer",
            issue_template_assignees_feature="product-owner",
            issue_template_assignees_docs="docs-maintainer",
            issue_template_milestone_bug="7",
            issue_template_milestone_feature="8",
            issue_template_milestone_docs="9",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/integrations?guild_id=1", response.headers["location"])
        self.assertIn("flash=success", response.headers["location"])
        self.assertTrue(response.headers["location"].endswith("#workflow-settings"))
        rows = await dashboard_app.db_fetchall(
            "SELECT key, value FROM guild_config WHERE guild_id = ? ORDER BY key",
            (1,),
        )
        values = {row["key"]: row["value"] for row in rows}
        self.assertEqual(values["github_default_repo"], "owner/repo")
        self.assertEqual(values["github_review_digest_channel"], "12345")
        self.assertEqual(values["github_review_digest_hour_utc"], "9")
        self.assertEqual(values["github_review_digest_stale_hours"], "48")
        self.assertEqual(values["github_issue_default_template"], "bug")
        self.assertEqual(values["github_issue_template_bug"], "Bug summary\n\nExpected\n\nActual")
        self.assertEqual(values["github_issue_template_labels_bug"], "bug, backend")
        self.assertEqual(values["github_issue_template_assignees_bug"], "octocat, maintainer")
        self.assertEqual(values["github_issue_template_milestone_bug"], "7")
        self.assertEqual(dashboard_app.github_integrations._github_get.await_count, 3)

    async def test_workflow_save_rejects_invalid_default_repo(self) -> None:
        request = SimpleNamespace(session=self._session([1]))

        response = await dashboard_app.integrations_github_workflow_save(
            request,
            guild_id=1,
            default_repo="not a repo",
            review_digest_channel="",
            review_digest_hour_utc="13",
            review_digest_repo="",
            review_digest_stale_hours="24",
            issue_default_template="",
            issue_template_bug="",
            issue_template_feature="",
            issue_template_docs="",
            issue_template_labels_bug="",
            issue_template_labels_feature="",
            issue_template_labels_docs="",
            issue_template_assignees_bug="",
            issue_template_assignees_feature="",
            issue_template_assignees_docs="",
            issue_template_milestone_bug="",
            issue_template_milestone_feature="",
            issue_template_milestone_docs="",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("flash=error", response.headers["location"])
        self.assertTrue(response.headers["location"].endswith("#workflow-settings"))

        parsed = urlparse(response.headers["location"])
        draft_token = parse_qs(parsed.query)["draft"][0]
        draft = _take_form_state(draft_token, 1)

        self.assertIsNotNone(draft)
        self.assertEqual(draft["form_name"], "workflow")
        self.assertEqual(draft["values"]["default_repo"], "not a repo")
        self.assertEqual(draft["values"]["review_digest_hour_utc"], "13")

    async def test_workflow_save_rejects_missing_milestone(self) -> None:
        request = SimpleNamespace(session=self._session([1]))
        dashboard_app.github_integrations._github_get = AsyncMock(return_value=(404, {"message": "Not Found"}))

        response = await dashboard_app.integrations_github_workflow_save(
            request,
            guild_id=1,
            default_repo="owner/repo",
            review_digest_channel="",
            review_digest_hour_utc="13",
            review_digest_repo="",
            review_digest_stale_hours="24",
            issue_default_template="",
            issue_template_bug="",
            issue_template_feature="",
            issue_template_docs="",
            issue_template_labels_bug="",
            issue_template_labels_feature="",
            issue_template_labels_docs="",
            issue_template_assignees_bug="",
            issue_template_assignees_feature="",
            issue_template_assignees_docs="",
            issue_template_milestone_bug="99",
            issue_template_milestone_feature="",
            issue_template_milestone_docs="",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("flash=error", response.headers["location"])
        self.assertIn("milestone+%2399", response.headers["location"])
        self.assertTrue(response.headers["location"].endswith("#workflow-settings"))

    async def test_user_link_save_persists_mapping(self) -> None:
        request = SimpleNamespace(session=self._session([1]))

        response = await dashboard_app.integrations_github_user_link_save(
            request,
            guild_id=1,
            discord_user_id="123456789",
            github_username="octocat",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("flash=success", response.headers["location"])
        self.assertTrue(response.headers["location"].endswith("#reviewer-links"))
        row = await dashboard_app.db_fetchone(
            "SELECT value FROM guild_config WHERE guild_id = ? AND key = ?",
            (1, "github_username_123456789"),
        )
        self.assertEqual(row["value"], "octocat")

    async def test_user_link_save_rejects_invalid_discord_id_with_error_flash(self) -> None:
        request = SimpleNamespace(session=self._session([1]))

        response = await dashboard_app.integrations_github_user_link_save(
            request,
            guild_id=1,
            discord_user_id="abc",
            github_username="octocat",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("flash=error", response.headers["location"])
        self.assertTrue(response.headers["location"].endswith("#reviewer-links"))

    def test_form_state_round_trip_is_single_use_and_guild_scoped(self) -> None:
        token = dashboard_app.github_integrations._redirect_with_form_error(
            1,
            "Test message",
            form_name="subscription",
            values={"repo": "owner/repo", "channel_id": "15", "events": "push"},
            anchor="subscriptions",
        ).headers["location"]

        draft_token = parse_qs(urlparse(token).query)["draft"][0]
        wrong_guild = _take_form_state(draft_token, 2)
        self.assertIsNone(wrong_guild)

        remaining_tokens = list(FORM_STATE_CACHE.keys())
        self.assertEqual(len(remaining_tokens), 0)

    async def test_user_link_delete_removes_mapping(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?)",
            (1, "github_username_123456789", "octocat"),
        )
        request = SimpleNamespace(session=self._session([1]))

        response = await dashboard_app.integrations_github_user_link_delete(
            request,
            guild_id=1,
            discord_user_id="123456789",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("flash=success", response.headers["location"])
        self.assertTrue(response.headers["location"].endswith("#reviewer-links"))
        row = await dashboard_app.db_fetchone(
            "SELECT value FROM guild_config WHERE guild_id = ? AND key = ?",
            (1, "github_username_123456789"),
        )
        self.assertIsNone(row)

    async def test_build_review_preview_summarizes_sections_and_reviewer_load(self) -> None:
        queue = [
            (
                {
                    "number": 10,
                    "title": "Needs review",
                    "html_url": "https://example.com/10",
                    "updated_at": "2026-04-09T10:00:00Z",
                    "user": {"login": "alice"},
                    "requested_reviewers": [{"login": "reviewer1"}],
                    "requested_teams": [{"slug": "backend"}],
                },
                [],
            ),
            (
                {
                    "number": 11,
                    "title": "Approved PR",
                    "html_url": "https://example.com/11",
                    "updated_at": "2026-04-09T11:00:00Z",
                    "user": {"login": "bob"},
                    "requested_reviewers": [],
                },
                [
                    {
                        "user": {"login": "reviewer1"},
                        "state": "APPROVED",
                        "submitted_at": "2026-04-09T11:30:00Z",
                    }
                ],
            ),
        ]

        preview = build_review_preview(queue, stale_hours=24)

        self.assertEqual(preview["total_open"], 2)
        self.assertEqual(preview["sections"][0]["label"], "Needs Review")
        self.assertEqual(preview["reviewer_load"][0]["reviewer"], "reviewer1")
        self.assertEqual(preview["team_load"][0]["team"], "backend")

    async def test_build_triage_preview_summarizes_issue_sections(self) -> None:
        issues = [
            {
                "number": 21,
                "title": "Missing labels",
                "html_url": "https://example.com/21",
                "updated_at": "2026-03-25T10:00:00Z",
                "user": {"login": "alice"},
                "assignees": [],
                "labels": [],
            },
            {
                "number": 22,
                "title": "Assigned issue",
                "html_url": "https://example.com/22",
                "updated_at": "2026-04-09T10:00:00Z",
                "user": {"login": "bob"},
                "assignees": [{"login": "bob"}],
                "labels": [{"name": "bug"}],
            },
        ]

        preview = build_triage_preview(issues, stale_days=7)

        self.assertEqual(preview["total_open"], 2)
        self.assertEqual(preview["counts"]["unassigned"], 1)
        self.assertEqual(preview["counts"]["unlabeled"], 1)
        self.assertEqual(preview["counts"]["stale"], 1)


if __name__ == "__main__":
    unittest.main()