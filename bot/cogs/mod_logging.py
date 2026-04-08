"""Mod-logging cog — sends rich embed audit logs to a configured channel.

Other cogs call ``mod_log.log(...)`` to record mod actions.
This cog also hooks into a wide range of Discord gateway events to
auto-log server activity without any cog calling in.

Logged events
-------------
Mod actions  : warn, mute, unmute, kick, ban, unban, softban, tempban,
               massban, case_edit, note_add, slowmode,
               channel_lock, channel_unlock, lockdown, unlockdown
Message      : message_delete, message_edit, bulk_message_delete
Member       : member_join, member_leave, member_ban, member_unban,
               member_nickname_change, member_roles_update, member_timeout
Channel      : channel_create, channel_delete, channel_update
Role         : role_create, role_delete, role_update
Voice        : voice_join, voice_leave, voice_move
Ticket       : ticket_open, ticket_close
AutoMod      : filter_trigger
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.database import Database

logger = logging.getLogger(__name__)

COLOUR_MAP: dict[str, discord.Color] = {
    # ── Mod actions ────────────────────────────────────────────────────
    "warn":             discord.Color.yellow(),
    "mute":             discord.Color.orange(),
    "unmute":           discord.Color.green(),
    "kick":             discord.Color.dark_orange(),
    "ban":              discord.Color.red(),
    "unban":            discord.Color.green(),
    "softban":          discord.Color.dark_red(),
    "tempban":          discord.Color.red(),
    "massban":          discord.Color.dark_red(),
    "case_edit":        discord.Color.light_grey(),
    "note_add":         discord.Color.blurple(),
    "slowmode":         discord.Color.blue(),
    "channel_lock":     discord.Color.dark_orange(),
    "channel_unlock":   discord.Color.green(),
    "lockdown":         discord.Color.dark_red(),
    "unlockdown":       discord.Color.green(),
    # ── Messages ───────────────────────────────────────────────────────
    "message_delete":   discord.Color.dark_grey(),
    "message_edit":     discord.Color.light_grey(),
    "bulk_delete":      discord.Color.dark_grey(),
    # ── Members ────────────────────────────────────────────────────────
    "member_join":      discord.Color.green(),
    "member_leave":     discord.Color.greyple(),
    "member_ban":       discord.Color.red(),
    "member_unban":     discord.Color.green(),
    "member_nickname":  discord.Color.blue(),
    "member_roles":     discord.Color.blue(),
    "member_timeout":   discord.Color.orange(),
    # ── Channels ───────────────────────────────────────────────────────
    "channel_create":   discord.Color.green(),
    "channel_delete":   discord.Color.red(),
    "channel_update":   discord.Color.blue(),
    # ── Roles ──────────────────────────────────────────────────────────
    "role_create":      discord.Color.green(),
    "role_delete":      discord.Color.red(),
    "role_update":      discord.Color.blue(),
    # ── Voice ──────────────────────────────────────────────────────────
    "voice_join":       discord.Color.green(),
    "voice_leave":      discord.Color.greyple(),
    "voice_move":       discord.Color.blue(),
    # ── Other ──────────────────────────────────────────────────────────
    "ticket_open":      discord.Color.blue(),
    "ticket_close":     discord.Color.greyple(),
    "filter_trigger":   discord.Color.dark_red(),
}

ACTION_EMOJI: dict[str, str] = {
    "warn":             "⚠️",
    "mute":             "🔇",
    "unmute":           "🔊",
    "kick":             "👢",
    "ban":              "🔨",
    "unban":            "✅",
    "softban":          "🔨",
    "tempban":          "⏳",
    "massban":          "🔨",
    "case_edit":        "✏️",
    "note_add":         "📝",
    "slowmode":         "⏱️",
    "channel_lock":     "🔒",
    "channel_unlock":   "🔓",
    "lockdown":         "🔒",
    "unlockdown":       "🔓",
    "message_delete":   "🗑️",
    "message_edit":     "✏️",
    "bulk_delete":      "🗑️",
    "member_join":      "📥",
    "member_leave":     "📤",
    "member_ban":       "🔨",
    "member_unban":     "✅",
    "member_nickname":  "🏷️",
    "member_roles":     "🎭",
    "member_timeout":   "⏳",
    "channel_create":   "➕",
    "channel_delete":   "➖",
    "channel_update":   "✏️",
    "role_create":      "➕",
    "role_delete":      "➖",
    "role_update":      "✏️",
    "voice_join":       "🔊",
    "voice_leave":      "🔇",
    "voice_move":       "↔️",
    "ticket_open":      "🎫",
    "ticket_close":     "🎫",
    "filter_trigger":   "🚫",
}


class ModLoggingCog(commands.Cog, name="ModLogging"):
    """Sends embed-based audit log entries to a server's mod-log channel."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        # Track invite counts per guild for join-invite attribution
        self._invite_cache: dict[int, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        raw = await self.db.get_guild_config(guild.id, "mod_log_channel")
        if not raw:
            return None
        ch = guild.get_channel(int(raw))
        return ch if isinstance(ch, discord.TextChannel) else None

    async def log(
        self,
        guild: discord.Guild,
        *,
        action: str,
        target: discord.User | discord.Member | None = None,
        moderator: discord.User | discord.Member | None = None,
        reason: str | None = None,
        extra: str | None = None,
        case_id: int | None = None,
    ) -> None:
        """Send a mod-log embed. Called by other cogs and internal listeners."""
        channel = await self._log_channel(guild)
        if channel is None:
            return

        colour = COLOUR_MAP.get(action, discord.Color.blurple())
        emoji = ACTION_EMOJI.get(action, "📋")
        embed = discord.Embed(
            title=f"{emoji} {action.replace('_', ' ').title()}",
            color=colour,
            timestamp=datetime.now(timezone.utc),
        )
        if case_id is not None:
            embed.set_footer(text=f"Case #{case_id}")
        if target:
            embed.add_field(name="User", value=f"{target} (`{target.id}`)", inline=True)
            if hasattr(target, "display_avatar"):
                embed.set_thumbnail(url=target.display_avatar.url)
        if moderator:
            embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        if extra:
            embed.add_field(name="Details", value=extra, inline=False)

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send to mod-log channel %s in guild %s", channel.id, guild.id)
        except discord.HTTPException as exc:
            logger.error("Failed to send mod-log embed: %s", exc)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(name="setmodlog", description="Set the channel for mod-log messages")
    @app_commands.describe(channel="The text channel to send mod-log embeds to")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_mod_log(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await self.db.set_guild_config(interaction.guild_id, "mod_log_channel", str(channel.id))  # type: ignore[arg-type]
        # Seed invite cache for this guild
        assert interaction.guild is not None
        await self._seed_invites(interaction.guild)
        await interaction.response.send_message(
            f"✅ Mod-log channel set to {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="modlogtest", description="Send a test embed to the mod-log channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def modlogtest(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await self.log(
            interaction.guild,
            action="warn",
            target=interaction.user,
            moderator=interaction.user,
            reason="This is a test entry.",
            extra="Mod-log is working correctly.",
        )
        await interaction.response.send_message("✅ Test entry sent to mod-log.", ephemeral=True)

    # ------------------------------------------------------------------
    # Invite cache helpers (for member-join attribution)
    # ------------------------------------------------------------------

    async def _seed_invites(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
            self._invite_cache[guild.id] = {inv.code: inv.uses or 0 for inv in invites}
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._seed_invites(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._seed_invites(guild)

    # ------------------------------------------------------------------
    # Member events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        created_ago = (datetime.now(timezone.utc) - member.created_at).days
        account_age = f"{created_ago} day(s) old"
        new_account_warn = " ⚠️ **New account!**" if created_ago < 7 else ""

        # Attempt invite attribution by diffing invite uses
        used_invite = "Unknown"
        try:
            current_invites = await guild.invites()
            old_cache = self._invite_cache.get(guild.id, {})
            for inv in current_invites:
                old_uses = old_cache.get(inv.code, 0)
                if (inv.uses or 0) > old_uses:
                    used_invite = f"`{inv.code}` by {inv.inviter} (uses: {inv.uses})"
                    break
            self._invite_cache[guild.id] = {inv.code: inv.uses or 0 for inv in current_invites}
        except (discord.Forbidden, discord.HTTPException):
            pass

        await self.log(
            guild,
            action="member_join",
            target=member,
            extra=(
                f"**Account Created:** <t:{int(member.created_at.timestamp())}:R> ({account_age}){new_account_warn}\n"
                f"**Member Count:** {guild.member_count}\n"
                f"**Invite Used:** {used_invite}"
            ),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        roles = [r.mention for r in member.roles if r != guild.default_role]
        joined_str = (
            f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown"
        )
        await self.log(
            guild,
            action="member_leave",
            target=member,
            extra=(
                f"**Joined:** {joined_str}\n"
                f"**Member Count:** {guild.member_count}\n"
                f"**Roles:** {', '.join(roles) or 'None'}"
            ),
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.log(guild, action="member_ban", target=user,
                       extra="(Logged from Discord audit — see cases for bot-issued bans)")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.log(guild, action="member_unban", target=user)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild = before.guild

        # Nickname change
        if before.nick != after.nick:
            await self.log(
                guild,
                action="member_nickname",
                target=after,
                extra=f"**Before:** {before.nick or '*None*'}\n**After:** {after.nick or '*None*'}",
            )

        # Role changes
        before_roles = set(before.roles)
        after_roles = set(after.roles)
        added = after_roles - before_roles
        removed = before_roles - after_roles
        if added or removed:
            parts = []
            if added:
                parts.append("**Added:** " + ", ".join(r.mention for r in added))
            if removed:
                parts.append("**Removed:** " + ", ".join(r.mention for r in removed))
            await self.log(
                guild,
                action="member_roles",
                target=after,
                extra="\n".join(parts),
            )

        # Timeout (Discord timeout = communication_disabled_until)
        before_to = getattr(before, "communication_disabled_until", None)
        after_to = getattr(after, "communication_disabled_until", None)
        if before_to != after_to:
            if after_to and after_to > datetime.now(timezone.utc):
                await self.log(
                    guild,
                    action="member_timeout",
                    target=after,
                    extra=f"**Timed out until:** <t:{int(after_to.timestamp())}:F>",
                )

    # ------------------------------------------------------------------
    # Message events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        content = (message.content or "*[no text content]*")[:1024]
        attach = ""
        if message.attachments:
            attach = "\n**Attachments:** " + ", ".join(a.filename for a in message.attachments)
        await self.log(
            message.guild,
            action="message_delete",
            target=message.author,
            extra=f"**Channel:** {message.channel.mention}\n**Content:**\n{content}{attach}",
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or not messages[0].guild:
            return
        guild = messages[0].guild
        channel = messages[0].channel
        non_bot = [m for m in messages if not m.author.bot]
        await self.log(
            guild,
            action="bulk_delete",
            extra=f"**Channel:** {channel.mention}\n**Messages deleted:** {len(messages)} ({len(non_bot)} non-bot)",
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.author.bot or not before.guild:
            return
        if before.content == after.content:
            return
        old = (before.content or "*[empty]*")[:512]
        new = (after.content or "*[empty]*")[:512]
        await self.log(
            before.guild,
            action="message_edit",
            target=before.author,
            extra=(
                f"**Channel:** {before.channel.mention}\n"
                f"**Before:**\n{old}\n"
                f"**After:**\n{new}\n"
                f"[Jump to message]({after.jump_url})"
            ),
        )

    # ------------------------------------------------------------------
    # Channel events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        kind = type(channel).__name__.replace("Channel", "").lower()
        await self.log(
            channel.guild,
            action="channel_create",
            extra=f"**Name:** {channel.mention}\n**Type:** {kind}\n**ID:** `{channel.id}`",
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        kind = type(channel).__name__.replace("Channel", "").lower()
        await self.log(
            channel.guild,
            action="channel_delete",
            extra=f"**Name:** #{channel.name}\n**Type:** {kind}\n**ID:** `{channel.id}`",
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            if before.topic != after.topic:
                changes.append(f"**Topic:** `{before.topic or '(none)'}` → `{after.topic or '(none)'}`")
            if before.slowmode_delay != after.slowmode_delay:
                changes.append(f"**Slowmode:** `{before.slowmode_delay}s` → `{after.slowmode_delay}s`")
            if before.nsfw != after.nsfw:
                changes.append(f"**NSFW:** `{before.nsfw}` → `{after.nsfw}`")
        if not changes:
            return
        await self.log(
            after.guild,
            action="channel_update",
            extra=f"**Channel:** {after.mention}\n" + "\n".join(changes),
        )

    # ------------------------------------------------------------------
    # Role events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self.log(
            role.guild,
            action="role_create",
            extra=f"**Role:** {role.mention} (`{role.id}`)\n**Color:** `{role.color}`",
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.log(
            role.guild,
            action="role_delete",
            extra=f"**Role:** `@{role.name}` (`{role.id}`)\n**Color:** `{role.color}`",
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"**Color:** `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            changes.append("**Permissions changed**")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** `{before.hoist}` → `{after.hoist}`")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** `{before.mentionable}` → `{after.mentionable}`")
        if not changes:
            return
        await self.log(
            after.guild,
            action="role_update",
            extra=f"**Role:** {after.mention}\n" + "\n".join(changes),
        )

    # ------------------------------------------------------------------
    # Voice state events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if before.channel == after.channel:
            return
        guild = member.guild

        if before.channel is None and after.channel is not None:
            await self.log(
                guild,
                action="voice_join",
                target=member,
                extra=f"**Channel:** {after.channel.mention}",
            )
        elif before.channel is not None and after.channel is None:
            await self.log(
                guild,
                action="voice_leave",
                target=member,
                extra=f"**Channel:** {before.channel.mention}",
            )
        elif before.channel is not None and after.channel is not None:
            await self.log(
                guild,
                action="voice_move",
                target=member,
                extra=f"**From:** {before.channel.mention} → **To:** {after.channel.mention}",
            )
