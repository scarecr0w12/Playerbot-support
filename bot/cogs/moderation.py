"""Full moderation cog — warn / mute / kick / ban / lock / slowmode with case tracking.

Every action:
1. Executes the Discord action (timeout, kick, ban …)
2. Records a case in the database
3. Fires a mod-log entry via the ModLogging cog
4. Optionally DMs the target user

Warnings accumulate; exceeding the threshold triggers an automatic action
(configurable per-guild via ``/modset`` commands, stored in the database).
"""

from __future__ import annotations

import asyncio
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

CASES_PER_PAGE = 5

COLOUR_MAP = {
    "warn": discord.Color.yellow(),
    "mute": discord.Color.orange(),
    "unmute": discord.Color.green(),
    "kick": discord.Color.dark_orange(),
    "ban": discord.Color.red(),
    "unban": discord.Color.green(),
    "softban": discord.Color.dark_red(),
    "tempban": discord.Color.red(),
    "lockdown": discord.Color.dark_grey(),
}


# ------------------------------------------------------------------
# Modal: reason input for moderation actions
# ------------------------------------------------------------------

class ReasonModal(discord.ui.Modal):
    """Popup modal that asks the moderator for a reason."""

    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Provide a reason for this action…",
        required=False,
        max_length=1024,
    )

    def __init__(self, title: str, callback) -> None:
        super().__init__(title=title)
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._callback(interaction, self.reason.value or "No reason provided")


# ------------------------------------------------------------------
# Paginator view for /modlog
# ------------------------------------------------------------------

class ModLogPaginator(discord.ui.View):
    """Interactive paginator for moderation case lists."""

    def __init__(self, embeds: list[discord.Embed]) -> None:
        super().__init__(timeout=120)
        self.embeds = embeds
        self.page = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= len(self.embeds) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)


class ModerationCog(commands.Cog, name="Moderation"):
    """Core moderation commands: warn, mute, kick, ban, etc."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    @property
    def mod_log(self) -> ModLoggingCog | None:
        return self.bot.get_cog("ModLogging")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Moderation command group
    # ------------------------------------------------------------------

    mod_group = app_commands.Group(name="mod", description="Moderation commands")

    @mod_group.command(name="warn", description="Issue a warning to a member")
    @app_commands.describe(member="The member to warn", reason="Reason for the warning")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(
        self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None
    ) -> None:
        reason = reason or "No reason provided"
        guild = interaction.guild
        assert guild is not None

        warn_id = await self.db.add_warning(guild.id, member.id, interaction.user.id, reason)
        case_id = await self.db.add_case(guild.id, member.id, interaction.user.id, "warn", reason)

        active = await self.db.get_active_warnings(guild.id, member.id)
        count = len(active)

        # DM the user
        try:
            await member.send(
                f"⚠️ You have been warned in **{guild.name}**.\n"
                f"**Reason:** {reason}\n"
                f"**Total active warnings:** {count}"
            )
        except discord.Forbidden:
            pass

        await interaction.response.send_message(
            f"⚠️ {member.mention} has been warned (#{count}). Case #{case_id}.", ephemeral=True
        )

        if self.mod_log:
            await self.mod_log.log(
                guild, action="warn", target=member, moderator=interaction.user,
                reason=reason, case_id=case_id, extra=f"Active warnings: {count}",
            )

        # Auto-action if threshold exceeded
        threshold = await self.db.get_setting_int(guild.id, "mod_max_warnings_before_action")
        if count >= threshold:
            await self._auto_action(interaction, member, count)

    async def _auto_action(
        self, interaction: discord.Interaction, member: discord.Member, warn_count: int
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        action = await self.db.get_setting(guild.id, "mod_warning_action")

        auto_reason = f"Automatic {action}: reached {warn_count} warnings"
        if action == "mute":
            mute_mins = await self.db.get_setting_int(guild.id, "mod_mute_duration_minutes")
            duration = timedelta(minutes=mute_mins)
            try:
                await member.timeout(duration, reason=auto_reason)
            except discord.Forbidden:
                return
            case_id = await self.db.add_case(
                guild.id, member.id, self.bot.user.id, "mute", auto_reason,  # type: ignore[union-attr]
                duration=int(duration.total_seconds()),
            )
        elif action == "kick":
            try:
                await member.kick(reason=auto_reason)
            except discord.Forbidden:
                return
            case_id = await self.db.add_case(
                guild.id, member.id, self.bot.user.id, "kick", auto_reason  # type: ignore[union-attr]
            )
        elif action == "ban":
            try:
                await member.ban(reason=auto_reason)
            except discord.Forbidden:
                return
            case_id = await self.db.add_case(
                guild.id, member.id, self.bot.user.id, "ban", auto_reason  # type: ignore[union-attr]
            )
        else:
            return

        if self.mod_log:
            await self.mod_log.log(
                guild, action=action, target=member, moderator=self.bot.user,  # type: ignore[arg-type]
                reason=auto_reason, case_id=case_id,
            )
        await self.db.clear_warnings(guild.id, member.id)

    # ------------------------------------------------------------------
    # /mute
    # ------------------------------------------------------------------

    @mod_group.command(name="mute", description="Timeout a member")
    @app_commands.describe(
        member="The member to timeout",
        duration="Timeout duration (e.g., 10m, 1h, 1d)",
        reason="Reason for the timeout"
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration_minutes: int | None = None,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"
        minutes = duration_minutes or await self.db.get_setting_int(guild.id, "mod_mute_duration_minutes")
        duration = timedelta(minutes=minutes)

        try:
            await member.timeout(duration, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to mute this member.", ephemeral=True)
            return

        case_id = await self.db.add_case(
            guild.id, member.id, interaction.user.id, "mute", reason,
            duration=int(duration.total_seconds()),
        )

        try:
            await member.send(
                f"🔇 You have been muted in **{guild.name}** for {minutes} minute(s).\n**Reason:** {reason}"
            )
        except discord.Forbidden:
            pass

        await interaction.response.send_message(
            f"🔇 {member.mention} has been muted for {minutes}m. Case #{case_id}.", ephemeral=True
        )

        if self.mod_log:
            await self.mod_log.log(
                guild, action="mute", target=member, moderator=interaction.user,
                reason=reason, case_id=case_id, extra=f"Duration: {minutes} minute(s)",
            )

    # ------------------------------------------------------------------
    # /unmute
    # ------------------------------------------------------------------

    @mod_group.command(name="unmute", description="Remove timeout from a member")
    @app_commands.describe(member="The member to unmute", reason="Reason")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unmute(
        self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"

        try:
            await member.timeout(None, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to unmute this member.", ephemeral=True)
            return

        case_id = await self.db.add_case(guild.id, member.id, interaction.user.id, "unmute", reason)
        await interaction.response.send_message(
            f"🔊 {member.mention} has been unmuted. Case #{case_id}.", ephemeral=True
        )

        if self.mod_log:
            await self.mod_log.log(
                guild, action="unmute", target=member, moderator=interaction.user,
                reason=reason, case_id=case_id,
            )

    # ------------------------------------------------------------------
    # /kick
    # ------------------------------------------------------------------

    @mod_group.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="The member to kick", reason="Reason for the kick")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(
        self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"

        try:
            await member.send(f"👢 You have been kicked from **{guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to kick this member.", ephemeral=True)
            return

        case_id = await self.db.add_case(guild.id, member.id, interaction.user.id, "kick", reason)
        await interaction.response.send_message(
            f"👢 {member.mention} has been kicked. Case #{case_id}.", ephemeral=True
        )

        if self.mod_log:
            await self.mod_log.log(
                guild, action="kick", target=member, moderator=interaction.user,
                reason=reason, case_id=case_id,
            )

    # ------------------------------------------------------------------
    # /ban
    # ------------------------------------------------------------------

    @mod_group.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(member="The member to ban", reason="Reason for the ban")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(
        self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"

        try:
            await member.send(f"🔨 You have been banned from **{guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

        try:
            await member.ban(reason=reason, delete_message_days=0)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban this member.", ephemeral=True)
            return

        case_id = await self.db.add_case(guild.id, member.id, interaction.user.id, "ban", reason)
        await interaction.response.send_message(
            f"🔨 {member.mention} has been banned. Case #{case_id}.", ephemeral=True
        )

        if self.mod_log:
            await self.mod_log.log(
                guild, action="ban", target=member, moderator=interaction.user,
                reason=reason, case_id=case_id,
            )

    # ------------------------------------------------------------------
    # /unban
    # ------------------------------------------------------------------

    @mod_group.command(name="unban", description="Unban a user by ID")
    @app_commands.describe(user_id="The ID of the user to unban", reason="Reason")
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(
        self, interaction: discord.Interaction, user_id: str, reason: str | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"

        try:
            user = await self.bot.fetch_user(int(user_id))
            await guild.unban(user, reason=reason)
        except (discord.NotFound, ValueError):
            await interaction.response.send_message("❌ User not found.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to unban.", ephemeral=True)
            return

        case_id = await self.db.add_case(guild.id, user.id, interaction.user.id, "unban", reason)
        await interaction.response.send_message(
            f"✅ {user} has been unbanned. Case #{case_id}.", ephemeral=True
        )

        if self.mod_log:
            await self.mod_log.log(
                guild, action="unban", target=user, moderator=interaction.user,
                reason=reason, case_id=case_id,
            )

    # ------------------------------------------------------------------
    # /warnings
    # ------------------------------------------------------------------

    @mod_group.command(name="warnings", description="View active warnings for a member")
    @app_commands.describe(member="The member to check")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild = interaction.guild
        assert guild is not None
        active = await self.db.get_active_warnings(guild.id, member.id)

        if not active:
            await interaction.response.send_message(f"{member.mention} has no active warnings.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Warnings for {member}",
            color=discord.Color.yellow(),
        )
        for w in active:
            embed.add_field(
                name=f"Warning #{w['id']}",
                value=f"**Reason:** {w['reason'] or 'N/A'}\n**Date:** {w['created_at']}",
                inline=False,
            )
        embed.set_footer(text=f"Total: {len(active)} active warning(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /clearwarnings
    # ------------------------------------------------------------------

    @mod_group.command(name="clearwarnings", description="Clear all active warnings for a member")
    @app_commands.describe(member="The member whose warnings to clear")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild = interaction.guild
        assert guild is not None
        count = await self.db.clear_warnings(guild.id, member.id)
        await interaction.response.send_message(
            f"✅ Cleared {count} warning(s) for {member.mention}.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /modlog  –  paginated case viewer
    # ------------------------------------------------------------------

    @mod_group.command(name="modlog", description="View recent moderation cases (paginated)")
    @app_commands.describe(member="Filter by member (optional)", page="Page number to start on")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def modlog(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        page: int = 1,
    ) -> None:
        guild = interaction.guild
        assert guild is not None

        total = await self.db.count_cases(guild.id, user_id=member.id if member else None)
        if total == 0:
            await interaction.response.send_message("No cases found.", ephemeral=True)
            return

        all_cases = await self.db.get_cases(
            guild.id, user_id=member.id if member else None, limit=500
        )

        pages: list[discord.Embed] = []
        total_pages = max(1, (len(all_cases) + CASES_PER_PAGE - 1) // CASES_PER_PAGE)
        title_suffix = f" — {member}" if member else f" — {guild.name}"

        for chunk_start in range(0, len(all_cases), CASES_PER_PAGE):
            chunk = all_cases[chunk_start : chunk_start + CASES_PER_PAGE]
            page_num = chunk_start // CASES_PER_PAGE + 1
            embed = discord.Embed(
                title=f"Mod Log{title_suffix}",
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Page {page_num}/{total_pages} · {total} total case(s)")
            for c in chunk:
                dur = f" · {c['duration']//60}m" if c["duration"] else ""
                embed.add_field(
                    name=f"Case #{c['id']} — {c['action'].upper()}{dur}",
                    value=(
                        f"**User:** <@{c['user_id']}>\n"
                        f"**Mod:** <@{c['moderator_id']}>\n"
                        f"**Reason:** {c['reason'] or 'N/A'}\n"
                        f"**Date:** {c['created_at']}"
                    ),
                    inline=False,
                )
            pages.append(embed)

        start_page = max(0, min(page - 1, total_pages - 1))
        view = ModLogPaginator(pages)
        view.page = start_page
        view._update_buttons()
        await interaction.response.send_message(embed=pages[start_page], view=view, ephemeral=True)

    # ------------------------------------------------------------------
    # /case  –  view a single case
    # ------------------------------------------------------------------

    @mod_group.command(name="case", description="View a single moderation case by ID")
    @app_commands.describe(case_id="The case number to look up")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def case(self, interaction: discord.Interaction, case_id: int) -> None:
        guild = interaction.guild
        assert guild is not None
        c = await self.db.get_case_by_id(guild.id, case_id)
        if not c:
            await interaction.response.send_message(f"❌ Case #{case_id} not found.", ephemeral=True)
            return

        dur_str = f"{c['duration']//60} minute(s)" if c["duration"] else "Permanent"
        embed = discord.Embed(
            title=f"Case #{c['id']} — {c['action'].upper()}",
            color=COLOUR_MAP.get(c["action"], discord.Color.blurple()),
            timestamp=datetime.fromisoformat(c["created_at"]),
        )
        embed.add_field(name="User", value=f"<@{c['user_id']}> (`{c['user_id']}`)", inline=True)
        embed.add_field(name="Moderator", value=f"<@{c['moderator_id']}> (`{c['moderator_id']}`)", inline=True)
        embed.add_field(name="Reason", value=c["reason"] or "N/A", inline=False)
        if c["duration"]:
            embed.add_field(name="Duration", value=dur_str, inline=True)
        embed.set_footer(text=f"Case #{c['id']}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /editcase  –  edit a case reason
    # ------------------------------------------------------------------

    @mod_group.command(name="editcase", description="Edit the reason for a moderation case")
    @app_commands.describe(case_id="The case ID to edit", reason="New reason text")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def editcase(self, interaction: discord.Interaction, case_id: int, reason: str) -> None:
        guild = interaction.guild
        assert guild is not None
        updated = await self.db.update_case_reason(guild.id, case_id, reason)
        if not updated:
            await interaction.response.send_message(f"❌ Case #{case_id} not found.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Case #{case_id} reason updated.", ephemeral=True
        )
        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="case_edit",
                moderator=interaction.user,
                extra=f"Case #{case_id} reason updated to: {reason}",
            )

    # ------------------------------------------------------------------
    # /delwarn  –  remove a specific warning by ID
    # ------------------------------------------------------------------

    @mod_group.command(name="delwarn", description="Remove a specific warning by its ID")
    @app_commands.describe(warning_id="The warning ID to remove")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def delwarn(self, interaction: discord.Interaction, warning_id: int) -> None:
        guild = interaction.guild
        assert guild is not None
        removed = await self.db.delete_warning(guild.id, warning_id)
        if not removed:
            await interaction.response.send_message(
                f"❌ Warning #{warning_id} not found or already cleared.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Warning #{warning_id} removed.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /note  –  add a private staff note on a user
    # ------------------------------------------------------------------

    @mod_group.command(name="note", description="Add a private staff note on a user")
    @app_commands.describe(member="The member to note", note="Note content (staff-only)")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def note(self, interaction: discord.Interaction, member: discord.Member, note: str) -> None:
        guild = interaction.guild
        assert guild is not None
        note_id = await self.db.add_note(guild.id, member.id, interaction.user.id, note)
        await interaction.response.send_message(
            f"📝 Note #{note_id} added for {member.mention}.", ephemeral=True
        )
        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="note_add",
                target=member,
                moderator=interaction.user,
                extra=note,
            )

    # ------------------------------------------------------------------
    # /notes  –  view staff notes for a user
    # ------------------------------------------------------------------

    @mod_group.command(name="notes", description="View staff notes for a user")
    @app_commands.describe(member="The member to look up")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def notes(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild = interaction.guild
        assert guild is not None
        all_notes = await self.db.get_notes(guild.id, member.id)
        if not all_notes:
            await interaction.response.send_message(
                f"No notes on {member.mention}.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"Notes for {member}",
            color=discord.Color.blurple(),
        )
        for n in all_notes:
            embed.add_field(
                name=f"Note #{n['id']} by <@{n['moderator_id']}>",
                value=f"{n['note']}\n*{n['created_at']}*",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /delnote  –  delete a staff note
    # ------------------------------------------------------------------

    @mod_group.command(name="delnote", description="Delete a staff note by ID")
    @app_commands.describe(note_id="The note ID to delete")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def delnote(self, interaction: discord.Interaction, note_id: int) -> None:
        guild = interaction.guild
        assert guild is not None
        removed = await self.db.delete_note(guild.id, note_id)
        if not removed:
            await interaction.response.send_message(f"❌ Note #{note_id} not found.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Note #{note_id} deleted.", ephemeral=True)

    # ------------------------------------------------------------------
    # /softban  –  ban + immediate unban (removes recent messages)
    # ------------------------------------------------------------------

    @mod_group.command(name="softban", description="Softban a member (ban then unban to clear messages)")
    @app_commands.describe(
        member="The member to softban",
        delete_message_days="Days of messages to delete (1–7)",
        reason="Reason",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def softban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        delete_message_days: int = 1,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"
        delete_message_days = max(1, min(7, delete_message_days))

        try:
            await member.send(
                f"🔨 You have been softbanned from **{guild.name}** (messages cleared).\n**Reason:** {reason}"
            )
        except discord.Forbidden:
            pass

        try:
            await guild.ban(member, reason=f"[SOFTBAN] {reason}", delete_message_days=delete_message_days)
            await guild.unban(member, reason="Softban — auto unban")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban this member.", ephemeral=True)
            return

        case_id = await self.db.add_case(guild.id, member.id, interaction.user.id, "softban", reason)
        await interaction.response.send_message(
            f"🔨 {member.mention} has been softbanned. Case #{case_id}.", ephemeral=True
        )
        if self.mod_log:
            await self.mod_log.log(
                guild, action="softban", target=member, moderator=interaction.user,
                reason=reason, case_id=case_id,
                extra=f"Messages deleted: {delete_message_days} day(s)",
            )

    # ------------------------------------------------------------------
    # /tempban  –  ban with auto-unban after a duration
    # ------------------------------------------------------------------

    @mod_group.command(name="tempban", description="Temporarily ban a member for a set duration")
    @app_commands.describe(
        member="The member to ban",
        duration_hours="Duration in hours",
        reason="Reason",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def tempban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration_hours: int,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"

        try:
            await member.send(
                f"🔨 You have been temporarily banned from **{guild.name}** for {duration_hours}h.\n**Reason:** {reason}"
            )
        except discord.Forbidden:
            pass

        try:
            await guild.ban(member, reason=f"[TEMPBAN {duration_hours}h] {reason}", delete_message_days=0)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban this member.", ephemeral=True)
            return

        case_id = await self.db.add_case(
            guild.id, member.id, interaction.user.id, "tempban", reason,
            duration=duration_hours * 3600,
        )
        await interaction.response.send_message(
            f"🔨 {member.mention} has been tempbanned for {duration_hours}h. Case #{case_id}.", ephemeral=True
        )
        if self.mod_log:
            await self.mod_log.log(
                guild, action="tempban", target=member, moderator=interaction.user,
                reason=reason, case_id=case_id,
                extra=f"Duration: {duration_hours} hour(s)",
            )

        await asyncio.sleep(duration_hours * 3600)
        try:
            await guild.unban(member, reason=f"Tempban expired (case #{case_id})")
            if self.mod_log:
                await self.mod_log.log(
                    guild, action="unban", target=member,
                    moderator=self.bot.user,  # type: ignore[arg-type]
                    reason=f"Tempban expired (case #{case_id})",
                )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # /massban  –  ban multiple users by ID
    # ------------------------------------------------------------------

    @mod_group.command(name="massban", description="Ban multiple users by their IDs (space-separated)")
    @app_commands.describe(
        user_ids="Space-separated list of user IDs",
        reason="Reason for the mass ban",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def massban(
        self, interaction: discord.Interaction, user_ids: str, reason: str | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "Mass ban"
        await interaction.response.defer(ephemeral=True)

        ids = [s.strip() for s in user_ids.split() if s.strip().isdigit()]
        if not ids:
            await interaction.followup.send("❌ No valid user IDs provided.", ephemeral=True)
            return

        banned, failed = 0, 0
        for uid in ids:
            try:
                user = await self.bot.fetch_user(int(uid))
                await guild.ban(user, reason=reason, delete_message_days=0)
                await self.db.add_case(guild.id, user.id, interaction.user.id, "ban", reason)
                banned += 1
            except Exception:
                failed += 1

        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="massban",
                moderator=interaction.user,
                reason=reason,
                extra=f"Banned: {banned} · Failed: {failed}",
            )
        await interaction.followup.send(
            f"🔨 Mass ban complete: **{banned}** banned, **{failed}** failed.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /slowmode  –  set channel slowmode
    # ------------------------------------------------------------------

    @mod_group.command(name="slowmode", description="Set slowmode for the current or a specified channel")
    @app_commands.describe(
        seconds="Slowmode delay in seconds (0 = disable, max 21600)",
        channel="Channel to apply slowmode to (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: int,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ This command only works in text channels.", ephemeral=True)
            return
        seconds = max(0, min(21600, seconds))
        try:
            await target.edit(slowmode_delay=seconds)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to edit that channel.", ephemeral=True)
            return
        state = f"{seconds}s" if seconds > 0 else "disabled"
        await interaction.response.send_message(
            f"⏱️ Slowmode for {target.mention} set to **{state}**.", ephemeral=True
        )
        if self.mod_log:
            assert interaction.guild is not None
            await self.mod_log.log(
                interaction.guild,
                action="slowmode",
                moderator=interaction.user,
                extra=f"Channel: {target.mention} → {state}",
            )

    # ------------------------------------------------------------------
    # /lock  –  deny @everyone from sending messages in a channel
    # ------------------------------------------------------------------

    @mod_group.command(name="lock", description="Lock a channel — prevent @everyone from sending messages")
    @app_commands.describe(
        channel="Channel to lock (defaults to current)",
        reason="Reason",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ This command only works in text channels.", ephemeral=True)
            return
        try:
            await target.set_permissions(
                guild.default_role,
                send_messages=False,
                reason=reason,
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to edit that channel.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔒 {target.mention} has been locked.", ephemeral=True
        )
        try:
            await target.send(f"🔒 This channel has been locked by a moderator.")
        except discord.Forbidden:
            pass
        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="channel_lock",
                moderator=interaction.user,
                reason=reason,
                extra=f"Channel: {target.mention}",
            )

    # ------------------------------------------------------------------
    # /unlock  –  restore @everyone send permissions
    # ------------------------------------------------------------------

    @mod_group.command(name="unlock", description="Unlock a channel — restore @everyone send permissions")
    @app_commands.describe(
        channel="Channel to unlock (defaults to current)",
        reason="Reason",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "No reason provided"
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ This command only works in text channels.", ephemeral=True)
            return
        try:
            await target.set_permissions(
                guild.default_role,
                send_messages=None,
                reason=reason,
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to edit that channel.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔓 {target.mention} has been unlocked.", ephemeral=True
        )
        try:
            await target.send("🔓 This channel has been unlocked.")
        except discord.Forbidden:
            pass
        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="channel_unlock",
                moderator=interaction.user,
                reason=reason,
                extra=f"Channel: {target.mention}",
            )

    # ------------------------------------------------------------------
    # /lockdown  –  lock all channels in the server
    # ------------------------------------------------------------------

    @mod_group.command(name="lockdown", description="Lock ALL text channels (server-wide lockdown)")
    @app_commands.describe(reason="Reason for lockdown")
    @app_commands.checks.has_permissions(administrator=True)
    async def lockdown(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "Server lockdown"
        await interaction.response.defer(ephemeral=True)

        locked = 0
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(guild.default_role, send_messages=False, reason=reason)
                locked += 1
            except discord.Forbidden:
                pass

        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="lockdown",
                moderator=interaction.user,
                reason=reason,
                extra=f"Locked {locked} channel(s)",
            )
        await interaction.followup.send(
            f"🔒 Server lockdown active — **{locked}** channels locked.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /unlockdown  –  lift server-wide lockdown
    # ------------------------------------------------------------------

    @mod_group.command(name="unlockdown", description="Lift server-wide lockdown (restore all channels)")
    @app_commands.describe(reason="Reason")
    @app_commands.checks.has_permissions(administrator=True)
    async def unlockdown(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        reason = reason or "Lockdown lifted"
        await interaction.response.defer(ephemeral=True)

        unlocked = 0
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(guild.default_role, send_messages=None, reason=reason)
                unlocked += 1
            except discord.Forbidden:
                pass

        if self.mod_log:
            await self.mod_log.log(
                guild,
                action="unlockdown",
                moderator=interaction.user,
                reason=reason,
                extra=f"Unlocked {unlocked} channel(s)",
            )
        await interaction.followup.send(
            f"🔓 Lockdown lifted — **{unlocked}** channels restored.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /userhistory  –  full mod summary for a user
    # ------------------------------------------------------------------

    @mod_group.command(name="userhistory", description="Full moderation history and notes for a user")
    @app_commands.describe(member="The member to look up")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def userhistory(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild = interaction.guild
        assert guild is not None
        cases = await self.db.get_cases(guild.id, user_id=member.id, limit=20)
        warnings = await self.db.get_active_warnings(guild.id, member.id)
        all_notes = await self.db.get_notes(guild.id, member.id)
        total_cases = await self.db.count_cases(guild.id, user_id=member.id)

        embed = discord.Embed(
            title=f"Mod History — {member}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "N/A", inline=True)
        embed.add_field(name="Active Warnings", value=str(len(warnings)), inline=True)
        embed.add_field(name="Total Cases", value=str(total_cases), inline=True)
        embed.add_field(name="Staff Notes", value=str(len(all_notes)), inline=True)
        embed.add_field(name="Currently Timed Out", value="Yes" if member.is_timed_out() else "No", inline=True)

        if cases:
            recent = "\n".join(
                f"**#{c['id']}** {c['action'].upper()} — {c['reason'] or 'N/A'} ({c['created_at'][:10]})"
                for c in cases[:5]
            )
            embed.add_field(name=f"Recent Cases (showing {min(5, len(cases))} of {total_cases})", value=recent, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /modset  –  admin commands to configure mod settings per-guild
    # ------------------------------------------------------------------

    modset_group = app_commands.Group(name="modset", description="Moderation settings (admin)")

    @modset_group.command(name="mute_duration", description="Set default mute duration in minutes")
    @app_commands.describe(minutes="Default mute duration in minutes")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_mute_duration(self, interaction: discord.Interaction, minutes: int) -> None:
        await self.db.set_guild_config(interaction.guild_id, "mod_mute_duration_minutes", str(minutes))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Default mute duration set to **{minutes}** minute(s).", ephemeral=True
        )

    @modset_group.command(name="warn_threshold", description="Set number of warnings before auto-action")
    @app_commands.describe(count="Number of warnings before automatic action")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_warn_threshold(self, interaction: discord.Interaction, count: int) -> None:
        await self.db.set_guild_config(interaction.guild_id, "mod_max_warnings_before_action", str(count))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Auto-action threshold set to **{count}** warnings.", ephemeral=True
        )

    @modset_group.command(name="warn_action", description="Set action when warning threshold is exceeded")
    @app_commands.describe(action="Action to take: mute, kick, or ban")
    @app_commands.choices(action=[
        app_commands.Choice(name="Mute", value="mute"),
        app_commands.Choice(name="Kick", value="kick"),
        app_commands.Choice(name="Ban", value="ban"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def set_warn_action(self, interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        await self.db.set_guild_config(interaction.guild_id, "mod_warning_action", action.value)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Warning auto-action set to **{action.name}**.", ephemeral=True
        )

    @modset_group.command(name="show", description="Show current moderation settings")
    @app_commands.checks.has_permissions(administrator=True)
    async def show_mod_settings(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        mute_dur = await self.db.get_setting(guild_id, "mod_mute_duration_minutes")
        threshold = await self.db.get_setting(guild_id, "mod_max_warnings_before_action")
        action = await self.db.get_setting(guild_id, "mod_warning_action")
        embed = discord.Embed(title="⚙️ Moderation Settings", color=discord.Color.blurple())
        embed.add_field(name="Default mute duration", value=f"{mute_dur} minute(s)", inline=True)
        embed.add_field(name="Warning threshold", value=f"{threshold} warnings", inline=True)
        embed.add_field(name="Auto-action", value=action, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
