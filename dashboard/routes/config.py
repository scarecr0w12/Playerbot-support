"""Config routes: /config, /config/set, /config/delete, /api/refresh-models."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    auth_redirect,
    ctx,
    db_execute,
    db_fetchall,
    get_authorized_guilds,
    is_master_session,
    require_guild_access,
    require_master_user,
)

logger = logging.getLogger("dashboard")
router = APIRouter()


def init(templates: Jinja2Templates, dynamic_schema) -> APIRouter:
    @router.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        config_rows = []
        config_values = {}
        if guild_id:
            config_rows = await db_fetchall(
                "SELECT key, value FROM guild_config WHERE guild_id = ? ORDER BY key",
                (guild_id,),
            )
            config_values = {row["key"]: row["value"] for row in config_rows}

        config_schema = await dynamic_schema.get_config_schema()

        return templates.TemplateResponse(request, "config.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "config_rows": config_rows,
            "config_values": config_values,
            "config_schema": config_schema,
            "config_categories": dynamic_schema.get_config_categories(),
            "can_refresh_models": is_master_session(request),
            "active_page": "config",
        }))

    @router.post("/config/set")
    async def config_set(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)

        form_data = await request.form()
        config_schema = await dynamic_schema.get_config_schema()

        for key, value in form_data.items():
            if key.startswith("config_"):
                config_key = key[7:]
                if config_key in config_schema:
                    if value.strip() == "":
                        default_value = config_schema[config_key].get("default", "")
                        if default_value:
                            logger.debug("Setting %s to default value: %s", config_key, default_value)
                            await db_execute(
                                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                                "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                                (guild_id, config_key, default_value),
                            )
                        else:
                            logger.debug("Removing %s (empty value, no default)", config_key)
                            await db_execute(
                                "DELETE FROM guild_config WHERE guild_id = ? AND key = ?",
                                (guild_id, config_key),
                            )
                    else:
                        logger.debug("Setting %s to value: %s", config_key, value)
                        await db_execute(
                            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                            (guild_id, config_key, str(value)),
                        )

        return RedirectResponse(f"/config?guild_id={guild_id}", status_code=302)

    @router.post("/config/delete")
    async def config_delete(request: Request, guild_id: int = Form(...), key: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
        return RedirectResponse(f"/config?guild_id={guild_id}", status_code=302)

    @router.post("/api/refresh-models")
    async def refresh_models(request: Request):
        if r := auth_redirect(request):
            return r
        require_master_user(request)
        try:
            await dynamic_schema.refresh_models()
            return JSONResponse({"success": True, "message": "Model list refreshed successfully"})
        except Exception as e:
            logger.error("Failed to refresh models: %s", e)
            return JSONResponse(
                {"success": False, "message": f"Failed to refresh models: {str(e)}"},
                status_code=500,
            )

    return router
