"""Invite Tracking cog - track which invites bring members to the server.

Features
--------
- Automatic invite tracking when members join
- Invite statistics and leaderboards
- Per-inviter analytics
- Recent invite activity monitoring
- Join source attribution
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)


class InviteTrackingCog(commands.Cog, name="Invite Tracking"):
    """Track and analyze server invite usage."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._invite_cache: dict[int, dict[str, discord.Invite]] = {}  # guild_id -> {code: invite}
        self.update_invites_task.start()

    def cog_unload(self) -> None:
        """Clean up tasks when cog is unloaded."""
        self.update_invites_task.cancel()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def update_invites_task(self) -> None:
        """Periodically update invite cache."""
        await self.bot.wait_until_ready()
        
        for guild in self.bot.guilds:
            if guild.me.guild_permissions.manage_guild:
                try:
                    invites = await guild.invites()
                    invite_dict = {invite.code: invite for invite in invites}
                    self._invite_cache[guild.id] = invite_dict
                    await self.db.update_invite_codes(guild.id, invites)
                except discord.Forbidden:
                    logger.warning(f"Cannot fetch invites for guild {guild.name}")

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Track which invite a member used."""
        guild = member.guild
        
        if not guild.me.guild_permissions.manage_guild:
            return

        try:
            # Get current invites
            current_invites = await guild.invites()
            current_dict = {invite.code: invite for invite in current_invites}
            
            # Get cached invites from before the join
            cached_invites = self._invite_cache.get(guild.id, {})
            
            # Find which invite was used
            used_invite = None
            for code, invite in current_dict.items():
                cached_invite = cached_invites.get(code)
                if cached_invite and invite.uses > cached_invite.uses:
                    used_invite = invite
                    break
                elif not cached_invite and invite.uses > 0:
                    # New invite that wasn't in cache
                    used_invite = invite
                    break

            # Update cache
            self._invite_cache[guild.id] = current_dict

            # Track the invite use
            if used_invite and used_invite.inviter:
                account_created = member.created_at.isoformat() if member.created_at else None
                success = await self.db.track_invite_use(
                    guild.id,
                    used_invite.code,
                    member.id,
                    used_invite.inviter.id,
                    account_created,
                )
                
                if success:
                    logger.info(
                        f"Tracked invite: {member} joined via {used_invite.code} "
                        f"invited by {used_invite.inviter}"
                    )
                else:
                    logger.debug(f"User {member} already tracked for guild {guild.name}")

        except discord.Forbidden:
            logger.warning(f"Cannot track invites for guild {guild.name} - missing permissions")
        except Exception as e:
            logger.error(f"Error tracking invite for {member}: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Track when a member leaves."""
        guild = member.guild
        await self.db.mark_user_left(guild.id, member.id)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(
        name="invites",
        description="Show invite statistics for the server or a specific user"
    )
    @app_commands.describe(
        user="User to check invite stats for (leave empty for server-wide stats)"
    )
    async def invites(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        """Show invite statistics."""
        guild = interaction.guild
        assert guild is not None

        if user:
            # Get stats for specific user
            stats = await self.db.get_invite_stats(guild.id, user.id)
            
            if not stats:
                await interaction.response.send_message(
                    f"❌ {user.mention} hasn't invited anyone to the server.",
                    ephemeral=True,
                )
                return

            stat = stats[0]
            embed = discord.Embed(
                title=f"📊 Invite Stats for {user.display_name}",
                color=discord.Color.blue(),
            )
            
            embed.set_thumbnail(url=user.display_avatar.url)
            embed.add_field(name="Total Invites", value=str(stat["total_invites"]), inline=True)
            embed.add_field(name="Active Members", value=str(stat["active_invites"]), inline=True)
            
            if stat["first_invite"]:
                first_date = datetime.fromisoformat(stat["first_invite"])
                embed.add_field(
                    name="First Invite",
                    value=discord.utils.format_dt(first_date, 'R'),
                    inline=True,
                )
            
            if stat["last_invite"]:
                last_date = datetime.fromisoformat(stat["last_invite"])
                embed.add_field(
                    name="Last Invite",
                    value=discord.utils.format_dt(last_date, 'R'),
                    inline=True,
                )

            # Calculate retention rate
            if stat["total_invites"] > 0:
                retention = (stat["active_invites"] / stat["total_invites"]) * 100
                embed.add_field(
                    name="Retention Rate",
                    value=f"{retention:.1f}%",
                    inline=True,
                )

        else:
            # Get server-wide leaderboard
            leaderboard = await self.db.get_invite_leaderboard(guild.id, 10)
            
            if not leaderboard:
                await interaction.response.send_message(
                    "❌ No invite data available for this server.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="🏆 Invite Leaderboard",
                description="Top inviters in the server",
                color=discord.Color.gold(),
            )

            for i, stat in enumerate(leaderboard, 1):
                inviter = guild.get_member(stat["inviter_id"])
                if inviter:
                    retention = (stat["active_invites"] / stat["total_invites"] * 100) if stat["total_invites"] > 0 else 0
                    embed.add_field(
                        name=f"#{i} {inviter.display_name}",
                        value=f"**{stat['total_invites']}** invites ({stat['active_invites']} active, {retention:.1f}% retention)",
                        inline=False,
                    )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="invite_source",
        description="Check how a specific user joined the server"
    )
    @app_commands.describe(
        user="User to check join source for"
    )
    async def invite_source(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        """Check how a user joined the server."""
        guild = interaction.guild
        assert guild is not None

        invite_info = await self.db.get_user_invite_info(guild.id, user.id)
        
        if not invite_info:
            await interaction.response.send_message(
                f"❌ No invite data found for {user.mention}. "
                "They may have joined before tracking was enabled.",
                ephemeral=True,
            )
            return

        inviter = guild.get_member(invite_info["inviter_id"])
        inviter_name = inviter.display_name if inviter else f"User ID {invite_info['inviter_id']}"
        
        joined_at = datetime.fromisoformat(invite_info["used_at"])
        
        embed = discord.Embed(
            title="🔍 Join Source Information",
            description=f"How {user.mention} joined the server",
            color=discord.Color.blue(),
        )
        
        embed.add_field(name="Invite Code", value=f"`{invite_info['invite_code']}`", inline=True)
        embed.add_field(name="Invited By", value=inviter_name, inline=True)
        embed.add_field(name="Joined At", value=discord.utils.format_dt(joined_at, 'R'), inline=True)
        
        if invite_info["account_created"]:
            try:
                account_created = datetime.fromisoformat(invite_info["account_created"])
                embed.add_field(
                    name="Account Created",
                    value=discord.utils.format_dt(account_created, 'R'),
                    inline=True,
                )
            except ValueError:
                pass

        if inviter:
            embed.set_thumbnail(url=inviter.display_avatar.url)
        
        embed.set_footer(text=f"User ID: {user.id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="recent_invites",
        description="Show recent invite activity in the server"
    )
    @app_commands.describe(
        days="Number of days to look back (1-30, default: 7)"
    )
    async def recent_invites(
        self,
        interaction: discord.Interaction,
        days: int = 7,
    ) -> None:
        """Show recent invite activity."""
        guild = interaction.guild
        assert guild is not None

        days = max(1, min(30, days))  # Clamp between 1 and 30
        
        recent = await self.db.get_recent_invites(guild.id, days)
        
        if not recent:
            await interaction.response.send_message(
                f"❌ No invite activity in the last {days} days.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📈 Recent Invite Activity ({days} days)",
            color=discord.Color.blue(),
        )

        # Group by day for better readability
        daily_stats = {}
        for invite in recent:
            used_at = datetime.fromisoformat(invite["used_at"])
            day_key = used_at.strftime("%Y-%m-%d")
            
            if day_key not in daily_stats:
                daily_stats[day_key] = {"total": 0, "active": 0, "invites": []}
            
            daily_stats[day_key]["total"] += 1
            if not invite["left_at"]:
                daily_stats[day_key]["active"] += 1
            
            # Add to detailed list (limit to prevent embed overflow)
            if len(daily_stats[day_key]["invites"]) < 3:
                user = guild.get_member(invite["user_id"])
                inviter = guild.get_member(invite["inviter_id"])
                if user and inviter:
                    status = "🟢" if not invite["left_at"] else "🔴"
                    daily_stats[day_key]["invites"].append(
                        f"{status} {user.display_name} invited by {inviter.display_name}"
                    )

        # Display daily summary
        for day, stats in sorted(daily_stats.items(), reverse=True)[:7]:  # Last 7 days
            date_obj = datetime.strptime(day, "%Y-%m-%d")
            retention = (stats["active"] / stats["total"] * 100) if stats["total"] > 0 else 0
            
            field_value = f"**{stats['total']}** invites ({stats['active']} active, {retention:.1f}% retention)"
            
            if stats["invites"]:
                field_value += f"\n{chr(10).join(stats['invites'])}"
            
            embed.add_field(
                name=discord.utils.format_dt(date_obj, 'D'),
                value=field_value,
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="sync_invites",
        description="Manually sync the invite cache with current server invites"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sync_invites(self, interaction: discord.Interaction) -> None:
        """Manually sync invite data."""
        guild = interaction.guild
        assert guild is not None

        if not guild.me.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ I need **Manage Server** permission to fetch invites.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            invites = await guild.invites()
            invite_dict = {invite.code: invite for invite in invites}
            self._invite_cache[guild.id] = invite_dict
            await self.db.update_invite_codes(guild.id, invites)
            
            await interaction.followup.send(
                f"✅ Synced {len(invites)} invites for the server.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to fetch server invites.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error syncing invites: {e}")
            await interaction.followup.send(
                "❌ An error occurred while syncing invites.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    """Load the InviteTracking cog."""
    await bot.add_cog(InviteTrackingCog(bot, bot.db))
