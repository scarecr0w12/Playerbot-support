"""GitHub integrations routes and workflow helpers for the dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import secrets
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
VALID_GITHUB_EVENTS = {"push", "pull_request", "issues", "release"}
ISSUE_TEMPLATE_KEYS = ("bug", "feature", "docs")
GITHUB_API = "https://api.github.com"
MAX_PREVIEW_PRS = 10
MAX_PREVIEW_ITEMS = 5
FORM_STATE_TTL = timedelta(minutes=15)
FORM_STATE_CACHE: dict[str, dict[str, Any]] = {}


def _purge_form_state_cache() -> None:
    now = datetime.now(timezone.utc)
    expired = [token for token, entry in FORM_STATE_CACHE.items() if entry["expires_at"] <= now]
    for token in expired:
        FORM_STATE_CACHE.pop(token, None)


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


def default_issue_template(template_key: str) -> str:
    templates = {
        "bug": "Problem summary\n\nExpected behavior\n\nActual behavior\n\nImpact",
        "feature": "Requested change\n\nWhy it matters\n\nAcceptance criteria",
        "docs": "What is unclear\n\nSuggested documentation update\n\nWho is affected",
    }
    return templates.get(template_key, "")


def extract_github_username_links(config_values: dict[str, str]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for key, value in config_values.items():
        if not key.startswith("github_username_") or not value:
            continue
        discord_user_id = key.removeprefix("github_username_")
        if not discord_user_id.isdigit():
            continue
        links.append({"discord_user_id": discord_user_id, "github_username": value})
    links.sort(key=lambda item: int(item["discord_user_id"]))
    return links


def normalise_github_events(raw: str) -> str:
    events = {event.strip().lower() for event in raw.split(",") if event.strip()}
    valid = sorted(events & VALID_GITHUB_EVENTS)
    return ",".join(valid)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _requested_reviewer_names(pr_data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for reviewer in pr_data.get("requested_reviewers") or []:
        login = reviewer.get("login")
        if login:
            names.append(login)
    for team in pr_data.get("requested_teams") or []:
        slug = team.get("slug")
        if slug:
            names.append(f"team:{slug}")
    return names


def _summarize_reviews(reviews: list[dict[str, Any]]) -> tuple[int, bool]:
    latest_by_user: dict[str, tuple[datetime, str]] = {}
    for review in reviews:
        login = (review.get("user") or {}).get("login")
        submitted_at = _parse_iso_dt(review.get("submitted_at")) or datetime.min.replace(tzinfo=timezone.utc)
        state = str(review.get("state") or "").upper()
        if not login or not state:
            continue
        previous = latest_by_user.get(login)
        if previous is None or submitted_at >= previous[0]:
            latest_by_user[login] = (submitted_at, state)
    latest_states = [state for _, state in latest_by_user.values()]
    approvals = sum(1 for state in latest_states if state == "APPROVED")
    changes_requested = any(state == "CHANGES_REQUESTED" for state in latest_states)
    return approvals, changes_requested


def _review_bucket(pr_data: dict[str, Any], reviews: list[dict[str, Any]], stale_cutoff: datetime) -> str:
    if pr_data.get("draft"):
        return "draft"
    approvals, changes_requested = _summarize_reviews(reviews)
    if changes_requested:
        return "changes_requested"
    if _requested_reviewer_names(pr_data):
        return "review_requested"
    if approvals > 0:
        return "approved"
    updated_at = _parse_iso_dt(pr_data.get("updated_at"))
    if updated_at and updated_at <= stale_cutoff:
        return "stale"
    return "waiting"


def _build_review_load(queue: list[tuple[dict[str, Any], list[dict[str, Any]]]], stale_cutoff: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reviewer_load: dict[str, dict[str, Any]] = {}
    team_load: dict[str, dict[str, Any]] = {}

    for pr_data, reviews in queue:
        bucket = _review_bucket(pr_data, reviews, stale_cutoff)
        if bucket != "review_requested":
            continue
        updated_at = _parse_iso_dt(pr_data.get("updated_at")) or datetime.now(timezone.utc)
        for reviewer in _requested_reviewer_names(pr_data):
            target = team_load if reviewer.startswith("team:") else reviewer_load
            display_name = reviewer.removeprefix("team:") if reviewer.startswith("team:") else reviewer
            info = target.setdefault(
                display_name,
                {"count": 0, "oldest": updated_at, "number": pr_data.get("number")},
            )
            info["count"] += 1
            if updated_at <= info["oldest"]:
                info["oldest"] = updated_at
                info["number"] = pr_data.get("number")

    def _serialize(load: dict[str, dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
        return [
            {
                key_name: name,
                "count": info["count"],
                "oldest_number": info["number"],
                "oldest_date": info["oldest"].strftime("%Y-%m-%d"),
            }
            for name, info in sorted(load.items(), key=lambda item: (-item[1]["count"], item[1]["oldest"]))[:5]
        ]

    return _serialize(reviewer_load, "reviewer"), _serialize(team_load, "team")


def build_triage_preview(issues: list[dict[str, Any]], stale_days: int) -> dict[str, Any]:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    unassigned = [issue for issue in issues if not issue.get("assignees")]
    unlabeled = [issue for issue in issues if not issue.get("labels")]
    stale = [
        issue
        for issue in issues
        if (_parse_iso_dt(issue.get("updated_at")) or datetime.now(timezone.utc)) <= stale_cutoff
    ]

    def _serialize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "number": issue.get("number"),
                "title": issue.get("title", ""),
                "url": issue.get("html_url", ""),
                "author": (issue.get("user") or {}).get("login", "?"),
                "updated_at": (issue.get("updated_at") or "")[:10],
                "labels": [label.get("name", "") for label in (issue.get("labels") or []) if label.get("name")],
                "assignees": [assignee.get("login", "") for assignee in (issue.get("assignees") or []) if assignee.get("login")],
            }
            for issue in items[:MAX_PREVIEW_ITEMS]
        ]

    return {
        "sections": [
            {"key": "unassigned", "label": "Unassigned", "count": len(unassigned), "items": _serialize(unassigned)},
            {"key": "unlabeled", "label": "Unlabeled", "count": len(unlabeled), "items": _serialize(unlabeled)},
            {"key": "stale", "label": f"Stale ({stale_days}d)", "count": len(stale), "items": _serialize(stale)},
        ],
        "total_open": len(issues),
        "stale_days": stale_days,
        "counts": {
            "unassigned": len(unassigned),
            "unlabeled": len(unlabeled),
            "stale": len(stale),
        },
    }


def build_review_preview(queue: list[tuple[dict[str, Any], list[dict[str, Any]]]], stale_hours: int) -> dict[str, Any]:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
    buckets: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}

    for pr_data, reviews in queue:
        bucket = _review_bucket(pr_data, reviews, stale_cutoff)
        buckets.setdefault(bucket, []).append((pr_data, reviews))

    sections = []
    labels = [
        ("review_requested", "Needs Review"),
        ("changes_requested", "Changes Requested"),
        ("approved", "Approved"),
        ("stale", "Stale"),
        ("waiting", "Waiting"),
    ]
    for key, label in labels:
        items = buckets.get(key) or []
        if not items:
            continue
        section_items = []
        for pr_data, reviews in items[:MAX_PREVIEW_ITEMS]:
            approvals, changes_requested = _summarize_reviews(reviews)
            section_items.append(
                {
                    "number": pr_data.get("number"),
                    "title": pr_data.get("title", ""),
                    "url": pr_data.get("html_url", ""),
                    "author": (pr_data.get("user") or {}).get("login", "?"),
                    "updated_at": (pr_data.get("updated_at") or "")[:10],
                    "requested": _requested_reviewer_names(pr_data),
                    "approvals": approvals,
                    "changes_requested": changes_requested,
                }
            )
        sections.append({"key": key, "label": label, "count": len(items), "items": section_items})

    reviewer_lines, team_lines = _build_review_load(queue, stale_cutoff)

    return {
        "sections": sections,
        "reviewer_load": reviewer_lines,
        "team_load": team_lines,
        "draft_count": len(buckets.get("draft") or []),
        "total_open": len(queue),
        "stale_hours": stale_hours,
    }


class GitHubIntegrationsModule:
    """Encapsulates the dashboard GitHub integrations feature set."""

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
        github_token: str | None,
        github_token_configured: bool,
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
        self.github_token = github_token
        self.github_token_configured = github_token_configured
        self.router = APIRouter()
        self._register_routes()

    def _register_routes(self) -> None:
        self.router.add_api_route(
            "/integrations",
            self.integrations_page,
            methods=["GET"],
            response_class=HTMLResponse,
        )
        self.router.add_api_route("/integrations/github/save", self.integrations_github_save, methods=["POST"])
        self.router.add_api_route("/integrations/github/delete", self.integrations_github_delete, methods=["POST"])
        self.router.add_api_route(
            "/integrations/github/reset_state",
            self.integrations_github_reset_state,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/integrations/github/workflow_save",
            self.integrations_github_workflow_save,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/integrations/github/user_link_save",
            self.integrations_github_user_link_save,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/integrations/github/user_link_delete",
            self.integrations_github_user_link_delete,
            methods=["POST"],
        )

    def _workflow_defaults(self) -> dict[str, Any]:
        return {
            "default_repo": "",
            "review_digest_channel": "",
            "review_digest_hour_utc": "13",
            "review_digest_repo": "",
            "review_digest_stale_hours": "24",
            "issue_default_template": "",
            "issue_templates": {key: default_issue_template(key) for key in ISSUE_TEMPLATE_KEYS},
            "issue_template_labels": {key: "" for key in ISSUE_TEMPLATE_KEYS},
            "issue_template_assignees": {key: "" for key in ISSUE_TEMPLATE_KEYS},
            "issue_template_milestones": {key: "" for key in ISSUE_TEMPLATE_KEYS},
        }

    def _subscription_form_defaults(self) -> dict[str, str]:
        return {
            "repo": "",
            "channel_id": "",
            "events": "push,pull_request,issues,release",
        }

    def _user_link_form_defaults(self) -> dict[str, str]:
        return {
            "discord_user_id": "",
            "github_username": "",
        }

    def _github_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DiscordBot-Dashboard/1.0",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    async def _github_get(self, path: str) -> tuple[int, Any]:
        async with httpx.AsyncClient(timeout=15.0, headers=self._github_headers()) as client:
            response = await client.get(f"{GITHUB_API}{path}")
        try:
            payload = response.json()
        except Exception:
            payload = None
        return response.status_code, payload

    async def _fetch_review_preview(self, repo: str, stale_hours: int) -> dict[str, Any]:
        status, pulls = await self._github_get(
            f"/repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page={MAX_PREVIEW_PRS}"
        )
        if status != 200 or not isinstance(pulls, list):
            raise HTTPException(status_code=502, detail="Could not fetch pull requests from GitHub")
        queue = []
        for pr_data in pulls[:MAX_PREVIEW_PRS]:
            number = pr_data.get("number")
            reviews_status, reviews = await self._github_get(f"/repos/{repo}/pulls/{number}/reviews?per_page=30")
            queue.append((pr_data, reviews if reviews_status == 200 and isinstance(reviews, list) else []))
        preview = build_review_preview(queue, stale_hours)
        preview["repo"] = repo
        return preview

    async def _fetch_triage_preview(self, repo: str, stale_days: int) -> dict[str, Any]:
        status, issues = await self._github_get(
            f"/repos/{repo}/issues?state=open&sort=updated&direction=asc&per_page=30"
        )
        if status != 200 or not isinstance(issues, list):
            raise HTTPException(status_code=502, detail="Could not fetch issues from GitHub")
        preview = build_triage_preview([issue for issue in issues if "pull_request" not in issue], stale_days)
        preview["repo"] = repo
        return preview

    async def _validate_issue_template_milestones(self, repo: str, milestone_values: dict[str, str]) -> None:
        for key, value in milestone_values.items():
            if not value:
                continue
            status, payload = await self._github_get(f"/repos/{repo}/milestones/{value}")
            if status == 404:
                raise HTTPException(status_code=400, detail=f"{key} milestone #{value} was not found in {repo}")
            if status != 200 or not isinstance(payload, dict):
                raise HTTPException(status_code=502, detail=f"Could not validate {key} milestone #{value} against {repo}")

    async def _github_subscription_exists(self, guild_id: int, repo: str) -> bool:
        row = await self.db_fetchone(
            "SELECT 1 AS ok FROM github_subscriptions WHERE guild_id = ? AND repo = ? LIMIT 1",
            (guild_id, repo),
        )
        return bool(row)

    async def _reset_github_poll_state(self, repo: str) -> int:
        return await self.db_execute("DELETE FROM github_poll_state WHERE repo = ?", (repo,))

    async def _save_config_value(self, guild_id: int, key: str, value: str) -> None:
        if value:
            await self.db_execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, value),
            )
            return
        await self.db_execute(
            "DELETE FROM guild_config WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )

    def _redirect_with_flash(
        self,
        guild_id: int,
        level: str,
        message: str,
        *,
        anchor: str | None = None,
        draft_token: str | None = None,
    ) -> RedirectResponse:
        params = {"guild_id": guild_id, "flash": level, "message": message}
        if draft_token:
            params["draft"] = draft_token
        query = urlencode(params)
        location = f"/integrations?{query}"
        if anchor:
            location = f"{location}#{anchor}"
        return RedirectResponse(location, status_code=302)

    def _redirect_with_form_error(
        self,
        guild_id: int,
        message: str,
        *,
        form_name: str,
        values: dict[str, Any],
        anchor: str,
    ) -> RedirectResponse:
        draft_token = _stash_form_state(guild_id, form_name, values)
        return self._redirect_with_flash(guild_id, "error", message, anchor=anchor, draft_token=draft_token)

    async def integrations_page(
        self,
        request: Request,
        guild_id: int | None = None,
        preview_mode: str | None = None,
        preview_repo: str | None = None,
        preview_stale_hours: int | None = None,
        preview_stale_days: int | None = None,
    ):
        if redirect := self.auth_redirect(request):
            return redirect

        guilds = await self.get_authorized_guilds(request, guild_id)
        subscriptions: list[dict[str, Any]] = []
        poll_state: list[dict[str, Any]] = []
        github_workflow = self._workflow_defaults()
        github_user_links: list[dict[str, str]] = []
        subscription_form = self._subscription_form_defaults()
        user_link_form = self._user_link_form_defaults()
        review_preview: dict[str, Any] | None = None
        flash: dict[str, str] | None = None
        restored_form_name: str | None = None

        flash_level = (request.query_params.get("flash") or "").strip()
        flash_message = (request.query_params.get("message") or "").strip()
        if flash_level and flash_message:
            flash = {"level": flash_level, "message": flash_message}

        draft_state = _take_form_state(request.query_params.get("draft"), guild_id)

        if guild_id:
            subscriptions = await self.db_fetchall(
                "SELECT * FROM github_subscriptions WHERE guild_id = ? ORDER BY repo, channel_id",
                (guild_id,),
            )
            state_rows = await self.db_fetchall(
                "SELECT * FROM github_poll_state ORDER BY updated_at DESC",
                (),
            )
            repos = {row["repo"] for row in subscriptions}
            poll_state = [row for row in state_rows if row["repo"] in repos]
            config_values = await self.get_guild_config_map(guild_id)
            github_workflow = {
                "default_repo": config_values.get("github_default_repo", ""),
                "review_digest_channel": config_values.get("github_review_digest_channel", ""),
                "review_digest_hour_utc": config_values.get("github_review_digest_hour_utc", "13"),
                "review_digest_repo": config_values.get("github_review_digest_repo", ""),
                "review_digest_stale_hours": config_values.get("github_review_digest_stale_hours", "24"),
                "issue_default_template": config_values.get("github_issue_default_template", ""),
                "issue_templates": {
                    key: config_values.get(f"github_issue_template_{key}", default_issue_template(key))
                    for key in ISSUE_TEMPLATE_KEYS
                },
                "issue_template_labels": {
                    key: config_values.get(f"github_issue_template_labels_{key}", "")
                    for key in ISSUE_TEMPLATE_KEYS
                },
                "issue_template_assignees": {
                    key: config_values.get(f"github_issue_template_assignees_{key}", "")
                    for key in ISSUE_TEMPLATE_KEYS
                },
                "issue_template_milestones": {
                    key: config_values.get(f"github_issue_template_milestone_{key}", "")
                    for key in ISSUE_TEMPLATE_KEYS
                },
            }
            github_user_links = extract_github_username_links(config_values)

            if draft_state:
                restored_form_name = str(draft_state["form_name"])
                if draft_state["form_name"] == "workflow":
                    github_workflow = draft_state["values"]
                elif draft_state["form_name"] == "subscription":
                    subscription_form = {
                        **subscription_form,
                        **{key: str(value) for key, value in draft_state["values"].items()},
                    }
                elif draft_state["form_name"] == "user_link":
                    user_link_form = {
                        **user_link_form,
                        **{key: str(value) for key, value in draft_state["values"].items()},
                    }

            if preview_mode in {"queue", "digest", "triage"}:
                resolved_preview_repo = (preview_repo or github_workflow["review_digest_repo"] or github_workflow["default_repo"]).strip()
                stale_hours = preview_stale_hours or int(github_workflow["review_digest_stale_hours"] or "24")
                stale_days = preview_stale_days or 7
                if not resolved_preview_repo:
                    review_preview = {"error": "No preview repo configured.", "mode": preview_mode, "repo": "", "stale_hours": stale_hours, "stale_days": stale_days}
                elif not GITHUB_REPO_RE.match(resolved_preview_repo):
                    review_preview = {"error": "Preview repo must be in owner/repo format.", "mode": preview_mode, "repo": resolved_preview_repo, "stale_hours": stale_hours, "stale_days": stale_days}
                else:
                    try:
                        if preview_mode == "triage":
                            review_preview = await self._fetch_triage_preview(resolved_preview_repo, stale_days)
                        else:
                            review_preview = await self._fetch_review_preview(resolved_preview_repo, stale_hours)
                        review_preview["mode"] = preview_mode
                    except HTTPException as exc:
                        review_preview = {
                            "error": exc.detail,
                            "mode": preview_mode,
                            "repo": resolved_preview_repo,
                            "stale_hours": stale_hours,
                            "stale_days": stale_days,
                        }

        return self.templates.TemplateResponse(
            request,
            "integrations.html",
            self.ctx(
                {
                    "guilds": guilds,
                    "guild_id": guild_id,
                    "subscriptions": subscriptions,
                    "poll_state": poll_state,
                    "github_workflow": github_workflow,
                    "github_user_links": github_user_links,
                    "subscription_form": subscription_form,
                    "user_link_form": user_link_form,
                    "restored_form_name": restored_form_name,
                    "review_preview": review_preview,
                    "flash": flash,
                    "github_token_configured": self.github_token_configured,
                    "active_page": "integrations",
                }
            ),
        )

    async def integrations_github_save(
        self,
        request: Request,
        guild_id: int = Form(...),
        channel_id: int = Form(...),
        repo: str = Form(...),
        events: str = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)
        repo = repo.strip()
        subscription_form = {
            "repo": repo,
            "channel_id": str(channel_id),
            "events": events.strip() or "push,pull_request,issues,release",
        }
        if not GITHUB_REPO_RE.match(repo):
            return self._redirect_with_form_error(
                guild_id,
                "Repository must use owner/repo format.",
                form_name="subscription",
                values=subscription_form,
                anchor="subscriptions",
            )
        events_value = normalise_github_events(events) or "push,pull_request,issues,release"
        await self.db_execute(
            "INSERT INTO github_subscriptions (guild_id, channel_id, repo, events, added_by) VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(guild_id, channel_id, repo) DO UPDATE SET events = excluded.events",
            (guild_id, channel_id, repo, events_value),
        )
        return self._redirect_with_flash(guild_id, "success", f"Saved subscription for {repo}.", anchor="subscriptions")

    async def integrations_github_delete(
        self,
        request: Request,
        guild_id: int = Form(...),
        subscription_id: int = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)
        row = await self.db_fetchone(
            "SELECT repo FROM github_subscriptions WHERE id = ? AND guild_id = ?",
            (subscription_id, guild_id),
        )
        if not row:
            return self._redirect_with_flash(guild_id, "error", "That GitHub subscription no longer exists.", anchor="subscriptions")
        deleted = await self.db_execute(
            "DELETE FROM github_subscriptions WHERE id = ? AND guild_id = ?",
            (subscription_id, guild_id),
        )
        repo = row["repo"] if row else None
        if deleted and repo:
            remaining = await self.db_fetchone(
                "SELECT COUNT(*) AS c FROM github_subscriptions WHERE repo = ?",
                (repo,),
            )
            if not remaining or remaining["c"] == 0:
                await self._reset_github_poll_state(repo)
        if not deleted:
            return self._redirect_with_flash(guild_id, "error", "That GitHub subscription could not be removed.", anchor="subscriptions")
        return self._redirect_with_flash(guild_id, "success", "Deleted GitHub subscription.", anchor="subscriptions")

    async def integrations_github_reset_state(
        self,
        request: Request,
        guild_id: int = Form(...),
        repo: str = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)
        repo = repo.strip()
        if not GITHUB_REPO_RE.match(repo):
            return self._redirect_with_flash(guild_id, "error", "Repository must use owner/repo format.", anchor="poll-state")
        if not await self._github_subscription_exists(guild_id, repo):
            return self._redirect_with_flash(guild_id, "error", "Reset is only available for repos subscribed in this guild.", anchor="poll-state")
        await self._reset_github_poll_state(repo)
        return self._redirect_with_flash(guild_id, "success", f"Reset poll state for {repo}.", anchor="poll-state")

    async def integrations_github_workflow_save(
        self,
        request: Request,
        guild_id: int = Form(...),
        default_repo: str = Form(""),
        review_digest_channel: str = Form(""),
        review_digest_hour_utc: str = Form("13"),
        review_digest_repo: str = Form(""),
        review_digest_stale_hours: str = Form("24"),
        issue_default_template: str = Form(""),
        issue_template_bug: str = Form(""),
        issue_template_feature: str = Form(""),
        issue_template_docs: str = Form(""),
        issue_template_labels_bug: str = Form(""),
        issue_template_labels_feature: str = Form(""),
        issue_template_labels_docs: str = Form(""),
        issue_template_assignees_bug: str = Form(""),
        issue_template_assignees_feature: str = Form(""),
        issue_template_assignees_docs: str = Form(""),
        issue_template_milestone_bug: str = Form(""),
        issue_template_milestone_feature: str = Form(""),
        issue_template_milestone_docs: str = Form(""),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)

        default_repo = default_repo.strip()
        review_digest_channel = review_digest_channel.strip()
        review_digest_hour_utc = review_digest_hour_utc.strip() or "13"
        review_digest_repo = review_digest_repo.strip()
        review_digest_stale_hours = review_digest_stale_hours.strip() or "24"
        issue_default_template = issue_default_template.strip()
        workflow_form = {
            "default_repo": default_repo,
            "review_digest_channel": review_digest_channel,
            "review_digest_hour_utc": review_digest_hour_utc,
            "review_digest_repo": review_digest_repo,
            "review_digest_stale_hours": review_digest_stale_hours,
            "issue_default_template": issue_default_template,
            "issue_templates": {
                "bug": issue_template_bug.strip(),
                "feature": issue_template_feature.strip(),
                "docs": issue_template_docs.strip(),
            },
            "issue_template_labels": {
                "bug": issue_template_labels_bug.strip(),
                "feature": issue_template_labels_feature.strip(),
                "docs": issue_template_labels_docs.strip(),
            },
            "issue_template_assignees": {
                "bug": issue_template_assignees_bug.strip(),
                "feature": issue_template_assignees_feature.strip(),
                "docs": issue_template_assignees_docs.strip(),
            },
            "issue_template_milestones": {
                "bug": issue_template_milestone_bug.strip(),
                "feature": issue_template_milestone_feature.strip(),
                "docs": issue_template_milestone_docs.strip(),
            },
        }

        if default_repo and not GITHUB_REPO_RE.match(default_repo):
            return self._redirect_with_form_error(
                guild_id,
                "Default repo must use owner/repo format.",
                form_name="workflow",
                values=workflow_form,
                anchor="workflow-settings",
            )
        if review_digest_repo and not GITHUB_REPO_RE.match(review_digest_repo):
            return self._redirect_with_form_error(guild_id, "Digest repo must use owner/repo format.", form_name="workflow", values=workflow_form, anchor="workflow-settings")
        if review_digest_channel and not review_digest_channel.isdigit():
            return self._redirect_with_form_error(guild_id, "Digest channel must be a numeric Discord channel ID.", form_name="workflow", values=workflow_form, anchor="workflow-settings")
        if not review_digest_hour_utc.isdigit() or not 0 <= int(review_digest_hour_utc) <= 23:
            return self._redirect_with_form_error(guild_id, "Digest hour must be between 0 and 23 UTC.", form_name="workflow", values=workflow_form, anchor="workflow-settings")
        if not review_digest_stale_hours.isdigit() or int(review_digest_stale_hours) < 1:
            return self._redirect_with_form_error(guild_id, "Digest stale hours must be at least 1.", form_name="workflow", values=workflow_form, anchor="workflow-settings")
        if issue_default_template and issue_default_template not in ISSUE_TEMPLATE_KEYS:
            return self._redirect_with_form_error(guild_id, "Choose a valid default issue template.", form_name="workflow", values=workflow_form, anchor="workflow-settings")
        milestone_values = {
            "github_issue_template_milestone_bug": issue_template_milestone_bug.strip(),
            "github_issue_template_milestone_feature": issue_template_milestone_feature.strip(),
            "github_issue_template_milestone_docs": issue_template_milestone_docs.strip(),
        }
        for key, value in milestone_values.items():
            if value and not value.isdigit():
                label = key.removeprefix("github_issue_template_milestone_").replace("_", " ")
                return self._redirect_with_form_error(guild_id, f"{label.title()} milestone must be numeric.", form_name="workflow", values=workflow_form, anchor="workflow-settings")

        if any(milestone_values.values()):
            config_values = await self.get_guild_config_map(guild_id)
            milestone_repo = default_repo or config_values.get("github_default_repo", "").strip()
            if not milestone_repo:
                return self._redirect_with_form_error(
                    guild_id,
                    "Set a default repo before saving issue template milestone defaults.",
                    form_name="workflow",
                    values=workflow_form,
                    anchor="workflow-settings",
                )
            if not GITHUB_REPO_RE.match(milestone_repo):
                return self._redirect_with_form_error(
                    guild_id,
                    "Default repo must use owner/repo format before milestones can be validated.",
                    form_name="workflow",
                    values=workflow_form,
                    anchor="workflow-settings",
                )
            try:
                await self._validate_issue_template_milestones(milestone_repo, milestone_values)
            except HTTPException as exc:
                return self._redirect_with_form_error(guild_id, str(exc.detail), form_name="workflow", values=workflow_form, anchor="workflow-settings")

        workflow_values = {
            "github_default_repo": default_repo,
            "github_review_digest_channel": review_digest_channel,
            "github_review_digest_hour_utc": review_digest_hour_utc,
            "github_review_digest_repo": review_digest_repo,
            "github_review_digest_stale_hours": review_digest_stale_hours,
            "github_issue_default_template": issue_default_template,
            "github_issue_template_bug": issue_template_bug.strip(),
            "github_issue_template_feature": issue_template_feature.strip(),
            "github_issue_template_docs": issue_template_docs.strip(),
            "github_issue_template_labels_bug": issue_template_labels_bug.strip(),
            "github_issue_template_labels_feature": issue_template_labels_feature.strip(),
            "github_issue_template_labels_docs": issue_template_labels_docs.strip(),
            "github_issue_template_assignees_bug": issue_template_assignees_bug.strip(),
            "github_issue_template_assignees_feature": issue_template_assignees_feature.strip(),
            "github_issue_template_assignees_docs": issue_template_assignees_docs.strip(),
            **milestone_values,
        }

        for key, value in workflow_values.items():
            await self._save_config_value(guild_id, key, value)

        return self._redirect_with_flash(guild_id, "success", "Saved GitHub workflow settings.", anchor="workflow-settings")

    async def integrations_github_user_link_save(
        self,
        request: Request,
        guild_id: int = Form(...),
        discord_user_id: str = Form(...),
        github_username: str = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)
        discord_user_id = discord_user_id.strip()
        github_username = github_username.strip()
        user_link_form = {
            "discord_user_id": discord_user_id,
            "github_username": github_username,
        }
        if not discord_user_id.isdigit():
            return self._redirect_with_form_error(guild_id, "Discord user ID must be numeric.", form_name="user_link", values=user_link_form, anchor="reviewer-links")
        if not github_username:
            return self._redirect_with_form_error(guild_id, "GitHub username is required.", form_name="user_link", values=user_link_form, anchor="reviewer-links")
        await self._save_config_value(guild_id, f"github_username_{discord_user_id}", github_username)
        return self._redirect_with_flash(guild_id, "success", f"Linked Discord user {discord_user_id} to {github_username}.", anchor="reviewer-links")

    async def integrations_github_user_link_delete(
        self,
        request: Request,
        guild_id: int = Form(...),
        discord_user_id: str = Form(...),
    ):
        if redirect := self.auth_redirect(request):
            return redirect
        await self.require_guild_access(request, guild_id)
        discord_user_id = discord_user_id.strip()
        if not discord_user_id.isdigit():
            return self._redirect_with_flash(guild_id, "error", "Discord user ID must be numeric.", anchor="reviewer-links")
        await self.db_execute(
            "DELETE FROM guild_config WHERE guild_id = ? AND key = ?",
            (guild_id, f"github_username_{discord_user_id}"),
        )
        return self._redirect_with_flash(guild_id, "success", f"Removed reviewer link for Discord user {discord_user_id}.", anchor="reviewer-links")