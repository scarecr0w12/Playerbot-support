#!/usr/bin/env python3
"""Entry point for the Discord support bot.

Initialises all services and loads every cog before starting the bot.
"""

from __future__ import annotations

import asyncio
import logging
import os

import discord
import uvicorn
from discord.ext import commands

from bot.config import Config
from bot.db import Database
from bot.llm_service import LLMService
from bot.mcp_manager import MCPManager, MCPServerConfig
from bot.qdrant_service import QdrantService

# Cogs
from bot.cogs.mcp import MCPCog
from bot.cogs.mod_logging import ModLoggingCog
from bot.cogs.moderation import ModerationCog
from bot.cogs.tickets import TicketsCog
from bot.cogs.automod import AutoModCog
from bot.cogs.welcome import WelcomeCog
from bot.cogs.support import SupportCog
from bot.cogs.admin import AdminCog
from bot.cogs.cleanup import CleanupCog
from bot.cogs.custom_commands import CustomCommandsCog
from bot.cogs.economy import EconomyCog
from bot.cogs.reports import ReportsCog
from bot.cogs.utility import UtilityCog
from bot.cogs.voice_music import VoiceMusicCog
from bot.cogs.permissions import PermissionsCog
from bot.cogs.levels import LevelsCog
from bot.cogs.giveaways import GiveawayCog
from bot.cogs.reminders import RemindersCog
from bot.cogs.starboard import StarboardCog
from bot.cogs.highlights import HighlightsCog
from bot.cogs.github import GitHubCog
from bot.cogs.gitlab import GitLabCog
from bot.cogs.reaction_roles import ReactionRolesCog
from bot.cogs.polls import PollsCog
from bot.cogs.raid_protection import RaidProtectionCog
from bot.cogs.invite_tracking import InviteTrackingCog
from bot.cogs.birthdays import BirthdayCog
from bot.cogs.social_alerts import SocialAlertsCog
from bot.dashboard_bridge import set_discord_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("support-bot")


async def main() -> None:
    config = Config()

    # --- Shared services ---
    db = Database()
    await db.setup()

    llm = LLMService(config)
    qdrant = QdrantService()
    mcp_manager = MCPManager()

    # --- Discord bot ---
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True   # Required for welcome / autorole / mod-log member events
    intents.invites = True   # Required for invite-tracking in mod-log join events

    bot = commands.Bot(command_prefix="!", intents=intents)

    async def _seed_guild(guild: discord.Guild) -> None:
        """Persist basic guild metadata so the dashboard can discover and label this guild."""
        await db.conn.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id, key, value) VALUES (?, 'registered', '1')",
            (guild.id,),
        )
        await db.conn.execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, 'guild_name', ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild.id, guild.name),
        )
        await db.conn.commit()

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
        synced = await bot.tree.sync()
        logger.info("Synced %d slash command(s)", len(synced))
        logger.info("Bot is ready — %d cog(s) loaded", len(bot.cogs))
        for guild in bot.guilds:
            await _seed_guild(guild)
            rows = await db.get_mcp_servers(guild.id, enabled_only=True)
            for row in rows:
                config_row = MCPServerConfig.from_db_row(row)
                await mcp_manager.connect_server(config_row)
        logger.info("Seeded %d guild(s) into guild_config", len(bot.guilds))
        if bot.user:
            permissions = discord.Permissions(
                send_messages=True,
                read_messages=True,
                manage_messages=True,
                manage_channels=True,
                manage_roles=True,
                kick_members=True,
                ban_members=True,
                moderate_members=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
                add_reactions=True,
                use_application_commands=True,
                connect=True,
                speak=True,
            )
            invite_url = discord.utils.oauth_url(bot.user.id, permissions=permissions)
            logger.info("Invite URL: %s", invite_url)

    @bot.event
    async def on_guild_join(guild: discord.Guild) -> None:
        await _seed_guild(guild)
        logger.info("Joined guild %s (%d) — seeded guild_config", guild.name, guild.id)

    # --- Register cogs (order matters: mod_logging & permissions first) ---
    await bot.add_cog(ModLoggingCog(bot, db))
    await bot.add_cog(PermissionsCog(bot, db))
    await bot.add_cog(ModerationCog(bot, db))
    await bot.add_cog(TicketsCog(bot, db))
    await bot.add_cog(AutoModCog(bot, db))
    await bot.add_cog(WelcomeCog(bot, db))
    await bot.add_cog(AdminCog(bot, db))
    await bot.add_cog(CleanupCog(bot, db))
    await bot.add_cog(CustomCommandsCog(bot, db))
    await bot.add_cog(EconomyCog(bot, db))
    await bot.add_cog(ReportsCog(bot, db))
    await bot.add_cog(UtilityCog(bot))
    await bot.add_cog(VoiceMusicCog(bot, db))
    await bot.add_cog(SupportCog(bot, db, llm, qdrant, mcp_manager))
    await bot.add_cog(MCPCog(bot, db, mcp_manager))
    await bot.add_cog(LevelsCog(bot, db))
    await bot.add_cog(GiveawayCog(bot, db))
    await bot.add_cog(RemindersCog(bot, db))
    await bot.add_cog(StarboardCog(bot, db))
    await bot.add_cog(HighlightsCog(bot, db))
    await bot.add_cog(GitHubCog(bot, db, config))
    await bot.add_cog(GitLabCog(bot, db, config))
    await bot.add_cog(ReactionRolesCog(bot, db))
    await bot.add_cog(PollsCog(bot, db))
    await bot.add_cog(RaidProtectionCog(bot, db))
    await bot.add_cog(InviteTrackingCog(bot, db))
    await bot.add_cog(BirthdayCog(bot, db))
    await bot.add_cog(SocialAlertsCog(bot, db))

    # --- Bot-level interaction check for custom permission overrides ---
    @bot.tree.interaction_check
    async def global_interaction_check(interaction: discord.Interaction) -> bool:
        perm_cog: PermissionsCog | None = bot.get_cog("Permissions")  # type: ignore[assignment]
        if perm_cog is None:
            return True
        result = await perm_cog.check_interaction(interaction)
        if result is False:
            await interaction.response.send_message(
                "❌ You don't have permission to use this command here.", ephemeral=True
            )
            return False
        return True

    set_discord_bot(bot)

    # --- Dashboard (run in thread to avoid blocking bot loop) ---
    import threading
    dashboard_host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    dashboard_port = int(os.getenv("DASHBOARD_PORT", "8080"))
    
    def run_dashboard():
        uvicorn.run(
            "dashboard.app:app",
            host=dashboard_host,
            port=dashboard_port,
            log_level="warning",
        )
    
    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()

    try:
        await bot.start(config.discord_token)
    finally:
        await mcp_manager.shutdown()
        await db.close()
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
