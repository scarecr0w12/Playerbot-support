"""Custom commands cog — user-defined text responses with variable substitution.

Variables supported in responses:
  {user}      — mention of the user who triggered the command
  {username}  — display name
  {server}    — server name
  {channel}   — channel mention
  {members}   — member count

Inspired by Red-DiscordBot's CustomCommands cog.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)


class CustomCommandsCog(commands.Cog, name="CustomCommands"):
    """Create, edit, delete, and trigger custom text commands."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------
    # Management commands
    # ------------------------------------------------------------------

    cc_group = app_commands.Group(name="cc", description="Custom command management")

    @cc_group.command(name="add", description="Create a custom command")
    @app_commands.describe(name="Command name (no spaces)", response="The response text (supports {user}, {server}, etc.)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cc_add(self, interaction: discord.Interaction, name: str, response: str) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        added = await self.db.add_custom_command(guild_id, name, response, interaction.user.id)
        if added:
            await interaction.response.send_message(
                f"✅ Custom command `{name}` created.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ A command named `{name}` already exists. Use `/cc edit` to modify it.",
                ephemeral=True,
            )

    @cc_group.command(name="edit", description="Edit an existing custom command")
    @app_commands.describe(name="Command name", response="New response text")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cc_edit(self, interaction: discord.Interaction, name: str, response: str) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        edited = await self.db.edit_custom_command(guild_id, name, response)
        if edited:
            await interaction.response.send_message(
                f"✅ Custom command `{name}` updated.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ No command named `{name}` found.", ephemeral=True
            )

    @cc_group.command(name="delete", description="Delete a custom command")
    @app_commands.describe(name="Command name to delete")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cc_delete(self, interaction: discord.Interaction, name: str) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        deleted = await self.db.delete_custom_command(guild_id, name)
        if deleted:
            await interaction.response.send_message(
                f"✅ Custom command `{name}` deleted.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ No command named `{name}` found.", ephemeral=True
            )

    @cc_group.command(name="list", description="List all custom commands")
    async def cc_list(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        cmds = await self.db.list_custom_commands(guild_id)
        if not cmds:
            await interaction.response.send_message("No custom commands configured.", ephemeral=True)
            return

        lines = [f"• `{c['name']}` — created by <@{c['creator_id']}>" for c in cmds]
        embed = discord.Embed(
            title="Custom Commands",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @cc_group.command(name="info", description="Show details for a custom command")
    @app_commands.describe(name="Command name")
    async def cc_info(self, interaction: discord.Interaction, name: str) -> None:
        guild_id = interaction.guild_id
        assert guild_id is not None
        cc = await self.db.get_custom_command(guild_id, name)
        if not cc:
            await interaction.response.send_message(f"⚠️ No command named `{name}` found.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Custom Command: {cc['name']}", color=discord.Color.blurple())
        embed.add_field(name="Response", value=cc["response"][:1024], inline=False)
        embed.add_field(name="Created by", value=f"<@{cc['creator_id']}>", inline=True)
        embed.add_field(name="Created at", value=cc["created_at"], inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Trigger: listen for prefix-based custom commands
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        # Check for bot prefix
        prefix = "!"
        if not message.content.startswith(prefix):
            return

        cmd_name = message.content[len(prefix):].split()[0].lower() if message.content[len(prefix):] else ""
        if not cmd_name:
            return

        cc = await self.db.get_custom_command(message.guild.id, cmd_name)
        if not cc:
            return

        response = cc["response"]
        # Variable substitution
        response = response.replace("{user}", message.author.mention)
        response = response.replace("{username}", message.author.display_name)
        response = response.replace("{server}", message.guild.name)
        response = response.replace("{channel}", message.channel.mention)
        response = response.replace("{members}", str(message.guild.member_count))

        await message.channel.send(response)
