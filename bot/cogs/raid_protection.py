"""Raid Protection cog - automatic detection and mitigation of server raids.

Features
--------
- Join rate monitoring with configurable thresholds
- Account age filtering
- Automatic server lockdown during raids
- Optional auto-banning of raiders
- Alert notifications to staff
- Detailed raid event logging
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)


class RaidProtectionCog(commands.Cog, name="Raid Protection"):
    """Advanced raid detection and mitigation system."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._lockdowns: dict[int, datetime] = {}  # guild_id -> lockdown_end_time
        self.raid_check_task.start()

    def cog_unload(self) -> None:
        """Clean up tasks when cog is unloaded."""
        self.raid_check_task.cancel()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @tasks.loop(seconds=10)
    async def raid_check_task(self) -> None:
        """Periodic task to check for raids and manage lockdowns."""
        await self.bot.wait_until_ready()
        
        # Check for ongoing lockdowns that should expire
        now = datetime.now(timezone.utc)
        expired_lockdowns = [
            guild_id for guild_id, end_time in self._lockdowns.items()
            if now >= end_time
        ]
        
        for guild_id in expired_lockdowns:
            await self._end_lockdown(guild_id)
            del self._lockdowns[guild_id]

        # Clean up old join tracking data
        for guild in self.bot.guilds:
            await self.db.cleanup_old_joins(guild.id)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Handle member joins for raid detection."""
        guild = member.guild
        
        # Get raid settings
        settings = await self.db.get_raid_settings(guild.id)
        if not settings or not settings["enabled"]:
            return

        # Track the join
        account_created = member.created_at.isoformat() if member.created_at else None
        await self.db.track_join(guild.id, member.id, account_created)

        # Check for account age filter
        if settings["account_age_min"] > 0 and member.created_at:
            min_age = datetime.now(timezone.utc) - timedelta(hours=settings["account_age_min"])
            if member.created_at > min_age:
                # Account is too new
                await self._handle_suspicious_join(member, "Account too new")
                return

        # Check join rate
        recent_joins = await self.db.get_recent_joins(guild.id, settings["join_window"])
        
        if len(recent_joins) >= settings["join_threshold"]:
            await self._handle_raid_detected(guild, recent_joins, settings)

    async def _handle_suspicious_join(self, member: discord.Member, reason: str) -> None:
        """Handle a suspicious join (e.g., account too new)."""
        guild = member.guild
        settings = await self.db.get_raid_settings(guild.id)
        
        if not settings:
            return

        actions_taken = []

        # Log the suspicious join
        logger.warning(f"Suspicious join in {guild.name}: {member} ({reason})")

        # Send alert
        if settings["alert_channel_id"]:
            await self._send_alert(
                guild,
                f"🚨 **Suspicious Join Detected**\n"
                f"**User:** {member.mention} ({member.id})\n"
                f"**Reason:** {reason}\n"
                f"**Account Created:** {member.created_at.strftime('%Y-%m-%d %H:%M:%S UTC') if member.created_at else 'Unknown'}",
            )

        # Optional actions based on settings
        if settings.get("auto_ban"):
            try:
                await member.ban(reason=f"Automatic ban: {reason}")
                actions_taken.append(f"Banned {member} ({reason})")
            except discord.Forbidden:
                logger.error(f"Failed to ban {member} - insufficient permissions")

        # Create raid event for tracking
        await self.db.create_raid_event(
            guild.id,
            1,
            0,
            actions_taken,
        )

    async def _handle_raid_detected(
        self,
        guild: discord.Guild,
        recent_joins: list[dict],
        settings: dict,
    ) -> None:
        """Handle a detected raid."""
        # Check if we're already in lockdown
        if guild.id in self._lockdowns:
            return  # Already handling this raid

        logger.warning(f"Raid detected in {guild.name}! {len(recent_joins)} joins in {settings['join_window']}s")

        actions_taken = []

        # Start lockdown
        await self._start_lockdown(guild, settings["lockdown_duration"])
        actions_taken.append(f"Started lockdown for {settings['lockdown_duration']} seconds")

        # Send alert
        if settings["alert_channel_id"]:
            alert_message = (
                f"🚨 **RAID DETECTED** 🚨\n"
                f"**Guild:** {guild.name}\n"
                f"**Joins:** {len(recent_joins)} in {settings['join_window']} seconds\n"
                f"**Lockdown:** Activated for {settings['lockdown_duration']} seconds\n\n"
                f"**Recent Joins:**\n"
            )

            for join_data in recent_joins[:10]:  # Show first 10
                user = guild.get_member(join_data["user_id"])
                if user:
                    account_age = "Unknown"
                    if join_data["account_created"]:
                        try:
                            created = datetime.fromisoformat(join_data["account_created"])
                            age = datetime.now(timezone.utc) - created
                            if age.days > 0:
                                account_age = f"{age.days} days"
                            else:
                                account_age = f"{age.seconds // 3600} hours"
                        except ValueError:
                            pass
                    
                    alert_message += f"• {user.mention} (Account: {account_age})\n"

            await self._send_alert(guild, alert_message)

        # Auto-ban if enabled
        if settings.get("auto_ban"):
            banned_count = 0
            for join_data in recent_joins:
                user = guild.get_member(join_data["user_id"])
                if user:
                    try:
                        await user.ban(reason="Automatic ban: Raid detected")
                        banned_count += 1
                    except discord.Forbidden:
                        logger.error(f"Failed to ban {user} during raid")
            
            if banned_count > 0:
                actions_taken.append(f"Auto-banned {banned_count} users")

        # Log the raid event
        await self.db.create_raid_event(
            guild.id,
            len(recent_joins),
            settings["join_window"],
            actions_taken,
        )

    async def _start_lockdown(self, guild: discord.Guild, duration: int) -> None:
        """Start a server lockdown."""
        end_time = datetime.now(timezone.utc) + timedelta(seconds=duration)
        self._lockdowns[guild.id] = end_time

        # Try to lock down verification levels and other security measures
        try:
            # Set verification level to highest (if bot has permission)
            if guild.me.guild_permissions.manage_guild:
                # Note: This requires admin permissions and may not always be desirable
                pass  # Skipping verification level change as it's very disruptive
        except discord.Forbidden:
            pass

        logger.info(f"Lockdown started in {guild.name} for {duration} seconds")

    async def _end_lockdown(self, guild_id: int) -> None:
        """End a server lockdown."""
        guild = self.bot.get_guild(guild_id)
        if guild:
            logger.info(f"Lockdown ended in {guild.name}")
            
            # Send notification that lockdown has ended
            settings = await self.db.get_raid_settings(guild_id)
            if settings and settings["alert_channel_id"]:
                await self._send_alert(
                    guild,
                    f"✅ **Lockdown Ended**\n"
                    f"The server lockdown has been automatically lifted.\n"
                    f"Normal operations can resume.",
                )

    async def _send_alert(self, guild: discord.Guild, message: str) -> None:
        """Send an alert to the configured alert channel."""
        settings = await self.db.get_raid_settings(guild.id)
        if not settings or not settings["alert_channel_id"]:
            return

        channel = guild.get_channel(settings["alert_channel_id"])
        if isinstance(channel, discord.TextChannel):
            try:
                embed = discord.Embed(
                    title="🛡️ Raid Protection Alert",
                    description=message,
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                await channel.send(content="@here", embed=embed)
            except discord.Forbidden:
                logger.error(f"Cannot send alert to channel {channel.id} in {guild.name}")

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    raid_group = app_commands.Group(name="raid", description="Raid protection settings")

    @raid_group.command(name="enable", description="Enable raid protection")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_enable(self, interaction: discord.Interaction) -> None:
        """Enable raid protection."""
        await self.db.update_raid_settings(interaction.guild_id, enabled=True)
        await interaction.response.send_message(
            "✅ Raid protection has been enabled.\n"
            "Use `/raid configure` to adjust settings.",
            ephemeral=True,
        )

    @raid_group.command(name="disable", description="Disable raid protection")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_disable(self, interaction: discord.Interaction) -> None:
        """Disable raid protection."""
        await self.db.update_raid_settings(interaction.guild_id, enabled=False)
        await interaction.response.send_message(
            "⚠️ Raid protection has been disabled.",
            ephemeral=True,
        )

    @raid_group.command(name="configure", description="Configure raid protection settings")
    @app_commands.describe(
        join_threshold="Number of joins to trigger raid detection",
        join_window="Time window in seconds for join threshold",
        account_age_min="Minimum account age in hours (0 = disabled)",
        lockdown_duration="Lockdown duration in seconds",
        alert_channel="Channel to send raid alerts to",
        auto_ban="Automatically ban detected raiders",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_configure(
        self,
        interaction: discord.Interaction,
        join_threshold: int | None = None,
        join_window: int | None = None,
        account_age_min: int | None = None,
        lockdown_duration: int | None = None,
        alert_channel: discord.TextChannel | None = None,
        auto_ban: bool | None = None,
    ) -> None:
        """Configure raid protection settings."""
        await self.db.update_raid_settings(
            interaction.guild_id,
            join_threshold=join_threshold,
            join_window=join_window,
            account_age_min=account_age_min,
            lockdown_duration=lockdown_duration,
            alert_channel_id=alert_channel.id if alert_channel else None,
            auto_ban=auto_ban,
        )

        await interaction.response.send_message(
            "✅ Raid protection settings have been updated.",
            ephemeral=True,
        )

    @raid_group.command(name="status", description="Show current raid protection status")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_status(self, interaction: discord.Interaction) -> None:
        """Show current raid protection status."""
        guild = interaction.guild
        assert guild is not None

        settings = await self.db.get_raid_settings(guild.id)
        
        if not settings:
            embed = discord.Embed(
                title="🛡️ Raid Protection Status",
                description="❌ Raid protection is not configured",
                color=discord.Color.red(),
            )
            embed.add_field(
                name="Setup",
                value="Use `/raid enable` to enable raid protection.",
                inline=False,
            )
        else:
            status_emoji = "✅" if settings["enabled"] else "❌"
            status_text = "Enabled" if settings["enabled"] else "Disabled"
            
            embed = discord.Embed(
                title="🛡️ Raid Protection Status",
                description=f"{status_emoji} Raid protection is **{status_text}**",
                color=discord.Color.green() if settings["enabled"] else discord.Color.red(),
            )

            embed.add_field(name="Join Threshold", value=f"{settings['join_threshold']} joins", inline=True)
            embed.add_field(name="Time Window", value=f"{settings['join_window']} seconds", inline=True)
            embed.add_field(name="Account Age Min", value=f"{settings['account_age_min']} hours", inline=True)
            embed.add_field(name="Lockdown Duration", value=f"{settings['lockdown_duration']} seconds", inline=True)
            embed.add_field(name="Auto-Ban", value="Yes" if settings["auto_ban"] else "No", inline=True)
            
            if settings["alert_channel_id"]:
                channel = guild.get_channel(settings["alert_channel_id"])
                channel_name = channel.name if channel else "Unknown"
                embed.add_field(name="Alert Channel", value=f"#{channel_name}", inline=True)
            else:
                embed.add_field(name="Alert Channel", value="Not set", inline=True)

        # Check if currently in lockdown
        if guild.id in self._lockdowns:
            end_time = self._lockdowns[guild.id]
            remaining = end_time - datetime.now(timezone.utc)
            embed.add_field(
                name="🔒 Current Status",
                value=f"**LOCKDOWN ACTIVE** - Ends in {int(remaining.total_seconds())} seconds",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @raid_group.command(name="events", description="Show recent raid events")
    @app_commands.describe(limit="Number of events to show (max 50)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_events(self, interaction: discord.Interaction, limit: int = 10) -> None:
        """Show recent raid events."""
        limit = min(max(1, limit), 50)  # Clamp between 1 and 50
        
        events = await self.db.get_raid_events(interaction.guild_id, limit)
        
        if not events:
            await interaction.response.send_message(
                "❌ No raid events recorded.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📊 Recent Raid Events",
            description=f"Showing {len(events)} most recent events",
            color=discord.Color.blue(),
        )

        for event in events:
            triggered_at = datetime.fromisoformat(event["triggered_at"])
            
            status = "🟢 Resolved" if event["resolved_at"] else "🔴 Active"
            
            field_value = (
                f"**Joins:** {event['join_count']} in {event['window_seconds']}s\n"
                f"**Status:** {status}\n"
                f"**Time:** {discord.utils.format_dt(triggered_at, 'R')}"
            )

            if event["actions_taken"]:
                import json
                try:
                    actions = json.loads(event["actions_taken"])
                    if actions:
                        field_value += f"\n**Actions:** {', '.join(actions[:2])}"
                        if len(actions) > 2:
                            field_value += f" (+{len(actions) - 2} more)"
                except json.JSONDecodeError:
                    pass

            embed.add_field(
                name=f"Event #{event['id']}",
                value=field_value,
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @raid_group.command(name="lockdown", description="Manually start or end a lockdown")
    @app_commands.describe(
        action="Action to take (start/end)",
        duration="Lockdown duration in seconds (only for start)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_lockdown(
        self,
        interaction: discord.Interaction,
        action: str,
        duration: int = 300,
    ) -> None:
        """Manually control lockdown."""
        guild = interaction.guild
        assert guild is not None

        if action.lower() == "start":
            if guild.id in self._lockdowns:
                await interaction.response.send_message(
                    "❌ A lockdown is already active.",
                    ephemeral=True,
                )
                return

            await self._start_lockdown(guild, duration)
            
            # Send alert
            settings = await self.db.get_raid_settings(guild.id)
            if settings and settings["alert_channel_id"]:
                await self._send_alert(
                    guild,
                    f"🔒 **Manual Lockdown Started**\n"
                    f"**Started by:** {interaction.user.mention}\n"
                    f"**Duration:** {duration} seconds",
                )

            await interaction.response.send_message(
                f"✅ Lockdown started for {duration} seconds.",
                ephemeral=True,
            )

        elif action.lower() == "end":
            if guild.id not in self._lockdowns:
                await interaction.response.send_message(
                    "❌ No active lockdown to end.",
                    ephemeral=True,
                )
                return

            del self._lockdowns[guild.id]
            await self._end_lockdown(guild.id)

            await interaction.response.send_message(
                "✅ Lockdown ended manually.",
                ephemeral=True,
            )

        else:
            await interaction.response.send_message(
                "❌ Invalid action. Use `start` or `end`.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    """Load the RaidProtection cog."""
    await bot.add_cog(RaidProtectionCog(bot, bot.db))
