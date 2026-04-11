"""Overview/index routes: /, /api/stats, /api/guilds."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    _safe_int,
    auth_redirect,
    build_guild_scope_clause,
    count_scoped_rows,
    ctx,
    db_fetchall,
    get_accessible_guilds,
    get_authorized_guilds,
    require_auth,
)

router = APIRouter()


def init(templates: Jinja2Templates) -> APIRouter:
    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request)
        guild_ids = [_safe_int(guild["guild_id"]) for guild in guilds]
        guild_ids = [gid for gid in guild_ids if gid is not None]

        total_cases = await count_scoped_rows("mod_cases", guild_ids)
        open_tickets = await count_scoped_rows("tickets", guild_ids, "status != 'closed'")
        active_warnings = await count_scoped_rows("warnings", guild_ids, "active = 1")
        open_reports = await count_scoped_rows("reports", guild_ids, "status = 'open'")
        active_giveaways = await count_scoped_rows("giveaways", guild_ids, "status = 'active'")
        economy_accounts = await count_scoped_rows("economy_accounts", guild_ids)
        level_entries = await count_scoped_rows("levels", guild_ids)
        custom_commands = await count_scoped_rows("custom_commands", guild_ids)

        if guild_ids:
            scope_clause, scope_params = build_guild_scope_clause(guild_ids)
            recent_cases = await db_fetchall(
                f"SELECT * FROM mod_cases WHERE {scope_clause} ORDER BY id DESC LIMIT 10",
                scope_params,
            )
        else:
            recent_cases = []

        stats = {
            "total_cases": total_cases,
            "open_tickets": open_tickets,
            "active_warnings": active_warnings,
            "guild_count": len(guilds),
            "open_reports": open_reports,
            "active_giveaways": active_giveaways,
            "economy_accounts": economy_accounts,
            "level_entries": level_entries,
            "custom_commands": custom_commands,
        }

        return templates.TemplateResponse(request, "index.html", ctx({
            "stats": stats,
            "recent_cases": recent_cases,
            "guilds": guilds,
            "active_page": "overview",
        }))

    @router.get("/api/stats")
    async def api_stats(request: Request):
        require_auth(request)
        guilds = await get_accessible_guilds(request)
        guild_ids = [_safe_int(guild["guild_id"]) for guild in guilds]
        guild_ids = [gid for gid in guild_ids if gid is not None]
        return {
            "total_cases": await count_scoped_rows("mod_cases", guild_ids),
            "open_tickets": await count_scoped_rows("tickets", guild_ids, "status != 'closed'"),
            "active_warnings": await count_scoped_rows("warnings", guild_ids, "active = 1"),
            "open_reports": await count_scoped_rows("reports", guild_ids, "status = 'open'"),
            "active_giveaways": await count_scoped_rows("giveaways", guild_ids, "status = 'active'"),
        }

    @router.get("/api/guilds")
    async def api_guilds(request: Request):
        require_auth(request)
        guilds = await get_accessible_guilds(request)
        return [g["guild_id"] for g in guilds]

    return router
