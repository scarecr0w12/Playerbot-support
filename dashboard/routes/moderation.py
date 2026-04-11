"""Moderation routes: /moderation, /warnings, /tickets, /automod."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
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
    # ── Moderation cases ───────────────────────────────────────────────

    @router.get("/moderation", response_class=HTMLResponse)
    async def moderation_page(request: Request, guild_id: int | None = None, user_id: int | None = None, page: int = 1):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        per_page = 25
        offset = (page - 1) * per_page
        cases = []
        total = 0

        if guild_id:
            if user_id:
                total_row = await db_fetchone("SELECT COUNT(*) as c FROM mod_cases WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                total = total_row["c"] if total_row else 0
                cases = await db_fetchall(
                    "SELECT * FROM mod_cases WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (guild_id, user_id, per_page, offset),
                )
            else:
                total_row = await db_fetchone("SELECT COUNT(*) as c FROM mod_cases WHERE guild_id = ?", (guild_id,))
                total = total_row["c"] if total_row else 0
                cases = await db_fetchall(
                    "SELECT * FROM mod_cases WHERE guild_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (guild_id, per_page, offset),
                )

        total_pages = max(1, (total + per_page - 1) // per_page)

        return templates.TemplateResponse(request, "moderation.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "user_id": user_id,
            "cases": cases,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "active_page": "moderation",
        }))

    @router.post("/moderation/delete")
    async def moderation_delete(request: Request, case_id: int = Form(...), guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM mod_cases WHERE id = ? AND guild_id = ?", (case_id, guild_id))
        return RedirectResponse(f"/moderation?guild_id={guild_id}", status_code=302)

    # ── Warnings ──────────────────────────────────────────────────────

    @router.get("/warnings", response_class=HTMLResponse)
    async def warnings_page(request: Request, guild_id: int | None = None, user_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        warnings = []
        if guild_id:
            if user_id:
                warnings = await db_fetchall(
                    "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY id DESC",
                    (guild_id, user_id),
                )
            else:
                warnings = await db_fetchall(
                    "SELECT * FROM warnings WHERE guild_id = ? AND active = 1 ORDER BY id DESC",
                    (guild_id,),
                )

        return templates.TemplateResponse(request, "warnings.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "user_id": user_id,
            "warnings": warnings,
            "active_page": "warnings",
        }))

    @router.post("/warnings/delete")
    async def warning_delete(request: Request, warning_id: int = Form(...), guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("UPDATE warnings SET active = 0 WHERE id = ? AND guild_id = ?", (warning_id, guild_id))
        return RedirectResponse(f"/warnings?guild_id={guild_id}", status_code=302)

    # ── Tickets ───────────────────────────────────────────────────────

    @router.get("/tickets", response_class=HTMLResponse)
    async def tickets_page(request: Request, guild_id: int | None = None, status: str = "open"):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        tickets = []
        if guild_id:
            if status == "all":
                tickets = await db_fetchall(
                    "SELECT * FROM tickets WHERE guild_id = ? ORDER BY id DESC",
                    (guild_id,),
                )
            else:
                tickets = await db_fetchall(
                    "SELECT * FROM tickets WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                    (guild_id, status),
                )

        return templates.TemplateResponse(request, "tickets.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "status": status,
            "tickets": tickets,
            "active_page": "tickets",
        }))

    @router.get("/tickets/{ticket_id}/transcript", response_class=HTMLResponse)
    async def ticket_transcript(request: Request, ticket_id: int):
        if r := auth_redirect(request):
            return r

        ticket = await db_fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if not ticket:
            raise HTTPException(404, "Ticket not found")
        await require_guild_access(request, int(ticket["guild_id"]))
        messages = await db_fetchall(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )

        return templates.TemplateResponse(request, "ticket_transcript.html", ctx({
            "ticket": ticket,
            "messages": messages,
            "active_page": "tickets",
        }))

    # ── Auto-mod ──────────────────────────────────────────────────────

    @router.get("/automod", response_class=HTMLResponse)
    async def automod_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        filters = []
        if guild_id:
            filters = await db_fetchall(
                "SELECT * FROM automod_filters WHERE guild_id = ? ORDER BY filter_type, pattern",
                (guild_id,),
            )

        return templates.TemplateResponse(request, "automod.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "filters": filters,
            "active_page": "automod",
        }))

    @router.post("/automod/add")
    async def automod_add(request: Request, guild_id: int = Form(...), filter_type: str = Form(...), pattern: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        try:
            await db_execute(
                "INSERT OR IGNORE INTO automod_filters (guild_id, filter_type, pattern) VALUES (?, ?, ?)",
                (guild_id, filter_type, pattern),
            )
        except Exception:
            pass
        return RedirectResponse(f"/automod?guild_id={guild_id}", status_code=302)

    @router.post("/automod/delete")
    async def automod_delete(request: Request, filter_id: int = Form(...), guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM automod_filters WHERE id = ? AND guild_id = ?", (filter_id, guild_id))
        return RedirectResponse(f"/automod?guild_id={guild_id}", status_code=302)

    return router
