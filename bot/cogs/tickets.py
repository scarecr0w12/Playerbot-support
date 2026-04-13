"""Ticket system cog — button-based creation, modal for details, channel-per-ticket.

Flow
----
1. Admin runs ``/ticket panel`` to post a persistent button embed in a channel.
2. User clicks **Open Ticket** → a modal asks for a subject & description.
3. Bot creates a private channel, pins the ticket info, and adds control buttons
   (Claim, Close, Transcript).
4. All messages in the ticket channel are logged for transcript.
5. Staff can ``/ticket close`` or click Close to archive the ticket.
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
    """Ticket system with buttons, transcripts, and auto-categorization."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlView(self))

    @property
    def mod_log(self) -> ModLoggingCog | None:
        return self.bot.get_cog("ModLogging")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Ticket command group
    # ------------------------------------------------------------------

    ticket_group = app_commands.Group(name="ticket", description="Ticket system management")

    @ticket_group.command(name="panel", description="Post a ticket panel with an Open Ticket button")
    @app_commands.describe(
        channel="Channel to post the panel in",
        title="Title for the ticket panel embed",
        description="Description for the ticket panel"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
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

    @ticket_group.command(name="category", description="Set the category for new ticket channels")
    @app_commands.describe(category="Category for new ticket channels")
    @app_commands.checks.has_permissions(manage_guild=True)
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

        await interaction.response.defer(ephemeral=True)

        # Generate HTML transcript
        html_content = await self._generate_html_transcript(ticket, messages)
        
        # Also generate plain text for compatibility
        text_content = await self._generate_text_transcript(ticket, messages)

        # Create files
        html_buf = io.BytesIO(html_content.encode("utf-8"))
        text_buf = io.BytesIO(text_content.encode("utf-8"))
        
        files = [
            discord.File(html_buf, filename=f"transcript-{ticket['id']}.html"),
            discord.File(text_buf, filename=f"transcript-{ticket['id']}.txt"),
        ]

        await interaction.followup.send("📜 Ticket transcript generated:", files=files, ephemeral=True)

    async def _generate_html_transcript(self, ticket: dict, messages: list[dict]) -> str:
        """Generate an HTML transcript with proper formatting."""
        guild = self.bot.get_guild(ticket["guild_id"])
        guild_name = guild.name if guild else "Unknown Server"
        
        # Get ticket creator and claimer
        creator = self.bot.get_user(ticket["user_id"])
        creator_name = creator.display_name if creator else f"User {ticket['user_id']}"
        
        claimer = None
        if ticket["claimed_by"]:
            claimer = self.bot.get_user(ticket["claimed_by"])
        
        created_at = datetime.fromisoformat(ticket["created_at"])
        closed_at = None
        if ticket["closed_at"]:
            closed_at = datetime.fromisoformat(ticket["closed_at"])

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ticket #{ticket['id']} Transcript</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
            color: #333;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{
            margin: 0;
            font-size: 2em;
        }}
        .header .meta {{
            margin-top: 10px;
            opacity: 0.9;
        }}
        .info {{
            padding: 20px;
            background: #f8f9fa;
            border-bottom: 1px solid #dee2e6;
        }}
        .info-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }}
        .info-item {{
            background: white;
            padding: 15px;
            border-radius: 5px;
            border-left: 4px solid #667eea;
        }}
        .info-label {{
            font-weight: bold;
            color: #666;
            font-size: 0.9em;
        }}
        .messages {{
            padding: 20px;
        }}
        .message {{
            margin-bottom: 20px;
            padding: 15px;
            border-radius: 8px;
            background: #f8f9fa;
            border-left: 4px solid #667eea;
        }}
        .message.staff {{
            border-left-color: #28a745;
            background: #f8fff9;
        }}
        .message-header {{
            display: flex;
            align-items: center;
            margin-bottom: 8px;
        }}
        .avatar {{
            width: 32px;
            height: 32px;
            border-radius: 50%;
            margin-right: 12px;
            background: #667eea;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
        }}
        .message-info {{
            flex: 1;
        }}
        .author {{
            font-weight: bold;
            color: #333;
        }}
        .timestamp {{
            font-size: 0.85em;
            color: #666;
        }}
        .message-content {{
            margin-left: 44px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}
        .footer {{
            padding: 20px;
            text-align: center;
            background: #f8f9fa;
            border-top: 1px solid #dee2e6;
            color: #666;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎫 Ticket #{ticket['id']}</h1>
            <div class="meta">
                <strong>{ticket['subject']}</strong><br>
                Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
            </div>
        </div>
        
        <div class="info">
            <div class="info-grid">
                <div class="info-item">
                    <div class="info-label">Server</div>
                    <div>{guild_name}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Created By</div>
                    <div>{creator_name}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Status</div>
                    <div>{ticket['status'].title()}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Created</div>
                    <div>{created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</div>
                </div>
                {f'''<div class="info-item">
                    <div class="info-label">Claimed By</div>
                    <div>{claimer.display_name if claimer else 'Unclaimed'}</div>
                </div>''' if claimer else ''}
                {f'''<div class="info-item">
                    <div class="info-label">Closed</div>
                    <div>{closed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</div>
                </div>''' if closed_at else ''}
            </div>
        </div>
        
        <div class="messages">
            <h2>Conversation</h2>
"""

        # Add messages
        for msg in messages:
            user = self.bot.get_user(msg["user_id"])
            author_name = user.display_name if user else f"User {msg['user_id']}"
            avatar_text = author_name[0].upper()
            
            # Check if user is staff (you might want to implement proper role checking)
            is_staff = user and user.id in [ticket["claimed_by"]] if ticket["claimed_by"] else False
            
            msg_time = datetime.fromisoformat(msg["created_at"])
            
            html += f"""
            <div class="message{' staff' if is_staff else ''}">
                <div class="message-header">
                    <div class="avatar">{avatar_text}</div>
                    <div class="message-info">
                        <div class="author">{author_name}</div>
                        <div class="timestamp">{msg_time.strftime('%Y-%m-%d %H:%M:%S UTC')}</div>
                    </div>
                </div>
                <div class="message-content">{msg['content'].replace('<', '&lt;').replace('>', '&gt;')}</div>
            </div>
"""

        html += """
        </div>
        
        <div class="footer">
            <p>Generated by Playerbot-support • Transcript ID: #{ticket['id']}</p>
        </div>
    </div>
</body>
</html>
"""
        return html

    async def _generate_text_transcript(self, ticket: dict, messages: list[dict]) -> str:
        """Generate a plain text transcript."""
        guild = self.bot.get_guild(ticket["guild_id"])
        guild_name = guild.name if guild else "Unknown Server"
        
        creator = self.bot.get_user(ticket["user_id"])
        creator_name = creator.display_name if creator else f"User {ticket['user_id']}"
        
        claimer = None
        if ticket["claimed_by"]:
            claimer = self.bot.get_user(ticket["claimed_by"])
        
        created_at = datetime.fromisoformat(ticket["created_at"])
        closed_at = None
        if ticket["closed_at"]:
            closed_at = datetime.fromisoformat(ticket["closed_at"])

        lines = [
            "=" * 60,
            f"TICKET #{ticket['id']} TRANSCRIPT",
            "=" * 60,
            f"Server: {guild_name}",
            f"Subject: {ticket['subject']}",
            f"Status: {ticket['status'].title()}",
            f"Created By: {creator_name}",
            f"Created At: {created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]
        
        if claimer:
            lines.append(f"Claimed By: {claimer.display_name}")
        
        if closed_at:
            lines.append(f"Closed At: {closed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        lines.extend([
            "=" * 60,
            "CONVERSATION",
            "=" * 60,
            ""
        ])

        for msg in messages:
            user = self.bot.get_user(msg["user_id"])
            author_name = user.display_name if user else f"User {msg['user_id']}"
            msg_time = datetime.fromisoformat(msg["created_at"])
            
            lines.append(f"[{msg_time.strftime('%Y-%m-%d %H:%M:%S')}] {author_name}:")
            lines.append(msg["content"])
            lines.append("")

        lines.extend([
            "=" * 60,
            f"End of transcript for ticket #{ticket['id']}",
            f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "=" * 60,
        ])

        return "\n".join(lines)

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

    @ticket_group.command(name="close", description="Close the current ticket")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_close(self, interaction: discord.Interaction) -> None:
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
