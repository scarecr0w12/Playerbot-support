"""MCP (Model Context Protocol) tool manager.

Manages per-guild MCP server connections and exposes their tools to the LLM
in OpenAI function-calling format.  Supports two transports:

* **stdio** — spawn a local subprocess (e.g. ``npx -y @modelcontextprotocol/server-filesystem /tmp``)
* **sse**   — connect to a remote HTTP SSE endpoint (e.g. ``http://localhost:3000/sse``)

Each guild independently chooses which MCP servers are active.  A shared
singleton ``MCPManager`` is attached to the bot and passed to
``LLMService.get_response`` per-request so the LLM can call MCP tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from contextlib import AsyncExitStack
from typing import Any

logger = logging.getLogger(__name__)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    logger.warning("mcp package not installed — MCP tool support disabled")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class MCPServerConfig:
    """Parsed configuration for a single MCP server."""

    __slots__ = ("name", "transport", "command", "args", "env", "url", "enabled", "guild_id")

    def __init__(
        self,
        *,
        name: str,
        transport: str,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
        enabled: bool = True,
        guild_id: int = 0,
    ) -> None:
        self.name = name
        self.transport = transport.lower()   # "stdio" | "sse"
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.url = url
        self.enabled = enabled
        self.guild_id = guild_id

    @classmethod
    def from_db_row(cls, row: Any) -> "MCPServerConfig":
        """Build from a database row (aiosqlite.Row or dict-like)."""
        transport = row["transport"]
        args: list[str] = []
        env: dict[str, str] = {}
        try:
            args = json.loads(row["args"] or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            env = json.loads(row["env"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        return cls(
            name=row["name"],
            transport=transport,
            command=row["command"] or None,
            args=args,
            env=env,
            url=row["url"] or None,
            enabled=bool(row["enabled"]),
            guild_id=int(row["guild_id"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "url": self.url,
            "enabled": self.enabled,
            "guild_id": self.guild_id,
        }


# ---------------------------------------------------------------------------
# Per-connection session wrapper
# ---------------------------------------------------------------------------

class _MCPConnection:
    """Holds a live ClientSession + its exit stack for one MCP server."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._session: Any = None          # mcp.ClientSession
        self._stack: AsyncExitStack | None = None
        self._tools: list[dict[str, Any]] = []   # cached OpenAI-format tool schemas
        self._tool_names: set[str] = set()

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    @property
    def tool_names(self) -> set[str]:
        return self._tool_names

    async def connect(self) -> None:
        if not _MCP_AVAILABLE:
            raise RuntimeError("mcp package not installed")

        self._stack = AsyncExitStack()
        try:
            if self.config.transport == "stdio":
                if not self.config.command:
                    raise ValueError(f"MCP server '{self.config.name}' has no command")
                cmd_parts = shlex.split(self.config.command) + list(self.config.args)
                params = StdioServerParameters(
                    command=cmd_parts[0],
                    args=cmd_parts[1:],
                    env=self.config.env or None,
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
            elif self.config.transport == "sse":
                if not self.config.url:
                    raise ValueError(f"MCP server '{self.config.name}' has no URL")
                read, write = await self._stack.enter_async_context(sse_client(self.config.url))
            else:
                raise ValueError(f"Unknown MCP transport: {self.config.transport!r}")

            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            await self._refresh_tools()
            logger.info(
                "MCP server '%s' (guild=%d) connected — %d tool(s) available",
                self.config.name,
                self.config.guild_id,
                len(self._tools),
            )
        except Exception:
            await self._stack.aclose()
            self._stack = None
            raise

    async def disconnect(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                logger.debug("Error closing MCP connection for '%s'", self.config.name, exc_info=True)
            self._stack = None
        self._session = None
        self._tools = []
        self._tool_names = set()

    async def _refresh_tools(self) -> None:
        """Fetch the tool list from the server and convert to OpenAI format."""
        if not self._session:
            return
        response = await self._session.list_tools()
        self._tools = []
        self._tool_names = set()
        for tool in response.tools:
            schema = tool.inputSchema if hasattr(tool, "inputSchema") else {}
            if not isinstance(schema, dict):
                schema = {}
            openai_tool = {
                "type": "function",
                "function": {
                    "name": f"mcp__{self.config.name}__{tool.name}",
                    "description": (tool.description or "").strip(),
                    "parameters": schema or {"type": "object", "properties": {}},
                },
            }
            self._tools.append(openai_tool)
            self._tool_names.add(openai_tool["function"]["name"])

    @property
    def is_alive(self) -> bool:
        """Return True if the session exists and the underlying stack is open."""
        return self._session is not None and self._stack is not None

    async def call_tool(self, openai_tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool by its OpenAI-namespaced name and return a string result."""
        if not self.is_alive:
            return f"[MCP server '{self.config.name}' is not connected]"

        # Strip the mcp__<server>__ prefix to get the real tool name
        prefix = f"mcp__{self.config.name}__"
        real_name = openai_tool_name[len(prefix):] if openai_tool_name.startswith(prefix) else openai_tool_name

        try:
            result = await self._session.call_tool(real_name, arguments)
            # result.content is a list of content blocks
            parts: list[str] = []
            for block in (result.content or []):
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif isinstance(block, dict):
                    parts.append(block.get("text") or json.dumps(block))
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else "(no output)"
        except Exception as exc:
            logger.warning("MCP tool call '%s' failed: %s", real_name, exc, exc_info=True)
            # Mark session as dead so get_tools_for_guild stops serving stale tools
            self._session = None
            self._tools = []
            self._tool_names = set()
            return f"[Tool error: {exc}]"


# ---------------------------------------------------------------------------
# MCPManager — public interface
# ---------------------------------------------------------------------------

class MCPManager:
    """Manages MCP server connections across all guilds.

    Usage::

        manager = MCPManager()
        await manager.connect_server(config)
        tools = manager.get_tools_for_guild(guild_id)
        result = await manager.call_tool(guild_id, tool_name, args)
        await manager.shutdown()
    """

    def __init__(self) -> None:
        # (guild_id, server_name) -> _MCPConnection
        self._connections: dict[tuple[int, str], _MCPConnection] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect_server(self, config: MCPServerConfig) -> bool:
        """Connect a single MCP server.  Returns True on success."""
        if not _MCP_AVAILABLE:
            logger.warning("Cannot connect MCP server '%s' — mcp package missing", config.name)
            return False

        key = (config.guild_id, config.name)
        async with self._lock:
            if key in self._connections:
                existing = self._connections[key]
                if existing._session is not None:
                    return True  # already connected
                del self._connections[key]

            conn = _MCPConnection(config)
            try:
                await conn.connect()
                self._connections[key] = conn
                return True
            except Exception:
                logger.exception(
                    "Failed to connect MCP server '%s' for guild %d",
                    config.name,
                    config.guild_id,
                )
                return False

    async def disconnect_server(self, guild_id: int, server_name: str) -> None:
        key = (guild_id, server_name)
        async with self._lock:
            conn = self._connections.pop(key, None)
        if conn:
            await conn.disconnect()

    async def reconnect_server(self, config: MCPServerConfig) -> bool:
        await self.disconnect_server(config.guild_id, config.name)
        return await self.connect_server(config)

    async def shutdown(self) -> None:
        """Disconnect all servers."""
        async with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()
        for conn in conns:
            await conn.disconnect()

    # ------------------------------------------------------------------
    # Tool access
    # ------------------------------------------------------------------

    def get_tools_for_guild(self, guild_id: int) -> list[dict[str, Any]]:
        """Return all OpenAI-format tool schemas for a guild's connected MCP servers."""
        tools: list[dict[str, Any]] = []
        for (gid, _), conn in self._connections.items():
            if gid == guild_id and conn.is_alive:
                tools.extend(conn.tools)
        return tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Return True if this tool name belongs to an MCP server."""
        return tool_name.startswith("mcp__")

    def _find_connection_for_tool(
        self, guild_id: int, tool_name: str
    ) -> "_MCPConnection | None":
        for (gid, _), conn in self._connections.items():
            if gid == guild_id and tool_name in conn.tool_names:
                return conn
        return None

    def _find_connection_by_server_name(
        self, guild_id: int, server_name: str
    ) -> "_MCPConnection | None":
        return self._connections.get((guild_id, server_name))

    async def call_tool(
        self, guild_id: int, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """Route a tool call to the correct MCP server, auto-reconnecting on dead sessions."""
        conn = self._find_connection_for_tool(guild_id, tool_name)

        # If not found by tool name, the session may be dead (tool_names cleared).
        # Recover by parsing the server name from the mcp__<server>__<tool> prefix.
        if conn is None and tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) >= 2:
                conn = self._find_connection_by_server_name(guild_id, parts[1])

        if conn is None:
            return f"[No MCP server found for tool '{tool_name}']"

        if not conn.is_alive:
            logger.info(
                "MCP connection for '%s' (guild=%d) is dead — attempting reconnect",
                conn.config.name, guild_id,
            )
            reconnected = await self.reconnect_server(conn.config)
            if not reconnected:
                return f"[MCP server '{conn.config.name}' is disconnected and could not reconnect]"
            conn = self._find_connection_for_tool(guild_id, tool_name)
            if conn is None:
                return f"[No MCP server found for tool '{tool_name}' after reconnect]"

        return await conn.call_tool(tool_name, arguments)

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def connected_servers(self, guild_id: int) -> list[str]:
        return [
            name
            for (gid, name), conn in self._connections.items()
            if gid == guild_id and conn.is_alive
        ]

    def all_connected(self) -> list[tuple[int, str]]:
        return [
            (gid, name)
            for (gid, name), conn in self._connections.items()
            if conn.is_alive
        ]
