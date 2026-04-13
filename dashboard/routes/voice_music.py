"""Dashboard: Voice & Music guild settings (stored in guild_config)."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    auth_redirect,
    ctx,
    db_execute,
    get_authorized_guilds,
    get_guild_config_map,
    require_guild_access,
)

router = APIRouter()

_DEFAULT_VOLUME = 100
_DEFAULT_INACTIVITY = 3
_DEFAULT_MAX_QUEUE = 50


def init(templates: Jinja2Templates) -> APIRouter:
    @router.get("/voice-music", response_class=HTMLResponse)
    async def voice_music_page(request: Request, guild_id: int | None = None):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        cfg: dict[str, str] = {}
        if guild_id:
            cfg = await get_guild_config_map(guild_id)

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            raw = cfg.get(key)
            if raw is None:
                return default
            try:
                return max(lo, min(hi, int(raw)))
            except ValueError:
                return default

        music_enabled = cfg.get("music_enabled", "1") != "0"
        music_default_volume = _int("music_default_volume", _DEFAULT_VOLUME, 0, 200)
        music_inactivity_minutes = _int("music_inactivity_minutes", _DEFAULT_INACTIVITY, 1, 60)
        music_max_queue = _int("music_max_queue", _DEFAULT_MAX_QUEUE, 5, 100)

        return templates.TemplateResponse(
            request,
            "voice_music.html",
            ctx(
                {
                    "guilds": guilds,
                    "guild_id": guild_id,
                    "active_page": "voice_music",
                    "music_enabled": music_enabled,
                    "music_default_volume": music_default_volume,
                    "music_inactivity_minutes": music_inactivity_minutes,
                    "music_max_queue": music_max_queue,
                }
            ),
        )

    @router.post("/voice-music/save")
    async def voice_music_save(
        request: Request,
        guild_id: int = Form(...),
        music_enabled: str = Form("0"),
        music_default_volume: int = Form(_DEFAULT_VOLUME),
        music_inactivity_minutes: int = Form(_DEFAULT_INACTIVITY),
        music_max_queue: int = Form(_DEFAULT_MAX_QUEUE),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)

        if music_enabled not in {"0", "1"}:
            raise HTTPException(status_code=400, detail="Invalid music_enabled")
        vol = max(0, min(200, music_default_volume))
        inactive = max(1, min(60, music_inactivity_minutes))
        qmax = max(5, min(100, music_max_queue))

        pairs = [
            ("music_enabled", music_enabled),
            ("music_default_volume", str(vol)),
            ("music_inactivity_minutes", str(inactive)),
            ("music_max_queue", str(qmax)),
        ]
        for key, value in pairs:
            await db_execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, value),
            )

        return RedirectResponse(f"/voice-music?guild_id={guild_id}", status_code=302)

    return router
