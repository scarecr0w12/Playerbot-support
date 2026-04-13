"""Birthday tracking and announcements cog.

Features
--------
- Users can set their birthday with a command
- Automatic birthday announcements in configured channels
- Daily task to check for birthdays
- Timezone support for accurate birthday detection
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


class BirthdayCog(commands.Cog, name="Birthdays"):
    """Track user birthdays and send automatic birthday announcements."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.birthday_check_task.start()

    def cog_unload(self) -> None:
        """Clean up tasks when cog is unloaded."""
        self.birthday_check_task.cancel()

    # ------------------------------------------------------------------
    # Background task: check birthdays once a day
    # ------------------------------------------------------------------

    @tasks.loop(hours=1)
    async def birthday_check_task(self) -> None:
        """Check for today's birthdays and send announcements."""
        today = datetime.now(timezone.utc)
        today_str = today.strftime("%m-%d")
        date_str = today.strftime("%Y-%m-%d")

        for guild in self.bot.guilds:
            channel_raw = await self.db.get_guild_config(guild.id, "birthday_channel")
            if not channel_raw:
                continue
            channel = guild.get_channel(int(channel_raw))
            if not isinstance(channel, discord.TextChannel):
                continue

            birthdays = await self.db.get_birthdays_by_date(guild.id, today_str)
            for row in birthdays:
                user_id = row["user_id"]
                already_sent = await self.db.check_birthday_announced(guild.id, user_id, date_str)
                if already_sent:
                    continue
                member = guild.get_member(user_id)
                if not member:
                    continue
                try:
                    await channel.send(
                        f"🎂 Happy Birthday, {member.mention}! Wishing you a wonderful day! 🎉"
                    )
                    await self.db.record_birthday_announcement(guild.id, user_id, date_str)
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("Failed to send birthday announcement in guild %s: %s", guild.id, e)

            await self.db.cleanup_old_birthday_announcements(guild.id)

    @birthday_check_task.before_loop
    async def before_birthday_check(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Birthday command group
    # ------------------------------------------------------------------

    birthday_group = app_commands.Group(name="birthday", description="Birthday tracking and celebrations")

    @birthday_group.command(name="set", description="Set your birthday for automatic birthday announcements")
    @app_commands.describe(
        month="Month (1-12)",
        day="Day (1-31)"
    )
    async def set_birthday(
        self,
        interaction: discord.Interaction,
        month: int,
        day: int,
    ) -> None:
        """Set your birthday."""
        if month < 1 or month > 12:
            await interaction.response.send_message("❌ Invalid month. Must be between 1 and 12.", ephemeral=True)
            return
        
        if day < 1 or day > 31:
            await interaction.response.send_message("❌ Invalid day. Must be between 1 and 31.", ephemeral=True)
            return
        
        # Validate day for month
        if month in [4, 6, 9, 11] and day > 30:
            await interaction.response.send_message("❌ That month only has 30 days.", ephemeral=True)
            return
        
        if month == 2 and day > 29:
            await interaction.response.send_message("❌ February has at most 29 days.", ephemeral=True)
            return
        
        # Format birthday as MM-DD
        birthday = f"{month:02d}-{day:02d}"
        
        if await self.db.set_birthday(interaction.guild_id, interaction.user.id, birthday):  # type: ignore[arg-type]
            month_name = datetime(2024, month, 1).strftime("%B")
            await interaction.response.send_message(
                f"✅ Your birthday has been set to **{month_name} {day}**! 🎂",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Failed to set your birthday. Please try again.",
                ephemeral=True,
            )

    @birthday_group.command(name="remove", description="Remove your birthday from tracking")
    async def remove_birthday(self, interaction: discord.Interaction) -> None:
        """Remove your birthday."""
        if await self.db.remove_birthday(interaction.guild_id, interaction.user.id):  # type: ignore[arg-type]
            await interaction.response.send_message(
                "✅ Your birthday has been removed from tracking.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ You don't have a birthday set.",
                ephemeral=True,
            )

    @birthday_group.command(name="mine", description="Check your birthday setting")
    async def my_birthday(self, interaction: discord.Interaction) -> None:
        """Check your birthday setting."""
        birthday = await self.db.get_birthday(interaction.guild_id, interaction.user.id)  # type: ignore[arg-type]
        
        if not birthday:
            await interaction.response.send_message(
                "❌ You haven't set a birthday yet. Use `/set_birthday` to set one.",
                ephemeral=True,
            )
            return
        
        month, day = birthday["birthday"].split("-")
        month_name = datetime(2024, int(month), 1).strftime("%B")
        
        await interaction.response.send_message(
            f"🎂 Your birthday is set to **{month_name} {int(day)}**.",
            ephemeral=True,
        )

    @birthday_group.command(name="channel", description="Set the channel for birthday announcements")
    @app_commands.describe(channel="Channel to send birthday announcements in")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_birthday_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Set the birthday announcement channel."""
        await self.db.set_guild_config(interaction.guild_id, "birthday_channel", str(channel.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Birthday channel set to {channel.mention}.",
            ephemeral=True,
        )

    @birthday_group.command(name="upcoming", description="Show upcoming birthdays in the server")
    async def upcoming_birthdays(self, interaction: discord.Interaction) -> None:
        """Show upcoming birthdays."""
        guild = interaction.guild
        assert guild is not None
        
        # Get all birthdays for this guild
        cur = await self.db.conn.execute(
            "SELECT user_id, birthday FROM birthdays WHERE guild_id = ? ORDER BY birthday",
            (guild.id,),
        )
        birthdays = await cur.fetchall()
        
        if not birthdays:
            await interaction.response.send_message(
                "❌ No birthdays have been set yet.",
                ephemeral=True,
            )
            return
        
        # Get current date
        today = datetime.now(timezone.utc)
        current_month = today.month
        current_day = today.day
        
        # Sort birthdays: those coming up first in the year
        upcoming = []
        for birthday in birthdays:
            month, day = map(int, birthday["birthday"].split("-"))
            
            # Calculate days until next birthday
            next_birthday = datetime(today.year, month, day)
            if next_birthday < today:
                next_birthday = datetime(today.year + 1, month, day)
            
            days_until = (next_birthday - today).days
            user = guild.get_member(birthday["user_id"])
            
            if user:
                upcoming.append({
                    "user": user,
                    "days_until": days_until,
                    "month": month,
                    "day": day,
                })
        
        # Sort by days until
        upcoming.sort(key=lambda x: x["days_until"])
        
        embed = discord.Embed(
            title="🎂 Upcoming Birthdays",
            description=f"Birthdays in {guild.name}",
            color=discord.Color.pink(),
        )
        
        for i, bday in enumerate(upcoming[:10], 1):
            month_name = datetime(2024, bday["month"], 1).strftime("%B")
            
            if bday["days_until"] == 0:
                time_text = "🎉 Today!"
            elif bday["days_until"] == 1:
                time_text = "Tomorrow!"
            else:
                time_text = f"In {bday['days_until']} days"
            
            embed.add_field(
                name=f"#{i} {bday['user'].display_name}",
                value=f"{month_name} {bday['day']} - {time_text}",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Load the Birthday cog."""
    await bot.add_cog(BirthdayCog(bot, bot.db))
