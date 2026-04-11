"""Ticket system cog — button-based creation, modal for details, channel-per-ticket.

Flow
----
1. Admin runs ``/ticket_panel`` to post a persistent button embed in a channel.
2. User clicks **Open Ticket** → a modal asks for a subject & description.
3. Bot creates a private channel, pins the ticket info, and adds control buttons
   (Claim, Close, Transcript).
4. All messages in the ticket channel are logged for transcript.
5. Staff can ``/ticket_close`` or click Close to archive the ticket.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database
    from bot.cogs.mod_logging import ModLoggingCog

logger = logging.getLogger(__name__)


# ======================================================================
# UI components
# ======================================================================

class TicketCreateModal(discord.ui.Modal, title="Open a Support Ticket"):
    """Modal popup that collects ticket subject and description."""

    subject = discord.ui.TextInput(
        label="Subject",
        placeholder="Brief summary of your issue…",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Describe your issue in detail…",
        required=False,
        max_length=2000,
    )

    def __init__(self, cog: TicketsCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_ticket(
            interaction,
            subject=self.subject.value,
            description=self.description.value,
        )


class TicketPanelView(discord.ui.View):
    """Persistent view attached to the ticket panel embed (never times out)."""

    def __init__(self, cog: TicketsCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="🎫 Open Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_panel:open",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TicketCreateModal(self.cog))


class TicketControlView(discord.ui.View):
    """Buttons inside an open ticket channel (Claim / Close / Transcript)."""

    def __init__(self, cog: TicketsCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="🙋 Claim",
        style=discord.ButtonStyle.success,
        custom_id="ticket_ctrl:claim",
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.claim_ticket(interaction)

    @discord.ui.button(
        label="📜 Transcript",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket_ctrl:transcript",
    )
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.send_transcript(interaction)

    @discord.ui.button(
        label="🔒 Close",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_ctrl:close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.close_ticket_button(interaction)


# ======================================================================
# Cog
# ======================================================================

class TicketsCog(commands.Cog, name="Tickets"):
    """Channel-per-ticket support system with modals and buttons."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def cog_load(self) -> None:
        # Register persistent views so they survive restarts
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlView(self))

    @property
    def mod_log(self) -> ModLoggingCog | None:
        return self.bot.get_cog("ModLogging")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # /ticket_panel — post the persistent Open-Ticket embed
    # ------------------------------------------------------------------

    @app_commands.command(name="ticket_panel", description="Post a ticket panel with an Open Ticket button")
    @app_commands.describe(channel="Channel to post the panel in")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        embed = discord.Embed(
            title="🎫 Support Tickets",
            description=(
                "Need help?  Click the button below to open a private support ticket.\n\n"
                "A staff member will be with you shortly."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="One open ticket per user at a time.")
        await channel.send(embed=embed, view=TicketPanelView(self))
        await interaction.response.send_message(f"✅ Ticket panel posted in {channel.mention}.", ephemeral=True)

    # ------------------------------------------------------------------
    # /ticket_category — set the category for ticket channels
    # ------------------------------------------------------------------

    @app_commands.command(name="ticket_category", description="Set the category for new ticket channels")
    @app_commands.describe(category="The category to create ticket channels under")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_category(
        self, interaction: discord.Interaction, category: discord.CategoryChannel
    ) -> None:
        await self.db.set_guild_config(interaction.guild_id, "ticket_category", str(category.id))  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Ticket category set to **{category.name}**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Core ticket logic
    # ------------------------------------------------------------------

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        subject: str,
        description: str | None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        user = interaction.user

        # Check for existing open ticket
        existing = await self.db.get_open_tickets(guild.id, user.id)
        if existing:
            await interaction.response.send_message(
                "❌ You already have an open ticket. Please close it before opening a new one.",
                ephemeral=True,
            )
            return

        # Determine category
        cat_id_raw = await self.db.get_guild_config(guild.id, "ticket_category")
        category = guild.get_channel(int(cat_id_raw)) if cat_id_raw else None

        # Create private channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        # Grant access to roles with manage_messages (staff)
        for role in guild.roles:
            if role.permissions.manage_messages and not role.is_default():
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel_name = f"ticket-{user.name[:16]}-{user.discriminator or user.id}"
        channel = await guild.create_text_channel(
            channel_name,
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason=f"Support ticket by {user}",
        )

        ticket_id = await self.db.create_ticket(guild.id, user.id, channel.id, subject)

        # Post ticket info embed + control buttons
        embed = discord.Embed(
            title=f"Ticket #{ticket_id} — {subject}",
            description=description or "*No additional details provided.*",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Opened by", value=user.mention, inline=True)
        embed.add_field(name="Status", value="🟢 Open", inline=True)
        embed.set_footer(text="Use the buttons below to manage this ticket.")

        msg = await channel.send(embed=embed, view=TicketControlView(self))
        await msg.pin()

        await interaction.response.send_message(
            f"✅ Ticket created! Head to {channel.mention}.", ephemeral=True
        )

        if self.mod_log:
            await self.mod_log.log(
                guild, action="ticket_open", target=user,
                extra=f"**Ticket:** #{ticket_id}\n**Subject:** {subject}\n**Channel:** {channel.mention}",
            )

    async def claim_ticket(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        ticket = await self.db.get_ticket_by_channel(interaction.channel_id)  # type: ignore[arg-type]
        if not ticket:
            await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
            return

        await self.db.claim_ticket(ticket["id"], interaction.user.id)
        await interaction.response.send_message(
            f"🙋 Ticket claimed by {interaction.user.mention}."
        )

    async def send_transcript(self, interaction: discord.Interaction) -> None:
        ticket = await self.db.get_ticket_by_channel(interaction.channel_id)  # type: ignore[arg-type]
        if not ticket:
            await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
            return

        messages = await self.db.get_ticket_transcript(ticket["id"])
        if not messages:
            await interaction.response.send_message("No messages recorded yet.", ephemeral=True)
            return

        lines: list[str] = []
        for m in messages:
            user = self.bot.get_user(m["user_id"])
            name = str(user) if user else str(m["user_id"])
            lines.append(f"[{m['created_at']}] {name}: {m['content']}")

        text = "\n".join(lines)
        buf = io.BytesIO(text.encode("utf-8"))
        file = discord.File(buf, filename=f"transcript-ticket-{ticket['id']}.txt")
        await interaction.response.send_message("📜 Here is the transcript:", file=file, ephemeral=True)

    async def close_ticket_button(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        ticket = await self.db.get_ticket_by_channel(interaction.channel_id)  # type: ignore[arg-type]
        if not ticket:
            await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
            return

        await self.db.close_ticket(ticket["id"])

        embed = discord.Embed(
            title="Ticket Closed",
            description="This ticket has been closed. The channel will be deleted in 10 seconds.",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed)

        if self.mod_log:
            opener = self.bot.get_user(ticket["user_id"])
            await self.mod_log.log(
                guild, action="ticket_close", target=opener,
                moderator=interaction.user,
                extra=f"**Ticket:** #{ticket['id']}",
            )

        # Delete channel after a brief delay
        import asyncio
        await asyncio.sleep(10)
        try:
            channel = guild.get_channel(interaction.channel_id)  # type: ignore[arg-type]
            if channel:
                await channel.delete(reason="Ticket closed")
        except discord.Forbidden:
            pass

    # ------------------------------------------------------------------
    # /ticket_close — slash command variant
    # ------------------------------------------------------------------

    @app_commands.command(name="ticket_close", description="Close the current ticket")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def ticket_close_cmd(self, interaction: discord.Interaction) -> None:
        await self.close_ticket_button(interaction)

    # ------------------------------------------------------------------
    # Listener: record messages in ticket channels
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        ticket = await self.db.get_ticket_by_channel(message.channel.id)
        if ticket and ticket["status"] != "closed":
            await self.db.add_ticket_message(ticket["id"], message.author.id, message.content or "[attachment]")
