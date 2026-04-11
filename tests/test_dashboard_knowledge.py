from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import dashboard.app as dashboard_app
import dashboard.helpers as dashboard_helpers
from bot.db import _SCHEMA as BOT_SCHEMA
from dashboard.routes.knowledge import router as _knowledge_router  # noqa: F401 – side-effect import

# Expose route handler functions used by tests via the app module namespace.
# The handlers are closures inside dashboard.routes.knowledge.init(), so we
# reach them via the registered routes on the app object.
def _get_route_handler(path: str, method: str = "POST"):
    for route in dashboard_app.app.routes:
        if hasattr(route, "path") and route.path == path:
            if hasattr(route, "methods") and method.upper() in (route.methods or set()):
                return route.endpoint
            if not hasattr(route, "methods"):
                return route.endpoint
    return None

dashboard_app.knowledge_toggle_fact = _get_route_handler("/knowledge/toggle-fact")
dashboard_app.knowledge_repair_crawl_metadata = _get_route_handler("/knowledge/repair-crawl-metadata")
dashboard_app.knowledge_reset = _get_route_handler("/knowledge/reset")
dashboard_app.knowledge_delete_fact = _get_route_handler("/knowledge/delete-fact")

# Expose helper functions that tests call via dashboard_app.*
dashboard_app.get_all_guilds = dashboard_helpers.get_all_guilds
dashboard_app.get_knowledge_entries = dashboard_helpers.get_knowledge_entries
dashboard_app.get_crawl_sources_with_metadata = dashboard_helpers.get_crawl_sources_with_metadata
dashboard_app.upsert_crawled_embedding = dashboard_helpers.upsert_crawled_embedding
dashboard_app.upsert_crawl_source = dashboard_helpers.upsert_crawl_source
dashboard_app.repair_legacy_crawl_metadata = dashboard_helpers.repair_legacy_crawl_metadata
dashboard_app.clear_knowledge_base = dashboard_helpers.clear_knowledge_base
dashboard_app.get_db = dashboard_helpers.get_db
dashboard_app.db_fetchall = dashboard_helpers.db_fetchall
dashboard_app.db_fetchone = dashboard_helpers.db_fetchone
dashboard_app.db_execute = dashboard_helpers.db_execute


class DashboardKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _session(guild_ids: list[int] | None = None, user_id: int = 1001) -> dict:
        return {
            "authenticated": True,
            "discord_user_id": user_id,
            "guild_access_ids": guild_ids or [],
        }

    async def asyncSetUp(self) -> None:
        self._original_db_path = dashboard_helpers.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        dashboard_helpers.DB_PATH = f"{self._tmpdir.name}/test.db"
        dashboard_app.DB_PATH = dashboard_helpers.DB_PATH

        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB,
                model TEXT,
                source_url TEXT,
                qdrant_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, name)
            )
            """
        )
        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                crawled_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, url)
            )
            """
        )

    async def asyncTearDown(self) -> None:
        dashboard_helpers.DB_PATH = self._original_db_path
        dashboard_app.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    async def test_get_knowledge_entries_groups_crawled_chunks_and_keeps_metadata(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO crawl_sources (guild_id, url, title, chunk_count, crawled_at) VALUES (?, ?, ?, ?, ?)",
            (1, "https://docs.example.com/start", "Docs Start", 2, "2026-04-08 12:05:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [1]", "alpha", "embed-v1", "https://docs.example.com/start", "pt-1", "2026-04-08 12:00:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [2]", "beta beta", "embed-v1", "https://docs.example.com/start", "pt-2", "2026-04-08 12:01:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Manual Fact", "manual text", "manual-model", None, "pt-3", "2026-04-08 11:30:00"),
        )

        entries = await dashboard_app.get_knowledge_entries(1)

        self.assertEqual(len(entries), 2)

        crawled = next(entry for entry in entries if entry["source_url"] == "https://docs.example.com/start")
        manual = next(entry for entry in entries if entry["source_url"] is None)

        self.assertEqual(crawled["name"], "Docs Start")
        self.assertEqual(crawled["model"], "embed-v1")
        self.assertEqual(crawled["chunk_count"], 2)
        self.assertEqual(crawled["text_len"], len("alpha") + len("beta beta"))
        self.assertEqual(crawled["created_at"], "2026-04-08 12:00:00")
        self.assertEqual(crawled["is_crawled"], 1)

        self.assertEqual(manual["name"], "Manual Fact")
        self.assertEqual(manual["model"], "manual-model")
        self.assertEqual(manual["chunk_count"], 1)
        self.assertEqual(manual["text_len"], len("manual text"))
        self.assertEqual(manual["created_at"], "2026-04-08 11:30:00")
        self.assertEqual(manual["is_crawled"], 0)

    async def test_get_crawl_sources_with_metadata_exposes_added_at_and_model(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO crawl_sources (guild_id, url, title, chunk_count, crawled_at) VALUES (?, ?, ?, ?, ?)",
            (1, "https://docs.example.com/start", "Docs Start", 2, "2026-04-08 12:05:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [1]", "alpha", "embed-v2", "https://docs.example.com/start", "pt-1", "2026-04-08 12:00:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [2]", "beta", "embed-v2", "https://docs.example.com/start", "pt-2", "2026-04-08 12:01:00"),
        )

        sources = await dashboard_app.get_crawl_sources_with_metadata(1)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["model"], "embed-v2")
        self.assertEqual(sources[0]["added_at"], "2026-04-08 12:00:00")

    async def test_upsert_crawled_embedding_preserves_created_at_and_updates_model(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [1]", "old text", "embed-v1", "https://docs.example.com/start", "pt-old", "2026-04-01 09:00:00"),
        )

        await dashboard_app.upsert_crawled_embedding(
            1,
            "Docs Start [1]",
            "new text",
            "embed-v2",
            "https://docs.example.com/start",
            "pt-new",
        )

        row = await dashboard_app.db_fetchone(
            "SELECT text, model, source_url, qdrant_id, created_at FROM embeddings WHERE guild_id = ? AND name = ?",
            (1, "Docs Start [1]"),
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["text"], "new text")
        self.assertEqual(row["model"], "embed-v2")
        self.assertEqual(row["source_url"], "https://docs.example.com/start")
        self.assertEqual(row["qdrant_id"], "pt-new")
        self.assertEqual(row["created_at"], "2026-04-01 09:00:00")

    async def test_repair_legacy_crawl_metadata_dedupes_and_backfills(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [1]", "same chunk", None, "https://docs.example.com/start", "pt-1", "2026-04-01 09:00:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Different Title [1]", "same chunk", None, "https://docs.example.com/start", "pt-2", "2026-04-01 09:05:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [2]", "second chunk", "embed-v1", "https://docs.example.com/start", "pt-3", "2026-04-01 09:06:00"),
        )

        fake_qdrant = SimpleNamespace(delete_embedding=AsyncMock())

        summary = await dashboard_app.repair_legacy_crawl_metadata(1, qdrant=fake_qdrant)

        self.assertEqual(
            summary,
            {"sources_repaired": 1, "duplicates_removed": 1, "models_filled": 1},
        )
        fake_qdrant.delete_embedding.assert_awaited_once_with(1, "pt-2")

        rows = await dashboard_app.db_fetchall(
            "SELECT name, text, model, created_at FROM embeddings WHERE guild_id = ? ORDER BY created_at",
            (1,),
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "same chunk")
        self.assertEqual(rows[0]["model"], "embed-v1")
        self.assertEqual(rows[1]["model"], "embed-v1")

        crawl_source = await dashboard_app.db_fetchone(
            "SELECT url, title, chunk_count, crawled_at FROM crawl_sources WHERE guild_id = ? AND url = ?",
            (1, "https://docs.example.com/start"),
        )
        self.assertIsNotNone(crawl_source)
        assert crawl_source is not None
        self.assertEqual(crawl_source["title"], "Docs Start")
        self.assertEqual(crawl_source["chunk_count"], 2)
        self.assertEqual(crawl_source["crawled_at"], "2026-04-01 09:06:00")

    async def test_repair_route_redirects_with_summary_params(self) -> None:
        async def fake_repair(guild_id: int, qdrant=None):
            self.assertEqual(guild_id, 1)
            return {"sources_repaired": 2, "duplicates_removed": 3, "models_filled": 4}

        original_repair = dashboard_helpers.repair_legacy_crawl_metadata
        dashboard_helpers.repair_legacy_crawl_metadata = fake_repair
        try:
            request = SimpleNamespace(session=self._session([1]))
            response = await dashboard_app.knowledge_repair_crawl_metadata(request, guild_id=1)
        finally:
            dashboard_helpers.repair_legacy_crawl_metadata = original_repair

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "/knowledge?guild_id=1&tab=crawl&repair=1&sources_repaired=2&duplicates_removed=3&models_filled=4",
        )

    async def test_clear_knowledge_base_removes_embeddings_and_sources(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Manual Fact", "manual text", "manual-model", None, "pt-1", "2026-04-08 11:30:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Docs Start [1]", "alpha", "embed-v1", "https://docs.example.com/start", "pt-2", "2026-04-08 12:00:00"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO crawl_sources (guild_id, url, title, chunk_count, crawled_at) VALUES (?, ?, ?, ?, ?)",
            (1, "https://docs.example.com/start", "Docs Start", 1, "2026-04-08 12:05:00"),
        )

        fake_qdrant = SimpleNamespace(reset_embeddings=AsyncMock())

        summary = await dashboard_app.clear_knowledge_base(1, qdrant=fake_qdrant)

        self.assertEqual(
            summary,
            {"embeddings_cleared": 2, "crawled_chunks_cleared": 1, "sources_cleared": 1},
        )
        fake_qdrant.reset_embeddings.assert_awaited_once_with(1)
        self.assertEqual(
            await dashboard_app.db_fetchone("SELECT COUNT(*) AS c FROM embeddings WHERE guild_id = ?", (1,)),
            {"c": 0},
        )
        self.assertEqual(
            await dashboard_app.db_fetchone("SELECT COUNT(*) AS c FROM crawl_sources WHERE guild_id = ?", (1,)),
            {"c": 0},
        )

    async def test_reset_route_redirects_with_clear_summary_params(self) -> None:
        async def fake_clear(guild_id: int, qdrant=None):
            self.assertEqual(guild_id, 1)
            return {"embeddings_cleared": 5, "crawled_chunks_cleared": 3, "sources_cleared": 2}

        original_clear = dashboard_helpers.clear_knowledge_base
        dashboard_helpers.clear_knowledge_base = fake_clear
        try:
            request = SimpleNamespace(session=self._session([1]))
            response = await dashboard_app.knowledge_reset(request, guild_id=1)
        finally:
            dashboard_helpers.clear_knowledge_base = original_clear

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "/knowledge?guild_id=1&tab=embeddings&cleared=1&embeddings_cleared=5&crawled_chunks_cleared=3&sources_cleared=2",
        )


class DashboardKnowledgeLegacySchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_db_path = dashboard_helpers.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        dashboard_helpers.DB_PATH = f"{self._tmpdir.name}/legacy.db"
        dashboard_app.DB_PATH = dashboard_helpers.DB_PATH

        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB,
                model TEXT,
                source_url TEXT,
                qdrant_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                crawled_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

    async def asyncTearDown(self) -> None:
        dashboard_helpers.DB_PATH = self._original_db_path
        dashboard_app.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    async def test_legacy_schema_crawl_upserts_do_not_require_unique_constraints(self) -> None:
        await dashboard_app.upsert_crawled_embedding(
            1,
            "Docs Start [1]",
            "alpha",
            "embed-v1",
            "https://docs.example.com/start",
            "pt-1",
        )
        await dashboard_app.upsert_crawled_embedding(
            1,
            "Docs Start [1]",
            "alpha updated",
            "embed-v2",
            "https://docs.example.com/start",
            "pt-2",
        )

        rows = await dashboard_app.db_fetchall(
            "SELECT id, text, model, qdrant_id FROM embeddings WHERE guild_id = ? AND name = ? ORDER BY id",
            (1, "Docs Start [1]"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "alpha updated")
        self.assertEqual(rows[0]["model"], "embed-v2")
        self.assertEqual(rows[0]["qdrant_id"], "pt-2")

        await dashboard_app.upsert_crawl_source(
            1,
            "https://docs.example.com/start",
            "Docs Start",
            1,
            "2026-04-08 12:00:00",
        )

        await dashboard_app.upsert_crawl_source(
            1,
            "https://docs.example.com/start",
            "Docs Start Updated",
            2,
            "2026-04-08 12:05:00",
        )

        source_rows = await dashboard_app.db_fetchall(
            "SELECT id, title, chunk_count, crawled_at FROM crawl_sources WHERE guild_id = ? AND url = ? ORDER BY id",
            (1, "https://docs.example.com/start"),
        )
        self.assertEqual(len(source_rows), 1)
        self.assertEqual(source_rows[0]["title"], "Docs Start Updated")
        self.assertEqual(source_rows[0]["chunk_count"], 2)
        self.assertEqual(source_rows[0]["crawled_at"], "2026-04-08 12:05:00")


class DashboardLearnedFactsSyncTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _session(guild_ids: list[int] | None = None, user_id: int = 1001) -> dict:
        return {
            "authenticated": True,
            "discord_user_id": user_id,
            "guild_access_ids": guild_ids or [],
        }

    async def asyncSetUp(self) -> None:
        self._original_db_path = dashboard_helpers.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        dashboard_helpers.DB_PATH = f"{self._tmpdir.name}/facts.db"
        dashboard_app.DB_PATH = dashboard_helpers.DB_PATH

        await dashboard_app.db_execute(
            """
            CREATE TABLE IF NOT EXISTS learned_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                fact TEXT NOT NULL,
                embedding BLOB,
                model TEXT,
                qdrant_id TEXT,
                source TEXT NOT NULL DEFAULT 'conversation',
                confidence REAL NOT NULL DEFAULT 1.0,
                approved INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, fact)
            )
            """
        )

    async def asyncTearDown(self) -> None:
        dashboard_helpers.DB_PATH = self._original_db_path
        dashboard_app.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    async def test_toggle_fact_updates_qdrant_payload(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO learned_facts (guild_id, fact, qdrant_id, source, approved) VALUES (?, ?, ?, ?, ?)",
            (1, "The support queue is triaged by moderators.", "fact-1", "conversation", 0),
        )

        fake_qdrant = SimpleNamespace(set_fact_approved=AsyncMock())
        with patch("bot.qdrant_service.QdrantService", return_value=fake_qdrant):
            request = SimpleNamespace(session=self._session([1]))
            response = await dashboard_app.knowledge_toggle_fact(request, fact_id=1, guild_id=1, approved=1)

        self.assertEqual(response.status_code, 302)
        fake_qdrant.set_fact_approved.assert_awaited_once_with(1, "fact-1", 1)
        row = await dashboard_app.db_fetchone("SELECT approved FROM learned_facts WHERE id = ?", (1,))
        self.assertEqual(row["approved"], 1)

    async def test_delete_fact_removes_qdrant_point(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO learned_facts (guild_id, fact, qdrant_id, source, approved) VALUES (?, ?, ?, ?, ?)",
            (1, "The support queue is triaged by moderators.", "fact-1", "conversation", 0),
        )

        fake_qdrant = SimpleNamespace(delete_fact=AsyncMock())
        with patch("bot.qdrant_service.QdrantService", return_value=fake_qdrant):
            request = SimpleNamespace(session=self._session([1]))
            response = await dashboard_app.knowledge_delete_fact(request, fact_id=1, guild_id=1)

        self.assertEqual(response.status_code, 302)
        fake_qdrant.delete_fact.assert_awaited_once_with(1, "fact-1")
        row = await dashboard_app.db_fetchone("SELECT COUNT(*) AS c FROM learned_facts WHERE id = ?", (1,))
        self.assertEqual(row["c"], 0)


class DashboardGuildListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_db_path = dashboard_helpers.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        dashboard_helpers.DB_PATH = f"{self._tmpdir.name}/guilds.db"
        dashboard_app.DB_PATH = dashboard_helpers.DB_PATH

        db = await dashboard_app.get_db()
        try:
            await db.executescript(BOT_SCHEMA)
            await db.commit()
        finally:
            await db.close()

    async def asyncTearDown(self) -> None:
        dashboard_helpers.DB_PATH = self._original_db_path
        dashboard_app.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    async def test_get_all_guilds_returns_names_and_dedupes_unioned_ids(self) -> None:
        await dashboard_app.db_execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?)",
            (1, "registered", "1"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?)",
            (1, "guild_name", "Alpha Squad"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "Doc A", "alpha", "embed-v1", None, "pt-1"),
        )
        await dashboard_app.db_execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, active) VALUES (?, ?, ?, ?, ?)",
            (2, 10, 11, "Heads up", 1),
        )

        guilds = await dashboard_app.get_all_guilds()

        self.assertEqual(
            guilds,
            [
                {"guild_id": 1, "guild_name": "Alpha Squad"},
                {"guild_id": 2, "guild_name": "Guild 2"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
