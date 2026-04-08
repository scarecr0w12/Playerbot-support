"""Giveaway cog — inspired by GiveawayBot and Carl-bot's giveaway module.

Features
--------
- /giveaway start  <prize> <duration> [winners] [channel] — creates a giveaway embed with entry button
- /giveaway end    <id>  — end a giveaway immediately and pick winners
- /giveaway reroll <id>  — reroll winners for an ended giveaway
- /giveaway cancel <id>  — cancel an active giveaway without picking winners
- /giveaway list         — list active giveaways in this guild
- /giveaway info   <id>  — view entry count and details for any giveaway
- Background task: polls every 30 s and ends expired giveaways automatically

Duration string format: 1d2h30m  (days / hours / minutes / seconds)
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot.database import Database

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(
    r"(?:(\d+)\s*d(?:ays?)?)?"
    r"(?:(\d+)\s*h(?:ours?)?)?"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?"
    r"(?:(\d+)\s*s(?:ec(?:onds?)?)?)?",
    re.IGNORECASE,
)


def _parse_duration(text: str) -> timedelta | None:
    m = _DURATION_RE.fullmatch(text.strip())
    if not m or not any(m.groups()):
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    td = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if td.total_seconds() < 1:
        return None
    return td


def _giveaway_embed(
    giveaway_id: int,
    prize: str,
    end_time: datetime,
    winner_count: int,
    host_id: int,
    entry_count: int = 0,
    ended: bool = False,
    winners: list[int] | None = None,
) -> discord.Embed:
    color = discord.Color.red() if ended else discord.Color.green()
    ts = int(end_time.timestamp())
    em = discord.Embed(
        title=f"🎉 {'[ENDED] ' if ended else ''}Giveaway #{giveaway_id}",
        description=f"**Prize:** {prize}",
        color=color,
    )
    if ended and winners:
        em.add_field(
            name="🏆 Winners",
            value="\n".join(f"<@{w}>" for w in winners) or "No winners",
            inline=False,
        )
    elif not ended:
        em.add_field(name="Ends", value=f"<t:{ts}:R> (<t:{ts}:f>)", inline=True)
    em.add_field(name="Winners", value=str(winner_count), inline=True)
    em.add_field(name="Entries", value=str(entry_count), inline=True)
    em.add_field(name="Hosted by", value=f"<@{host_id}>", inline=True)
    if not ended:
        em.set_footer(text=f"Giveaway ID: {giveaway_id} · Click 🎉 to enter!")
    else:
        em.set_footer(text=f"Giveaway ID: {giveaway_id} · Ended")
    return em


class GiveawayEntryView(discord.ui.View):
    """Persistent button view attached to giveaway messages."""

    def __init__(self, cog: GiveawayCog, giveaway_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.giveaway_id = giveaway_id
        self.enter_button.custom_id = f"giveaway:enter:{giveaway_id}"

    @discord.ui.button(label="🎉 Enter", style=discord.ButtonStyle.success)
    async def enter_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_entry(interaction, self.giveaway_id)


class GiveawayCog(commands.Cog, name="Giveaways"):
    """Full-featured giveaway system with countdown, entry buttons, and auto-end."""

    def __init__(self, bot: commands.Bot, db: "Database") -> None:
        self.bot = bot
        self.db = db
        self._active_views: dict[int, GiveawayEntryView] = {}

    async def cog_load(self) -> None:
        await self._restore_active_views()
        self._giveaway_loop.start()

    async def cog_unload(self) -> None:
        self._giveaway_loop.cancel()

    async def _restore_active_views(self) -> None:
        rows = await self.db.get_active_giveaways()
        for row in rows:
            view = GiveawayEntryView(self, row["id"])
            self.bot.add_view(view, message_id=row["message_id"])
            self._active_views[row["id"]] = view

    # ------------------------------------------------------------------
    # Background loop: end expired giveaways
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def _giveaway_loop(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = await self.db.get_active_giveaways()
        for row in rows:
            if row["end_time"] <= now:
                await self._end_giveaway(row["id"])

    @_giveaway_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Core: handle entry
    # ------------------------------------------------------------------

    async def handle_entry(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        row = await self.db.get_giveaway(giveaway_id)
        if not row or row["status"] != "active":
            await interaction.response.send_message("❌ This giveaway has ended.", ephemeral=True)
            return

        entered = await self.db.enter_giveaway(giveaway_id, interaction.user.id)
        count = await self.db.get_giveaway_entry_count(giveaway_id)

        if entered:
            await interaction.response.send_message(
                f"✅ You entered the giveaway! **{count}** total entries.", ephemeral=True
            )
        else:
            left = await self.db.leave_giveaway(giveaway_id, interaction.user.id)
            count = await self.db.get_giveaway_entry_count(giveaway_id)
            await interaction.response.send_message(
                f"↩️ You withdrew your entry. **{count}** total entries.", ephemeral=True
            )

        await self._update_embed(row, count=count)

    # ------------------------------------------------------------------
    # Core: end giveaway and pick winners
    # ------------------------------------------------------------------

    async def _end_giveaway(self, giveaway_id: int) -> list[int]:
        row = await self.db.get_giveaway(giveaway_id)
        if not row or row["status"] != "active":
            return []

        await self.db.end_giveaway(giveaway_id)

        entries = await self.db.get_giveaway_entries(giveaway_id)
        winner_count = min(row["winner_count"], len(entries))
        winners = random.sample(entries, winner_count) if entries else []

        end_time = datetime.fromisoformat(row["end_time"])
        count = len(entries)
        em = _giveaway_embed(
            giveaway_id=giveaway_id,
            prize=row["prize"],
            end_time=end_time,
            winner_count=row["winner_count"],
            host_id=row["host_id"],
            entry_count=count,
            ended=True,
            winners=winners,
        )

        guild = self.bot.get_guild(row["guild_id"])
        if guild and row["message_id"]:
            channel = guild.get_channel(row["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(row["message_id"])
                    await msg.edit(embed=em, view=None)
                except (discord.NotFound, discord.Forbidden):
                    pass

                if winners:
                    winner_mentions = " ".join(f"<@{w}>" for w in winners)
                    await channel.send(
                        f"🎉 Giveaway **#{giveaway_id}** ended! "
                        f"Congratulations {winner_mentions}! You won **{row['prize']}**!"
                    )
                    for wid in winners:
                        user = self.bot.get_user(wid)
                        if user:
                            try:
                                await user.send(
                                    f"🎉 You won the giveaway for **{row['prize']}** in **{guild.name}**!"
                                )
                            except discord.Forbidden:
                                pass
                else:
                    await channel.send(f"😢 Giveaway **#{giveaway_id}** ended with no valid entries.")

        if giveaway_id in self._active_views:
            del self._active_views[giveaway_id]

        return winners

    async def _update_embed(self, row, count: int) -> None:
        guild = self.bot.get_guild(row["guild_id"])
        if not guild or not row["message_id"]:
            return
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            msg = await channel.fetch_message(row["message_id"])
            end_time = datetime.fromisoformat(row["end_time"])
            em = _giveaway_embed(
                giveaway_id=row["id"],
                prize=row["prize"],
                end_time=end_time,
                winner_count=row["winner_count"],
                host_id=row["host_id"],
                entry_count=count,
            )
            await msg.edit(embed=em)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ==================================================================
    # Slash commands
    # ==================================================================

    giveaway_group = app_commands.Group(name="giveaway", description="Giveaway commands")

    @giveaway_group.command(name="start", description="Start a new giveaway")
    @app_commands.describe(
        prize="What you're giving away",
        duration="Duration e.g. 1d2h30m",
        winners="Number of winners (default 1)",
        channel="Channel to post in (default: current)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def start(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: int = 1,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        td = _parse_duration(duration)
        if td is None:
            await interaction.response.send_message(
                "❌ Invalid duration. Use format like `1d`, `2h`, `30m`, or `1d12h`.",
                ephemeral=True,
            )
            return

        winners = max(1, min(winners, 20))
        end_dt = datetime.now(timezone.utc) + td
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message("❌ Target must be a text channel.", ephemeral=True)
            return

        giveaway_id = await self.db.create_giveaway(
            guild_id=guild.id,
            channel_id=target_channel.id,
            prize=prize,
            end_time=end_dt.isoformat(),
            winner_count=winners,
            host_id=interaction.user.id,
        )

        em = _giveaway_embed(
            giveaway_id=giveaway_id,
            prize=prize,
            end_time=end_dt,
            winner_count=winners,
            host_id=interaction.user.id,
            entry_count=0,
        )
        view = GiveawayEntryView(self, giveaway_id)
        self._active_views[giveaway_id] = view
        msg = await target_channel.send(embed=em, view=view)
        await self.db.set_giveaway_message(giveaway_id, msg.id)
        self.bot.add_view(view, message_id=msg.id)

        await interaction.response.send_message(
            f"✅ Giveaway **#{giveaway_id}** started in {target_channel.mention}!", ephemeral=True
        )

    @giveaway_group.command(name="end", description="End a giveaway immediately and pick winners")
    @app_commands.describe(giveaway_id="The giveaway ID to end")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def end(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        row = await self.db.get_giveaway(giveaway_id)
        if not row or row["guild_id"] != interaction.guild_id or row["status"] != "active":
            await interaction.response.send_message("❌ Active giveaway not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        winners = await self._end_giveaway(giveaway_id)
        winner_text = ", ".join(f"<@{w}>" for w in winners) if winners else "No entries"
        await interaction.followup.send(f"✅ Giveaway **#{giveaway_id}** ended. Winners: {winner_text}")

    @giveaway_group.command(name="reroll", description="Reroll winners for an ended giveaway")
    @app_commands.describe(giveaway_id="The giveaway ID to reroll")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def reroll(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        row = await self.db.get_giveaway(giveaway_id)
        if not row or row["guild_id"] != interaction.guild_id or row["status"] != "ended":
            await interaction.response.send_message("❌ Ended giveaway not found.", ephemeral=True)
            return

        entries = await self.db.get_giveaway_entries(giveaway_id)
        winner_count = min(row["winner_count"], len(entries))
        if not entries:
            await interaction.response.send_message("❌ No entries to reroll from.", ephemeral=True)
            return

        winners = random.sample(entries, winner_count)
        winner_mentions = " ".join(f"<@{w}>" for w in winners)

        guild = interaction.guild
        assert guild is not None
        channel = guild.get_channel(row["channel_id"])
        if isinstance(channel, discord.TextChannel):
            await channel.send(
                f"🔄 Reroll! New winners for giveaway **#{giveaway_id}** ({row['prize']}): {winner_mentions}!"
            )

        await interaction.response.send_message(
            f"✅ Rerolled! New winners: {winner_mentions}", ephemeral=True
        )

    @giveaway_group.command(name="cancel", description="Cancel an active giveaway")
    @app_commands.describe(giveaway_id="The giveaway ID to cancel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        row = await self.db.get_giveaway(giveaway_id)
        if not row or row["guild_id"] != interaction.guild_id or row["status"] != "active":
            await interaction.response.send_message("❌ Active giveaway not found.", ephemeral=True)
            return

        await self.db.end_giveaway(giveaway_id)

        guild = interaction.guild
        assert guild is not None
        channel = guild.get_channel(row["channel_id"])
        if isinstance(channel, discord.TextChannel) and row["message_id"]:
            try:
                msg = await channel.fetch_message(row["message_id"])
                end_time = datetime.fromisoformat(row["end_time"])
                em = _giveaway_embed(
                    giveaway_id=giveaway_id,
                    prize=row["prize"],
                    end_time=end_time,
                    winner_count=row["winner_count"],
                    host_id=row["host_id"],
                    ended=True,
                    winners=[],
                )
                em.description = f"**Prize:** {row['prize']}\n\n*Giveaway cancelled.*"
                await msg.edit(embed=em, view=None)
            except (discord.NotFound, discord.Forbidden):
                pass

        if giveaway_id in self._active_views:
            del self._active_views[giveaway_id]

        await interaction.response.send_message(f"✅ Giveaway **#{giveaway_id}** cancelled.", ephemeral=True)

    @giveaway_group.command(name="list", description="List active giveaways in this server")
    @app_commands.guild_only()
    async def list_giveaways(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        rows = await self.db.get_active_giveaways(guild.id)
        if not rows:
            await interaction.response.send_message("No active giveaways.", ephemeral=True)
            return

        lines = []
        for row in rows:
            end_dt = datetime.fromisoformat(row["end_time"])
            ts = int(end_dt.timestamp())
            lines.append(
                f"**#{row['id']}** — {row['prize']} · {row['winner_count']} winner(s) · ends <t:{ts}:R>"
            )

        em = discord.Embed(
            title=f"🎉 Active Giveaways — {guild.name}",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    @giveaway_group.command(name="info", description="View details and entry count for a giveaway")
    @app_commands.describe(giveaway_id="The giveaway ID")
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        row = await self.db.get_giveaway(giveaway_id)
        if not row or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
            return

        count = await self.db.get_giveaway_entry_count(giveaway_id)
        end_time = datetime.fromisoformat(row["end_time"])
        em = _giveaway_embed(
            giveaway_id=giveaway_id,
            prize=row["prize"],
            end_time=end_time,
            winner_count=row["winner_count"],
            host_id=row["host_id"],
            entry_count=count,
            ended=row["status"] == "ended",
        )
        await interaction.response.send_message(embed=em, ephemeral=True)
