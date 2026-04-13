"""Welcome cog — customisable welcome messages, auto-role, rules acceptance, and welcome images.

Features
--------
- Configurable welcome channel and message (with {user}, {server} placeholders).
- Auto-role assignment on join.
- Rules acceptance via button — grants a "Verified" role.
- Visual welcome cards with customizable backgrounds and text.
"""

from __future__ import annotations

import io
import logging
import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageFilter

if TYPE_CHECKING:
    from bot.db import Database

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
    # Welcome command group
    # ------------------------------------------------------------------

    welcome_group = app_commands.Group(name="welcome", description="Welcome message and image settings")

    @welcome_group.command(name="channel", description="Set the welcome channel")
    @app_commands.describe(channel="Channel to send welcome messages in")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_welcome_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.db.set_guild_config(interaction.guild_id, "welcome_channel", str(channel.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Welcome channel set to {channel.mention}.", ephemeral=True
        )

    @welcome_group.command(name="message", description="Set the welcome message template")
    @app_commands.describe(
        message="Use {user} for mention, {username} for name, {server} for server name"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def set_welcome_message(self, interaction: discord.Interaction, message: str) -> None:
        await self.db.set_guild_config(interaction.guild_id, "welcome_message", message)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Welcome message updated.\n**Preview:**\n{message}", ephemeral=True
        )

    @welcome_group.command(name="autorole", description="Set a role to auto-assign to new members")
    @app_commands.describe(role="The role to give new members")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_autorole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.db.set_guild_config(interaction.guild_id, "autorole", str(role.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Auto-role set to **{role.name}**.", ephemeral=True
        )

    @welcome_group.command(name="verified_role", description="Set the role granted by rules acceptance")
    @app_commands.describe(role="The role to grant when a user accepts the rules")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_verified_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.db.set_guild_config(interaction.guild_id, "verified_role", str(role.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Verified role set to **{role.name}**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Welcome image configuration
    # ------------------------------------------------------------------

    @welcome_group.command(name="images_enable", description="Enable visual welcome cards")
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_images_enable(self, interaction: discord.Interaction) -> None:
        await self.db.set_guild_config(interaction.guild_id, "welcome_images_enabled", "true")  # type: ignore[arg-type]
        await interaction.response.send_message(
            "✅ Welcome images enabled. New members will receive a visual welcome card.",
            ephemeral=True,
        )

    @welcome_group.command(name="images_disable", description="Disable visual welcome cards")
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_images_disable(self, interaction: discord.Interaction) -> None:
        await self.db.set_guild_config(interaction.guild_id, "welcome_images_enabled", "false")  # type: ignore[arg-type]
        await interaction.response.send_message(
            "⚠️ Welcome images disabled. Members will receive text welcomes only.",
            ephemeral=True,
        )

    @welcome_group.command(name="images_style", description="Set welcome image style")
    @app_commands.describe(
        style="Style of welcome image (modern, minimalist, colorful)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_images_style(self, interaction: discord.Interaction, style: str) -> None:
        valid_styles = ["modern", "minimalist", "colorful"]
        if style.lower() not in valid_styles:
            await interaction.response.send_message(
                f"❌ Invalid style. Choose from: {', '.join(valid_styles)}",
                ephemeral=True,
            )
            return
        
        await self.db.set_guild_config(interaction.guild_id, "welcome_images_style", style.lower())  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Welcome image style set to **{style.lower()}**.",
            ephemeral=True,
        )

    @welcome_group.command(name="images_preview", description="Preview a welcome image")
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_images_preview(self, interaction: discord.Interaction) -> None:
        """Generate a preview welcome image."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            image_file = await self.generate_welcome_image(interaction.user, interaction.guild)  # type: ignore[arg-type]
            await interaction.followup.send("🖼️ Welcome image preview:", file=image_file, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to generate preview: {e}")
            await interaction.followup.send(
                "❌ Failed to generate preview. Make sure Pillow is installed.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # Welcome image generation
    # ------------------------------------------------------------------

    async def generate_welcome_image(self, member: discord.Member, guild: discord.Guild) -> discord.File:
        """Generate a welcome image for a new member."""
        # Get configured style or default to modern
        style = await self.db.get_guild_config(guild.id, "welcome_images_style") or "modern"
        
        # Create base image
        width, height = 800, 400
        
        # Generate gradient background based on style
        if style == "colorful":
            colors = self._get_random_gradient_colors()
            img = self._create_gradient_background(width, height, colors)
        elif style == "minimalist":
            img = Image.new("RGB", (width, height), (240, 240, 245))
        else:  # modern
            colors = [(66, 135, 245), (60, 100, 180)]  # Discord-like blue gradient
            img = self._create_gradient_background(width, height, colors)

        draw = ImageDraw.Draw(img)

        # Download and add user avatar
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(member.display_avatar.url) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        avatar = Image.open(io.BytesIO(avatar_data))
                        avatar = avatar.resize((120, 120))
                        avatar = avatar.convert("RGBA")
                        
                        # Create circular mask
                        mask = Image.new("L", (120, 120), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse((0, 0, 120, 120), fill=255)
                        
                        # Apply circular mask
                        avatar_output = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
                        avatar_output.paste(avatar, (0, 0))
                        avatar_output.putalpha(mask)
                        
                        # Add border
                        border = Image.new("RGBA", (126, 126), (255, 255, 255, 200))
                        border_mask = Image.new("L", (126, 126), 0)
                        border_draw = ImageDraw.Draw(border_mask)
                        border_draw.ellipse((0, 0, 126, 126), fill=255)
                        border.putalpha(border_mask)
                        
                        # Paste avatar with border
                        img.paste(border, (50, 140), border)
                        img.paste(avatar_output, (53, 143), avatar_output)
        except Exception as e:
            logger.warning(f"Could not fetch avatar for {member}: {e}")
            # Draw placeholder circle
            draw.ellipse((50, 140, 170, 260), fill=(150, 150, 150), outline=(200, 200, 200), width=3)

        # Draw welcome text
        try:
            # Try to use a nice font, fallback to default
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
            font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()

        # Draw "WELCOME" text
        draw.text((200, 50), "WELCOME", fill=(255, 255, 255), font=font_large)

        # Draw username
        username = member.display_name[:20]  # Limit length
        draw.text((200, 110), username, fill=(255, 255, 255), font=font_medium)

        # Draw server info
        member_text = f"Member #{guild.member_count}"
        draw.text((200, 160), f"to {guild.name}", fill=(200, 200, 255), font=font_small)
        draw.text((200, 190), member_text, fill=(200, 200, 255), font=font_small)

        # Add decorative elements for modern style
        if style == "modern":
            draw.rectangle((0, height-50, width, height), fill=(50, 50, 60))
            draw.text((20, height-40), "Playerbot-support", fill=(150, 150, 150), font=font_small)

        # Save to bytes
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        return discord.File(buffer, filename=f"welcome_{member.id}.png")

    def _create_gradient_background(self, width: int, height: int, colors: tuple) -> Image.Image:
        """Create a gradient background image."""
        img = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(img)
        
        for y in range(height):
            ratio = y / height
            r = int(colors[0][0] * (1 - ratio) + colors[1][0] * ratio)
            g = int(colors[0][1] * (1 - ratio) + colors[1][1] * ratio)
            b = int(colors[0][2] * (1 - ratio) + colors[1][2] * ratio)
            draw.rectangle((0, y, width, y+1), fill=(r, g, b))
        
        return img

    def _get_random_gradient_colors(self) -> tuple:
        """Generate random gradient colors."""
        color_sets = [
            ((255, 100, 100), (200, 50, 50)),   # Red
            ((100, 255, 100), (50, 200, 50)),   # Green
            ((100, 100, 255), (50, 50, 200)),   # Blue
            ((255, 200, 100), (200, 150, 50)),  # Orange
            ((200, 100, 255), (150, 50, 200)),  # Purple
            ((100, 255, 200), (50, 200, 150)),  # Teal
        ]
        return random.choice(color_sets)

    @welcome_group.command(name="rules_panel", description="Post a rules acceptance panel with a button")
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

        # Check if welcome images are enabled
        welcome_images_enabled = await self.db.get_guild_config(guild.id, "welcome_images_enabled")
        if welcome_images_enabled == "true":
            # Generate welcome image
            try:
                image_file = await self.generate_welcome_image(member, guild)
                await channel.send(file=image_file)
            except Exception as e:
                logger.error(f"Failed to generate welcome image: {e}")
                # Fallback to regular embed if image generation fails

        # Also send the text embed
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
