from __future__ import annotations

import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bot.db.base as database_module
from bot.social_alert_utils import normalize_twitch_account, normalize_youtube_account
from bot.cogs.social_alerts import (
    SocialAlertsCog,
    _safe_format,
)
from bot.db import Database


class _FakeResponse:
    def __init__(self, *, status: int, json_data=None, text_data: str | None = None) -> None:
        self.status = status
        self._json_data = json_data
        self._text_data = text_data or ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data


class _FakeSession:
    def __init__(self, *, gets=None, posts=None) -> None:
        self._gets = list(gets or [])
        self._posts = list(posts or [])

    def get(self, *args, **kwargs):
        return self._gets.pop(0)

    def post(self, *args, **kwargs):
        return self._posts.pop(0)


class SocialAlertDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_db_path = database_module.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        database_module.DB_PATH = f"{self._tmpdir.name}/test.db"
        self.db = Database()
        await self.db.setup()

    async def asyncTearDown(self) -> None:
        if self.db._db is not None:
            await self.db._db.close()
        database_module.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    async def test_social_alerts_allow_same_account_across_platforms(self) -> None:
        first = await self.db.add_social_alert(1, 10, "twitch", "creator", "stream", "hi")
        second = await self.db.add_social_alert(1, 10, "youtube", "creator", "stream", "hi")

        self.assertTrue(first)
        self.assertTrue(second)


class SocialAlertHelperTests(unittest.TestCase):
    def test_safe_format_keeps_unknown_placeholders(self) -> None:
        rendered = _safe_format("{creator} playing {game} {missing}", {"creator": "Alice", "game": "Chess"})

        self.assertEqual(rendered, "Alice playing Chess {missing}")

    def test_normalize_twitch_account_supports_channel_urls(self) -> None:
        self.assertEqual(normalize_twitch_account("https://www.twitch.tv/ExampleStreamer"), "examplestreamer")

    def test_normalize_youtube_account_supports_handle_urls(self) -> None:
        self.assertEqual(normalize_youtube_account("https://www.youtube.com/@ExampleLive"), "@ExampleLive")


class SocialAlertCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_alert_accepts_sqlite_row(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        alert = conn.execute(
            """
            SELECT
                1 AS id,
                123 AS guild_id,
                456 AS channel_id,
                'rss' AS platform,
                'https://example.com/feed.xml' AS account_id,
                'stream' AS alert_type,
                'Now live: {title} {link}' AS message_template
            """
        ).fetchone()
        self.assertIsNotNone(alert)

        channel = AsyncMock()
        db = MagicMock()
        db.check_alert_history = AsyncMock(return_value=False)
        db.record_alert_history = AsyncMock()

        bot = MagicMock()
        cog = SocialAlertsCog(bot, db, config=SimpleNamespace())
        item = MagicMock()
        item.content_id = "rss:item-1"
        item.platform = "rss"
        item.creator_name = "Creator"
        item.date_text = "2026-04-26T12:00:00Z"
        item.description = "Example description"
        item.game_name = None
        item.link = "https://example.com/posts/1"
        item.thumbnail_url = None
        item.title = "Example title"
        item.viewer_count = None
        item.timestamp = None

        cog._resolve_channel = AsyncMock(return_value=channel)
        cog._fetch_alert_items = AsyncMock(return_value=[item])
        try:
            await cog._process_alert(MagicMock(), alert)
        finally:
            cog.cog_unload()
            conn.close()

        channel.send.assert_awaited_once()
        db.record_alert_history.assert_awaited_once_with(123, 1, "rss:item-1")

    async def test_resolve_channel_fetches_when_not_cached(self) -> None:
        guild = MagicMock()
        guild.get_channel.return_value = None
        channel = AsyncMock()

        bot = MagicMock()
        bot.get_guild.return_value = guild
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(return_value=channel)

        cog = SocialAlertsCog(bot, MagicMock(), config=SimpleNamespace())
        try:
            result = await cog._resolve_channel({"guild_id": 1, "channel_id": 22})
        finally:
            cog.cog_unload()

        self.assertIs(result, channel)
        bot.fetch_channel.assert_awaited_once_with(22)

    async def test_fetch_twitch_items_returns_live_stream(self) -> None:
        bot = MagicMock()
        cog = SocialAlertsCog(
            bot,
            MagicMock(),
            config=SimpleNamespace(twitch_client_id="client", twitch_client_secret="secret", youtube_api_key=None),
        )
        session = _FakeSession(
            posts=[_FakeResponse(status=200, json_data={"access_token": "token", "expires_in": 3600})],
            gets=[
                _FakeResponse(status=200, json_data={"data": [{"id": "u1", "login": "creator", "display_name": "Creator"}]}),
                _FakeResponse(status=200, json_data={"data": [{"id": "s1", "title": "Ranked grind", "started_at": "2026-04-26T12:00:00Z", "game_name": "VALORANT", "viewer_count": 42, "thumbnail_url": "https://img/{width}x{height}.jpg"}]}),
            ],
        )

        try:
            items = await cog._fetch_twitch_items(session, {"account_id": "Creator"})
        finally:
            cog.cog_unload()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content_id, "twitch:s1")
        self.assertEqual(items[0].creator_name, "Creator")
        self.assertEqual(items[0].viewer_count, 42)

    async def test_fetch_youtube_items_returns_live_video(self) -> None:
        bot = MagicMock()
        cog = SocialAlertsCog(
            bot,
            MagicMock(),
            config=SimpleNamespace(twitch_client_id=None, twitch_client_secret=None, youtube_api_key="yt-key"),
        )
        session = _FakeSession(
            gets=[
                _FakeResponse(status=200, json_data={"items": [{"id": "UC123", "snippet": {"title": "Example Live"}}]}),
                _FakeResponse(status=200, json_data={"items": [{"id": {"videoId": "vid123"}, "snippet": {"title": "Going live", "channelTitle": "Example Live", "publishedAt": "2026-04-26T12:05:00Z", "thumbnails": {"high": {"url": "https://img.youtube/high.jpg"}}}}]}),
            ]
        )

        try:
            items = await cog._fetch_youtube_items(session, {"account_id": "@ExampleLive"})
        finally:
            cog.cog_unload()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content_id, "youtube:vid123")
        self.assertEqual(items[0].creator_name, "Example Live")
        self.assertEqual(items[0].thumbnail_url, "https://img.youtube/high.jpg")