"""Social media and stream alerting.

Supports RSS feeds plus live stream announcements for Twitch and YouTube.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.social_alert_utils import (
    default_social_alert_template,
    format_social_alert_platform,
    normalize_twitch_account,
    normalize_youtube_account,
)

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)

RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


class _SafeTemplateValues(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(slots=True)
class AlertItem:
    content_id: str
    title: str
    link: str
    date_text: str = ""
    creator_name: str = ""
    description: str = ""
    platform: str = "rss"
    thumbnail_url: str | None = None
    game_name: str | None = None
    viewer_count: int | None = None
    timestamp: datetime | None = None


def _safe_format(template: str, values: dict[str, Any]) -> str:
    return template.format_map(_SafeTemplateValues(values))


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pick_best_thumbnail(thumbnails: dict[str, dict[str, Any]] | None) -> str | None:
    if not thumbnails:
        return None
    for key in ("maxres", "standard", "high", "medium", "default"):
        candidate = thumbnails.get(key)
        if candidate and candidate.get("url"):
            return str(candidate["url"])
    return None


def _coerce_alert_record(alert: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    if isinstance(alert, dict):
        return alert
    return {key: alert[key] for key in alert.keys()}


class SocialAlertsCog(commands.Cog, name="Social Alerts"):
    """Monitor RSS feeds and live stream providers for new alerts."""

    def __init__(self, bot: commands.Bot, db: Database, config: Any | None = None) -> None:
        self.bot = bot
        self.db = db
        self.config = config or getattr(bot, "config", None)
        self._twitch_access_token: str | None = None
        self._twitch_access_token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._youtube_channel_cache: dict[str, tuple[str, str]] = {}
        self.feed_check_task.start()

    def cog_unload(self) -> None:
        """Clean up tasks when cog is unloaded."""
        self.feed_check_task.cancel()

    # ------------------------------------------------------------------
    # Background task: poll RSS feeds every 15 minutes
    # ------------------------------------------------------------------

    @tasks.loop(minutes=15)
    async def feed_check_task(self) -> None:
        """Poll all enabled RSS feeds and send alerts for new items."""
        alerts = await self.db.get_all_enabled_social_alerts()
        if not alerts:
            return

        async with aiohttp.ClientSession() as session:
            for alert in alerts:
                try:
                    await self._process_alert(session, alert)
                except Exception as e:
                    logger.error("Error processing alert %s: %s", alert["id"], e)

        await self.db.cleanup_alert_history()

    @feed_check_task.before_loop
    async def before_feed_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_alert(self, session: aiohttp.ClientSession, alert: dict[str, Any] | sqlite3.Row) -> None:
        """Fetch a platform target and post new alerts to the configured channel."""
        alert = _coerce_alert_record(alert)
        channel = await self._resolve_channel(alert)
        if channel is None:
            return

        items = await self._fetch_alert_items(session, alert)
        for item in items:
            if await self.db.check_alert_history(alert["id"], item.content_id):
                continue

            message = self._render_message(alert, item)
            embed = self._build_embed(alert, item)
            try:
                await channel.send(message, embed=embed)
                await self.db.record_alert_history(alert["guild_id"], alert["id"], item.content_id)
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning("Could not send alert %s to channel %s: %s", alert["id"], alert["channel_id"], e)
                break

    async def _resolve_channel(self, alert: dict) -> discord.abc.Messageable | None:
        guild = self.bot.get_guild(alert["guild_id"])
        if not guild:
            return None

        channel = guild.get_channel(alert["channel_id"])
        if channel is None:
            channel = self.bot.get_channel(alert["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(alert["channel_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not resolve social alert channel %s: %s", alert["channel_id"], exc)
                return None
        if not hasattr(channel, "send"):
            return None
        return channel

    async def _fetch_alert_items(self, session: aiohttp.ClientSession, alert: dict) -> list[AlertItem]:
        platform = str(alert.get("platform") or "rss").lower()
        if platform == "rss":
            return await self._fetch_rss_items(session, alert)
        if platform == "twitch":
            return await self._fetch_twitch_items(session, alert)
        if platform == "youtube":
            return await self._fetch_youtube_items(session, alert)
        logger.warning("Unsupported social alert platform %s for alert %s", platform, alert.get("id"))
        return []

    async def _fetch_rss_items(self, session: aiohttp.ClientSession, alert: dict) -> list[AlertItem]:
        async with session.get(alert["account_id"], timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("RSS alert %s returned status %s", alert.get("id"), resp.status)
                return []
            content = await resp.text()

        root = ET.fromstring(content)
        entries = root.findall(".//item")
        if not entries:
            entries = root.findall(".//atom:entry", RSS_NS)

        items: list[AlertItem] = []
        for entry in reversed(entries[:10]):
            link = self._rss_link(entry)
            content_id = self._rss_text(entry, "guid") or link
            if not content_id:
                continue

            title = self._rss_text(entry, "title") or "No title"
            date_text = self._rss_text(entry, "pubDate") or self._rss_text(entry, "atom:published") or self._rss_text(entry, "atom:updated") or ""
            creator = self._rss_text(entry, "author") or self._rss_text(entry, "dc:creator") or self._rss_text(entry, "atom:author/atom:name") or ""
            description = self._rss_text(entry, "description") or self._rss_text(entry, "atom:summary") or ""
            items.append(
                AlertItem(
                    content_id=content_id,
                    title=title,
                    link=link,
                    date_text=date_text,
                    creator_name=creator,
                    description=description,
                    platform="rss",
                    thumbnail_url=self._rss_thumbnail(entry),
                    timestamp=_parse_timestamp(date_text),
                )
            )
        return items

    async def _fetch_twitch_items(self, session: aiohttp.ClientSession, alert: dict) -> list[AlertItem]:
        headers = await self._twitch_headers(session)
        if headers is None:
            return []

        account = normalize_twitch_account(alert["account_id"])
        if not account:
            return []

        async with session.get(
            "https://api.twitch.tv/helix/users",
            params={"login": account},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Twitch user lookup for %s returned status %s", account, resp.status)
                return []
            user_payload = await resp.json()

        users = user_payload.get("data") or []
        if not users:
            return []
        user = users[0]

        async with session.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_id": user["id"]},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Twitch stream lookup for %s returned status %s", account, resp.status)
                return []
            stream_payload = await resp.json()

        streams = stream_payload.get("data") or []
        if not streams:
            return []
        stream = streams[0]
        started_at = stream.get("started_at") or ""
        thumbnail = stream.get("thumbnail_url")
        if isinstance(thumbnail, str):
            thumbnail = thumbnail.replace("{width}", "1280").replace("{height}", "720")

        return [
            AlertItem(
                content_id=f"twitch:{stream['id']}",
                title=stream.get("title") or f"{user.get('display_name') or account} is live",
                link=f"https://www.twitch.tv/{user.get('login') or account}",
                date_text=started_at,
                creator_name=user.get("display_name") or user.get("login") or account,
                description=(stream.get("game_name") and f"Playing {stream['game_name']}") or "",
                platform="twitch",
                thumbnail_url=thumbnail,
                game_name=stream.get("game_name"),
                viewer_count=stream.get("viewer_count"),
                timestamp=_parse_timestamp(started_at),
            )
        ]

    async def _fetch_youtube_items(self, session: aiohttp.ClientSession, alert: dict) -> list[AlertItem]:
        api_key = getattr(self.config, "youtube_api_key", None)
        if not api_key:
            logger.warning("YouTube alert requested without YOUTUBE_API_KEY configured")
            return []

        channel_id, fallback_title = await self._resolve_youtube_channel(session, alert["account_id"])
        if not channel_id:
            return []

        async with session.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "channelId": channel_id,
                "eventType": "live",
                "maxResults": 1,
                "order": "date",
                "type": "video",
                "key": api_key,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("YouTube live lookup for %s returned status %s", channel_id, resp.status)
                return []
            payload = await resp.json()

        items = payload.get("items") or []
        if not items:
            return []
        video = items[0]
        snippet = video.get("snippet") or {}
        video_id = (video.get("id") or {}).get("videoId")
        if not video_id:
            return []

        published_at = snippet.get("publishedAt") or ""
        creator = snippet.get("channelTitle") or fallback_title or normalize_youtube_account(alert["account_id"])
        return [
            AlertItem(
                content_id=f"youtube:{video_id}",
                title=snippet.get("title") or f"{creator} is live",
                link=f"https://www.youtube.com/watch?v={video_id}",
                date_text=published_at,
                creator_name=creator,
                description=snippet.get("description") or "",
                platform="youtube",
                thumbnail_url=_pick_best_thumbnail(snippet.get("thumbnails")),
                timestamp=_parse_timestamp(published_at),
            )
        ]

    async def _twitch_headers(self, session: aiohttp.ClientSession) -> dict[str, str] | None:
        client_id = getattr(self.config, "twitch_client_id", None)
        client_secret = getattr(self.config, "twitch_client_secret", None)
        if not client_id or not client_secret:
            logger.warning("Twitch alert requested without TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET configured")
            return None

        now = datetime.now(timezone.utc)
        if self._twitch_access_token and now < self._twitch_access_token_expires_at:
            return {
                "Authorization": f"Bearer {self._twitch_access_token}",
                "Client-Id": client_id,
            }

        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Twitch token request failed with status %s", resp.status)
                return None
            payload = await resp.json()

        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in") or 0)
        if not token:
            return None
        self._twitch_access_token = token
        self._twitch_access_token_expires_at = now + timedelta(seconds=max(expires_in - 60, 60))
        return {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id,
        }

    async def _resolve_youtube_channel(self, session: aiohttp.ClientSession, account: str) -> tuple[str | None, str]:
        api_key = getattr(self.config, "youtube_api_key", None)
        if not api_key:
            return None, ""

        normalized = normalize_youtube_account(account)
        cached = self._youtube_channel_cache.get(normalized)
        if cached is not None:
            return cached

        params = {"part": "snippet", "key": api_key}
        endpoint = "https://www.googleapis.com/youtube/v3/channels"
        if normalized.startswith("UC"):
            params["id"] = normalized
        elif normalized.startswith("@"):
            params["forHandle"] = normalized[1:]
        else:
            endpoint = "https://www.googleapis.com/youtube/v3/search"
            params.update({"q": normalized, "type": "channel", "maxResults": 1})

        async with session.get(
            endpoint,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("YouTube channel lookup for %s returned status %s", normalized, resp.status)
                return None, ""
            payload = await resp.json()

        items = payload.get("items") or []
        if not items:
            return None, ""

        item = items[0]
        if endpoint.endswith("/search"):
            channel_id = (item.get("id") or {}).get("channelId")
        else:
            channel_id = item.get("id")
        snippet = item.get("snippet") or {}
        channel_title = snippet.get("title") or normalized
        result = (channel_id, channel_title)
        if channel_id:
            self._youtube_channel_cache[normalized] = result
        return result

    def _render_message(self, alert: dict, item: AlertItem) -> str:
        template = alert.get("message_template") or default_social_alert_template(item.platform)
        return _safe_format(
            template,
            {
                "account": alert.get("account_id", ""),
                "creator": item.creator_name,
                "date": item.date_text,
                "description": item.description,
                "game": item.game_name or "",
                "link": item.link,
                "platform": item.platform,
                "thumbnail": item.thumbnail_url or "",
                "title": item.title,
                "viewers": item.viewer_count if item.viewer_count is not None else "",
            },
        )

    def _build_embed(self, alert: dict, item: AlertItem) -> discord.Embed:
        color = {
            "rss": discord.Color.blue(),
            "twitch": discord.Color.from_rgb(145, 70, 255),
            "youtube": discord.Color.red(),
        }.get(item.platform, discord.Color.blurple())

        embed = discord.Embed(
            title=item.title,
            url=item.link,
            description=item.description or None,
            color=color,
            timestamp=item.timestamp,
        )
        platform_label = format_social_alert_platform(item.platform)
        if item.creator_name:
            embed.set_author(name=item.creator_name)
        embed.add_field(name="Platform", value=platform_label, inline=True)
        if item.game_name:
            embed.add_field(name="Category", value=item.game_name, inline=True)
        if item.viewer_count is not None:
            embed.add_field(name="Viewers", value=str(item.viewer_count), inline=True)
        if item.thumbnail_url:
            embed.set_image(url=item.thumbnail_url)
        embed.set_footer(text=f"Alert #{alert['id']}")
        return embed

    def _rss_text(self, entry: ET.Element, path: str) -> str:
        node = entry.find(path, RSS_NS)
        if node is not None and node.text:
            return node.text.strip()
        return ""

    def _rss_link(self, entry: ET.Element) -> str:
        link_node = entry.find("link")
        if link_node is not None and link_node.text:
            return link_node.text.strip()

        atom_link = entry.find("atom:link[@rel='alternate']", RSS_NS) or entry.find("atom:link", RSS_NS)
        if atom_link is not None:
            href = atom_link.attrib.get("href")
            if href:
                return href.strip()
        return ""

    def _rss_thumbnail(self, entry: ET.Element) -> str | None:
        enclosure = entry.find("enclosure")
        if enclosure is not None and enclosure.attrib.get("url"):
            return enclosure.attrib["url"]

        for path in ("media:content", "media:thumbnail", "yt:thumbnail"):
            node = entry.find(path, RSS_NS)
            if node is not None and node.attrib.get("url"):
                return node.attrib["url"]
        return None

    # ------------------------------------------------------------------
    # Social alerts command group
    # ------------------------------------------------------------------

    social_group = app_commands.Group(name="social", description="Social media feed monitoring")

    @social_group.command(name="add", description="Add an RSS feed alert")
    @app_commands.describe(
        channel="Channel to send alerts to",
        rss_url="RSS feed URL",
        message="Custom message template (use {title}, {link}, {date})"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def social_alert_add(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        rss_url: str,
        message: str = "📰 **{title}**\n{link}",
    ) -> None:
        """Add an RSS feed alert."""
        if not rss_url.startswith("http"):
            await interaction.response.send_message(
                "❌ Invalid RSS URL. Must start with http:// or https://",
                ephemeral=True,
            )
            return

        if await self.db.add_social_alert(
            interaction.guild_id,  # type: ignore[arg-type]
            channel.id,
            "rss",
            rss_url,
            "new",
            message,
        ):
            await interaction.response.send_message(
                f"✅ RSS alert added for {rss_url}\nAlerts will be sent to {channel.mention}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ An alert for this RSS URL already exists.",
                ephemeral=True,
            )

    @social_group.command(name="add_stream", description="Track a Twitch or YouTube live stream")
    @app_commands.describe(
        channel="Channel to send alerts to",
        platform="Streaming platform",
        account="Twitch channel, or YouTube channel ID / @handle / channel URL",
        message="Custom message template (supports {creator}, {title}, {link}, {game}, {viewers}, {platform})",
    )
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="Twitch", value="twitch"),
            app_commands.Choice(name="YouTube", value="youtube"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def social_alert_add_stream(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        platform: app_commands.Choice[str],
        account: str,
        message: str | None = None,
    ) -> None:
        """Add a live stream alert for Twitch or YouTube."""
        if platform.value == "twitch":
            if not getattr(self.config, "twitch_client_id", None) or not getattr(self.config, "twitch_client_secret", None):
                await interaction.response.send_message(
                    "❌ Twitch tracking requires TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in the bot environment.",
                    ephemeral=True,
                )
                return
            normalized_account = normalize_twitch_account(account)
        else:
            if not getattr(self.config, "youtube_api_key", None):
                await interaction.response.send_message(
                    "❌ YouTube tracking requires YOUTUBE_API_KEY in the bot environment.",
                    ephemeral=True,
                )
                return
            normalized_account = normalize_youtube_account(account)

        if not normalized_account:
            await interaction.response.send_message(
                "❌ Please provide a valid Twitch channel or YouTube channel reference.",
                ephemeral=True,
            )
            return

        if await self.db.add_social_alert(
            interaction.guild_id,  # type: ignore[arg-type]
            channel.id,
            platform.value,
            normalized_account,
            "stream",
            message or default_social_alert_template(platform.value),
        ):
            await interaction.response.send_message(
                f"✅ {platform.name} stream alert added for `{normalized_account}`\nAlerts will be sent to {channel.mention}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ An alert for this platform/account already exists.",
                ephemeral=True,
            )

    @social_group.command(name="list", description="List all social media alerts")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def social_alert_list(self, interaction: discord.Interaction) -> None:
        """List all social media alerts."""
        guild = interaction.guild
        assert guild is not None

        alerts = await self.db.get_social_alerts(guild.id)
        
        if not alerts:
            await interaction.response.send_message(
                "❌ No social media alerts configured.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📢 Social Media Alerts",
            description=f"Found {len(alerts)} alert(s)",
            color=discord.Color.blue(),
        )

        for alert in alerts[:10]:
            channel = guild.get_channel(alert["channel_id"])
            channel_name = channel.name if channel else "unknown"
            
            status = "✅ Enabled" if alert["enabled"] else "❌ Disabled"
            
            # Truncate account_id for display
            account_display = alert["account_id"][:50] + "..." if len(alert["account_id"]) > 50 else alert["account_id"]
            
            platform_name = format_social_alert_platform(alert["platform"])
            embed.add_field(
                name=f"#{alert['id']} {platform_name} Alert",
                value=f"**Status:** {status}\n**Channel:** #{channel_name}\n**Account:** `{account_display}`\n**Type:** {alert['alert_type']}",
                inline=False,
            )

        if len(alerts) > 10:
            embed.set_footer(text=f"Showing 10 of {len(alerts)} alerts")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="remove", description="Remove a social media alert")
    @app_commands.describe(alert_id="Alert ID to remove (use /social list to find IDs)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def social_alert_remove(
        self,
        interaction: discord.Interaction,
        alert_id: int,
    ) -> None:
        """Remove a social media alert."""
        if await self.db.remove_social_alert(interaction.guild_id, alert_id):  # type: ignore[arg-type]
            await interaction.response.send_message(
                f"✅ Alert #{alert_id} has been removed.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"❌ Alert #{alert_id} not found.",
                ephemeral=True,
            )

    @social_group.command(name="toggle", description="Toggle a social media alert on/off")
    @app_commands.describe(alert_id="Alert ID to toggle")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def social_alert_toggle(
        self,
        interaction: discord.Interaction,
        alert_id: int,
    ) -> None:
        """Toggle a social media alert."""
        result = await self.db.toggle_social_alert(interaction.guild_id, alert_id)  # type: ignore[arg-type]
        
        if result is None:
            await interaction.response.send_message(
                f"❌ Alert #{alert_id} not found.",
                ephemeral=True,
            )
        elif result:
            await interaction.response.send_message(
                f"✅ Alert #{alert_id} has been enabled.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ Alert #{alert_id} has been disabled.",
                ephemeral=True,
            )

    @social_group.command(name="test", description="Preview an RSS, Twitch, or YouTube alert target")
    @app_commands.describe(
        platform="Platform to test",
        target="RSS URL, Twitch channel, or YouTube channel ID / @handle / channel URL",
    )
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="RSS", value="rss"),
            app_commands.Choice(name="Twitch", value="twitch"),
            app_commands.Choice(name="YouTube", value="youtube"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def social_alert_test(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        target: str,
    ) -> None:
        """Preview a feed or live stream target by fetching recent items."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            async with aiohttp.ClientSession() as session:
                alert = {
                    "id": 0,
                    "platform": platform.value,
                    "account_id": target,
                    "alert_type": "stream" if platform.value in {"twitch", "youtube"} else "new",
                    "message_template": default_social_alert_template(platform.value),
                }
                items = await self._fetch_alert_items(session, alert)

                if not items:
                    await interaction.followup.send(
                        f"❌ No active {platform.name} content found for `{target}`.",
                        ephemeral=True,
                    )
                    return

                preview = items[:3]
                embed = discord.Embed(
                    title=f"Preview: {platform.name}",
                    description=f"Found {len(items)} matching item(s)",
                    color=discord.Color.green(),
                )
                for item in preview:
                    embed.add_field(
                        name=item.title,
                        value=item.link[:200],
                        inline=False,
                    )
                if preview[0].thumbnail_url:
                    embed.set_image(url=preview[0].thumbnail_url)
                await interaction.followup.send(embed=embed, ephemeral=True)
                    
        except Exception as e:
            logger.error("Error testing social alert target: %s", e)
            await interaction.followup.send(
                f"❌ Error testing alert target: {str(e)}",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    """Load the SocialAlerts cog."""
    await bot.add_cog(SocialAlertsCog(bot, bot.db, getattr(bot, "config", None)))
