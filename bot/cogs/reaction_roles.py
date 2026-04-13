"""Reaction Roles cog - emoji-based role assignment system.

Features
--------
- Create reaction role messages with emoji-to-role mappings
- Support for unique roles (only one role per message)
- Automatic reaction addition to setup messages
- Clean removal and management of reaction roles
- Persistent views for reaction role messages
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


class ReactionRoleView(discord.ui.View):
    """Persistent view for managing reaction role messages."""

    def __init__(self, cog: "ReactionRolesCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="📝 Edit Roles",
        style=discord.ButtonStyle.secondary,
        custom_id="reaction_roles:edit",
    )
    async def edit_roles(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Open a modal to edit reaction roles for this message."""
        guild = interaction.guild
        assert guild is not None
        
        # Check if user has manage_roles permission
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "❌ You need **Manage Roles** permission to edit reaction roles.",
                ephemeral=True,
            )
            return

        # Get current reaction roles for this message
        message_id = interaction.message.id
        roles = await self.cog.db.get_reaction_roles(guild.id, message_id)
        
        if not roles:
            await interaction.response.send_message(
                "❌ No reaction roles found for this message.",
                ephemeral=True,
            )
            return

        # Show current roles
        embed = discord.Embed(
            title="📋 Current Reaction Roles",
            description="Use `/reaction_role remove` to remove specific roles, or `/reaction_role clear` to remove all.",
            color=discord.Color.blue(),
        )
        
        for role_data in roles:
            role = guild.get_role(role_data["role_id"])
            if role:
                unique_text = " (unique)" if role_data["unique_role"] else ""
                embed.add_field(
                    name=f"{role_data['emoji']} → {role.name}{unique_text}",
                    value=f"Role ID: {role.id}",
                    inline=False,
                )

        await interaction.response.send_message(embed=embed, ephemeral=True)


class ReactionRolesCog(commands.Cog, name="Reaction Roles"):
    """Reaction role management - assign roles via emoji reactions."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.bot.add_view(ReactionRoleView(self))

    # ------------------------------------------------------------------
    # Reaction role command group
    # ------------------------------------------------------------------

    reaction_group = app_commands.Group(name="rr", description="Reaction role management")

    @reaction_group.command(name="create", description="Create a reaction role message")
    @app_commands.describe(
        channel="Channel to send the message in",
        title="Title of the reaction role message",
        description="Description of what the roles are for",
        roles="Role mappings (format: emoji:role_id, comma-separated)",
        unique="Whether users can only have one role from this message"
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        description: str,
        roles: str,
        unique: bool = False,
    ) -> None:
        """Create a new reaction role message."""
        guild = interaction.guild
        assert guild is not None

        # Parse role mappings
        role_mappings = []
        errors = []

        for mapping in roles.split(","):
            mapping = mapping.strip()
            if not mapping:
                continue

            if ":" not in mapping:
                errors.append(f"Invalid format: `{mapping}` (use emoji:role_id)")
                continue

            emoji, role_id_str = mapping.split(":", 1)
            emoji = emoji.strip()
            role_id_str = role_id_str.strip()

            try:
                role_id = int(role_id_str)
                role = guild.get_role(role_id)
                if not role:
                    errors.append(f"Role not found: `{role_id}`")
                    continue

                if role.position >= guild.me.top_role.position:
                    errors.append(f"Role `{role.name}` is higher than bot's top role")
                    continue

                role_mappings.append((emoji, role))
            except ValueError:
                errors.append(f"Invalid role ID: `{role_id_str}`")

        if errors:
            embed = discord.Embed(
                title="❌ Reaction Role Creation Failed",
                description="\n".join(errors),
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Create the embed
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blurple(),
        )
        
        embed.add_field(
            name="📋 Available Roles",
            value="\n".join(f"{emoji} → **{role.name}**" for emoji, role in role_mappings),
            inline=False,
        )
        
        embed.set_footer(text="React below to get your role!")
        
        # Send the message
        try:
            message = await channel.send(embed=embed, view=ReactionRoleView(self))
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to send messages in that channel.",
                ephemeral=True,
            )
            return

        # Save reaction roles to database
        success_count = 0
        for emoji, role in role_mappings:
            if await self.db.add_reaction_role(
                guild.id, message.id, channel.id, emoji, role.id, unique
            ):
                success_count += 1
                try:
                    await message.add_reaction(emoji)
                except discord.Forbidden:
                    logger.warning(f"Could not add reaction {emoji} to message {message.id}")

        if success_count == len(role_mappings):
            await interaction.response.send_message(
                f"✅ Reaction role message created with {success_count} role mappings!\n"
                f"📎 [Jump to message]({message.jump_url})",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"⚠️ Created message but only {success_count}/{len(role_mappings)} roles were saved.",
                ephemeral=True,
            )

    @reaction_group.command(name="add", description="Add a reaction role to an existing message")
    @app_commands.describe(
        message="Message to add reaction role to (right-click > Copy Message Link)",
        emoji="Emoji to use for the role",
        role="Role to assign when emoji is reacted",
        unique="Whether to remove other roles from this message when adding"
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add_reaction_role(
        self,
        interaction: discord.Interaction,
        message: str,
        emoji: str,
        role: discord.Role,
        unique: bool = False,
    ) -> None:
        """Add a reaction role to an existing message."""
        guild = interaction.guild
        assert guild is not None

        # Parse message ID from link or direct ID
        try:
            if "/" in message:
                # Message link format
                parts = message.split("/")
                message_id = int(parts[-1])
                channel_id = int(parts[-2])
            else:
                # Direct message ID
                message_id = int(message)
                channel_id = interaction.channel_id
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid message format. Use a message link or direct message ID.",
                ephemeral=True,
            )
            return

        # Get the channel and message
        try:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise ValueError("Not a text channel")
            
            target_message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, ValueError):
            await interaction.response.send_message(
                "❌ Could not find that message. Make sure I have access to the channel.",
                ephemeral=True,
            )
            return

        # Check role hierarchy
        if role.position >= guild.me.top_role.position:
            await interaction.response.send_message(
                "❌ That role is higher than my top role, so I cannot assign it.",
                ephemeral=True,
            )
            return

        # Add to database
        if not await self.db.add_reaction_role(
            guild.id, message_id, channel_id, emoji, role.id, unique
        ):
            await interaction.response.send_message(
                "❌ That emoji is already used for a role on this message.",
                ephemeral=True,
            )
            return

        # Add reaction to message
        try:
            await target_message.add_reaction(emoji)
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ Reaction role saved but I couldn't add the emoji reaction to the message.\n"
                "Make sure I have 'Add Reactions' permission.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Added reaction role: {emoji} → **{role.name}**\n"
            f"📎 [Jump to message]({target_message.jump_url})",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /reaction_role remove
    # ------------------------------------------------------------------

    @reaction_group.command(name="remove", description="Remove a reaction role from a message")
    @app_commands.describe(
        message="Message to remove reaction role from",
        emoji="Emoji to remove"
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove_reaction_role(
        self,
        interaction: discord.Interaction,
        message: str,
        emoji: str,
    ) -> None:
        """Remove a reaction role from a message."""
        guild = interaction.guild
        assert guild is not None

        # Parse message ID
        try:
            if "/" in message:
                parts = message.split("/")
                message_id = int(parts[-1])
                channel_id = int(parts[-2])
            else:
                message_id = int(message)
                channel_id = interaction.channel_id
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid message format.",
                ephemeral=True,
            )
            return

        # Get the reaction role data
        reaction_role = await self.db.get_reaction_role(guild.id, message_id, emoji)
        if not reaction_role:
            await interaction.response.send_message(
                "❌ No reaction role found for that emoji on this message.",
                ephemeral=True,
            )
            return

        # Remove from database
        if await self.db.remove_reaction_role(guild.id, message_id, emoji):
            # Try to remove the reaction from the message
            try:
                channel = guild.get_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    target_message = await channel.fetch_message(message_id)
                    await target_message.clear_reaction(emoji)
            except (discord.NotFound, discord.Forbidden):
                pass  # It's okay if we can't remove the reaction

            role = guild.get_role(reaction_role["role_id"])
            role_name = role.name if role else f"ID {reaction_role['role_id']}"

            await interaction.response.send_message(
                f"✅ Removed reaction role: {emoji} → **{role_name}**",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Failed to remove reaction role.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /reaction_role clear
    # ------------------------------------------------------------------

    @reaction_group.command(name="clear", description="Remove all reaction roles from a message")
    @app_commands.describe(
        message="Message to clear all reaction roles from"
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def clear_reaction_roles(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        """Remove all reaction roles from a message."""
        guild = interaction.guild
        assert guild is not None

        # Parse message ID
        try:
            if "/" in message:
                parts = message.split("/")
                message_id = int(parts[-1])
                channel_id = int(parts[-2])
            else:
                message_id = int(message)
                channel_id = interaction.channel_id
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid message format.",
                ephemeral=True,
            )
            return

        # Remove all reaction roles
        removed_count = await self.db.remove_all_reaction_roles(guild.id, message_id)

        if removed_count > 0:
            # Try to clear all reactions from the message
            try:
                channel = guild.get_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    target_message = await channel.fetch_message(message_id)
                    await target_message.clear_reactions()
            except (discord.NotFound, discord.Forbidden):
                pass

            await interaction.response.send_message(
                f"✅ Removed {removed_count} reaction roles from the message.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ No reaction roles found for that message.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /reaction_role list
    # ------------------------------------------------------------------

    @reaction_group.command(name="list", description="List all reaction roles in the server")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def list_reaction_roles(self, interaction: discord.Interaction) -> None:
        """List all reaction roles in the server."""
        guild = interaction.guild
        assert guild is not None

        reaction_roles = await self.db.get_reaction_roles(guild.id)
        
        if not reaction_roles:
            await interaction.response.send_message(
                "❌ No reaction roles configured in this server.",
                ephemeral=True,
            )
            return

        # Group by message
        messages = {}
        for role_data in reaction_roles:
            msg_id = role_data["message_id"]
            if msg_id not in messages:
                messages[msg_id] = []
            messages[msg_id].append(role_data)

        embed = discord.Embed(
            title="📋 Reaction Roles",
            description=f"Total: {len(reaction_roles)} reaction roles across {len(messages)} messages",
            color=discord.Color.blue(),
        )

        for msg_id, roles in list(messages.items())[:10]:  # Limit to 10 messages
            channel = guild.get_channel(roles[0]["channel_id"])
            channel_name = channel.name if channel else "unknown-channel"
            
            role_list = []
            for role_data in roles:
                role = guild.get_role(role_data["role_id"])
                role_name = role.name if role else f"ID:{role_data['role_id']}"
                unique_text = " 🔒" if role_data["unique_role"] else ""
                role_list.append(f"{role_data['emoji']} → {role_name}{unique_text}")
            
            embed.add_field(
                name=f"📄 Message in #{channel_name}",
                value="\n".join(role_list),
                inline=False,
            )

        if len(messages) > 10:
            embed.set_footer(text(f"Showing 10 of {len(messages)} messages"))

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Handle reaction additions for role assignment."""
        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member:
            return

        # Get reaction role data
        reaction_role = await self.db.get_reaction_role(
            payload.guild_id, payload.message_id, str(payload.emoji)
        )
        
        if not reaction_role:
            return

        role = guild.get_role(reaction_role["role_id"])
        if not role:
            return

        # Check if user already has the role
        if role in member.roles:
            return

        # Remove other roles from this message if unique
        if reaction_role["unique_role"]:
            other_roles = await self.db.get_reaction_roles(
                payload.guild_id, payload.message_id
            )
            for other_role_data in other_roles:
                if other_role_data["emoji"] != str(payload.emoji):
                    other_role = guild.get_role(other_role_data["role_id"])
                    if other_role and other_role in member.roles:
                        try:
                            await member.remove_roles(other_role)
                        except discord.Forbidden:
                            pass

        # Add the role
        try:
            await member.add_roles(role)
            logger.info(
                f"Assigned role {role.name} to {member} via reaction {payload.emoji}"
            )
        except discord.Forbidden:
            logger.warning(
                f"Failed to assign role {role.name} to {member} - missing permissions"
            )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Handle reaction removals for role removal."""
        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member:
            return

        # Get reaction role data
        reaction_role = await self.db.get_reaction_role(
            payload.guild_id, payload.message_id, str(payload.emoji)
        )
        
        if not reaction_role:
            return

        role = guild.get_role(reaction_role["role_id"])
        if not role:
            return

        # Remove the role if user has it
        if role in member.roles:
            try:
                await member.remove_roles(role)
                logger.info(
                    f"Removed role {role.name} from {member} via reaction removal {payload.emoji}"
                )
            except discord.Forbidden:
                logger.warning(
                    f"Failed to remove role {role.name} from {member} - missing permissions"
                )


async def setup(bot: commands.Bot) -> None:
    """Load the ReactionRoles cog."""
    await bot.add_cog(ReactionRolesCog(bot, bot.db))
