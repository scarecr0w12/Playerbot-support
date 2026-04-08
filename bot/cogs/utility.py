"""Utility / General cog — informational and fun commands.

Inspired by Red-DiscordBot's General cog.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
import humanize

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

EIGHT_BALL_RESPONSES = [
    "It is certain.", "It is decidedly so.", "Without a doubt.",
    "Yes — definitely.", "You may rely on it.", "As I see it, yes.",
    "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
    "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
]


class UtilityCog(commands.Cog, name="Utility"):
    """General-purpose informational and fun commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /userinfo
    # ------------------------------------------------------------------

    @app_commands.command(name="userinfo", description="Show information about a user")
    @app_commands.describe(member="The user to look up (defaults to yourself)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            target = interaction.user  # type: ignore[assignment]

        m: discord.Member = target  # type: ignore[assignment]
        embed = discord.Embed(title=str(m), color=m.color if m.color.value else discord.Color.blurple())
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.add_field(name="ID", value=str(m.id), inline=True)
        embed.add_field(name="Nickname", value=m.nick or "None", inline=True)
        embed.add_field(
            name="Account Created",
            value=f"{discord.utils.format_dt(m.created_at, 'R')}",
            inline=True,
        )
        if m.joined_at:
            embed.add_field(
                name="Joined Server",
                value=f"{discord.utils.format_dt(m.joined_at, 'R')}",
                inline=True,
            )
        roles = [r.mention for r in m.roles if not r.is_default()]
        if roles:
            embed.add_field(
                name=f"Roles ({len(roles)})",
                value=" ".join(roles[:20]) + ("…" if len(roles) > 20 else ""),
                inline=False,
            )
        embed.add_field(name="Bot", value="Yes" if m.bot else "No", inline=True)
        if m.premium_since:
            embed.add_field(name="Boosting Since", value=discord.utils.format_dt(m.premium_since, "R"), inline=True)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /serverinfo
    # ------------------------------------------------------------------

    @app_commands.command(name="serverinfo", description="Show information about this server")
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Owner", value=str(guild.owner), inline=True)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Text Channels", value=str(len(guild.text_channels)), inline=True)
        embed.add_field(name="Voice Channels", value=str(len(guild.voice_channels)), inline=True)
        embed.add_field(name="Boost Level", value=str(guild.premium_tier), inline=True)
        embed.add_field(name="Boosts", value=str(guild.premium_subscription_count), inline=True)
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(guild.created_at, "R"),
            inline=True,
        )
        if guild.description:
            embed.add_field(name="Description", value=guild.description, inline=False)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /avatar
    # ------------------------------------------------------------------

    @app_commands.command(name="avatar", description="Show a user's avatar")
    @app_commands.describe(member="The user whose avatar to show")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        embed = discord.Embed(title=f"{target}'s Avatar", color=discord.Color.blurple())
        embed.set_image(url=target.display_avatar.with_size(1024).url)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /poll
    # ------------------------------------------------------------------

    @app_commands.command(name="poll", description="Create a simple yes/no/maybe poll")
    @app_commands.describe(question="The poll question")
    async def poll(self, interaction: discord.Interaction, question: str) -> None:
        embed = discord.Embed(
            title="📊 Poll",
            description=question,
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Poll by {interaction.user}")
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        for emoji in ("👍", "👎", "🤷"):
            await msg.add_reaction(emoji)

    # ------------------------------------------------------------------
    # /8ball
    # ------------------------------------------------------------------

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your yes/no question")
    async def eight_ball(self, interaction: discord.Interaction, question: str) -> None:
        answer = random.choice(EIGHT_BALL_RESPONSES)
        embed = discord.Embed(color=discord.Color.purple())
        embed.add_field(name="🎱 Question", value=question, inline=False)
        embed.add_field(name="Answer", value=answer, inline=False)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /coinflip
    # ------------------------------------------------------------------

    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction) -> None:
        result = random.choice(["🪙 **Heads!**", "🪙 **Tails!**"])
        await interaction.response.send_message(result)

    # ------------------------------------------------------------------
    # /roll
    # ------------------------------------------------------------------

    @app_commands.command(name="roll", description="Roll a random number")
    @app_commands.describe(maximum="Maximum value (default 100)")
    async def roll(self, interaction: discord.Interaction, maximum: int = 100) -> None:
        result = random.randint(1, max(maximum, 1))
        await interaction.response.send_message(f"🎲 You rolled **{result}** (1-{maximum})")

    # ------------------------------------------------------------------
    # /choose
    # ------------------------------------------------------------------

    @app_commands.command(name="choose", description="Let the bot choose between options")
    @app_commands.describe(options="Comma-separated options")
    async def choose(self, interaction: discord.Interaction, options: str) -> None:
        choices = [c.strip() for c in options.split(",") if c.strip()]
        if len(choices) < 2:
            await interaction.response.send_message("❌ Give me at least 2 options separated by commas.", ephemeral=True)
            return
        pick = random.choice(choices)
        await interaction.response.send_message(f"🤔 I choose… **{pick}**!")

    # ------------------------------------------------------------------
    # /ping
    # ------------------------------------------------------------------

    @app_commands.command(name="ping", description="Check the bot's latency")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: **{latency_ms}ms**")

    # ------------------------------------------------------------------
    # /botinfo
    # ------------------------------------------------------------------

    @app_commands.command(name="botinfo", description="Show information about the bot")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Bot Info", color=discord.Color.blurple())
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        total_members = sum(g.member_count or 0 for g in self.bot.guilds)
        embed.add_field(name="Total Members", value=str(total_members), inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        cog_count = len(self.bot.cogs)
        cmd_count = len(self.bot.tree.get_commands())
        embed.add_field(name="Cogs Loaded", value=str(cog_count), inline=True)
        embed.add_field(name="Slash Commands", value=str(cmd_count), inline=True)
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
