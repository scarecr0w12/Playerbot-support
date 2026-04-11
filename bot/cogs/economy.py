"""Economy cog — virtual currency system with bank, payday, slots, leaderboard.

Inspired by Red-DiscordBot's Economy and Bank cogs.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)

SLOT_EMOJIS = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣"]

DEFAULT_PAYDAY_AMOUNT = 100
DEFAULT_PAYDAY_COOLDOWN_HOURS = 12
DEFAULT_CURRENCY_NAME = "credits"


class EconomyCog(commands.Cog, name="Economy"):
    """Virtual currency: payday, transfer, slots, leaderboard."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def _currency_name(self, guild_id: int) -> str:
        name = await self.db.get_guild_config(guild_id, "currency_name")
        return name or DEFAULT_CURRENCY_NAME

    async def _payday_amount(self, guild_id: int) -> int:
        raw = await self.db.get_guild_config(guild_id, "payday_amount")
        return int(raw) if raw else DEFAULT_PAYDAY_AMOUNT

    async def _payday_cooldown(self, guild_id: int) -> int:
        raw = await self.db.get_guild_config(guild_id, "payday_cooldown_hours")
        return int(raw) if raw else DEFAULT_PAYDAY_COOLDOWN_HOURS

    # ------------------------------------------------------------------
    # /balance
    # ------------------------------------------------------------------

    @app_commands.command(name="balance", description="Check your (or another user's) balance")
    @app_commands.describe(member="User to check (defaults to yourself)")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        guild = interaction.guild
        assert guild is not None
        target = member or interaction.user
        bal = await self.db.get_balance(guild.id, target.id)
        currency = await self._currency_name(guild.id)
        embed = discord.Embed(
            description=f"💰 {target.mention} has **{bal:,}** {currency}.",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /payday
    # ------------------------------------------------------------------

    @app_commands.command(name="payday", description="Collect your daily credits")
    async def payday(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        user_id = interaction.user.id

        cooldown_hours = await self._payday_cooldown(guild.id)
        last_raw = await self.db.get_last_payday(guild.id, user_id)
        if last_raw:
            last = datetime.fromisoformat(last_raw)
            next_payday = last + timedelta(hours=cooldown_hours)
            if datetime.now(timezone.utc) < next_payday:
                remaining = next_payday - datetime.now(timezone.utc)
                hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                minutes = remainder // 60
                await interaction.response.send_message(
                    f"⏳ Your next payday is in **{hours}h {minutes}m**.", ephemeral=True
                )
                return

        amount = await self._payday_amount(guild.id)
        new_bal = await self.db.add_balance(guild.id, user_id, amount)
        await self.db.set_last_payday(guild.id, user_id, datetime.now(timezone.utc).isoformat())
        currency = await self._currency_name(guild.id)
        await interaction.response.send_message(
            f"💵 You collected **{amount:,}** {currency}! New balance: **{new_bal:,}**."
        )

    # ------------------------------------------------------------------
    # /transfer
    # ------------------------------------------------------------------

    @app_commands.command(name="transfer", description="Transfer credits to another user")
    @app_commands.describe(member="Recipient", amount="Amount to transfer")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        guild = interaction.guild
        assert guild is not None
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You can't transfer to yourself.", ephemeral=True)
            return

        success = await self.db.transfer_balance(guild.id, interaction.user.id, member.id, amount)
        currency = await self._currency_name(guild.id)
        if success:
            await interaction.response.send_message(
                f"✅ Transferred **{amount:,}** {currency} to {member.mention}."
            )
        else:
            await interaction.response.send_message("❌ Insufficient balance.", ephemeral=True)

    # ------------------------------------------------------------------
    # /slots
    # ------------------------------------------------------------------

    @app_commands.command(name="slots", description="Play the slot machine")
    @app_commands.describe(bet="Amount to bet")
    async def slots(self, interaction: discord.Interaction, bet: int) -> None:
        guild = interaction.guild
        assert guild is not None
        if bet <= 0:
            await interaction.response.send_message("❌ Bet must be positive.", ephemeral=True)
            return

        bal = await self.db.get_balance(guild.id, interaction.user.id)
        if bal < bet:
            await interaction.response.send_message("❌ Insufficient balance.", ephemeral=True)
            return

        # Spin the slots
        reels = [random.choice(SLOT_EMOJIS) for _ in range(3)]
        line = " | ".join(reels)

        # Determine winnings
        if reels[0] == reels[1] == reels[2]:
            if reels[0] == "7️⃣":
                multiplier = 10
                result = "🎰 **JACKPOT!**"
            elif reels[0] == "💎":
                multiplier = 5
                result = "💎 **BIG WIN!**"
            else:
                multiplier = 3
                result = "🎉 **Three of a kind!**"
            winnings = bet * multiplier
            await self.db.add_balance(guild.id, interaction.user.id, winnings - bet)
        elif reels[0] == reels[1] or reels[1] == reels[2]:
            winnings = bet * 2
            await self.db.add_balance(guild.id, interaction.user.id, winnings - bet)
            result = "✨ **Two matching!**"
        else:
            winnings = 0
            await self.db.add_balance(guild.id, interaction.user.id, -bet)
            result = "😢 No match."

        currency = await self._currency_name(guild.id)
        new_bal = await self.db.get_balance(guild.id, interaction.user.id)

        embed = discord.Embed(title="🎰 Slot Machine", color=discord.Color.gold())
        embed.add_field(name="Reels", value=f"**[ {line} ]**", inline=False)
        embed.add_field(name="Result", value=result, inline=True)
        embed.add_field(
            name="Payout",
            value=f"+**{winnings:,}** {currency}" if winnings > 0 else f"-**{bet:,}** {currency}",
            inline=True,
        )
        embed.set_footer(text=f"Balance: {new_bal:,} {currency}")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /leaderboard
    # ------------------------------------------------------------------

    @app_commands.command(name="leaderboard", description="View the richest members")
    @app_commands.describe(limit="Number of entries (max 25)")
    async def leaderboard(self, interaction: discord.Interaction, limit: int = 10) -> None:
        guild = interaction.guild
        assert guild is not None
        limit = min(max(limit, 1), 25)
        rows = await self.db.get_leaderboard(guild.id, limit)
        currency = await self._currency_name(guild.id)

        if not rows:
            await interaction.response.send_message("No accounts yet.", ephemeral=True)
            return

        lines: list[str] = []
        for i, row in enumerate(rows, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**{i}.**")
            lines.append(f"{medal} <@{row['user_id']}> — **{row['balance']:,}** {currency}")

        embed = discord.Embed(
            title=f"💰 Leaderboard — {guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # Admin: economy settings
    # ------------------------------------------------------------------

    econset_group = app_commands.Group(name="econset", description="Economy settings (admin)")

    @econset_group.command(name="payday_amount", description="Set the payday amount")
    @app_commands.describe(amount="Credits per payday")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_payday_amount(self, interaction: discord.Interaction, amount: int) -> None:
        await self.db.set_guild_config(interaction.guild_id, "payday_amount", str(amount))  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Payday amount set to **{amount}**.", ephemeral=True)

    @econset_group.command(name="payday_cooldown", description="Set payday cooldown in hours")
    @app_commands.describe(hours="Cooldown hours")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_payday_cooldown(self, interaction: discord.Interaction, hours: int) -> None:
        await self.db.set_guild_config(interaction.guild_id, "payday_cooldown_hours", str(hours))  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Payday cooldown set to **{hours}** hour(s).", ephemeral=True)

    @econset_group.command(name="currency_name", description="Set the currency name")
    @app_commands.describe(name="Name for the currency (e.g. coins, credits)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_currency_name(self, interaction: discord.Interaction, name: str) -> None:
        await self.db.set_guild_config(interaction.guild_id, "currency_name", name)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Currency name set to **{name}**.", ephemeral=True)

    @econset_group.command(name="set_balance", description="Set a user's balance (admin)")
    @app_commands.describe(member="Target user", amount="New balance")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_set_balance(
        self, interaction: discord.Interaction, member: discord.Member, amount: int
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        await self.db.set_balance(guild.id, member.id, amount)
        currency = await self._currency_name(guild.id)
        await interaction.response.send_message(
            f"✅ {member.mention}'s balance set to **{amount:,}** {currency}.", ephemeral=True
        )
