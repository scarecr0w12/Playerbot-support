"""Leveling / XP cog — inspired by Red-DiscordBot's LevelUp and MEE6-style systems.

Features
--------
- XP per message (15-25, randomised) with a per-user cooldown (default 60s)
- Formula: required_xp(level) = 5*level^2 + 50*level + 100
- Level-up announcements (configurable channel or current channel)
- Level roles: assign/remove roles at configured level thresholds
- /rank — rich embed with level, XP progress bar, and rank position
- /levels leaderboard — top members
- Admin: toggle on/off, set XP rate, set cooldown, set announce channel
- Admin: add/remove level roles, reset guild XP, set/reset individual user XP
- Excluded channels and roles (no XP gain)
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)

XP_MIN = 15
XP_MAX = 25
DEFAULT_COOLDOWN_SECONDS = 60


def xp_for_level(level: int) -> int:
    """Total XP required to reach `level` from 0."""
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp: int) -> int:
    """Compute the level a given total XP corresponds to."""
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level


def xp_progress(total_xp: int) -> tuple[int, int, int]:
    """Return (current_level, xp_into_level, xp_needed_for_next)."""
    level = 0
    remaining = total_xp
    while remaining >= xp_for_level(level):
        remaining -= xp_for_level(level)
        level += 1
    return level, remaining, xp_for_level(level)


def progress_bar(current: int, maximum: int, length: int = 12) -> str:
    filled = int(length * current / maximum) if maximum else 0
    return "█" * filled + "░" * (length - filled)


class LevelsCog(commands.Cog, name="Levels"):
    """XP/Leveling system with rank cards, leaderboard, and level roles."""

    def __init__(self, bot: commands.Bot, db: "Database") -> None:
        self.bot = bot
        self.db = db
        self._cooldowns: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self._cleanup_cooldowns.start()

    async def cog_unload(self) -> None:
        self._cleanup_cooldowns.cancel()

    @tasks.loop(minutes=10)
    async def _cleanup_cooldowns(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        expired = [k for k, v in self._cooldowns.items() if now - v > 300]
        for k in expired:
            del self._cooldowns[k]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _is_enabled(self, guild_id: int) -> bool:
        val = await self.db.get_guild_config(guild_id, "levels_enabled")
        return val != "0"

    async def _get_cooldown(self, guild_id: int) -> int:
        raw = await self.db.get_guild_config(guild_id, "levels_cooldown")
        try:
            return int(raw) if raw else DEFAULT_COOLDOWN_SECONDS
        except ValueError:
            return DEFAULT_COOLDOWN_SECONDS

    async def _get_xp_rate(self, guild_id: int) -> tuple[int, int]:
        raw_min = await self.db.get_guild_config(guild_id, "levels_xp_min")
        raw_max = await self.db.get_guild_config(guild_id, "levels_xp_max")
        try:
            mn = int(raw_min) if raw_min else XP_MIN
            mx = int(raw_max) if raw_max else XP_MAX
            return mn, mx
        except ValueError:
            return XP_MIN, XP_MAX

    async def _get_excluded_channels(self, guild_id: int) -> list[int]:
        raw = await self.db.get_guild_config(guild_id, "levels_exclude_channels")
        if not raw:
            return []
        try:
            return [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            return []

    async def _get_excluded_roles(self, guild_id: int) -> list[int]:
        raw = await self.db.get_guild_config(guild_id, "levels_exclude_roles")
        if not raw:
            return []
        try:
            return [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            return []

    async def _get_level_roles(self, guild_id: int) -> dict[int, int]:
        """Return {level: role_id} mapping."""
        raw = await self.db.get_guild_config(guild_id, "levels_roles")
        if not raw:
            return {}
        result: dict[int, int] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" in pair:
                try:
                    lvl, rid = pair.split(":", 1)
                    result[int(lvl)] = int(rid)
                except ValueError:
                    pass
        return result

    async def _save_level_roles(self, guild_id: int, mapping: dict[int, int]) -> None:
        serialised = ",".join(f"{lvl}:{rid}" for lvl, rid in sorted(mapping.items()))
        await self.db.set_guild_config(guild_id, "levels_roles", serialised)

    async def _assign_level_roles(
        self, member: discord.Member, new_level: int, level_roles: dict[int, int]
    ) -> None:
        guild = member.guild
        for lvl, rid in level_roles.items():
            role = guild.get_role(rid)
            if not role:
                continue
            if new_level >= lvl:
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"Reached level {lvl}")
                    except discord.Forbidden:
                        pass
            else:
                if role in member.roles:
                    try:
                        await member.remove_roles(role, reason=f"Below level {lvl}")
                    except discord.Forbidden:
                        pass

    # ------------------------------------------------------------------
    # on_message — XP gain
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not message.content:
            return

        guild_id = message.guild.id
        user_id = message.author.id

        if not await self._is_enabled(guild_id):
            return

        excluded_channels = await self._get_excluded_channels(guild_id)
        if message.channel.id in excluded_channels:
            return

        excluded_roles = await self._get_excluded_roles(guild_id)
        if any(r.id in excluded_roles for r in getattr(message.author, "roles", [])):
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        key = (guild_id, user_id)
        cooldown = await self._get_cooldown(guild_id)
        last = self._cooldowns.get(key, 0)
        if now_ts - last < cooldown:
            return

        self._cooldowns[key] = now_ts
        xp_min, xp_max = await self._get_xp_rate(guild_id)
        gained = random.randint(xp_min, xp_max)
        now_str = datetime.now(timezone.utc).isoformat()
        updated = await self.db.add_xp(guild_id, user_id, gained, now_str)

        old_level = updated["level"]
        new_level, _, _ = xp_progress(updated["xp"])

        if new_level > old_level:
            await self.db.set_level(guild_id, user_id, new_level)
            level_roles = await self._get_level_roles(guild_id)
            if level_roles and isinstance(message.author, discord.Member):
                await self._assign_level_roles(message.author, new_level, level_roles)

            announce_ch_raw = await self.db.get_guild_config(guild_id, "levels_announce_channel")
            announce_ch = None
            if announce_ch_raw:
                announce_ch = message.guild.get_channel(int(announce_ch_raw))

            target_channel = announce_ch or message.channel
            em = discord.Embed(
                description=f"🎉 {message.author.mention} leveled up to **level {new_level}**!",
                color=discord.Color.gold(),
            )
            try:
                await target_channel.send(embed=em)  # type: ignore[union-attr]
            except discord.Forbidden:
                pass

    # ==================================================================
    # /rank
    # ==================================================================

    @app_commands.command(name="rank", description="View your XP rank card")
    @app_commands.describe(member="User to check (defaults to yourself)")
    @app_commands.guild_only()
    async def rank(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        target = member or interaction.user

        row = await self.db.get_level_row(guild.id, target.id)
        total_xp = row["xp"] if row else 0
        stored_level = row["level"] if row else 0

        current_level, xp_into_level, xp_needed = xp_progress(total_xp)
        rank_pos = await self.db.get_level_rank(guild.id, target.id)
        bar = progress_bar(xp_into_level, xp_needed)

        em = discord.Embed(color=discord.Color.blurple())
        em.set_author(name=f"{target.display_name}'s Rank", icon_url=target.display_avatar.url)
        em.add_field(name="Level", value=str(current_level), inline=True)
        em.add_field(name="Rank", value=f"#{rank_pos}", inline=True)
        em.add_field(name="Total XP", value=f"{total_xp:,}", inline=True)
        em.add_field(
            name=f"Progress to Level {current_level + 1}",
            value=f"`{bar}` {xp_into_level:,} / {xp_needed:,} XP",
            inline=False,
        )
        em.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=em)

    # ==================================================================
    # /levels leaderboard
    # ==================================================================

    levels_group = app_commands.Group(name="levels", description="Leveling system commands")

    @levels_group.command(name="leaderboard", description="View the XP leaderboard")
    @app_commands.describe(limit="Number of entries (max 25)")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction, limit: int = 10) -> None:
        guild = interaction.guild
        assert guild is not None
        limit = min(max(limit, 1), 25)
        rows = await self.db.get_level_leaderboard(guild.id, limit)

        if not rows:
            await interaction.response.send_message("No XP data yet.", ephemeral=True)
            return

        lines: list[str] = []
        for i, row in enumerate(rows, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**{i}.**")
            lvl, xp_in, needed = xp_progress(row["xp"])
            lines.append(f"{medal} <@{row['user_id']}> — **Lv.{lvl}** ({row['xp']:,} XP)")

        em = discord.Embed(
            title=f"⭐ XP Leaderboard — {guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=em)

    # ==================================================================
    # Admin subgroup: /levels admin ...
    # ==================================================================

    @levels_group.command(name="toggle", description="Enable or disable the leveling system")
    @app_commands.describe(enabled="True to enable, False to disable")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self.db.set_guild_config(interaction.guild_id, "levels_enabled", "1" if enabled else "0")  # type: ignore[arg-type]
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Leveling system {state}.", ephemeral=True)

    @levels_group.command(name="set_announce", description="Set the level-up announcement channel")
    @app_commands.describe(channel="Channel for level-up messages (leave empty to use message channel)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_announce(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        val = str(channel.id) if channel else ""
        await self.db.set_guild_config(interaction.guild_id, "levels_announce_channel", val)  # type: ignore[arg-type]
        msg = f"✅ Announce channel set to {channel.mention}." if channel else "✅ Announce channel cleared (uses message channel)."
        await interaction.response.send_message(msg, ephemeral=True)

    @levels_group.command(name="set_cooldown", description="Set XP gain cooldown in seconds")
    @app_commands.describe(seconds="Seconds between XP gains per user")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_cooldown(self, interaction: discord.Interaction, seconds: int) -> None:
        await self.db.set_guild_config(interaction.guild_id, "levels_cooldown", str(seconds))  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ XP cooldown set to **{seconds}s**.", ephemeral=True)

    @levels_group.command(name="set_xp_rate", description="Set XP gained per eligible message")
    @app_commands.describe(min_xp="Minimum XP per message", max_xp="Maximum XP per message")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_xp_rate(self, interaction: discord.Interaction, min_xp: int, max_xp: int) -> None:
        if min_xp < 1 or max_xp < min_xp:
            await interaction.response.send_message("❌ Invalid range.", ephemeral=True)
            return
        await self.db.set_guild_config(interaction.guild_id, "levels_xp_min", str(min_xp))  # type: ignore[arg-type]
        await self.db.set_guild_config(interaction.guild_id, "levels_xp_max", str(max_xp))  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ XP rate set to **{min_xp}–{max_xp}** per message.", ephemeral=True)

    @levels_group.command(name="add_role", description="Assign a role when a member reaches a level")
    @app_commands.describe(level="Level threshold", role="Role to assign")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_role(
        self, interaction: discord.Interaction, level: int, role: discord.Role
    ) -> None:
        mapping = await self._get_level_roles(interaction.guild_id)  # type: ignore[arg-type]
        mapping[level] = role.id
        await self._save_level_roles(interaction.guild_id, mapping)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ {role.mention} will be assigned at level **{level}**.", ephemeral=True
        )

    @levels_group.command(name="remove_role", description="Remove a level role assignment")
    @app_commands.describe(level="Level threshold to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_role(self, interaction: discord.Interaction, level: int) -> None:
        mapping = await self._get_level_roles(interaction.guild_id)  # type: ignore[arg-type]
        if level not in mapping:
            await interaction.response.send_message(f"❌ No role configured for level {level}.", ephemeral=True)
            return
        del mapping[level]
        await self._save_level_roles(interaction.guild_id, mapping)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Level {level} role assignment removed.", ephemeral=True)

    @levels_group.command(name="exclude_channel", description="Exclude a channel from XP gain")
    @app_commands.describe(channel="Channel to exclude", remove="Set True to un-exclude")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def exclude_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel, remove: bool = False
    ) -> None:
        guild_id = interaction.guild_id  # type: ignore[assignment]
        excluded = await self._get_excluded_channels(guild_id)
        if remove:
            excluded = [c for c in excluded if c != channel.id]
            msg = f"✅ {channel.mention} removed from exclusion list."
        else:
            if channel.id not in excluded:
                excluded.append(channel.id)
            msg = f"✅ {channel.mention} excluded from XP gain."
        await self.db.set_guild_config(guild_id, "levels_exclude_channels", ",".join(str(c) for c in excluded))
        await interaction.response.send_message(msg, ephemeral=True)

    @levels_group.command(name="set_xp", description="Manually set a user's XP (admin)")
    @app_commands.describe(member="Target member", xp="New total XP value")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def admin_set_xp(
        self, interaction: discord.Interaction, member: discord.Member, xp: int
    ) -> None:
        new_level, _, _ = xp_progress(max(0, xp))
        await self.db.set_xp(interaction.guild_id, member.id, max(0, xp), new_level)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ {member.mention} XP set to **{xp:,}** (Level {new_level}).", ephemeral=True
        )

    @levels_group.command(name="reset", description="Reset all XP data for this server")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def reset_levels(self, interaction: discord.Interaction) -> None:
        count = await self.db.reset_levels(interaction.guild_id)  # type: ignore[arg-type]
        await interaction.response.send_message(f"🗑️ Reset XP for {count} member(s).", ephemeral=True)
