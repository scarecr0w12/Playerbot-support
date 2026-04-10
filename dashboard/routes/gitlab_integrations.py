"""GitLab integrations routes for the dashboard."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

GITLAB_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+$")
VALID_GITLAB_EVENTS = {"push", "merge_request", "issues", "release"}
FORM_STATE_TTL = timedelta(minutes=15)
FORM_STATE_CACHE: dict[str, dict[str, Any]] = {}


def _purge_form_state_cache() -> None:
    now = datetime.now(timezone.utc)
    expired = [t for t, e in FORM_STATE_CACHE.items() if e["expires_at"] <= now]
    for t in expired:
        FORM_STATE_CACHE.pop(t, None)


def _stash_form_state(guild_id: int, form_name: str, values: dict[str, Any]) -> str:
    _purge_form_state_cache()
    token = secrets.token_urlsafe(18)
    FORM_STATE_CACHE[token] = {
        "guild_id": guild_id,
        "form_name": form_name,
        "values": values,
        "expires_at": datetime.now(timezone.utc) + FORM_STATE_TTL,
    }
    return token


def _take_form_state(token: str | None, guild_id: int | None) -> dict[str, Any] | None:
    if not token or guild_id is None:
        return None
    _purge_form_state_cache()
    entry = FORM_STATE_CACHE.pop(token, None)
    if not entry or entry.get("guild_id") != guild_id:
        return None
    return {"form_name": entry["form_name"], "values": entry["values"]}


def normalise_gitlab_events(raw: str) -> str:
    events = {e.strip().lower() for e in raw.split(",") if e.strip()}
    valid = sorted(events & VALID_GITLAB_EVENTS)
    return ",".join(valid)


class GitLabIntegrationsModule:
    """Encapsulates the dashboard GitLab integrations feature set."""

    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        ctx: Callable[[dict[str, Any]], dict[str, Any]],
        auth_redirect: Callable[[Request], RedirectResponse | None],
        get_authorized_guilds: Callable[[Request, int | None], Awaitable[list[dict[str, Any]]]],
        get_guild_config_map: Callable[[int], Awaitable[dict[str, str]]],
        require_guild_access: Callable[[Request, int], Awaitable[None]],
        db_fetchall: Callable[[str, tuple[Any, ...]], Awaitable[list[dict[str, Any]]]],
        db_fetchone: Callable[[str, tuple[Any, ...]], Awaitable[dict[str, Any] | None]],
        db_execute: Callable[[str, tuple[Any, ...]], Awaitable[int]],
        gitlab_token: str | None,
        gitlab_token_configured: bool,
        gitlab_url: str,
    ) -> None:
        self.templates = templates
        self.ctx = ctx
        self.auth_redirect = auth_redirect
        self.get_authorized_guilds = get_authorized_guilds
        self.get_guild_config_map = get_guild_config_map
        self.require_guild_access = require_guild_access
        self.db_fetchall = db_fetchall
        self.db_fetchone = db_fetchone
        self.db_execute = db_execute
        self.gitlab_token = gitlab_token
        self.gitlab_token_configured = gitlab_token_configured
        self.gitlab_url = gitlab_url.rstrip("/")
        self.router = APIRouter()
        self._register_routes()

    def _register_routes(self) -> None:
        self.router.add_api_route(
            "/integrations/gitlab",
            self.integrations_gitlab_page,
            methods=["GET"],
            response_class=HTMLResponse,
        )
        self.router.add_api_route(
            "/integrations/gitlab/save",
            self.integrations_gitlab_save,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/integrations/gitlab/delete",
            self.integrations_gitlab_delete,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/integrations/gitlab/reset_state",
            self.integrations_gitlab_reset_state,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/integrations/gitlab/default_project_save",
            self.integrations_gitlab_default_project_save,
            methods=["POST"],
        )

    def _subscription_form_defaults(self) -> dict[str, str]:
        return {
            "project": "",
            "channel_id": "",
            "events": "push,merge_request,issues,release",
        }

    def _gitlab_headers(self) -> dict[str, str]:
        h: dict[str, str] = {"User-Agent": "DiscordBot-Dashboard/1.0"}
        if self.gitlab_token:
            h["PRIVATE-TOKEN"] = self.gitlab_token
        return h

    async def _gitlab_get(self, path: str) -> tuple[int, Any]:
        url = f"{self.gitlab_url}/api/v4{path}"
        async with httpx.AsyncClient(timeout=15.0, headers=self._gitlab_headers()) as client:
            response = await client.get(url)
        try:
            payload = response.json()
        except Exception:
            payload = None
        return response.status_code, payload

    def _redirect(
        self,
        guild_id: int,
        level: str,
        message: str,
        *,
        anchor: str | None = None,
        draft_token: str | None = None,
    ) -> RedirectResponse:
        params: dict[str, Any] = {"guild_id": guild_id, "flash": level, "message": message}
        if draft_token:
            params["draft"] = draft_token
        query = urlencode(params)
        location = f"/integrations/gitlab?{query}"
        if anchor:
            location += f"#{anchor}"
        return RedirectResponse(location, status_code=302)

    def _redirect_form_error(
        self,
        guild_id: int,
        message: str,
        *,
        form_name: str,
        values: dict[str, Any],
        anchor: str,
    ) -> RedirectResponse:
        token = _stash_form_state(guild_id, form_name, values)
        return self._redirect(guild_id, "error", message, anchor=anchor, draft_token=token)

    async def _save_config_value(self, guild_id: int, key: str, value: str) -> None:
        if value:
            await self.db_execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, value),
            )
        else:
            await self.db_execute(
                "DELETE FROM guild_config WHERE guild_id = ? AND key = ?",
                (guild_id, key),
            )

    # ------------------------------------------------------------------
    # Page

    async def integrations_gitlab_page(
        self,
        request: Request,
        guild_id: int | None = None,
    ):
        if redirect := self.auth_redirect(request):
            return redirect

        guilds = await self.get_authorized_guilds(request, guild_id)
        gl_subscriptions: list[dict[str, Any]] = []
        gl_poll_state: list[dict[str, Any]] = []
        gl_default_project: str = ""
        subscription_form = self._subscription_form_defaults()
        flash: dict[str, str] | None = None
        restored_form_name: str | None = None

        flash_level = (request.query_params.get("flash") or "").strip()
        flash_message = (request.query_params.get("message") or "").strip()
        if flash_level and flash_message:
            flash = {"level": flash_level, "message": flash_message}

        draft_state = _take_form_state(request.query_params.get("draft"), guild_id)

        if guild_id:
            gl_subscriptions = await self.db_fetchall(
                "SELECT * FROM gitlab_subscriptions WHERE guild_id = ? ORDER BY project, channel_id",
                (guild_id,),
            )
            projects = {row["project"] for row in gl_subscriptions}
            state_rows = await self.db_fetchall(
                "SELECT * FROM gitlab_poll_state ORDER BY updated_at DESC",
                (),
            )
            gl_poll_state = [row for row in state_rows if row["project"] in projects]

            config_values = await self.get_guild_config_map(guild_id)
            gl_default_project = config_values.get("gitlab_default_project", "")

            if draft_state:
                restored_form_name = str(draft_state["form_name"])
                if draft_state["form_name"] == "gl_subscription":
                    subscription_form = {
                        **subscription_form,
                        **{k: str(v) for k, v in draft_state["values"].items()},
                    }

        return self.templates.TemplateResponse(
            request,
            "integrations_gitlab.html",
            self.ctx(
                {
                    "guilds": guilds,
                    "guild_id": guild_id,
                    "gl_subscriptions": gl_subscriptions,
                    "gl_poll_state": gl_poll_state,
                    "gl_default_project": gl_default_project,
                    "subscription_form": subscription_form,
                    "restored_form_name": restored_form_name,
                    "flash": flash,
                    "gitlab_token_configured": self.gitlab_token_configured,
                    "gitlab_url": self.gitlab_url,
                    "active_page": "integrations",
                    "active_subpage": "gitlab",
                }
            ),
        )

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe

    async def integrations_gitlab_save(
        self,
        request: Request,
        guild_id: int = Form(...),
        channel_id: int = Form(...),
        project: str = Form(...),
        events: str = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)

        project = project.strip()
        subscription_form = {
            "project": project,
            "channel_id": str(channel_id),
            "events": events.strip() or "push,merge_request,issues,release",
        }

        if not GITLAB_PROJECT_RE.match(project):
            return self._redirect_form_error(
                guild_id,
                "Project must use namespace/project format.",
                form_name="gl_subscription",
                values=subscription_form,
                anchor="gl-subscriptions",
            )

        events_value = normalise_gitlab_events(events) or "push,merge_request,issues,release"

        # Verify project exists on GitLab
        encoded = quote(project, safe="")
        status, _ = await self._gitlab_get(f"/projects/{encoded}")
        if status == 404:
            return self._redirect_form_error(
                guild_id,
                f"Project '{project}' was not found on GitLab.",
                form_name="gl_subscription",
                values=subscription_form,
                anchor="gl-subscriptions",
            )
        if status not in (200, 201):
            return self._redirect_form_error(
                guild_id,
                "Could not verify project — GitLab API error. Check the token and project path.",
                form_name="gl_subscription",
                values=subscription_form,
                anchor="gl-subscriptions",
            )

        await self.db_execute(
            "INSERT INTO gitlab_subscriptions (guild_id, channel_id, project, events, added_by) "
            "VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(guild_id, channel_id, project) DO UPDATE SET events = excluded.events",
            (guild_id, channel_id, project, events_value),
        )
        return self._redirect(guild_id, "success", f"Saved subscription for {project}.", anchor="gl-subscriptions")

    async def integrations_gitlab_delete(
        self,
        request: Request,
        guild_id: int = Form(...),
        subscription_id: int = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)

        row = await self.db_fetchone(
            "SELECT project FROM gitlab_subscriptions WHERE id = ? AND guild_id = ?",
            (subscription_id, guild_id),
        )
        if not row:
            return self._redirect(guild_id, "error", "That GitLab subscription no longer exists.", anchor="gl-subscriptions")

        deleted = await self.db_execute(
            "DELETE FROM gitlab_subscriptions WHERE id = ? AND guild_id = ?",
            (subscription_id, guild_id),
        )
        project = row["project"] if row else None
        if deleted and project:
            remaining = await self.db_fetchone(
                "SELECT COUNT(*) AS c FROM gitlab_subscriptions WHERE project = ?",
                (project,),
            )
            if not remaining or remaining["c"] == 0:
                await self.db_execute("DELETE FROM gitlab_poll_state WHERE project = ?", (project,))

        if not deleted:
            return self._redirect(guild_id, "error", "Could not remove that subscription.", anchor="gl-subscriptions")
        return self._redirect(guild_id, "success", "Deleted GitLab subscription.", anchor="gl-subscriptions")

    # ------------------------------------------------------------------
    # Poll state reset

    async def integrations_gitlab_reset_state(
        self,
        request: Request,
        guild_id: int = Form(...),
        project: str = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)

        project = project.strip()
        if not GITLAB_PROJECT_RE.match(project):
            return self._redirect(guild_id, "error", "Project must use namespace/project format.", anchor="gl-poll-state")

        row = await self.db_fetchone(
            "SELECT 1 AS ok FROM gitlab_subscriptions WHERE guild_id = ? AND project = ? LIMIT 1",
            (guild_id, project),
        )
        if not row:
            return self._redirect(guild_id, "error", "Reset is only available for projects subscribed in this guild.", anchor="gl-poll-state")

        await self.db_execute("DELETE FROM gitlab_poll_state WHERE project = ?", (project,))
        return self._redirect(guild_id, "success", f"Reset poll state for {project}.", anchor="gl-poll-state")

    # ------------------------------------------------------------------
    # Default project

    async def integrations_gitlab_default_project_save(
        self,
        request: Request,
        guild_id: int = Form(...),
        default_project: str = Form(""),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)

        default_project = default_project.strip()
        if default_project and not GITLAB_PROJECT_RE.match(default_project):
            return self._redirect(guild_id, "error", "Default project must use namespace/project format.", anchor="gl-settings")

        await self._save_config_value(guild_id, "gitlab_default_project", default_project)
        msg = f"Default project set to {default_project}." if default_project else "Default project cleared."
        return self._redirect(guild_id, "success", msg, anchor="gl-settings")
