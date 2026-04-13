"""Admin cog — role management, selfrole, announcements, nickname management.

Inspired by Red-DiscordBot's Admin cog.
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


class SelfRoleView(discord.ui.View):
    """Persistent select-menu view for self-assignable roles."""

    def __init__(self, cog: AdminCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="🎭 Pick Roles",
        style=discord.ButtonStyle.primary,
        custom_id="admin:selfrole_pick",
    )
    async def pick_roles(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        assert guild is not None
        role_ids = await self.cog.db.get_selfroles(guild.id)
        roles = [guild.get_role(r) for r in role_ids]
        roles = [r for r in roles if r is not None]

        if not roles:
            await interaction.response.send_message("No self-assignable roles configured.", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=r.name,
                value=str(r.id),
                default=r in interaction.user.roles if isinstance(interaction.user, discord.Member) else False,
            )
            for r in roles[:25]
        ]

        view = SelfRoleSelectView(roles)
        await interaction.response.send_message(
            "Select the roles you want:", view=view, ephemeral=True
        )


class SelfRoleSelect(discord.ui.Select):
    def __init__(self, roles: list[discord.Role]) -> None:
        options = [
            discord.SelectOption(label=r.name, value=str(r.id)) for r in roles[:25]
        ]
        super().__init__(
            placeholder="Choose your roles…",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id="admin:selfrole_select",
        )
        self._roles = {r.id: r for r in roles}

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return

        selected_ids = {int(v) for v in self.values}
        added: list[str] = []
        removed: list[str] = []

        for role_id, role in self._roles.items():
            if role_id in selected_ids and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Self-role")
                    added.append(role.name)
                except discord.Forbidden:
                    pass
            elif role_id not in selected_ids and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Self-role")
                    removed.append(role.name)
                except discord.Forbidden:
                    pass

        parts: list[str] = []
        if added:
            parts.append(f"**Added:** {', '.join(added)}")
        if removed:
            parts.append(f"**Removed:** {', '.join(removed)}")
        if not parts:
            parts.append("No changes made.")

        await interaction.response.send_message("\n".join(parts), ephemeral=True)


class SelfRoleSelectView(discord.ui.View):
    def __init__(self, roles: list[discord.Role]) -> None:
        super().__init__(timeout=120)
        self.add_item(SelfRoleSelect(roles))


class AdminCog(commands.Cog, name="Admin"):
    """Administrative utilities: nick changes, announcements, config view."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------
    # Admin command group
    # ------------------------------------------------------------------

    admin_group = app_commands.Group(name="admin", description="Administrative utilities")

    @admin_group.command(name="nick", description="Change a member's nickname")
    @app_commands.describe(member="Target member", nickname="New nickname (leave empty to reset)")
    @app_commands.checks.has_permissions(manage_nicknames=True)
    async def nick(
        self, interaction: discord.Interaction, member: discord.Member, nickname: str | None = None
    ) -> None:
        try:
            await member.edit(nick=nickname, reason=f"Changed by {interaction.user}")
            if nickname:
                await interaction.response.send_message(
                    f"✅ {member.mention}'s nickname set to **{nickname}**.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"✅ {member.mention}'s nickname has been reset.", ephemeral=True
                )
        except discord.Forbidden:
            await interaction.response.send_message("❌ I can't change that member's nickname.", ephemeral=True)

    @admin_group.command(name="announce", description="Send an announcement embed to a channel")
    @app_commands.describe(
        channel="Target channel",
        title="Announcement title",
        message="Announcement body",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        message: str,
    ) -> None:
        embed = discord.Embed(
            title=title,
            description=message,
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Announced by {interaction.user}")
        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Announcement posted in {channel.mention}.", ephemeral=True
        )

    @admin_group.command(name="serverconfig", description="View current bot configuration for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def serverconfig(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        keys = [
            "mod_log_channel", "ticket_category", "welcome_channel",
            "welcome_message", "autorole", "verified_role", "automod_enabled",
            "payday_amount", "payday_cooldown_hours",
        ]
        embed = discord.Embed(title=f"Bot Config — {guild.name}", color=discord.Color.blurple())
        for key in keys:
            val = await self.db.get_guild_config(guild.id, key)
            embed.add_field(name=key, value=f"`{val}`" if val else "*not set*", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Role management
    # ------------------------------------------------------------------

    role_group = app_commands.Group(name="role", description="Role management commands", parent=admin_group)

    @role_group.command(name="add", description="Add a role to a member")
    @app_commands.describe(member="Target member", role="Role to add")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        try:
            await member.add_roles(role, reason=f"Added by {interaction.user}")
            await interaction.response.send_message(
                f"✅ Added **{role.name}** to {member.mention}.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ I can't assign that role (hierarchy issue).", ephemeral=True)

    @role_group.command(name="remove", description="Remove a role from a member")
    @app_commands.describe(member="Target member", role="Role to remove")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        try:
            await member.remove_roles(role, reason=f"Removed by {interaction.user}")
            await interaction.response.send_message(
                f"✅ Removed **{role.name}** from {member.mention}.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ I can't remove that role (hierarchy issue).", ephemeral=True)

    @role_group.command(name="members", description="List members with a specific role")
    @app_commands.describe(role="The role to inspect")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_members(self, interaction: discord.Interaction, role: discord.Role) -> None:
        members = role.members
        if not members:
            await interaction.response.send_message(f"No members have **{role.name}**.", ephemeral=True)
            return
        text = ", ".join(m.mention for m in members[:50])
        if len(members) > 50:
            text += f"\n… and {len(members) - 50} more"
        embed = discord.Embed(title=f"Members with {role.name}", description=text, color=role.color)
        embed.set_footer(text=f"Total: {len(members)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Self-roles
    # ------------------------------------------------------------------

    selfrole_group = app_commands.Group(name="selfrole", description="Self-assignable role management")

    @selfrole_group.command(name="add", description="Mark a role as self-assignable")
    @app_commands.describe(role="The role to make self-assignable")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def selfrole_add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        added = await self.db.add_selfrole(interaction.guild_id, role.id)  # type: ignore[arg-type]
        if added:
            await interaction.response.send_message(f"✅ **{role.name}** is now self-assignable.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ That role is already self-assignable.", ephemeral=True)

    @selfrole_group.command(name="remove", description="Remove a role from self-assignable list")
    @app_commands.describe(role="The role to remove")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def selfrole_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        removed = await self.db.remove_selfrole(interaction.guild_id, role.id)  # type: ignore[arg-type]
        if removed:
            await interaction.response.send_message(f"✅ **{role.name}** is no longer self-assignable.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ That role was not self-assignable.", ephemeral=True)

    @selfrole_group.command(name="list", description="List all self-assignable roles")
    async def selfrole_list(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        role_ids = await self.db.get_selfroles(guild.id)
        roles = [guild.get_role(r) for r in role_ids]
        roles = [r for r in roles if r is not None]
        if not roles:
            await interaction.response.send_message("No self-assignable roles configured.", ephemeral=True)
            return
        text = "\n".join(f"• {r.mention}" for r in roles)
        embed = discord.Embed(title="Self-Assignable Roles", description=text, color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @selfrole_group.command(name="panel", description="Post a self-role picker panel")
    @app_commands.describe(channel="Channel to post the panel in")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def selfrole_panel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        embed = discord.Embed(
            title="🎭 Self-Assignable Roles",
            description="Click the button below to pick your roles!",
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed, view=SelfRoleView(self))
        await interaction.response.send_message(f"✅ Self-role panel posted in {channel.mention}.", ephemeral=True)

    # ------------------------------------------------------------------
    # Nickname management
    # ------------------------------------------------------------------

    @app_commands.command(name="nick", description="Change a member's nickname")
    @app_commands.describe(member="Target member", nickname="New nickname (leave empty to reset)")
    @app_commands.checks.has_permissions(manage_nicknames=True)
    async def nick(
        self, interaction: discord.Interaction, member: discord.Member, nickname: str | None = None
    ) -> None:
        try:
            await member.edit(nick=nickname, reason=f"Changed by {interaction.user}")
            if nickname:
                await interaction.response.send_message(
                    f"✅ {member.mention}'s nickname set to **{nickname}**.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"✅ {member.mention}'s nickname has been reset.", ephemeral=True
                )
        except discord.Forbidden:
            await interaction.response.send_message("❌ I can't change that member's nickname.", ephemeral=True)

    # ------------------------------------------------------------------
    # Announcements
    # ------------------------------------------------------------------

    @app_commands.command(name="announce", description="Send an announcement embed to a channel")
    @app_commands.describe(
        channel="Target channel",
        title="Announcement title",
        message="Announcement body",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        message: str,
    ) -> None:
        embed = discord.Embed(
            title=title,
            description=message,
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Announced by {interaction.user}")
        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Announcement posted in {channel.mention}.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Server info (quick access)
    # ------------------------------------------------------------------

    @app_commands.command(name="serverconfig", description="View current bot configuration for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def serverconfig(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        keys = [
            "mod_log_channel", "ticket_category", "welcome_channel",
            "welcome_message", "autorole", "verified_role", "automod_enabled",
            "payday_amount", "payday_cooldown_hours",
        ]
        embed = discord.Embed(title=f"Bot Config — {guild.name}", color=discord.Color.blurple())
        for key in keys:
            val = await self.db.get_guild_config(guild.id, key)
            embed.add_field(name=key, value=f"`{val}`" if val else "*not set*", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
