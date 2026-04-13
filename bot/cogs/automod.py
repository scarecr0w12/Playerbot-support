"""Auto-moderation cog — spam detection, word/link filters, raid protection.

Filters are stored per-guild in the database and can be managed via slash commands.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database
    from bot.cogs.mod_logging import ModLoggingCog

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


class AutoModCog(commands.Cog, name="AutoMod"):
    """Automatic moderation: spam, word filter, link filter."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

        # Spam tracking: guild_id -> user_id -> list of timestamps
        self._spam_tracker: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

        # Cache filters to avoid DB reads on every message
        self._filter_cache: dict[int, dict[str, set[str]]] = {}

    @property
    def mod_log(self) -> ModLoggingCog | None:
        return self.bot.get_cog("ModLogging")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Filter cache management
    # ------------------------------------------------------------------

    async def _get_filters(self, guild_id: int, filter_type: str) -> set[str]:
        if guild_id not in self._filter_cache:
            self._filter_cache[guild_id] = {}
        if filter_type not in self._filter_cache[guild_id]:
            rows = await self.db.get_filters(guild_id, filter_type)
            self._filter_cache[guild_id][filter_type] = {r["pattern"] for r in rows}
        return self._filter_cache[guild_id][filter_type]

    def _invalidate_cache(self, guild_id: int) -> None:
        self._filter_cache.pop(guild_id, None)

    # ------------------------------------------------------------------
    # Slash commands: manage filters
    # ------------------------------------------------------------------

    filter_group = app_commands.Group(name="filter", description="Manage auto-mod filters")

    @filter_group.command(name="add_word", description="Add a word to the filter list")
    @app_commands.describe(word="The word or phrase to block")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_word(self, interaction: discord.Interaction, word: str) -> None:
        added = await self.db.add_filter(interaction.guild_id, "word", word.lower())  # type: ignore[arg-type]
        self._invalidate_cache(interaction.guild_id)  # type: ignore[arg-type]
        if added:
            await interaction.response.send_message(f"✅ Word filter added: `{word}`", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ That filter already exists.", ephemeral=True)

    @filter_group.command(name="remove_word", description="Remove a word from the filter list")
    @app_commands.describe(word="The word or phrase to unblock")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_word(self, interaction: discord.Interaction, word: str) -> None:
        removed = await self.db.remove_filter(interaction.guild_id, "word", word.lower())  # type: ignore[arg-type]
        self._invalidate_cache(interaction.guild_id)  # type: ignore[arg-type]
        if removed:
            await interaction.response.send_message(f"✅ Word filter removed: `{word}`", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ That filter was not found.", ephemeral=True)

    @filter_group.command(name="add_link", description="Block a link domain")
    @app_commands.describe(domain="Domain to block (e.g. badsite.com)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_link(self, interaction: discord.Interaction, domain: str) -> None:
        added = await self.db.add_filter(interaction.guild_id, "link", domain.lower())  # type: ignore[arg-type]
        self._invalidate_cache(interaction.guild_id)  # type: ignore[arg-type]
        if added:
            await interaction.response.send_message(f"✅ Link filter added: `{domain}`", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ That filter already exists.", ephemeral=True)

    @filter_group.command(name="remove_link", description="Unblock a link domain")
    @app_commands.describe(domain="Domain to unblock")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_link(self, interaction: discord.Interaction, domain: str) -> None:
        removed = await self.db.remove_filter(interaction.guild_id, "link", domain.lower())  # type: ignore[arg-type]
        self._invalidate_cache(interaction.guild_id)  # type: ignore[arg-type]
        if removed:
            await interaction.response.send_message(f"✅ Link filter removed: `{domain}`", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ That filter was not found.", ephemeral=True)

    @filter_group.command(name="list", description="List all active filters")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_filters(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        all_filters = await self.db.get_filters(guild_id)

        if not all_filters:
            await interaction.response.send_message("No filters configured.", ephemeral=True)
            return

        embed = discord.Embed(title="Auto-Mod Filters", color=discord.Color.orange())
        words = [f["pattern"] for f in all_filters if f["filter_type"] == "word"]
        links = [f["pattern"] for f in all_filters if f["filter_type"] == "link"]

        if words:
            embed.add_field(name="🔤 Word Filters", value=", ".join(f"`{w}`" for w in words), inline=False)
        if links:
            embed.add_field(name="🔗 Link Filters", value=", ".join(f"`{l}`" for l in links), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /automodset toggle — enable/disable automod
    # ------------------------------------------------------------------

    @automodset_group.command(name="toggle", description="Enable or disable auto-moderation")
    @app_commands.describe(enabled="Turn automod on or off")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self.db.set_guild_config(interaction.guild_id, "automod_enabled", str(int(enabled)))  # type: ignore[arg-type]
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Auto-moderation **{state}**.", ephemeral=True)

    # ------------------------------------------------------------------
    # Message listener — the core filter engine
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        # Skip if member has manage_messages (staff)
        if isinstance(message.author, discord.Member) and message.author.guild_permissions.manage_messages:
            return

        # Check if automod is enabled for this guild
        enabled_raw = await self.db.get_guild_config(message.guild.id, "automod_enabled")
        if enabled_raw == "0":
            return

        guild_id = message.guild.id
        content_lower = message.content.lower() if message.content else ""

        # --- Word filter ---
        word_filters = await self._get_filters(guild_id, "word")
        for word in word_filters:
            if word in content_lower:
                await self._handle_violation(message, "word_filter", f"Blocked word: `{word}`")
                return

        # --- Link filter ---
        link_filters = await self._get_filters(guild_id, "link")
        if link_filters:
            urls = URL_PATTERN.findall(message.content or "")
            for url in urls:
                for domain in link_filters:
                    if domain in url.lower():
                        await self._handle_violation(message, "link_filter", f"Blocked domain: `{domain}`")
                        return

        # --- Spam detection ---
        await self._check_spam(message)

        # --- Mention spam detection ---
        await self._check_mention_spam(message)

    async def _check_spam(self, message: discord.Message) -> None:
        guild_id = message.guild.id  # type: ignore[union-attr]
        user_id = message.author.id
        now = time.time()
        window = await self.db.get_setting_int(guild_id, "automod_spam_interval")
        threshold = await self.db.get_setting_int(guild_id, "automod_spam_threshold")

        timestamps = self._spam_tracker[guild_id][user_id]
        timestamps.append(now)
        # Prune old timestamps
        self._spam_tracker[guild_id][user_id] = [t for t in timestamps if now - t < window]

        if len(self._spam_tracker[guild_id][user_id]) >= threshold:
            self._spam_tracker[guild_id][user_id] = []
            await self._handle_violation(
                message, "spam", f"Sent {threshold}+ messages in {window}s"
            )

    async def _check_mention_spam(self, message: discord.Message) -> None:
        guild_id = message.guild.id  # type: ignore[union-attr]
        
        # Check if mention spam protection is enabled
        mention_enabled = await self.db.get_guild_config(guild_id, "automod_mention_enabled")
        if mention_enabled != "true":
            return

        # Get thresholds
        threshold = await self.db.get_setting_int(guild_id, "automod_mention_threshold") or 5
        role_mentions = len(message.role_mentions)
        user_mentions = len(message.mentions)
        total_mentions = role_mentions + user_mentions

        if total_mentions >= threshold:
            await self._handle_violation(
                message, 
                "mention_spam", 
                f"Too many mentions ({total_mentions}/{threshold})"
            )

    async def _handle_violation(self, message: discord.Message, violation_type: str, detail: str) -> None:
        try:
            await message.delete()
        except discord.Forbidden:
            pass

        try:
            await message.channel.send(
                f"⚠️ {message.author.mention}, your message was removed by auto-mod. ({detail})",
                delete_after=8,
            )
        except discord.Forbidden:
            pass

        if self.mod_log and message.guild:
            await self.mod_log.log(
                message.guild,
                action="filter_trigger",
                target=message.author,
                extra=f"**Type:** {violation_type}\n**Detail:** {detail}\n**Channel:** {message.channel.mention}",
            )

    # ------------------------------------------------------------------
    # /automodset  –  admin commands to configure automod per-guild
    # ------------------------------------------------------------------

    automodset_group = app_commands.Group(name="automodset", description="Auto-mod settings (admin)")

    @automodset_group.command(name="spam_threshold", description="Set messages count that triggers spam detection")
    @app_commands.describe(count="Number of messages within the interval to flag as spam")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_spam_threshold(self, interaction: discord.Interaction, count: int) -> None:
        await self.db.set_guild_config(interaction.guild_id, "automod_spam_threshold", str(count))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Spam threshold set to **{count}** messages.", ephemeral=True
        )

    @automodset_group.command(name="spam_interval", description="Set time window (seconds) for spam detection")
    @app_commands.describe(seconds="Time window in seconds")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_spam_interval(self, interaction: discord.Interaction, seconds: int) -> None:
        await self.db.set_guild_config(interaction.guild_id, "automod_spam_interval", str(seconds))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Spam interval set to **{seconds}** second(s).", ephemeral=True
        )

    @automodset_group.command(name="mention_enable", description="Enable mention spam protection")
    @app_commands.checks.has_permissions(administrator=True)
    async def mention_enable(self, interaction: discord.Interaction) -> None:
        await self.db.set_guild_config(interaction.guild_id, "automod_mention_enabled", "true")  # type: ignore[arg-type]
        await interaction.response.send_message("✅ Mention spam protection enabled.", ephemeral=True)

    @automodset_group.command(name="mention_disable", description="Disable mention spam protection")
    @app_commands.checks.has_permissions(administrator=True)
    async def mention_disable(self, interaction: discord.Interaction) -> None:
        await self.db.set_guild_config(interaction.guild_id, "automod_mention_enabled", "false")  # type: ignore[arg-type]
        await interaction.response.send_message("⚠️ Mention spam protection disabled.", ephemeral=True)

    @automodset_group.command(name="mention_threshold", description="Set mention spam threshold")
    @app_commands.describe(threshold="Maximum mentions per message before deletion")
    @app_commands.checks.has_permissions(administrator=True)
    async def mention_threshold(self, interaction: discord.Interaction, threshold: int) -> None:
        if threshold < 1 or threshold > 50:
            await interaction.response.send_message("❌ Threshold must be between 1 and 50.", ephemeral=True)
            return
        await self.db.set_guild_config(interaction.guild_id, "automod_mention_threshold", str(threshold))  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Mention threshold set to {threshold} mentions.", ephemeral=True)

    @automodset_group.command(name="show", description="Show current auto-mod settings")
    @app_commands.checks.has_permissions(administrator=True)
    async def show_automod_settings(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        threshold = await self.db.get_setting(guild_id, "automod_spam_threshold")
        interval = await self.db.get_setting(guild_id, "automod_spam_interval")
        enabled_raw = await self.db.get_guild_config(guild_id, "automod_enabled")
        enabled = "Disabled" if enabled_raw == "0" else "Enabled"
        mention_enabled = await self.db.get_guild_config(guild_id, "automod_mention_enabled")
        mention_threshold = await self.db.get_setting(guild_id, "automod_mention_threshold")
        embed = discord.Embed(title="⚙️ Auto-Mod Settings", color=discord.Color.orange())
        embed.add_field(name="Status", value=enabled, inline=True)
        embed.add_field(name="Spam threshold", value=f"{threshold} messages", inline=True)
        embed.add_field(name="Spam interval", value=f"{interval} second(s)", inline=True)
        embed.add_field(name="Mention spam protection", value="Enabled" if mention_enabled == "true" else "Disabled", inline=True)
        embed.add_field(name="Mention threshold", value=f"{mention_threshold} mentions", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
