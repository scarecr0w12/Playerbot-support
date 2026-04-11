"""Assistant routes: /assistant and all /assistant/* sub-routes."""

from __future__ import annotations

import json
import re

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
    get_guild_config_map,
    require_guild_access,
)

router = APIRouter()


def init(templates: Jinja2Templates, bot_config) -> APIRouter:
    @router.get("/assistant", response_class=HTMLResponse)
    async def assistant_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        config_values: dict = {}
        triggers: list = []
        custom_functions: list = []
        listen_channels: list = []
        channel_prompts: list = []
        prompt_templates: list = []
        mcp_servers: list = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        conversations = {"messages": 0, "users": 0, "channels": 0, "tokens": 0}

        if guild_id:
            config_values = await get_guild_config_map(guild_id)
            triggers = await db_fetchall(
                "SELECT id, pattern FROM assistant_triggers WHERE guild_id = ? ORDER BY pattern",
                (guild_id,),
            )
            custom_functions = await db_fetchall(
                "SELECT id, name, description, parameters, code, enabled FROM custom_functions WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            )
            usage = await db_fetchone(
                "SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, COALESCE(SUM(completion_tokens), 0) AS completion_tokens FROM token_usage WHERE guild_id = ?",
                (guild_id,),
            ) or usage
            conversations = await db_fetchone(
                "SELECT COUNT(*) AS messages, COUNT(DISTINCT user_id) AS users, COUNT(DISTINCT channel_id) AS channels, COALESCE(SUM(token_count), 0) AS tokens FROM conversation_history WHERE guild_id = ?",
                (guild_id,),
            ) or conversations

            listen_rows = await db_fetchall(
                "SELECT key, value FROM guild_config WHERE guild_id = ? AND key LIKE 'listen_channel_%' ORDER BY key",
                (guild_id,),
            )
            for row in listen_rows:
                try:
                    listen_channels.append({
                        "channel_id": int(row["key"].split("_")[-1]),
                        "value": row["value"],
                    })
                except ValueError:
                    continue

            prompt_rows = await db_fetchall(
                "SELECT key, value FROM guild_config WHERE guild_id = ? AND key LIKE 'channel_prompt_%' AND COALESCE(value, '') != '' ORDER BY key",
                (guild_id,),
            )
            for row in prompt_rows:
                try:
                    channel_prompts.append({
                        "channel_id": int(row["key"].split("_")[-1]),
                        "value": row["value"],
                    })
                except ValueError:
                    continue

            prompt_templates = await db_fetchall(
                "SELECT id, name, content, created_by, created_at FROM prompt_templates WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            )
            mcp_servers = await db_fetchall(
                "SELECT id, name, transport, command, url, enabled, created_at FROM mcp_servers WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            )

        return templates.TemplateResponse(request, "assistant.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "config_values": config_values,
            "triggers": triggers,
            "custom_functions": custom_functions,
            "listen_channels": listen_channels,
            "channel_prompts": channel_prompts,
            "prompt_templates": prompt_templates,
            "mcp_servers": mcp_servers,
            "system_prompt": bot_config.system_prompt,
            "usage": usage,
            "conversations": conversations,
            "active_page": "assistant",
        }))

    @router.post("/assistant/triggers/add")
    async def assistant_trigger_add(request: Request, guild_id: int = Form(...), pattern: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        pattern = pattern.strip()
        if pattern:
            re.compile(pattern)
            await db_execute(
                "INSERT OR IGNORE INTO assistant_triggers (guild_id, pattern) VALUES (?, ?)",
                (guild_id, pattern),
            )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/triggers/delete")
    async def assistant_trigger_delete(request: Request, guild_id: int = Form(...), trigger_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM assistant_triggers WHERE id = ? AND guild_id = ?", (trigger_id, guild_id))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/functions/save")
    async def assistant_function_save(
        request: Request,
        guild_id: int = Form(...),
        name: str = Form(...),
        description: str = Form(...),
        parameters: str = Form(...),
        code: str = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        json.loads(parameters)
        await db_execute(
            "INSERT INTO custom_functions (guild_id, name, description, parameters, code, enabled) VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET description = excluded.description, parameters = excluded.parameters, code = excluded.code",
            (guild_id, name.strip(), description.strip(), parameters.strip(), code),
        )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/functions/toggle")
    async def assistant_function_toggle(request: Request, guild_id: int = Form(...), function_id: int = Form(...), enabled: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "UPDATE custom_functions SET enabled = ? WHERE id = ? AND guild_id = ?",
            (enabled, function_id, guild_id),
        )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/functions/delete")
    async def assistant_function_delete(request: Request, guild_id: int = Form(...), function_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM custom_functions WHERE id = ? AND guild_id = ?", (function_id, guild_id))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/listen/save")
    async def assistant_listen_save(request: Request, guild_id: int = Form(...), channel_id: int = Form(...), enabled: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        key = f"listen_channel_{channel_id}"
        if enabled:
            await db_execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, '1') ON CONFLICT(guild_id, key) DO UPDATE SET value = '1'",
                (guild_id, key),
            )
        else:
            await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/channel-prompts/save")
    async def assistant_channel_prompt_save(request: Request, guild_id: int = Form(...), channel_id: int = Form(...), prompt: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        key = f"channel_prompt_{channel_id}"
        if prompt.strip():
            await db_execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, prompt.strip()),
            )
        else:
            await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/channel-prompts/delete")
    async def assistant_channel_prompt_delete(request: Request, guild_id: int = Form(...), channel_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, f"channel_prompt_{channel_id}"))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/templates/save")
    async def assistant_template_save(
        request: Request,
        guild_id: int = Form(...),
        name: str = Form(...),
        content: str = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        user_id = request.session.get("user", {}).get("id", 0)
        await db_execute(
            "INSERT INTO prompt_templates (guild_id, name, content, created_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET content = excluded.content, created_by = excluded.created_by, created_at = datetime('now')",
            (guild_id, name.strip(), content.strip(), user_id),
        )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/templates/apply")
    async def assistant_template_apply(request: Request, guild_id: int = Form(...), name: str = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, 'assistant_active_template', ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, name),
        )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/templates/clear")
    async def assistant_template_clear(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, 'assistant_active_template', '') "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = ''",
            (guild_id,),
        )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/templates/delete")
    async def assistant_template_delete(request: Request, guild_id: int = Form(...), template_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        tpl = await db_fetchone("SELECT name FROM prompt_templates WHERE id = ? AND guild_id = ?", (template_id, guild_id))
        if tpl:
            await db_execute("DELETE FROM prompt_templates WHERE id = ? AND guild_id = ?", (template_id, guild_id))
            active = await db_fetchone(
                "SELECT value FROM guild_config WHERE guild_id = ? AND key = 'assistant_active_template'",
                (guild_id,),
            )
            if active and active["value"] == tpl["name"]:
                await db_execute(
                    "UPDATE guild_config SET value = '' WHERE guild_id = ? AND key = 'assistant_active_template'",
                    (guild_id,),
                )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/usage/reset")
    async def assistant_usage_reset(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM token_usage WHERE guild_id = ?", (guild_id,))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/conversations/reset")
    async def assistant_conversations_reset(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM conversation_history WHERE guild_id = ?", (guild_id,))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/mcp/add")
    async def assistant_mcp_add(
        request: Request,
        guild_id: int = Form(...),
        name: str = Form(...),
        transport: str = Form(...),
        command: str = Form(""),
        url: str = Form(""),
        env: str = Form("{}"),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        name = name.strip().lower().replace(" ", "_")
        if transport not in ("stdio", "sse"):
            raise HTTPException(status_code=400, detail="Invalid transport")
        try:
            json.loads(env or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid env JSON")
        await db_execute(
            "INSERT INTO mcp_servers (guild_id, name, transport, command, args, env, url) "
            "VALUES (?, ?, ?, ?, '[]', ?, ?) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET transport=excluded.transport, "
            "command=excluded.command, env=excluded.env, url=excluded.url",
            (guild_id, name, transport, command.strip() or None, env.strip() or "{}", url.strip() or None),
        )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/mcp/toggle")
    async def assistant_mcp_toggle(
        request: Request,
        guild_id: int = Form(...),
        server_id: int = Form(...),
        enabled: int = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute(
            "UPDATE mcp_servers SET enabled = ? WHERE id = ? AND guild_id = ?",
            (enabled, server_id, guild_id),
        )
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    @router.post("/assistant/mcp/delete")
    async def assistant_mcp_delete(
        request: Request,
        guild_id: int = Form(...),
        server_id: int = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM mcp_servers WHERE id = ? AND guild_id = ?", (server_id, guild_id))
        return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)

    return router
