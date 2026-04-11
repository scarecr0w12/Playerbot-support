"""Discord slash commands for managing MCP (Model Context Protocol) servers per guild.

Admins can register, remove, list, enable/disable, and reload MCP servers via
these commands.  The MCPManager handles all live connections.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.db import Database
    from bot.mcp_manager import MCPManager

from bot.mcp_manager import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPCog(commands.Cog, name="MCP"):
    """Manage MCP tool servers for the AI assistant."""

    def __init__(self, bot: commands.Bot, db: "Database", mcp_manager: "MCPManager") -> None:
        self.bot = bot
        self.db = db
        self.mcp = mcp_manager

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------

    mcp_group = app_commands.Group(
        name="mcp",
        description="Manage MCP tool servers for the AI assistant",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ------------------------------------------------------------------
    # /mcp add
    # ------------------------------------------------------------------

    @mcp_group.command(name="add", description="Register an MCP server for this guild")
    @app_commands.describe(
        name="Unique label for this server (e.g. filesystem)",
        transport="Connection type: stdio or sse",
        command="For stdio: the command to run (e.g. npx -y @modelcontextprotocol/server-filesystem /tmp)",
        url="For sse: the HTTP SSE endpoint URL",
        env="Optional JSON object of env vars passed to the process, e.g. {\"KEY\": \"val\"}",
    )
    @app_commands.choices(transport=[
        app_commands.Choice(name="stdio (local subprocess)", value="stdio"),
        app_commands.Choice(name="sse (remote HTTP)", value="sse"),
    ])
    async def mcp_add(
        self,
        interaction: discord.Interaction,
        name: str,
        transport: str,
        command: str = "",
        url: str = "",
        env: str = "{}",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id  # type: ignore[union-attr]

        name = name.strip().lower().replace(" ", "_")
        if not name:
            await interaction.followup.send("❌ Name cannot be empty.", ephemeral=True)
            return

        if transport == "stdio" and not command.strip():
            await interaction.followup.send("❌ A `command` is required for stdio transport.", ephemeral=True)
            return
        if transport == "sse" and not url.strip():
            await interaction.followup.send("❌ A `url` is required for sse transport.", ephemeral=True)
            return

        try:
            json.loads(env)
        except json.JSONDecodeError:
            await interaction.followup.send("❌ `env` must be a valid JSON object, e.g. `{}`.", ephemeral=True)
            return

        added = await self.db.add_mcp_server(
            guild_id,
            name,
            transport,
            command.strip() or None,
            "[]",
            env.strip() or "{}",
            url.strip() or None,
        )
        if not added:
            await interaction.followup.send(
                f"❌ An MCP server named **{name}** already exists. Use `/mcp remove` first.",
                ephemeral=True,
            )
            return

        row = await self.db.get_mcp_server(guild_id, name)
        config = MCPServerConfig.from_db_row(row)
        config.guild_id = guild_id  # type: ignore[misc]

        ok = await self.mcp.connect_server(config)
        tool_count = len(self.mcp.get_tools_for_guild(guild_id))
        if ok:
            await interaction.followup.send(
                f"✅ MCP server **{name}** connected ({tool_count} tool(s) available).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Server **{name}** saved but connection failed — check logs. "
                "Use `/mcp reload` after fixing the configuration.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /mcp remove
    # ------------------------------------------------------------------

    @mcp_group.command(name="remove", description="Remove an MCP server from this guild")
    @app_commands.describe(name="Name of the MCP server to remove")
    async def mcp_remove(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        name = name.strip().lower()

        await self.mcp.disconnect_server(guild_id, name)
        removed = await self.db.remove_mcp_server(guild_id, name)
        if removed:
            await interaction.followup.send(f"✅ MCP server **{name}** removed.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ No MCP server named **{name}** found.", ephemeral=True)

    # ------------------------------------------------------------------
    # /mcp list
    # ------------------------------------------------------------------

    @mcp_group.command(name="list", description="List all registered MCP servers for this guild")
    async def mcp_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id  # type: ignore[union-attr]

        rows = await self.db.get_mcp_servers(guild_id)
        if not rows:
            await interaction.followup.send("No MCP servers registered for this guild.", ephemeral=True)
            return

        connected = set(self.mcp.connected_servers(guild_id))
        lines: list[str] = []
        for row in rows:
            status = "🟢" if row["name"] in connected else ("⚪" if row["enabled"] else "🔴")
            transport_info = row["command"] or row["url"] or "—"
            lines.append(
                f"{status} **{row['name']}** `{row['transport']}` — {transport_info[:60]}"
            )

        em = discord.Embed(
            title=f"🔌 MCP Servers ({len(rows)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        em.set_footer(text="🟢 connected  ⚪ enabled/disconnected  🔴 disabled")
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------
    # /mcp toggle
    # ------------------------------------------------------------------

    @mcp_group.command(name="toggle", description="Enable or disable an MCP server")
    @app_commands.describe(name="Name of the MCP server to toggle")
    async def mcp_toggle(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        name = name.strip().lower()

        result = await self.db.toggle_mcp_server(guild_id, name)
        if result is None:
            await interaction.followup.send(f"❌ No MCP server named **{name}** found.", ephemeral=True)
            return

        if result:
            row = await self.db.get_mcp_server(guild_id, name)
            config = MCPServerConfig.from_db_row(row)
            config.guild_id = guild_id  # type: ignore[misc]
            await self.mcp.connect_server(config)
            await interaction.followup.send(f"✅ MCP server **{name}** **enabled** and (re)connected.", ephemeral=True)
        else:
            await self.mcp.disconnect_server(guild_id, name)
            await interaction.followup.send(f"🔴 MCP server **{name}** **disabled** and disconnected.", ephemeral=True)

    # ------------------------------------------------------------------
    # /mcp reload
    # ------------------------------------------------------------------

    @mcp_group.command(name="reload", description="Reconnect an MCP server (after config changes)")
    @app_commands.describe(name="Name of the MCP server to reload")
    async def mcp_reload(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id  # type: ignore[union-attr]
        name = name.strip().lower()

        row = await self.db.get_mcp_server(guild_id, name)
        if row is None:
            await interaction.followup.send(f"❌ No MCP server named **{name}** found.", ephemeral=True)
            return
        if not row["enabled"]:
            await interaction.followup.send(
                f"⚠️ Server **{name}** is disabled — enable it first with `/mcp toggle`.", ephemeral=True
            )
            return

        config = MCPServerConfig.from_db_row(row)
        ok = await self.mcp.reconnect_server(config)
        if ok:
            tool_count = len([
                t for t in self.mcp.get_tools_for_guild(guild_id)
            ])
            await interaction.followup.send(
                f"✅ MCP server **{name}** reconnected ({tool_count} tool(s)).", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ Failed to reconnect **{name}** — check logs.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /mcp tools
    # ------------------------------------------------------------------

    @mcp_group.command(name="tools", description="List tools exposed by all connected MCP servers")
    async def mcp_tools(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id  # type: ignore[union-attr]

        tools = self.mcp.get_tools_for_guild(guild_id)
        if not tools:
            await interaction.followup.send("No MCP tools available. Connect a server first.", ephemeral=True)
            return

        lines: list[str] = []
        for t in tools:
            fn = t.get("function", {})
            lines.append(f"• **{fn.get('name', '?')}** — {fn.get('description', '')[:80]}")

        em = discord.Embed(
            title=f"🛠️ MCP Tools ({len(tools)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=em, ephemeral=True)
