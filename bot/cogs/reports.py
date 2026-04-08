"""Reports cog — user-to-staff reporting system.

Users submit reports via ``/report``; staff review, resolve, or dismiss them.

Inspired by Red-DiscordBot's Reports cog.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.database import Database
    from bot.cogs.mod_logging import ModLoggingCog

logger = logging.getLogger(__name__)


class ReportModal(discord.ui.Modal, title="Report a User"):
    """Modal popup for submitting a report."""

    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Describe why you are reporting this user…",
        max_length=1500,
    )

    def __init__(self, cog: ReportsCog, reported_user: discord.Member) -> None:
        super().__init__()
        self.cog = cog
        self.reported_user = reported_user

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_report(interaction, self.reported_user, self.reason.value)


class ReportsCog(commands.Cog, name="Reports"):
    """User reporting system: submit, review, resolve."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

        # Context menu: right-click → Report User
        self.ctx_menu = app_commands.ContextMenu(name="Report User", callback=self.report_context_menu)
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @property
    def mod_log(self) -> ModLoggingCog | None:
        return self.bot.get_cog("ModLogging")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Context menu handler
    # ------------------------------------------------------------------

    async def report_context_menu(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.send_modal(ReportModal(self, member))

    # ------------------------------------------------------------------
    # /report
    # ------------------------------------------------------------------

    @app_commands.command(name="report", description="Report a user to the staff team")
    @app_commands.describe(member="The user to report", reason="Why you are reporting them")
    async def report(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ) -> None:
        await self.submit_report(interaction, member, reason)

    async def submit_report(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ) -> None:
        guild = interaction.guild
        assert guild is not None

        report_id = await self.db.create_report(guild.id, interaction.user.id, member.id, reason)

        await interaction.response.send_message(
            f"✅ Report #{report_id} submitted. Staff will review it soon.", ephemeral=True
        )

        # Notify in mod-log channel
        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="report",
                target=member,
                moderator=interaction.user,
                reason=reason,
                extra=f"**Report #{report_id}**",
            )

        # Also try sending to a reports channel if configured
        reports_ch_raw = await self.db.get_guild_config(guild.id, "reports_channel")
        if reports_ch_raw:
            ch = guild.get_channel(int(reports_ch_raw))
            if isinstance(ch, discord.TextChannel):
                embed = discord.Embed(
                    title=f"📩 New Report #{report_id}",
                    color=discord.Color.orange(),
                )
                embed.add_field(name="Reported User", value=f"{member.mention} ({member.id})", inline=True)
                embed.add_field(name="Reporter", value=f"{interaction.user.mention}", inline=True)
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.set_footer(text=f"Use /report_resolve {report_id} to handle this report")
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass

    # ------------------------------------------------------------------
    # /report_resolve
    # ------------------------------------------------------------------

    @app_commands.command(name="report_resolve", description="Resolve an open report")
    @app_commands.describe(
        report_id="The report ID to resolve",
        note="Resolution note",
        dismiss="Dismiss instead of resolve",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def report_resolve(
        self,
        interaction: discord.Interaction,
        report_id: int,
        note: str | None = None,
        dismiss: bool = False,
    ) -> None:
        status = "dismissed" if dismiss else "resolved"
        success = await self.db.resolve_report(report_id, interaction.user.id, note, status)
        if success:
            await interaction.response.send_message(
                f"✅ Report #{report_id} **{status}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ Report #{report_id} not found or already resolved.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /reports — list open reports
    # ------------------------------------------------------------------

    @app_commands.command(name="reports", description="View open reports")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def reports_list(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        reports = await self.db.get_open_reports(guild.id)
        if not reports:
            await interaction.response.send_message("No open reports.", ephemeral=True)
            return

        embed = discord.Embed(title="📩 Open Reports", color=discord.Color.orange())
        for r in reports[:20]:
            embed.add_field(
                name=f"Report #{r['id']}",
                value=(
                    f"**Reported:** <@{r['reported_user_id']}>\n"
                    f"**By:** <@{r['reporter_id']}>\n"
                    f"**Reason:** {(r['reason'] or 'N/A')[:200]}\n"
                    f"**Date:** {r['created_at']}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /set_reports_channel
    # ------------------------------------------------------------------

    @app_commands.command(name="set_reports_channel", description="Set the channel for report notifications")
    @app_commands.describe(channel="The channel to send report embeds to")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_reports_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.db.set_guild_config(interaction.guild_id, "reports_channel", str(channel.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Reports channel set to {channel.mention}.", ephemeral=True
        )
