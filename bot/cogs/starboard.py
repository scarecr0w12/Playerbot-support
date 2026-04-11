"""Starboard cog — democratic pinning system via star reactions.

Features
--------
- /starboard set_channel <channel>  — set the starboard output channel
- /starboard set_threshold <n>      — minimum stars to appear on starboard (default 3)
- /starboard set_emoji <emoji>      — custom star emoji (default ⭐)
- /starboard toggle                 — enable/disable
- /starboard ignore_channel         — exclude a channel from starboard
- /starboard info                   — view current config

Behaviour
---------
- Watches raw_reaction_add / raw_reaction_remove events for the star emoji
- Counts unique reactors (self-reactions and bot messages are ignored)
- If count >= threshold and message not yet on starboard → posts embed
- If already on starboard → edits embed to update star count
- If count drops below threshold → deletes starboard post
- On raw_message_delete → removes starboard entry
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 3
DEFAULT_EMOJI = "⭐"


def _build_starboard_embed(
    message: discord.Message, star_count: int, emoji: str
) -> discord.Embed:
    color = discord.Color.gold()
    em = discord.Embed(
        description=message.content[:4000] if message.content else "",
        color=color,
        timestamp=message.created_at,
    )
    em.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )
    em.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)
    em.set_footer(text=f"{emoji} {star_count}  ·  #{getattr(message.channel, 'name', '?')}")

    if message.attachments:
        img = next((a for a in message.attachments if a.content_type and a.content_type.startswith("image")), None)
        if img:
            em.set_image(url=img.url)

    if message.embeds:
        first = message.embeds[0]
        if first.image:
            em.set_image(url=first.image.url)
        if not em.description and first.description:
            em.description = first.description[:4000]

    return em


class StarboardCog(commands.Cog, name="Starboard"):
    """Reaction-based starboard — star messages to feature them in a dedicated channel."""

    def __init__(self, bot: commands.Bot, db: "Database") -> None:
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    async def _get_channel(self, guild_id: int) -> discord.TextChannel | None:
        raw = await self.db.get_guild_config(guild_id, "starboard_channel")
        if not raw:
            return None
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return None
        ch = guild.get_channel(int(raw))
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _get_threshold(self, guild_id: int) -> int:
        raw = await self.db.get_guild_config(guild_id, "starboard_threshold")
        try:
            return int(raw) if raw else DEFAULT_THRESHOLD
        except ValueError:
            return DEFAULT_THRESHOLD

    async def _get_emoji(self, guild_id: int) -> str:
        return (await self.db.get_guild_config(guild_id, "starboard_emoji")) or DEFAULT_EMOJI

    async def _is_enabled(self, guild_id: int) -> bool:
        val = await self.db.get_guild_config(guild_id, "starboard_enabled")
        return val != "0"

    async def _get_ignored_channels(self, guild_id: int) -> list[int]:
        raw = await self.db.get_guild_config(guild_id, "starboard_ignore_channels")
        if not raw:
            return []
        try:
            return [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            return []

    # ------------------------------------------------------------------
    # Reaction events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id:
            return

        guild_id = payload.guild_id
        if not await self._is_enabled(guild_id):
            return

        star_emoji = await self._get_emoji(guild_id)
        emoji_str = str(payload.emoji)
        if emoji_str != star_emoji:
            return

        ignored = await self._get_ignored_channels(guild_id)
        if payload.channel_id in ignored:
            return

        starboard_channel = await self._get_channel(guild_id)
        if not starboard_channel:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        source_channel = guild.get_channel(payload.channel_id)
        if not isinstance(source_channel, discord.TextChannel):
            return

        if source_channel.id == starboard_channel.id:
            return

        try:
            message = await source_channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        if message.author.bot:
            return

        reaction = discord.utils.get(message.reactions, emoji=star_emoji)
        star_count = 0
        if reaction:
            users = [u async for u in reaction.users()]
            star_count = sum(1 for u in users if not u.bot and u.id != message.author.id)

        threshold = await self._get_threshold(guild_id)
        db_row = await self.db.get_starboard_message(payload.message_id)

        if star_count >= threshold:
            em = _build_starboard_embed(message, star_count, star_emoji)

            if db_row and db_row["starboard_msg_id"]:
                try:
                    sb_msg = await starboard_channel.fetch_message(db_row["starboard_msg_id"])
                    await sb_msg.edit(embed=em)
                except discord.NotFound:
                    sb_msg = await starboard_channel.send(embed=em)
                    await self.db.set_starboard_msg_id(payload.message_id, sb_msg.id)
            else:
                sb_msg = await starboard_channel.send(embed=em)
                await self.db.upsert_starboard_message(
                    message_id=payload.message_id,
                    guild_id=guild_id,
                    channel_id=payload.channel_id,
                    author_id=message.author.id,
                    star_count=star_count,
                    starboard_msg_id=sb_msg.id,
                )
        else:
            if db_row and db_row["starboard_msg_id"]:
                try:
                    sb_msg = await starboard_channel.fetch_message(db_row["starboard_msg_id"])
                    await sb_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                await self.db.delete_starboard_message(payload.message_id)
            elif db_row:
                await self.db.upsert_starboard_message(
                    message_id=payload.message_id,
                    guild_id=guild_id,
                    channel_id=payload.channel_id,
                    author_id=message.author.id,
                    star_count=star_count,
                )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if not payload.guild_id:
            return
        db_row = await self.db.get_starboard_message(payload.message_id)
        if not db_row:
            return

        guild = self.bot.get_guild(payload.guild_id)
        starboard_channel = await self._get_channel(payload.guild_id)
        if starboard_channel and db_row["starboard_msg_id"]:
            try:
                sb_msg = await starboard_channel.fetch_message(db_row["starboard_msg_id"])
                await sb_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        await self.db.delete_starboard_message(payload.message_id)

    # ==================================================================
    # Slash commands
    # ==================================================================

    starboard_group = app_commands.Group(
        name="starboard",
        description="Starboard configuration",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @starboard_group.command(name="set_channel", description="Set the starboard output channel")
    @app_commands.describe(channel="Channel where starred messages will appear")
    @app_commands.guild_only()
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await self.db.set_guild_config(interaction.guild_id, "starboard_channel", str(channel.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Starboard channel set to {channel.mention}.", ephemeral=True
        )

    @starboard_group.command(name="set_threshold", description="Minimum stars to appear on the starboard")
    @app_commands.describe(threshold="Number of stars required (default 3)")
    @app_commands.guild_only()
    async def set_threshold(self, interaction: discord.Interaction, threshold: int) -> None:
        if threshold < 1:
            await interaction.response.send_message("❌ Threshold must be at least 1.", ephemeral=True)
            return
        await self.db.set_guild_config(interaction.guild_id, "starboard_threshold", str(threshold))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Starboard threshold set to **{threshold}** ⭐.", ephemeral=True
        )

    @starboard_group.command(name="set_emoji", description="Set the star reaction emoji")
    @app_commands.describe(emoji="Emoji to use (default ⭐)")
    @app_commands.guild_only()
    async def set_emoji(self, interaction: discord.Interaction, emoji: str) -> None:
        await self.db.set_guild_config(interaction.guild_id, "starboard_emoji", emoji)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Starboard emoji set to {emoji}.", ephemeral=True)

    @starboard_group.command(name="toggle", description="Enable or disable the starboard")
    @app_commands.describe(enabled="True to enable, False to disable")
    @app_commands.guild_only()
    async def toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self.db.set_guild_config(interaction.guild_id, "starboard_enabled", "1" if enabled else "0")  # type: ignore[arg-type]
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Starboard {state}.", ephemeral=True)

    @starboard_group.command(name="ignore_channel", description="Exclude a channel from the starboard")
    @app_commands.describe(channel="Channel to exclude", remove="Set True to un-exclude")
    @app_commands.guild_only()
    async def ignore_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel, remove: bool = False
    ) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        ignored = await self._get_ignored_channels(guild_id)
        if remove:
            ignored = [c for c in ignored if c != channel.id]
            msg = f"✅ {channel.mention} removed from starboard ignore list."
        else:
            if channel.id not in ignored:
                ignored.append(channel.id)
            msg = f"✅ {channel.mention} will be ignored by the starboard."
        await self.db.set_guild_config(guild_id, "starboard_ignore_channels", ",".join(str(c) for c in ignored))
        await interaction.response.send_message(msg, ephemeral=True)

    @starboard_group.command(name="info", description="View current starboard configuration")
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        enabled = await self._is_enabled(guild_id)
        channel = await self._get_channel(guild_id)
        threshold = await self._get_threshold(guild_id)
        emoji = await self._get_emoji(guild_id)
        ignored = await self._get_ignored_channels(guild_id)

        em = discord.Embed(title="⭐ Starboard Config", color=discord.Color.gold())
        em.add_field(name="Enabled", value="✅ Yes" if enabled else "❌ No", inline=True)
        em.add_field(name="Channel", value=channel.mention if channel else "Not set", inline=True)
        em.add_field(name="Threshold", value=f"{threshold} {emoji}", inline=True)
        em.add_field(name="Emoji", value=emoji, inline=True)
        em.add_field(
            name="Ignored Channels",
            value=", ".join(f"<#{c}>" for c in ignored) or "None",
            inline=False,
        )
        await interaction.response.send_message(embed=em, ephemeral=True)
