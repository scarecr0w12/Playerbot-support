"""Welcome cog — customisable welcome messages, auto-role, and rules acceptance.

Features
--------
- Configurable welcome channel and message (with {user}, {server} placeholders).
- Auto-role assignment on join.
- Rules acceptance via button — grants a "Verified" role.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.database import Database

logger = logging.getLogger(__name__)


# ======================================================================
# Persistent view: rules acceptance button
# ======================================================================

class RulesAcceptView(discord.ui.View):
    """Persistent button that grants a configured verified role."""

    def __init__(self, cog: WelcomeCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="✅ I accept the rules",
        style=discord.ButtonStyle.success,
        custom_id="welcome:accept_rules",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        assert guild is not None
        role_id_raw = await self.cog.db.get_guild_config(guild.id, "verified_role")
        if not role_id_raw:
            await interaction.response.send_message(
                "⚠️ Verified role not configured. Ask an admin to run `/set_verified_role`.",
                ephemeral=True,
            )
            return

        role = guild.get_role(int(role_id_raw))
        if not role:
            await interaction.response.send_message("⚠️ Configured role not found.", ephemeral=True)
            return

        member = interaction.user
        if isinstance(member, discord.Member):
            if role in member.roles:
                await interaction.response.send_message("You're already verified!", ephemeral=True)
                return
            try:
                await member.add_roles(role, reason="Rules accepted")
                await interaction.response.send_message("✅ You have been verified!", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("❌ I couldn't assign the role.", ephemeral=True)


# ======================================================================
# Cog
# ======================================================================

class WelcomeCog(commands.Cog, name="Welcome"):
    """Welcome messages, auto-role, and rules verification."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def cog_load(self) -> None:
        self.bot.add_view(RulesAcceptView(self))

    # ------------------------------------------------------------------
    # Configuration commands
    # ------------------------------------------------------------------

    @app_commands.command(name="set_welcome_channel", description="Set the welcome channel")
    @app_commands.describe(channel="Channel to send welcome messages in")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_welcome_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.db.set_guild_config(interaction.guild_id, "welcome_channel", str(channel.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Welcome channel set to {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="set_welcome_message", description="Set the welcome message template")
    @app_commands.describe(
        message="Use {user} for mention, {username} for name, {server} for server name"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def set_welcome_message(self, interaction: discord.Interaction, message: str) -> None:
        await self.db.set_guild_config(interaction.guild_id, "welcome_message", message)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Welcome message updated.\n**Preview:**\n{message}", ephemeral=True
        )

    @app_commands.command(name="set_autorole", description="Set a role to auto-assign to new members")
    @app_commands.describe(role="The role to give new members")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_autorole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.db.set_guild_config(interaction.guild_id, "autorole", str(role.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Auto-role set to **{role.name}**.", ephemeral=True
        )

    @app_commands.command(name="set_verified_role", description="Set the role granted by rules acceptance")
    @app_commands.describe(role="The role to grant when a user accepts the rules")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_verified_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.db.set_guild_config(interaction.guild_id, "verified_role", str(role.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Verified role set to **{role.name}**.", ephemeral=True
        )

    @app_commands.command(name="rules_panel", description="Post a rules acceptance panel with a button")
    @app_commands.describe(channel="Channel to post the rules panel in", rules_text="The rules text to display")
    @app_commands.checks.has_permissions(administrator=True)
    async def rules_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        rules_text: str,
    ) -> None:
        embed = discord.Embed(
            title="📜 Server Rules",
            description=rules_text,
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Click the button below to accept and gain access.")
        await channel.send(embed=embed, view=RulesAcceptView(self))
        await interaction.response.send_message(
            f"✅ Rules panel posted in {channel.mention}.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild

        # Auto-role
        autorole_raw = await self.db.get_guild_config(guild.id, "autorole")
        if autorole_raw:
            role = guild.get_role(int(autorole_raw))
            if role:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except discord.Forbidden:
                    logger.warning("Cannot assign autorole in guild %s", guild.id)

        # Welcome message
        channel_raw = await self.db.get_guild_config(guild.id, "welcome_channel")
        if not channel_raw:
            return
        channel = guild.get_channel(int(channel_raw))
        if not isinstance(channel, discord.TextChannel):
            return

        template = await self.db.get_guild_config(guild.id, "welcome_message")
        if not template:
            template = "Welcome to **{server}**, {user}! 🎉"

        text = template.format(
            user=member.mention,
            username=member.display_name,
            server=guild.name,
        )

        embed = discord.Embed(
            description=text,
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{guild.member_count}")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send welcome message in guild %s", guild.id)
