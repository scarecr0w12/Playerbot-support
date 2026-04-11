"""Cleanup cog — bulk message deletion commands.

Inspired by Red-DiscordBot's Cleanup cog.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database
    from bot.cogs.mod_logging import ModLoggingCog

logger = logging.getLogger(__name__)


class CleanupCog(commands.Cog, name="Cleanup"):
    """Bulk message deletion with various filters."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    @property
    def mod_log(self) -> ModLoggingCog | None:
        return self.bot.get_cog("ModLogging")  # type: ignore[return-value]

    async def _do_purge(
        self,
        interaction: discord.Interaction,
        limit: int,
        check=None,
        description: str = "",
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ Can only purge in text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        deleted = await channel.purge(limit=limit, check=check, bulk=True)
        await interaction.followup.send(
            f"🗑️ Deleted **{len(deleted)}** message(s). {description}", ephemeral=True
        )

        if self.mod_log and interaction.guild:
            await self.mod_log.log(
                interaction.guild,
                action="message_delete",
                moderator=interaction.user,
                extra=f"**Purged {len(deleted)} messages** in {channel.mention}\n{description}",
            )

    # ------------------------------------------------------------------
    # /purge — delete N messages
    # ------------------------------------------------------------------

    @app_commands.command(name="purge", description="Delete a number of messages from this channel")
    @app_commands.describe(count="Number of messages to delete (max 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, count: int) -> None:
        count = min(max(count, 1), 100)
        await self._do_purge(interaction, count, description=f"(last {count})")

    # ------------------------------------------------------------------
    # /purge_user — delete messages from a specific user
    # ------------------------------------------------------------------

    @app_commands.command(name="purge_user", description="Delete recent messages from a specific user")
    @app_commands.describe(member="Target user", count="Number of messages to scan (max 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge_user(self, interaction: discord.Interaction, member: discord.Member, count: int = 100) -> None:
        count = min(max(count, 1), 100)
        await self._do_purge(
            interaction, count,
            check=lambda m: m.author.id == member.id,
            description=f"(from {member})",
        )

    # ------------------------------------------------------------------
    # /purge_bots — delete messages from bots
    # ------------------------------------------------------------------

    @app_commands.command(name="purge_bots", description="Delete recent bot messages")
    @app_commands.describe(count="Number of messages to scan (max 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge_bots(self, interaction: discord.Interaction, count: int = 100) -> None:
        count = min(max(count, 1), 100)
        await self._do_purge(
            interaction, count,
            check=lambda m: m.author.bot,
            description="(bot messages)",
        )

    # ------------------------------------------------------------------
    # /purge_contains — delete messages containing specific text
    # ------------------------------------------------------------------

    @app_commands.command(name="purge_contains", description="Delete messages containing specific text")
    @app_commands.describe(text="Text to search for", count="Number of messages to scan (max 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge_contains(self, interaction: discord.Interaction, text: str, count: int = 100) -> None:
        count = min(max(count, 1), 100)
        text_lower = text.lower()
        await self._do_purge(
            interaction, count,
            check=lambda m: text_lower in (m.content or "").lower(),
            description=f'(containing "{text}")',
        )

    # ------------------------------------------------------------------
    # /purge_embeds — delete messages with embeds/attachments
    # ------------------------------------------------------------------

    @app_commands.command(name="purge_embeds", description="Delete messages with embeds or attachments")
    @app_commands.describe(count="Number of messages to scan (max 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge_embeds(self, interaction: discord.Interaction, count: int = 100) -> None:
        count = min(max(count, 1), 100)
        await self._do_purge(
            interaction, count,
            check=lambda m: bool(m.embeds or m.attachments),
            description="(embeds/attachments)",
        )
