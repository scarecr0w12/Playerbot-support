"""Miscellaneous routes: /permissions, /reports, /custom-commands, /reminders."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
