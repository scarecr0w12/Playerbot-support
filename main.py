#!/usr/bin/env python3
"""Entry point for the Discord support bot.

Initialises all services and loads every cog before starting the bot.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot.config import Config
from bot.database import Database
from bot.llm_service import LLMService

# Cogs
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
from bot.cogs.permissions import PermissionsCog

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

    # --- Discord bot ---
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True   # Required for welcome / autorole / mod-log member events
    intents.invites = True   # Required for invite-tracking in mod-log join events

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
        synced = await bot.tree.sync()
        logger.info("Synced %d slash command(s)", len(synced))
        logger.info("Bot is ready — %d cog(s) loaded", len(bot.cogs))

    # --- Register cogs (order matters: mod_logging & permissions first) ---
    await bot.add_cog(ModLoggingCog(bot, db))
    await bot.add_cog(PermissionsCog(bot, db))
    await bot.add_cog(ModerationCog(bot, db, config))
    await bot.add_cog(TicketsCog(bot, db))
    await bot.add_cog(AutoModCog(bot, db, config))
    await bot.add_cog(WelcomeCog(bot, db))
    await bot.add_cog(AdminCog(bot, db))
    await bot.add_cog(CleanupCog(bot, db))
    await bot.add_cog(CustomCommandsCog(bot, db))
    await bot.add_cog(EconomyCog(bot, db))
    await bot.add_cog(ReportsCog(bot, db))
    await bot.add_cog(UtilityCog(bot))
    await bot.add_cog(SupportCog(bot, db, llm, config))

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

    try:
        await bot.start(config.discord_token)
    finally:
        await db.close()
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
