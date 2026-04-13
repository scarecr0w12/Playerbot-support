"""Miscellaneous routes: /permissions, /reports, /custom-commands, /reminders."""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    auth_redirect,
    ctx,
    db_execute,
    db_fetchall,
    get_authorized_guilds,
    require_guild_access,
)

router = APIRouter()

_DISCORD_API = "https://discord.com/api/v10"
_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# ---------------------------------------------------------------------------
# Static command catalogue — all bot slash commands grouped by category.
# Each entry: {"name": "mod warn", "description": "..."}
# ---------------------------------------------------------------------------
COMMAND_CATALOGUE: list[dict] = [
    # Moderation
    {"group": "Moderation", "name": "mod warn",           "description": "Issue a warning to a member"},
    {"group": "Moderation", "name": "mod mute",           "description": "Temporarily mute a member"},
    {"group": "Moderation", "name": "mod unmute",         "description": "Remove a mute from a member"},
    {"group": "Moderation", "name": "mod kick",           "description": "Kick a member from the server"},
    {"group": "Moderation", "name": "mod ban",            "description": "Ban a member from the server"},
    {"group": "Moderation", "name": "mod unban",          "description": "Unban a user"},
    {"group": "Moderation", "name": "mod cases",          "description": "View moderation cases"},
    {"group": "Moderation", "name": "mod warnings",       "description": "View warnings for a member"},
    {"group": "Moderation", "name": "mod clear_warnings", "description": "Clear warnings for a member"},
    {"group": "Moderation", "name": "modset mute_duration","description": "Set default mute duration"},
    # Cleanup
    {"group": "Cleanup", "name": "cleanup purge",         "description": "Delete recent messages"},
    {"group": "Cleanup", "name": "cleanup purge_user",    "description": "Delete recent messages from a user"},
    {"group": "Cleanup", "name": "cleanup purge_bots",    "description": "Delete recent bot messages"},
    {"group": "Cleanup", "name": "cleanup purge_contains","description": "Delete messages containing text"},
    {"group": "Cleanup", "name": "cleanup purge_embeds",  "description": "Delete messages with embeds"},
    # Tickets
    {"group": "Tickets", "name": "ticket panel",          "description": "Post a ticket open panel"},
    {"group": "Tickets", "name": "ticket close",          "description": "Close a ticket"},
    {"group": "Tickets", "name": "ticket add",            "description": "Add a user to a ticket"},
    {"group": "Tickets", "name": "ticket remove",         "description": "Remove a user from a ticket"},
    # Auto-Mod
    {"group": "Auto-Mod", "name": "filter add_word",      "description": "Add a word to the filter"},
    {"group": "Auto-Mod", "name": "filter remove_word",   "description": "Remove a word from the filter"},
    {"group": "Auto-Mod", "name": "filter list",          "description": "List filter words"},
    {"group": "Auto-Mod", "name": "automodset",           "description": "Configure auto-mod settings"},
    # Economy
    {"group": "Economy", "name": "balance",               "description": "Check your balance"},
    {"group": "Economy", "name": "daily",                 "description": "Claim daily reward"},
    {"group": "Economy", "name": "pay",                   "description": "Transfer coins to another user"},
    {"group": "Economy", "name": "leaderboard",           "description": "Show economy leaderboard"},
    {"group": "Economy", "name": "shop",                  "description": "Browse the shop"},
    # Levels
    {"group": "Levels", "name": "rank",                   "description": "View your XP rank card"},
    {"group": "Levels", "name": "levels leaderboard",     "description": "View XP leaderboard"},
    {"group": "Levels", "name": "levels set_xp",          "description": "Set XP for a member (admin)"},
    # Utility
    {"group": "Utility", "name": "util userinfo",         "description": "Show info about a user"},
    {"group": "Utility", "name": "util serverinfo",       "description": "Show server info"},
    {"group": "Utility", "name": "util avatar",           "description": "Show a user's avatar"},
    {"group": "Utility", "name": "util ping",             "description": "Show bot latency"},
    # Polls
    {"group": "Polls", "name": "poll create",             "description": "Create a poll"},
    {"group": "Polls", "name": "poll end",                "description": "End a poll"},
    # Giveaways
    {"group": "Giveaways", "name": "giveaway start",      "description": "Start a giveaway"},
    {"group": "Giveaways", "name": "giveaway end",        "description": "End a giveaway early"},
    {"group": "Giveaways", "name": "giveaway reroll",     "description": "Reroll a giveaway"},
    # Music
    {"group": "Music", "name": "music play",             "description": "Play a song"},
    {"group": "Music", "name": "music skip",             "description": "Skip the current song"},
    {"group": "Music", "name": "music stop",             "description": "Stop playback"},
    {"group": "Music", "name": "music queue",            "description": "Show the queue"},
    {"group": "Music", "name": "music volume",           "description": "Set volume"},
    # Voice
    {"group": "Voice", "name": "voice move",             "description": "Move a member to another voice channel"},
    {"group": "Voice", "name": "voice kick",             "description": "Kick a member from a voice channel"},
    # Custom Commands
    {"group": "Custom Commands", "name": "cc add",        "description": "Create a custom command"},
    {"group": "Custom Commands", "name": "cc edit",       "description": "Edit a custom command"},
    {"group": "Custom Commands", "name": "cc delete",     "description": "Delete a custom command"},
    {"group": "Custom Commands", "name": "cc list",       "description": "List custom commands"},
    # Permissions
    {"group": "Permissions", "name": "perm allow_role",  "description": "Allow a role to use a command"},
    {"group": "Permissions", "name": "perm deny_role",   "description": "Deny a role from a command"},
    {"group": "Permissions", "name": "perm allow_channel","description": "Allow a command in a channel"},
    {"group": "Permissions", "name": "perm deny_channel", "description": "Deny a command in a channel"},
    {"group": "Permissions", "name": "perm reset",       "description": "Remove a permission override"},
    {"group": "Permissions", "name": "perm show",        "description": "Show overrides for a command"},
    # Admin
    {"group": "Admin", "name": "admin",                  "description": "Admin utilities"},
    # MCP
    {"group": "MCP", "name": "mcp add",                  "description": "Register an MCP server"},
    {"group": "MCP", "name": "mcp remove",               "description": "Remove an MCP server"},
    {"group": "MCP", "name": "mcp list",                 "description": "List MCP servers"},
    # Raid Protection
    {"group": "Raid Protection", "name": "raid enable",  "description": "Enable raid protection"},
    {"group": "Raid Protection", "name": "raid disable", "description": "Disable raid protection"},
    # Invite Tracking
    {"group": "Invite Tracking", "name": "invite stats", "description": "Show invite statistics"},
    # Birthdays
    {"group": "Birthdays", "name": "birthday set",       "description": "Set your birthday"},
    {"group": "Birthdays", "name": "birthday channel",   "description": "Set birthday announcement channel"},
    # Social Alerts
    {"group": "Social Alerts", "name": "social add",     "description": "Add an RSS feed alert"},
    {"group": "Social Alerts", "name": "social remove",  "description": "Remove an RSS feed alert"},
]


def init(templates: Jinja2Templates) -> APIRouter:
    # ── Permissions ───────────────────────────────────────────────────

    @router.get("/permissions", response_class=HTMLResponse)
    async def permissions_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        permission_rows: list = []
        if guild_id:
            permission_rows = await db_fetchall(
                "SELECT * FROM command_permissions WHERE guild_id = ? ORDER BY command, target_type, target_id",
                (guild_id,),
            )

        return templates.TemplateResponse(request, "permissions.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "permission_rows": permission_rows,
            "command_catalogue": COMMAND_CATALOGUE,
            "active_page": "permissions",
        }))

    @router.post("/permissions/save")
    async def permissions_save(
        request: Request,
        guild_id: int = Form(...),
        command: str = Form(...),
        target_type: str = Form(...),
        target_id: int = Form(...),
        allowed: int = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        if target_type not in {"role", "channel", "user"}:
            raise HTTPException(status_code=400, detail="Invalid target type")
        await db_execute(
            "INSERT INTO command_permissions (guild_id, command, target_type, target_id, allowed) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, command, target_type, target_id) DO UPDATE SET allowed = excluded.allowed",
            (guild_id, command.strip().lstrip("/"), target_type, target_id, allowed),
        )
        return RedirectResponse(f"/permissions?guild_id={guild_id}", status_code=302)

    @router.post("/permissions/delete")
    async def permissions_delete(request: Request, guild_id: int = Form(...), permission_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM command_permissions WHERE id = ? AND guild_id = ?", (permission_id, guild_id))
        return RedirectResponse(f"/permissions?guild_id={guild_id}", status_code=302)

    @router.get("/api/guild-roles/{guild_id}")
    async def guild_roles_api(request: Request, guild_id: int):
        if r := auth_redirect(request):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        await require_guild_access(request, guild_id)
        if not _BOT_TOKEN:
            return JSONResponse({"roles": []})
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{_DISCORD_API}/guilds/{guild_id}/roles",
                    headers={"Authorization": f"Bot {_BOT_TOKEN}"},
                )
                resp.raise_for_status()
                roles = [
                    {"id": r["id"], "name": r["name"], "color": r["color"]}
                    for r in resp.json()
                    if not r["managed"] and r["name"] != "@everyone"
                ]
                roles.sort(key=lambda r: r["name"].lower())
                return JSONResponse({"roles": roles})
        except Exception:
            return JSONResponse({"roles": []})

    @router.post("/permissions/toggle")
    async def permissions_toggle(
        request: Request,
        guild_id: int = Form(...),
        permission_id: int = Form(...),
        allowed: int = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "UPDATE command_permissions SET allowed = ? WHERE id = ? AND guild_id = ?",
            (allowed, permission_id, guild_id),
        )
        return JSONResponse({"ok": True, "allowed": allowed})

    # ── Reports ───────────────────────────────────────────────────────

    @router.get("/reports", response_class=HTMLResponse)
    async def reports_page(request: Request, guild_id: int | None = None, status: str = "open"):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        reports = []
        if guild_id:
            if status == "all":
                reports = await db_fetchall(
                    "SELECT * FROM reports WHERE guild_id = ? ORDER BY id DESC",
                    (guild_id,),
                )
            else:
                reports = await db_fetchall(
                    "SELECT * FROM reports WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                    (guild_id, status),
                )

        return templates.TemplateResponse(request, "reports.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "status": status,
            "reports": reports,
            "active_page": "reports",
        }))

    @router.post("/reports/resolve")
    async def reports_resolve(request: Request, report_id: int = Form(...), guild_id: int = Form(...), note: str = Form("")):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "UPDATE reports SET status = 'resolved', resolution_note = ?, resolved_at = datetime('now') WHERE id = ? AND guild_id = ?",
            (note, report_id, guild_id),
        )
        return RedirectResponse(f"/reports?guild_id={guild_id}", status_code=302)

    @router.post("/reports/dismiss")
    async def reports_dismiss(request: Request, report_id: int = Form(...), guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "UPDATE reports SET status = 'dismissed', resolved_at = datetime('now') WHERE id = ? AND guild_id = ?",
            (report_id, guild_id),
        )
        return RedirectResponse(f"/reports?guild_id={guild_id}", status_code=302)

    # ── Custom commands ───────────────────────────────────────────────

    @router.get("/custom-commands", response_class=HTMLResponse)
    async def custom_commands_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        commands = []
        if guild_id:
            commands = await db_fetchall(
                "SELECT * FROM custom_commands WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            )

        return templates.TemplateResponse(request, "custom_commands.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "commands": commands,
            "active_page": "custom_commands",
        }))

    @router.post("/custom-commands/add")
    async def custom_commands_add(request: Request, guild_id: int = Form(...), name: str = Form(...), response: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        try:
            await db_execute(
                "INSERT OR REPLACE INTO custom_commands (guild_id, name, response, creator_id) VALUES (?, ?, ?, 0)",
                (guild_id, name.lower().strip(), response),
            )
        except Exception:
            pass
        return RedirectResponse(f"/custom-commands?guild_id={guild_id}", status_code=302)

    @router.post("/custom-commands/delete")
    async def custom_commands_delete(request: Request, cmd_id: int = Form(...), guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM custom_commands WHERE id = ? AND guild_id = ?", (cmd_id, guild_id))
        return RedirectResponse(f"/custom-commands?guild_id={guild_id}", status_code=302)

    # ── Reminders ─────────────────────────────────────────────────────

    @router.get("/reminders", response_class=HTMLResponse)
    async def reminders_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        reminders = []
        if guild_id:
            reminders = await db_fetchall(
                "SELECT * FROM reminders WHERE guild_id = ? ORDER BY end_time ASC",
                (guild_id,),
            )

        return templates.TemplateResponse(request, "reminders.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "reminders": reminders,
            "active_page": "reminders",
        }))

    @router.post("/reminders/delete")
    async def reminders_delete(request: Request, reminder_id: int = Form(...), guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return RedirectResponse(f"/reminders?guild_id={guild_id}", status_code=302)

    return router
