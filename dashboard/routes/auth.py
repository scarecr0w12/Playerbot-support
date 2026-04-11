"""Auth routes: /login, /auth/discord/callback, /logout."""

from __future__ import annotations

import secrets
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    _safe_int,
    auth_redirect,
    build_login_context,
    discord_avatar_url,
    discord_oauth_configured,
    fetch_discord_identity,
    fetch_discord_oauth_token,
    guild_is_manageable,
    is_authenticated,
    is_master_user_id,
)

router = APIRouter()


def init(templates: Jinja2Templates) -> APIRouter:
    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if is_authenticated(request):
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "login.html", build_login_context(request))

    @router.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        return templates.TemplateResponse(
            request,
            "login.html",
            build_login_context(request, "Password login is disabled. Sign in with Discord."),
        )

    @router.get("/auth/discord/callback")
    async def discord_auth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        if error:
            return templates.TemplateResponse(
                request, "login.html", build_login_context(request, f"Discord login failed: {error}")
            )
        if not discord_oauth_configured():
            return templates.TemplateResponse(
                request, "login.html", build_login_context(request, "Discord OAuth is not configured on the server.")
            )

        expected_state = request.session.pop("discord_oauth_state", None)
        if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
            return templates.TemplateResponse(
                request, "login.html", build_login_context(request, "Discord login session expired. Please try again.")
            )

        try:
            access_token = await fetch_discord_oauth_token(code)
            user, guilds = await fetch_discord_identity(access_token)
        except httpx.HTTPError:
            return templates.TemplateResponse(
                request, "login.html", build_login_context(request, "Unable to complete Discord login right now.")
            )

        user_id = _safe_int(user.get("id"))
        if user_id is None:
            return templates.TemplateResponse(
                request, "login.html", build_login_context(request, "Discord login returned an invalid user profile.")
            )

        guild_access_ids = sorted(
            {
                _safe_int(guild.get("id"))
                for guild in guilds
                if guild_is_manageable(guild) and _safe_int(guild.get("id")) is not None
            }
        )

        request.session.clear()
        request.session.update(
            {
                "authenticated": True,
                "authenticated_at": int(time.time()),
                "discord_user_id": user_id,
                "guild_access_ids": guild_access_ids,
                "user": {
                    "id": str(user_id),
                    "username": user.get("username") or "Discord User",
                    "global_name": user.get("global_name"),
                    "avatar_url": discord_avatar_url(user),
                },
                "is_master_user": is_master_user_id(user_id),
            }
        )
        return RedirectResponse("/", status_code=303)

    @router.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    return router
