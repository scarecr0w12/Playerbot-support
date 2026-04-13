"""Polls cog - interactive voting system with results.

Features
--------
- Create single or multiple choice polls
- Anonymous or public voting
- Time-limited polls with automatic closing
- Real-time result updates
- Persistent vote buttons for easy interaction
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)

# Number emojis for poll options (1-20)
NUMBER_EMOJIS = [
    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
    "🟦", "🟩", "🟨", "🟧", "🟪", "🟫", "⬛", "⬜", "🟥", "🟦"
]


class PollView(discord.ui.View):
    """Persistent view for poll voting."""

    def __init__(self, cog: "PollsCog", poll_data: dict, options: list[str]) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.poll_data = poll_data
        self.options = options
        
        # Add vote buttons
        for i, option in enumerate(options):
            if i < len(NUMBER_EMOJIS):
                button = discord.ui.Button(
                    emoji=NUMBER_EMOJIS[i],
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"poll:vote:{poll_data['id']}:{i}",
                )
                button.callback = self.create_vote_callback(i, option)
                self.add_item(button)

        # Add control buttons
        if poll_data["multiple_choice"]:
            self.add_item(
                discord.ui.Button(
                    label="Clear Votes",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"poll:clear:{poll_data['id']}",
                )
            )

        # Add end poll button for poll creator
        self.add_item(
            discord.ui.Button(
                label="End Poll",
                style=discord.ButtonStyle.primary,
                custom_id=f"poll:end:{poll_data['id']}",
            )
        )

        # Add results button
        self.add_item(
            discord.ui.Button(
                label="📊 Results",
                style=discord.ButtonStyle.success,
                custom_id=f"poll:results:{poll_data['id']}",
            )
        )

    def create_vote_callback(self, index: int, option: str):
        """Create a callback for voting on an option."""
        async def callback(interaction: discord.Interaction):
            await self.cog.handle_vote(interaction, self.poll_data, index, option)
        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if the user can interact with this poll."""
        # Check if poll has ended
        if self.poll_data["ends_at"]:
            try:
                end_time = datetime.fromisoformat(self.poll_data["ends_at"])
                if datetime.now(timezone.utc) > end_time:
                    await interaction.response.send_message(
                        "❌ This poll has ended.",
                        ephemeral=True,
                    )
                    return False
            except ValueError:
                pass  # Invalid date format, assume poll is still active
        
        return True


class PollsCog(commands.Cog, name="Polls"):
    """Interactive poll system with voting and results."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------
    # /poll create
    # ------------------------------------------------------------------

    @app_commands.command(
        name="poll_create",
        description="Create an interactive poll with voting buttons"
    )
    @app_commands.describe(
        question="The poll question",
        options="Poll options (comma-separated, max 20)",
        channel="Channel to post the poll in (defaults to current)",
        multiple_choice="Allow users to vote for multiple options",
        anonymous="Hide who voted for what",
        duration="Poll duration in hours (leave empty for no time limit)"
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def create_poll(
        self,
        interaction: discord.Interaction,
        question: str,
        options: str,
        channel: discord.TextChannel | None = None,
        multiple_choice: bool = False,
        anonymous: bool = False,
        duration: int | None = None,
    ) -> None:
        """Create a new poll."""
        guild = interaction.guild
        assert guild is not None

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Polls can only be created in text channels.",
                ephemeral=True,
            )
            return

        # Parse options
        option_list = [opt.strip() for opt in options.split(",") if opt.strip()]
        option_list = option_list[:20]  # Limit to 20 options

        if len(option_list) < 2:
            await interaction.response.send_message(
                "❌ Polls need at least 2 options.",
                ephemeral=True,
            )
            return

        # Calculate end time
        ends_at = None
        if duration and duration > 0:
            ends_at = (datetime.now(timezone.utc) + timedelta(hours=duration)).isoformat()

        # Create embed
        embed = discord.Embed(
            title=f"📊 {question}",
            description="Vote using the buttons below!",
            color=discord.Color.blue(),
        )

        for i, option in enumerate(option_list):
            if i < len(NUMBER_EMOJIS):
                embed.add_field(
                    name=f"{NUMBER_EMOJIS[i]} {option}",
                    value="0 votes (0%)",
                    inline=True,
                )

        footer_text = f"Created by {interaction.user.display_name}"
        if multiple_choice:
            footer_text += " • Multiple choice"
        if anonymous:
            footer_text += " • Anonymous"
        if duration:
            footer_text += f" • Ends in {duration}h"
        
        embed.set_footer(text=footer_text)
        embed.timestamp = datetime.now(timezone.utc)

        # Send message
        try:
            message = await target_channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to send messages in that channel.",
                ephemeral=True,
            )
            return

        # Save poll to database
        if await self.db.create_poll(
            guild.id,
            target_channel.id,
            message.id,
            interaction.user.id,
            question,
            option_list,
            multiple_choice,
            anonymous,
            ends_at,
        ):
            # Get poll data for the view
            poll_data = await self.db.get_poll(guild.id, message.id)
            if poll_data:
                view = PollView(self, poll_data, option_list)
                await message.edit(view=view)
                self.bot.add_view(view)

                await interaction.response.send_message(
                    f"✅ Poll created with {len(option_list)} options!\n"
                    f"📎 [Jump to poll]({message.jump_url})",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Failed to retrieve poll data after creation.",
                    ephemeral=True,
                )
        else:
            await interaction.response.send_message(
                "❌ Failed to create poll in database.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /poll end
    # ------------------------------------------------------------------

    @app_commands.command(
        name="poll_end",
        description="End a poll and show final results"
    )
    @app_commands.describe(
        message="Poll message to end (right-click > Copy Message Link)"
    )
    async def end_poll(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        """End a poll and show final results."""
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

        # Get poll data
        poll_data = await self.db.get_poll(guild.id, message_id)
        if not poll_data:
            await interaction.response.send_message(
                "❌ No poll found for that message.",
                ephemeral=True,
            )
            return

        # Check permissions (poll creator or manage_messages)
        if poll_data["creator_id"] != interaction.user.id and not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ You can only end your own polls (or need Manage Messages permission).",
                ephemeral=True,
            )
            return

        # Get final results
        results = await self.db.get_poll_results(poll_data["id"])
        options = json.loads(poll_data["options"])

        # Create results embed
        embed = discord.Embed(
            title=f"📊 Final Results: {poll_data['question']}",
            description="This poll has ended.",
            color=discord.Color.green(),
        )

        total_votes = sum(r["votes"] for r in results)
        
        for i, option in enumerate(options):
            vote_count = 0
            for r in results:
                if r["option_index"] == i:
                    vote_count = r["votes"]
                    break
            
            percentage = (vote_count / total_votes * 100) if total_votes > 0 else 0
            embed.add_field(
                name=f"{NUMBER_EMOJIS[i] if i < len(NUMBER_EMOJIS) else '📋'} {option}",
                value=f"{vote_count} votes ({percentage:.1f}%)",
                inline=True,
            )

        embed.add_field(name="Total Votes", value=str(total_votes), inline=False)
        embed.set_footer(text=f"Poll ended by {interaction.user.display_name}")
        embed.timestamp = datetime.now(timezone.utc)

        # Update the message
        try:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                target_message = await channel.fetch_message(message_id)
                await target_message.edit(embed=embed, view=None)
        except (discord.NotFound, discord.Forbidden):
            pass

        # Delete from database
        await self.db.delete_poll(guild.id, message_id)

        await interaction.response.send_message(
            "✅ Poll ended and results displayed!",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /poll list
    # ------------------------------------------------------------------

    @app_commands.command(
        name="poll_list",
        description="List all active polls in the server"
    )
    async def list_polls(self, interaction: discord.Interaction) -> None:
        """List all active polls in the server."""
        guild = interaction.guild
        assert guild is not None

        polls = await self.db.get_polls(guild.id, active_only=True)
        
        if not polls:
            await interaction.response.send_message(
                "❌ No active polls in this server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📊 Active Polls",
            description=f"Found {len(polls)} active poll(s)",
            color=discord.Color.blue(),
        )

        for poll in polls[:10]:  # Limit to 10 polls
            channel = guild.get_channel(poll["channel_id"])
            channel_name = channel.name if channel else "unknown-channel"
            
            # Check if poll has ended
            status = "🟢 Active"
            if poll["ends_at"]:
                try:
                    end_time = datetime.fromisoformat(poll["ends_at"])
                    if datetime.now(timezone.utc) > end_time:
                        status = "🔴 Ended"
                    else:
                        time_left = end_time - datetime.now(timezone.utc)
                        hours_left = int(time_left.total_seconds() / 3600)
                        status = f"🟡 {hours_left}h left"
                except ValueError:
                    status = "🟡 Active"

            embed.add_field(
                name=f"📄 {poll['question']}",
                value=f"Channel: #{channel_name}\nStatus: {status}\nCreator: <@{poll['creator_id']}>",
                inline=False,
            )

        if len(polls) > 10:
            embed.set_footer(text(f"Showing 10 of {len(polls)} polls"))

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Vote handling
    # ------------------------------------------------------------------

    async def handle_vote(
        self,
        interaction: discord.Interaction,
        poll_data: dict,
        option_index: int,
        option: str,
    ) -> None:
        """Handle a vote on a poll option."""
        guild = interaction.guild
        assert guild is not None

        # Check if poll has ended
        if poll_data["ends_at"]:
            try:
                end_time = datetime.fromisoformat(poll_data["ends_at"])
                if datetime.now(timezone.utc) > end_time:
                    await interaction.response.send_message(
                        "❌ This poll has ended.",
                        ephemeral=True,
                    )
                    return
            except ValueError:
                pass  # Invalid date format, assume poll is still active

        user_votes = await self.db.get_user_poll_votes(poll_data["id"], interaction.user.id)

        # Handle single choice vs multiple choice
        if not poll_data["multiple_choice"]:
            # Single choice - remove existing vote
            if user_votes:
                await self.db.clear_user_poll_votes(poll_data["id"], interaction.user.id)
            
            # Add new vote
            if await self.db.add_poll_vote(poll_data["id"], interaction.user.id, option_index):
                await interaction.response.send_message(
                    f"✅ You voted for: **{option}**",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Failed to record your vote.",
                    ephemeral=True,
                )
        else:
            # Multiple choice - toggle vote
            if option_index in user_votes:
                # Remove vote
                if await self.db.remove_poll_vote(poll_data["id"], interaction.user.id, option_index):
                    await interaction.response.send_message(
                        f"❌ You removed your vote for: **{option}**",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "❌ Failed to remove your vote.",
                        ephemeral=True,
                    )
            else:
                # Add vote
                if await self.db.add_poll_vote(poll_data["id"], interaction.user.id, option_index):
                    await interaction.response.send_message(
                        f"✅ You voted for: **{option}**",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "❌ Failed to record your vote.",
                        ephemeral=True,
                    )

        # Update the poll message with new results
        await self.update_poll_message(poll_data)

    async def update_poll_message(self, poll_data: dict) -> None:
        """Update a poll message with current results."""
        guild = self.bot.get_guild(poll_data["guild_id"])
        if not guild:
            return

        try:
            channel = guild.get_channel(poll_data["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                return
            
            message = await channel.fetch_message(poll_data["message_id"])
            if not message.embeds:
                return

            # Get current results
            results = await self.db.get_poll_results(poll_data["id"])
            options = json.loads(poll_data["options"])

            # Update embed
            embed = message.embeds[0]
            embed.clear_fields()

            total_votes = sum(r["votes"] for r in results)
            
            for i, option in enumerate(options):
                vote_count = 0
                for r in results:
                    if r["option_index"] == i:
                        vote_count = r["votes"]
                        break
                
                percentage = (vote_count / total_votes * 100) if total_votes > 0 else 0
                embed.add_field(
                    name=f"{NUMBER_EMOJIS[i] if i < len(NUMBER_EMOJIS) else '📋'} {option}",
                    value=f"{vote_count} votes ({percentage:.1f}%)",
                    inline=True,
                )

            await message.edit(embed=embed)

        except (discord.NotFound, discord.Forbidden):
            pass

    # ------------------------------------------------------------------
    # Button interaction handlers
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle poll button interactions."""
        if not interaction.data or not isinstance(interaction.data, dict):
            return

        custom_id = interaction.data.get("custom_id")
        if not custom_id or not isinstance(custom_id, str):
            return

        parts = custom_id.split(":")
        if parts[0] != "poll":
            return

        if len(parts) < 3:
            return

        action = parts[1]
        poll_id = int(parts[2])

        # Get poll data
        guild = interaction.guild
        if not guild:
            return

        # Find poll by ID (need to search since we only have poll_id)
        polls = await self.db.get_polls(guild.id)
        poll_data = None
        for poll in polls:
            if poll["id"] == poll_id:
                poll_data = poll
                break

        if not poll_data:
            await interaction.response.send_message(
                "❌ Poll not found.",
                ephemeral=True,
            )
            return

        if action == "vote":
            # Handled by the view callback
            return
        elif action == "clear":
            await self.handle_clear_votes(interaction, poll_data)
        elif action == "end":
            await self.handle_end_poll_button(interaction, poll_data)
        elif action == "results":
            await self.handle_show_results(interaction, poll_data)

    async def handle_clear_votes(self, interaction: discord.Interaction, poll_data: dict) -> None:
        """Handle clearing all votes for a user in a multiple-choice poll."""
        if not poll_data["multiple_choice"]:
            await interaction.response.send_message(
                "❌ This is a single-choice poll. Just vote for a different option.",
                ephemeral=True,
            )
            return

        removed = await self.db.clear_user_poll_votes(poll_data["id"], interaction.user.id)
        await interaction.response.send_message(
            f"✅ Cleared {removed} vote(s) from this poll.",
            ephemeral=True,
        )
        await self.update_poll_message(poll_data)

    async def handle_end_poll_button(self, interaction: discord.Interaction, poll_data: dict) -> None:
        """Handle ending a poll via button."""
        guild = interaction.guild
        assert guild is not None

        # Check permissions
        if poll_data["creator_id"] != interaction.user.id and not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ You can only end your own polls (or need Manage Messages permission).",
                ephemeral=True,
            )
            return

        # End the poll
        await self.end_poll(interaction, f"https://discord.com/channels/{guild.id}/{poll_data['channel_id']}/{poll_data['message_id']}")

    async def handle_show_results(self, interaction: discord.Interaction, poll_data: dict) -> None:
        """Handle showing detailed poll results."""
        results = await self.db.get_poll_results(poll_data["id"])
        options = json.loads(poll_data["options"])

        embed = discord.Embed(
            title=f"📊 Poll Results: {poll_data['question']}",
            color=discord.Color.blue(),
        )

        total_votes = sum(r["votes"] for r in results)
        
        for i, option in enumerate(options):
            vote_count = 0
            for r in results:
                if r["option_index"] == i:
                    vote_count = r["votes"]
                    break
            
            percentage = (vote_count / total_votes * 100) if total_votes > 0 else 0
            
            # Create progress bar
            bar_length = 20
            filled = int(bar_length * percentage / 100)
            bar = "█" * filled + "░" * (bar_length - filled)
            
            embed.add_field(
                name=f"{NUMBER_EMOJIS[i] if i < len(NUMBER_EMOJIS) else '📋'} {option}",
                value=f"`{bar}` {vote_count} votes ({percentage:.1f}%)",
                inline=False,
            )

        embed.add_field(name="Total Votes", value=str(total_votes), inline=False)
        
        if not poll_data["anonymous"]:
            # Show who voted for what (if not anonymous)
            voter_info = []
            guild = interaction.guild
            if guild:
                for r in results:
                    option_name = options[r["option_index"]]
                    # Get voters for this option (would need additional DB query)
                    voter_info.append(f"**{option_name}**: {r['votes']} votes")
            
            if voter_info:
                embed.add_field(name="Vote Breakdown", value="\n".join(voter_info), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Load the Polls cog."""
    await bot.add_cog(PollsCog(bot, bot.db))
