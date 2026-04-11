"""Community routes: /community and all /community/* sub-routes."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    auth_redirect,
    ctx,
    db_execute,
    db_fetchall,
    get_authorized_guilds,
    get_guild_config_map,
    require_guild_access,
)

router = APIRouter()


def init(templates: Jinja2Templates) -> APIRouter:
    @router.get("/community", response_class=HTMLResponse)
    async def community_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        config_values: dict = {}
        selfroles: list = []
        highlights: list = []
        starboard_entries: list = []

        if guild_id:
            config_values = await get_guild_config_map(guild_id)
            selfroles = await db_fetchall(
                "SELECT role_id FROM selfroles WHERE guild_id = ? ORDER BY role_id",
                (guild_id,),
            )
            highlights = await db_fetchall(
                "SELECT h.user_id, h.keyword, COALESCE((SELECT gc.value FROM guild_config gc WHERE gc.guild_id = h.guild_id AND gc.key = 'highlight_pause_' || h.user_id), '0') AS paused "
                "FROM highlights h WHERE h.guild_id = ? ORDER BY h.user_id, h.keyword",
                (guild_id,),
            )
            starboard_entries = await db_fetchall(
                "SELECT * FROM starboard_messages WHERE guild_id = ? ORDER BY star_count DESC, created_at DESC LIMIT 50",
                (guild_id,),
            )

        return templates.TemplateResponse(request, "community.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "config_values": config_values,
            "selfroles": selfroles,
            "highlights": highlights,
            "starboard_entries": starboard_entries,
            "active_page": "community",
        }))

    @router.post("/community/selfroles/add")
    async def community_selfrole_add(request: Request, guild_id: int = Form(...), role_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("INSERT OR IGNORE INTO selfroles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
        return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)

    @router.post("/community/selfroles/delete")
    async def community_selfrole_delete(request: Request, guild_id: int = Form(...), role_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM selfroles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
        return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)

    @router.post("/community/highlights/add")
    async def community_highlight_add(request: Request, guild_id: int = Form(...), user_id: int = Form(...), keyword: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        keyword = keyword.strip().lower()
        if keyword:
            await db_execute("INSERT OR IGNORE INTO highlights (user_id, guild_id, keyword) VALUES (?, ?, ?)", (user_id, guild_id, keyword))
        return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)

    @router.post("/community/highlights/delete")
    async def community_highlight_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...), keyword: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM highlights WHERE guild_id = ? AND user_id = ? AND keyword = ?", (guild_id, user_id, keyword))
        return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)

    @router.post("/community/highlights/toggle-pause")
    async def community_highlight_toggle_pause(request: Request, guild_id: int = Form(...), user_id: int = Form(...), paused: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        key = f"highlight_pause_{user_id}"
        await db_execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, "1" if paused else "0"),
        )
        return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)

    @router.post("/community/starboard/save")
    async def community_starboard_save(
        request: Request,
        guild_id: int = Form(...),
        starboard_enabled: str = Form("1"),
        starboard_channel: str = Form(""),
        starboard_threshold: str = Form("3"),
        starboard_emoji: str = Form("⭐"),
        starboard_ignore_channels: str = Form(""),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        settings = {
            "starboard_enabled": starboard_enabled,
            "starboard_channel": starboard_channel.strip(),
            "starboard_threshold": starboard_threshold.strip() or "3",
            "starboard_emoji": starboard_emoji.strip() or "⭐",
            "starboard_ignore_channels": starboard_ignore_channels.strip(),
        }
        for key, value in settings.items():
            if value:
                await db_execute(
                    "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                    (guild_id, key, value),
                )
            else:
                await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
        return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)

    return router
