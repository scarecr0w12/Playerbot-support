"""Reminders cog — inspired by Red-DiscordBot's Reminder and Fifo cogs.

Features
--------
- /remindme <time> <message>   — set a reminder; bot DMs you (or pings in channel) when due
- /reminders list              — view all your pending reminders
- /reminders delete <id>       — cancel a specific reminder
- /reminders clear             — cancel all your pending reminders
- Background task polls every 30 s and dispatches due reminders

Time formats:
  Relative: 30s · 10m · 2h · 1d · 1d12h30m · 2d6h
  Absolute (UTC): 2026-04-10 14:30  or  04/10 14:30
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot.database import Database

logger = logging.getLogger(__name__)

_RELATIVE_RE = re.compile(
    r"(?:(\d+)\s*d(?:ays?)?)?"
    r"(?:(\d+)\s*h(?:ours?)?)?"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?"
    r"(?:(\d+)\s*s(?:ec(?:onds?)?)?)?",
    re.IGNORECASE,
)
_ABS_FMT_1 = "%Y-%m-%d %H:%M"
_ABS_FMT_2 = "%m/%d %H:%M"
_ABS_FMT_3 = "%Y-%m-%dT%H:%M"


def _parse_time(text: str) -> datetime | None:
    """Parse a relative or absolute time string into an aware UTC datetime."""
    text = text.strip()

    # Try relative first
    m = _RELATIVE_RE.fullmatch(text)
    if m and any(m.groups()):
        days = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        minutes = int(m.group(3) or 0)
        seconds = int(m.group(4) or 0)
        td = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        if td.total_seconds() >= 1:
            return datetime.now(timezone.utc) + td

    # Try absolute formats
    for fmt in (_ABS_FMT_1, _ABS_FMT_3):
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # MM/DD without year — assume current/next year
    try:
        naive = datetime.strptime(text, _ABS_FMT_2)
        dt = naive.replace(year=datetime.now(timezone.utc).year, tzinfo=timezone.utc)
        if dt < datetime.now(timezone.utc):
            dt = dt.replace(year=dt.year + 1)
        return dt
    except ValueError:
        pass

    return None


class RemindersCog(commands.Cog, name="Reminders"):
    """Set time-based reminders — the bot will DM you (or ping you in channel) when they're due."""

    def __init__(self, bot: commands.Bot, db: "Database") -> None:
        self.bot = bot
        self.db = db

    async def cog_load(self) -> None:
        self._reminder_loop.start()

    async def cog_unload(self) -> None:
        self._reminder_loop.cancel()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def _reminder_loop(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        due = await self.db.get_due_reminders(now)
        for row in due:
            await self._dispatch_reminder(row)
            await self.db.delete_reminder(row["id"])

    @_reminder_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _dispatch_reminder(self, row) -> None:
        user = self.bot.get_user(row["user_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(row["user_id"])
            except discord.NotFound:
                return

        end_time = datetime.fromisoformat(row["end_time"])
        ts = int(end_time.timestamp())
        em = discord.Embed(
            title="⏰ Reminder",
            description=row["message"],
            color=discord.Color.blurple(),
        )
        em.set_footer(text=f"Set for <t:{ts}:f>")

        sent = False
        if row["channel_id"]:
            channel = self.bot.get_channel(row["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(f"<@{row['user_id']}>", embed=em)
                    sent = True
                except discord.Forbidden:
                    pass

        if not sent:
            try:
                await user.send(embed=em)
            except discord.Forbidden:
                pass

    # ==================================================================
    # /remindme
    # ==================================================================

    @app_commands.command(name="remindme", description="Set a reminder")
    @app_commands.describe(
        time='When to remind you — relative (1h30m, 2d) or absolute (2026-04-10 14:30)',
        message="What to remind you about",
        private="DM you instead of pinging in this channel (default: True)",
    )
    async def remindme(
        self,
        interaction: discord.Interaction,
        time: str,
        message: str,
        private: bool = True,
    ) -> None:
        end_dt = _parse_time(time)
        if end_dt is None:
            await interaction.response.send_message(
                "❌ Couldn't parse that time. Try `1h30m`, `2d`, or `2026-04-10 14:30`.",
                ephemeral=True,
            )
            return

        if end_dt <= datetime.now(timezone.utc):
            await interaction.response.send_message("❌ That time is in the past.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        channel_id = None if private else (interaction.channel_id if interaction.channel else None)

        reminder_id = await self.db.create_reminder(
            user_id=interaction.user.id,
            message=message,
            end_time=end_dt.isoformat(),
            guild_id=guild_id,
            channel_id=channel_id,
        )

        ts = int(end_dt.timestamp())
        await interaction.response.send_message(
            f"✅ Reminder **#{reminder_id}** set for <t:{ts}:f> (<t:{ts}:R>).",
            ephemeral=True,
        )

    # ==================================================================
    # /reminders subgroup
    # ==================================================================

    reminders_group = app_commands.Group(name="reminders", description="Manage your reminders")

    @reminders_group.command(name="list", description="List all your pending reminders")
    async def list_reminders(self, interaction: discord.Interaction) -> None:
        rows = await self.db.get_user_reminders(interaction.user.id)
        if not rows:
            await interaction.response.send_message("You have no pending reminders.", ephemeral=True)
            return

        lines: list[str] = []
        for row in rows:
            end_dt = datetime.fromisoformat(row["end_time"])
            ts = int(end_dt.timestamp())
            msg_preview = row["message"][:60] + ("…" if len(row["message"]) > 60 else "")
            lines.append(f"**#{row['id']}** — <t:{ts}:R> — {msg_preview}")

        em = discord.Embed(
            title="⏰ Your Reminders",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    @reminders_group.command(name="delete", description="Cancel a specific reminder by ID")
    @app_commands.describe(reminder_id="The reminder ID to cancel")
    async def delete_reminder(self, interaction: discord.Interaction, reminder_id: int) -> None:
        rows = await self.db.get_user_reminders(interaction.user.id)
        ids = [r["id"] for r in rows]
        if reminder_id not in ids:
            await interaction.response.send_message("❌ Reminder not found.", ephemeral=True)
            return
        await self.db.delete_reminder(reminder_id)
        await interaction.response.send_message(f"✅ Reminder **#{reminder_id}** cancelled.", ephemeral=True)

    @reminders_group.command(name="clear", description="Cancel all your pending reminders")
    async def clear_reminders(self, interaction: discord.Interaction) -> None:
        rows = await self.db.get_user_reminders(interaction.user.id)
        for row in rows:
            await self.db.delete_reminder(row["id"])
        await interaction.response.send_message(
            f"✅ Cleared **{len(rows)}** reminder(s).", ephemeral=True
        )
