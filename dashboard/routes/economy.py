"""Economy/levels/giveaways routes."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    auth_redirect,
    ctx,
    db_execute,
    db_fetchall,
    db_fetchone,
    get_authorized_guilds,
    require_guild_access,
)

router = APIRouter()


def init(templates: Jinja2Templates) -> APIRouter:
    # ── Economy ───────────────────────────────────────────────────────

    @router.get("/economy", response_class=HTMLResponse)
    async def economy_page(request: Request, guild_id: int | None = None, page: int = 1):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        per_page = 25
        offset = (page - 1) * per_page
        accounts = []
        total = 0

        if guild_id:
            total_row = await db_fetchone("SELECT COUNT(*) as c FROM economy_accounts WHERE guild_id = ?", (guild_id,))
            total = total_row["c"] if total_row else 0
            accounts = await db_fetchall(
                "SELECT * FROM economy_accounts WHERE guild_id = ? ORDER BY balance DESC LIMIT ? OFFSET ?",
                (guild_id, per_page, offset),
            )

        total_pages = max(1, (total + per_page - 1) // per_page)

        return templates.TemplateResponse(request, "economy.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "accounts": accounts,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "active_page": "economy",
        }))

    @router.post("/economy/set")
    async def economy_set(request: Request, guild_id: int = Form(...), user_id: int = Form(...), balance: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "INSERT INTO economy_accounts (guild_id, user_id, balance) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = excluded.balance",
            (guild_id, user_id, balance),
        )
        return RedirectResponse(f"/economy?guild_id={guild_id}", status_code=302)

    @router.post("/economy/delete")
    async def economy_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM economy_accounts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return RedirectResponse(f"/economy?guild_id={guild_id}", status_code=302)

    # ── Levels ────────────────────────────────────────────────────────

    @router.get("/levels", response_class=HTMLResponse)
    async def levels_page(request: Request, guild_id: int | None = None, page: int = 1):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        per_page = 25
        offset = (page - 1) * per_page
        entries = []
        total = 0

        if guild_id:
            total_row = await db_fetchone("SELECT COUNT(*) as c FROM levels WHERE guild_id = ?", (guild_id,))
            total = total_row["c"] if total_row else 0
            entries = await db_fetchall(
                "SELECT * FROM levels WHERE guild_id = ? ORDER BY xp DESC LIMIT ? OFFSET ?",
                (guild_id, per_page, offset),
            )

        total_pages = max(1, (total + per_page - 1) // per_page)

        return templates.TemplateResponse(request, "levels.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "entries": entries,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "active_page": "levels",
        }))

    @router.post("/levels/set")
    async def levels_set(request: Request, guild_id: int = Form(...), user_id: int = Form(...), xp: int = Form(...), level: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level",
            (guild_id, user_id, xp, level),
        )
        return RedirectResponse(f"/levels?guild_id={guild_id}", status_code=302)

    @router.post("/levels/delete")
    async def levels_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return RedirectResponse(f"/levels?guild_id={guild_id}", status_code=302)

    # ── Giveaways ─────────────────────────────────────────────────────

    @router.get("/giveaways", response_class=HTMLResponse)
    async def giveaways_page(request: Request, guild_id: int | None = None, status: str = "active"):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        giveaways = []
        if guild_id:
            if status == "all":
                giveaways = await db_fetchall(
                    "SELECT * FROM giveaways WHERE guild_id = ? ORDER BY id DESC",
                    (guild_id,),
                )
            else:
                giveaways = await db_fetchall(
                    "SELECT * FROM giveaways WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                    (guild_id, status),
                )

        return templates.TemplateResponse(request, "giveaways.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "status": status,
            "giveaways": giveaways,
            "active_page": "giveaways",
        }))

    return router
