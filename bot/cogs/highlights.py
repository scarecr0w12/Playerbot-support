"""Highlights / keyword notification cog — inspired by Discord's built-in highlights and
Red-DiscordBot community cogs.

Features
--------
- /highlight add <keyword>    — subscribe to a keyword; get a DM when it appears in any channel
- /highlight remove <keyword> — unsubscribe from a keyword
- /highlight list             — view your active keywords
- /highlight clear            — remove all your keywords in this server
- /highlight pause            — temporarily pause notifications (toggle)

Behaviour
---------
- on_message: case-insensitive full-word scan of each message
- Skips messages from the highlight owner themselves
- Skips channels where the user has no read permission
- Includes 3 lines of context around the trigger message in the DM
- Rate-limits DMs: at most one DM per keyword per user per 60 s to prevent spam
- Users can have up to 25 keywords per guild
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)

MAX_KEYWORDS = 25
DM_COOLDOWN_SECONDS = 60
CONTEXT_MESSAGES = 3


class HighlightsCog(commands.Cog, name="Highlights"):
    """Keyword notification system — get DM'd when your keywords are mentioned."""

    def __init__(self, bot: commands.Bot, db: "Database") -> None:
        self.bot = bot
        self.db = db
        self._dm_cooldown: dict[tuple[int, int, str], float] = {}

    def _is_on_cooldown(self, user_id: int, guild_id: int, keyword: str) -> bool:
        key = (user_id, guild_id, keyword)
        last = self._dm_cooldown.get(key, 0)
        return (time.monotonic() - last) < DM_COOLDOWN_SECONDS

    def _set_cooldown(self, user_id: int, guild_id: int, keyword: str) -> None:
        self._dm_cooldown[(user_id, guild_id, keyword)] = time.monotonic()

    def _keyword_in_message(self, keyword: str, content: str) -> bool:
        pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
        return bool(re.search(pattern, content, re.IGNORECASE))

    async def _is_paused(self, user_id: int, guild_id: int) -> bool:
        val = await self.db.get_guild_config(guild_id, f"highlight_pause_{user_id}")
        return val == "1"

    # ------------------------------------------------------------------
    # on_message scanner
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not message.content:
            return

        guild_id = message.guild.id
        rows = await self.db.get_guild_highlights(guild_id)
        if not rows:
            return

        content_lower = message.content.lower()
        notified: set[int] = set()

        for row in rows:
            user_id = row["user_id"]
            keyword = row["keyword"]

            if user_id == message.author.id:
                continue
            if user_id in notified:
                continue
            if not self._keyword_in_message(keyword, content_lower):
                continue
            if self._is_on_cooldown(user_id, guild_id, keyword):
                continue
            if await self._is_paused(user_id, guild_id):
                continue

            member = message.guild.get_member(user_id)
            if member is None:
                continue

            perms = message.channel.permissions_for(member)
            if not perms.read_messages:
                continue

            self._set_cooldown(user_id, guild_id, keyword)
            notified.add(user_id)

            context_lines: list[str] = []
            try:
                async for ctx_msg in message.channel.history(limit=CONTEXT_MESSAGES + 1, before=message):
                    if ctx_msg.content:
                        context_lines.insert(0, f"**{ctx_msg.author.display_name}:** {ctx_msg.content[:200]}")
            except discord.Forbidden:
                pass

            context_lines.append(f"➡️ **{message.author.display_name}:** {message.content[:800]}")
            context_text = "\n".join(context_lines)

            em = discord.Embed(
                title=f"🔔 Keyword highlight: `{keyword}`",
                description=context_text[:4000],
                color=discord.Color.blurple(),
            )
            em.add_field(name="Channel", value=f"{message.channel.mention} in **{message.guild.name}**", inline=False)
            em.add_field(name="Jump", value=f"[Go to message]({message.jump_url})", inline=False)
            em.set_footer(text=f"You subscribed to \"{keyword}\" in {message.guild.name}")

            try:
                await member.send(embed=em)
            except discord.Forbidden:
                pass

    # ==================================================================
    # /highlight commands
    # ==================================================================

    highlight_group = app_commands.Group(name="highlight", description="Keyword highlight notifications")

    @highlight_group.command(name="add", description="Get notified when a keyword is mentioned")
    @app_commands.describe(keyword="Word or phrase to highlight (case-insensitive)")
    @app_commands.guild_only()
    async def add_highlight(self, interaction: discord.Interaction, keyword: str) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        user_id = interaction.user.id
        keyword = keyword.lower().strip()

        if len(keyword) < 2:
            await interaction.response.send_message("❌ Keyword must be at least 2 characters.", ephemeral=True)
            return

        existing = await self.db.get_user_highlights(user_id, guild_id)
        if len(existing) >= MAX_KEYWORDS:
            await interaction.response.send_message(
                f"❌ You can have at most **{MAX_KEYWORDS}** highlights per server.", ephemeral=True
            )
            return

        if keyword in existing:
            await interaction.response.send_message(
                f"❌ You already have `{keyword}` highlighted.", ephemeral=True
            )
            return

        await self.db.add_highlight(user_id, guild_id, keyword)
        await interaction.response.send_message(
            f"✅ You'll now be notified when `{keyword}` is mentioned.", ephemeral=True
        )

    @highlight_group.command(name="remove", description="Remove a keyword highlight")
    @app_commands.describe(keyword="Keyword to remove")
    @app_commands.guild_only()
    async def remove_highlight(self, interaction: discord.Interaction, keyword: str) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        removed = await self.db.remove_highlight(interaction.user.id, guild_id, keyword.lower().strip())
        if not removed:
            await interaction.response.send_message(f"❌ `{keyword}` not found in your highlights.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Removed `{keyword}` from your highlights.", ephemeral=True)

    @highlight_group.command(name="list", description="View your active keyword highlights")
    @app_commands.guild_only()
    async def list_highlights(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        keywords = await self.db.get_user_highlights(interaction.user.id, guild_id)
        if not keywords:
            await interaction.response.send_message("You have no highlights set up.", ephemeral=True)
            return

        paused = await self._is_paused(interaction.user.id, guild_id)
        em = discord.Embed(
            title="🔔 Your Highlights",
            description="\n".join(f"• `{kw}`" for kw in sorted(keywords)),
            color=discord.Color.blurple(),
        )
        em.set_footer(text=f"{'⏸ Paused' if paused else '▶ Active'} · {len(keywords)}/{MAX_KEYWORDS} keywords")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @highlight_group.command(name="clear", description="Remove all your keyword highlights in this server")
    @app_commands.guild_only()
    async def clear_highlights(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        count = await self.db.clear_user_highlights(interaction.user.id, guild_id)
        await interaction.response.send_message(
            f"✅ Cleared **{count}** highlight(s).", ephemeral=True
        )

    @highlight_group.command(name="pause", description="Pause or resume your highlight notifications")
    @app_commands.guild_only()
    async def pause_highlights(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        user_id = interaction.user.id
        currently_paused = await self._is_paused(user_id, guild_id)
        new_state = "0" if currently_paused else "1"
        await self.db.set_guild_config(guild_id, f"highlight_pause_{user_id}", new_state)
        msg = "▶ Highlight notifications **resumed**." if currently_paused else "⏸ Highlight notifications **paused**."
        await interaction.response.send_message(msg, ephemeral=True)
