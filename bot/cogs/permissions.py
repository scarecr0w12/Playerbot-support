"""Permissions cog — fine-grained per-command/channel/role permission overrides.

Allows admins to allow or deny specific slash commands for specific roles,
channels, or users — beyond Discord's built-in permission system.

Inspired by Red-DiscordBot's Permissions cog.
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


class PermissionsCog(commands.Cog, name="Permissions"):
    """Per-command permission overrides for roles, channels, and users."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------
    # /perm allow / deny / reset / show
    # ------------------------------------------------------------------

    perm_group = app_commands.Group(name="perm", description="Command permission overrides")

    @perm_group.command(name="allow_role", description="Allow a role to use a command")
    @app_commands.describe(command="Slash command name", role="The role to allow")
    @app_commands.checks.has_permissions(administrator=True)
    async def allow_role(self, interaction: discord.Interaction, command: str, role: discord.Role) -> None:
        await self.db.set_command_permission(interaction.guild_id, command, "role", role.id, True)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ **{role.name}** can now use `/{command}`.", ephemeral=True
        )

    @perm_group.command(name="deny_role", description="Deny a role from using a command")
    @app_commands.describe(command="Slash command name", role="The role to deny")
    @app_commands.checks.has_permissions(administrator=True)
    async def deny_role(self, interaction: discord.Interaction, command: str, role: discord.Role) -> None:
        await self.db.set_command_permission(interaction.guild_id, command, "role", role.id, False)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ **{role.name}** is now denied from `/{command}`.", ephemeral=True
        )

    @perm_group.command(name="allow_channel", description="Allow a command in a specific channel")
    @app_commands.describe(command="Slash command name", channel="The channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def allow_channel(
        self, interaction: discord.Interaction, command: str, channel: discord.TextChannel
    ) -> None:
        await self.db.set_command_permission(interaction.guild_id, command, "channel", channel.id, True)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ `/{command}` is allowed in {channel.mention}.", ephemeral=True
        )

    @perm_group.command(name="deny_channel", description="Deny a command in a specific channel")
    @app_commands.describe(command="Slash command name", channel="The channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def deny_channel(
        self, interaction: discord.Interaction, command: str, channel: discord.TextChannel
    ) -> None:
        await self.db.set_command_permission(interaction.guild_id, command, "channel", channel.id, False)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ `/{command}` is denied in {channel.mention}.", ephemeral=True
        )

    @perm_group.command(name="allow_user", description="Allow a user to use a command")
    @app_commands.describe(command="Slash command name", member="The user")
    @app_commands.checks.has_permissions(administrator=True)
    async def allow_user(
        self, interaction: discord.Interaction, command: str, member: discord.Member
    ) -> None:
        await self.db.set_command_permission(interaction.guild_id, command, "user", member.id, True)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ {member.mention} can now use `/{command}`.", ephemeral=True
        )

    @perm_group.command(name="deny_user", description="Deny a user from using a command")
    @app_commands.describe(command="Slash command name", member="The user")
    @app_commands.checks.has_permissions(administrator=True)
    async def deny_user(
        self, interaction: discord.Interaction, command: str, member: discord.Member
    ) -> None:
        await self.db.set_command_permission(interaction.guild_id, command, "user", member.id, False)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ {member.mention} is now denied from `/{command}`.", ephemeral=True
        )

    @perm_group.command(name="reset", description="Remove a permission override")
    @app_commands.describe(
        command="Slash command name",
        target_type="Type: role, channel, or user",
        target_id="ID of the role/channel/user",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def perm_reset(
        self, interaction: discord.Interaction, command: str, target_type: str, target_id: str
    ) -> None:
        removed = await self.db.remove_command_permission(
            interaction.guild_id, command, target_type, int(target_id)  # type: ignore[arg-type]
        )
        if removed:
            await interaction.response.send_message(
                f"✅ Permission override for `/{command}` removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message("⚠️ No matching override found.", ephemeral=True)

    @perm_group.command(name="show", description="Show permission overrides for a command")
    @app_commands.describe(command="Slash command name")
    @app_commands.checks.has_permissions(administrator=True)
    async def perm_show(self, interaction: discord.Interaction, command: str) -> None:
        guild = interaction.guild
        assert guild is not None
        perms = await self.db.get_command_permissions(guild.id, command)
        if not perms:
            await interaction.response.send_message(
                f"No overrides for `/{command}`.", ephemeral=True
            )
            return

        lines: list[str] = []
        for p in perms:
            status = "✅ Allow" if p["allowed"] else "❌ Deny"
            t = p["target_type"]
            tid = p["target_id"]
            if t == "role":
                target = f"Role <@&{tid}>"
            elif t == "channel":
                target = f"Channel <#{tid}>"
            else:
                target = f"User <@{tid}>"
            lines.append(f"{status} — {target}")

        embed = discord.Embed(
            title=f"Permissions: /{command}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Interaction check hook (called by bot-level check)
    # ------------------------------------------------------------------

    async def check_interaction(self, interaction: discord.Interaction) -> bool | None:
        """Check if an interaction is allowed by custom overrides.

        Returns True/False if overridden, None if no override exists.
        """
        if not interaction.guild:
            return None
        command = interaction.command
        if command is None:
            return None
        member = interaction.user
        if not isinstance(member, discord.Member):
            return None

        role_ids = [r.id for r in member.roles]
        return await self.db.check_command_allowed(
            interaction.guild.id,
            command.name,
            member.id,
            interaction.channel_id,  # type: ignore[arg-type]
            role_ids,
        )
