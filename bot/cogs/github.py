"""GitHub integration cog.

Features
--------
Repo Monitoring (polling every 60 s)
  - Pushes, Pull Requests, Issues, Releases posted as rich Discord embeds.
  - Per-guild, per-channel subscriptions stored in the DB.
  - Conditional HTTP requests using ETags to avoid burning rate-limit quota.

GitHub API Slash Commands (/github …)
  - repo       — rich repo overview embed.
  - user       — GitHub user profile.
  - issue      — look up a specific issue/PR by number.
  - issues     — list open issues for a repo.
  - prs        — list open pull-requests for a repo.
  - releases   — latest releases for a repo.
  - search     — search repositories on GitHub.
  - ratelimit  — show current API rate-limit status.

Subscription Management (/github subscribe/unsubscribe/subscriptions)

RAG Ingestion (/github ingest)
  - Fetches README + docs/ tree of a repo and ingests into the RAG knowledge
    base (requires the SupportCog / embeddings system to be present).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot.database import Database
    from bot.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
POLL_INTERVAL_SECONDS = 60
MAX_ISSUES_LISTED = 8
MAX_RELEASES_LISTED = 5
MAX_SEARCH_RESULTS = 6
GITHUB_COLOR = 0x24292E
_VALID_EVENTS = {"push", "pull_request", "issues", "release"}
_DEFAULT_EVENTS = "push,pull_request,issues,release"

# Regex: "owner/repo" — basic validation
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(iso: str | None) -> str:
    """Return a short human-readable date from an ISO-8601 string."""
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]


def _trunc(text: str | None, n: int = 200) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[:n - 1] + "…"


def _repo_color(repo_data: dict) -> int:
    return GITHUB_COLOR


def _make_repo_embed(data: dict) -> discord.Embed:
    em = discord.Embed(
        title=data.get("full_name", ""),
        url=data.get("html_url", ""),
        description=_trunc(data.get("description") or "", 300),
        color=GITHUB_COLOR,
    )
    lang = data.get("language") or "—"
    stars = data.get("stargazers_count", 0)
    forks = data.get("forks_count", 0)
    issues = data.get("open_issues_count", 0)
    watchers = data.get("watchers_count", 0)
    em.add_field(name="Language", value=lang, inline=True)
    em.add_field(name="⭐ Stars", value=f"{stars:,}", inline=True)
    em.add_field(name="🍴 Forks", value=f"{forks:,}", inline=True)
    em.add_field(name="👁️ Watchers", value=f"{watchers:,}", inline=True)
    em.add_field(name="🐛 Open Issues", value=f"{issues:,}", inline=True)
    visibility = "Private 🔒" if data.get("private") else "Public 🌐"
    em.add_field(name="Visibility", value=visibility, inline=True)
    topics = data.get("topics") or []
    if topics:
        em.add_field(name="Topics", value=" · ".join(f"`{t}`" for t in topics[:10]), inline=False)
    license_data = data.get("license") or {}
    license_name = license_data.get("spdx_id") or license_data.get("name") or "—"
    em.set_footer(text=f"License: {license_name}  |  Created {_ts(data.get('created_at'))}  |  Updated {_ts(data.get('updated_at'))}")
    owner = data.get("owner") or {}
    if owner.get("avatar_url"):
        em.set_thumbnail(url=owner["avatar_url"])
    return em


def _make_user_embed(data: dict) -> discord.Embed:
    em = discord.Embed(
        title=data.get("name") or data.get("login", ""),
        url=data.get("html_url", ""),
        description=_trunc(data.get("bio") or "", 300),
        color=GITHUB_COLOR,
    )
    em.set_thumbnail(url=data.get("avatar_url", ""))
    em.add_field(name="Login", value=f"`{data.get('login', '')}`", inline=True)
    em.add_field(name="Public Repos", value=str(data.get("public_repos", 0)), inline=True)
    em.add_field(name="Followers", value=str(data.get("followers", 0)), inline=True)
    em.add_field(name="Following", value=str(data.get("following", 0)), inline=True)
    if data.get("company"):
        em.add_field(name="Company", value=data["company"], inline=True)
    if data.get("location"):
        em.add_field(name="Location", value=data["location"], inline=True)
    if data.get("blog"):
        em.add_field(name="Website", value=data["blog"], inline=False)
    em.set_footer(text=f"Member since {_ts(data.get('created_at'))}")
    return em


# ---------------------------------------------------------------------------
# GitHub API client (async, token-aware)
# ---------------------------------------------------------------------------

class GitHubClient:
    """Minimal async GitHub REST API client."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    def _headers(self, extra: dict | None = None) -> dict:
        h: dict = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DiscordBot-GitHubCog/1.0",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if extra:
            h.update(extra)
        return h

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def get(self, path: str, *, extra_headers: dict | None = None) -> tuple[int, dict | list | None, dict]:
        """GET *path* (relative to GITHUB_API). Returns (status, body, response_headers)."""
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        session = await self._session_get()
        try:
            async with session.get(
                url,
                headers=self._headers(extra_headers),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp_headers = dict(resp.headers)
                if resp.status == 304:
                    return 304, None, resp_headers
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = None
                return resp.status, body, resp_headers
        except Exception as exc:
            logger.warning("GitHub API error for %s: %s", url, exc)
            return 0, None, {}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Notification embed builders
# ---------------------------------------------------------------------------

def _push_embed(repo: str, payload: dict) -> discord.Embed:
    ref = payload.get("ref", "")
    branch = ref.split("/")[-1] if "/" in ref else ref
    commits = payload.get("commits") or []
    pusher = payload.get("pusher", {}).get("name", "someone")
    head = payload.get("head_commit") or {}
    repo_url = f"https://github.com/{repo}"
    em = discord.Embed(
        title=f"📦 Push to `{repo}` on `{branch}`",
        url=f"{repo_url}/tree/{branch}",
        color=0x2DA44E,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=pusher, url=f"https://github.com/{pusher}")
    lines = []
    for c in commits[:5]:
        sha = c.get("id", "")[:7]
        msg = _trunc(c.get("message", "").splitlines()[0], 72)
        url = c.get("url", "")
        lines.append(f"[`{sha}`]({url}) {msg}")
    if len(commits) > 5:
        lines.append(f"…and {len(commits) - 5} more")
    em.description = "\n".join(lines) or _trunc(head.get("message", ""), 200)
    em.set_footer(text=f"{repo}  •  {len(commits)} commit(s)")
    return em


def _pr_embed(repo: str, payload: dict) -> discord.Embed | None:
    action = payload.get("action", "")
    if action not in ("opened", "closed", "reopened", "merged"):
        return None
    pr = payload.get("pull_request") or {}
    if action == "closed" and pr.get("merged"):
        action = "merged"
    color_map = {"opened": 0x2DA44E, "closed": 0xCF222E, "reopened": 0x2DA44E, "merged": 0x8250DF}
    color = color_map.get(action, GITHUB_COLOR)
    icon_map = {"opened": "🟢", "closed": "🔴", "reopened": "🟢", "merged": "🟣"}
    icon = icon_map.get(action, "⚪")
    sender = payload.get("sender", {}).get("login", "")
    em = discord.Embed(
        title=f"{icon} PR #{pr.get('number')} {action}: {_trunc(pr.get('title', ''), 80)}",
        url=pr.get("html_url", ""),
        description=_trunc(pr.get("body") or "", 300),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=sender, url=f"https://github.com/{sender}",
                  icon_url=payload.get("sender", {}).get("avatar_url"))
    base = pr.get("base", {}).get("label", "")
    head = pr.get("head", {}).get("label", "")
    em.add_field(name="Branch", value=f"`{head}` → `{base}`", inline=True)
    em.add_field(name="Changed Files", value=str(pr.get("changed_files", "?")), inline=True)
    em.add_field(name="Commits", value=str(pr.get("commits", "?")), inline=True)
    em.set_footer(text=repo)
    return em


def _issue_embed(repo: str, payload: dict) -> discord.Embed | None:
    action = payload.get("action", "")
    if action not in ("opened", "closed", "reopened"):
        return None
    issue = payload.get("issue") or {}
    color_map = {"opened": 0x2DA44E, "closed": 0xCF222E, "reopened": 0x2DA44E}
    icon_map = {"opened": "🟢", "closed": "🔴", "reopened": "🟢"}
    sender = payload.get("sender", {}).get("login", "")
    em = discord.Embed(
        title=f"{icon_map.get(action, '⚪')} Issue #{issue.get('number')} {action}: {_trunc(issue.get('title', ''), 80)}",
        url=issue.get("html_url", ""),
        description=_trunc(issue.get("body") or "", 300),
        color=color_map.get(action, GITHUB_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=sender, url=f"https://github.com/{sender}",
                  icon_url=payload.get("sender", {}).get("avatar_url"))
    labels = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]
    if labels:
        em.add_field(name="Labels", value=", ".join(f"`{l}`" for l in labels[:6]), inline=False)
    em.set_footer(text=repo)
    return em


def _release_embed(repo: str, payload: dict) -> discord.Embed | None:
    action = payload.get("action", "")
    if action not in ("published", "released"):
        return None
    release = payload.get("release") or {}
    sender = payload.get("sender", {}).get("login", "")
    em = discord.Embed(
        title=f"🚀 Release: {_trunc(release.get('name') or release.get('tag_name', ''), 80)}",
        url=release.get("html_url", ""),
        description=_trunc(release.get("body") or "", 400),
        color=0xFBCA04,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=sender, url=f"https://github.com/{sender}",
                  icon_url=payload.get("sender", {}).get("avatar_url"))
    em.add_field(name="Tag", value=f"`{release.get('tag_name', '?')}`", inline=True)
    em.add_field(name="Pre-release", value="Yes" if release.get("prerelease") else "No", inline=True)
    assets = release.get("assets") or []
    if assets:
        em.add_field(name="Assets", value=str(len(assets)), inline=True)
    em.set_footer(text=repo)
    return em


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class GitHubCog(commands.Cog, name="GitHub"):
    """Full GitHub integration: monitoring, API queries, and RAG ingestion."""

    def __init__(self, bot: commands.Bot, db: "Database", config: "Config") -> None:
        self.bot = bot
        self.db = db
        self.gh = GitHubClient(token=config.github_token)
        self._poll_task_started = False

    # ------------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self._poller.start()
        self._poll_task_started = True
        logger.info("GitHubCog loaded — poller started")

    async def cog_unload(self) -> None:
        self._poller.cancel()
        await self.gh.close()

    # ------------------------------------------------------------------ poller

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poller(self) -> None:
        try:
            await self._poll_all()
        except Exception as exc:
            logger.exception("GitHub poller error: %s", exc)

    @_poller.before_loop
    async def _before_poller(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_all(self) -> None:
        subs = await self.db.get_all_github_subscriptions()
        if not subs:
            return

        # Deduplicate repos so we don't poll the same repo multiple times
        repos_seen: set[str] = set()
        repo_subs: dict[str, list] = {}
        for sub in subs:
            repo = sub["repo"]
            repo_subs.setdefault(repo, []).append(sub)
            repos_seen.add(repo)

        for repo, subscribers in repo_subs.items():
            await self._poll_repo(repo, subscribers)

    async def _poll_repo(self, repo: str, subscribers: list) -> None:
        """Poll GitHub events endpoint for a repo and dispatch to subscribed channels."""
        state = await self.db.get_github_poll_state(repo, "events")
        etag = state["etag"] if state else None
        last_id = state["last_id"] if state else None

        extra_headers: dict = {}
        if etag:
            extra_headers["If-None-Match"] = etag

        status, body, resp_headers = await self.gh.get(
            f"/repos/{repo}/events?per_page=30",
            extra_headers=extra_headers,
        )

        new_etag = resp_headers.get("ETag") or resp_headers.get("etag")
        if status == 304 or body is None:
            return
        if status != 200 or not isinstance(body, list):
            return

        events = body  # list newest-first
        if not events:
            await self.db.set_github_poll_state(repo, "events", last_id, new_etag)
            return

        newest_id = str(events[0].get("id", ""))

        # Collect new events (stop at last_id)
        new_events: list[dict] = []
        for ev in events:
            eid = str(ev.get("id", ""))
            if last_id and eid == last_id:
                break
            new_events.append(ev)

        await self.db.set_github_poll_state(repo, "events", newest_id, new_etag)

        # Process newest-last so embeds appear in chronological order
        for ev in reversed(new_events):
            await self._dispatch_event(repo, ev, subscribers)

    async def _dispatch_event(self, repo: str, event: dict, subscribers: list) -> None:
        event_type = event.get("type", "")
        payload = event.get("payload") or {}

        embed: discord.Embed | None = None

        if event_type == "PushEvent":
            embed = _push_embed(repo, payload)
            event_key = "push"
        elif event_type == "PullRequestEvent":
            embed = _pr_embed(repo, payload)
            event_key = "pull_request"
        elif event_type == "IssuesEvent":
            embed = _issue_embed(repo, payload)
            event_key = "issues"
        elif event_type == "ReleaseEvent":
            embed = _release_embed(repo, payload)
            event_key = "release"
        else:
            return

        if embed is None:
            return

        for sub in subscribers:
            sub_events = {e.strip() for e in sub["events"].split(",")}
            if event_key not in sub_events:
                continue
            channel = self.bot.get_channel(sub["channel_id"])
            if channel is None or not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning("GitHub: no permission to send in channel %d", sub["channel_id"])
            except Exception as exc:
                logger.warning("GitHub dispatch error: %s", exc)

    # ------------------------------------------------------------------ slash commands

    github_group = app_commands.Group(name="github", description="GitHub integration commands")

    # ---- repo info

    @github_group.command(name="repo", description="Show information about a GitHub repository.")
    @app_commands.describe(repo="Repository in owner/repo format, e.g. discord/discord-api-docs")
    async def gh_repo(self, interaction: discord.Interaction, repo: str) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format. Use `owner/repo`.", ephemeral=True)
            return
        await interaction.response.defer()
        status, data, _ = await self.gh.get(f"/repos/{repo}")
        if status == 404:
            await interaction.followup.send(f"❌ Repository `{repo}` not found.")
            return
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ GitHub API error. Try again later.")
            return
        await interaction.followup.send(embed=_make_repo_embed(data))

    # ---- user info

    @github_group.command(name="user", description="Show a GitHub user's profile.")
    @app_commands.describe(username="GitHub username")
    async def gh_user(self, interaction: discord.Interaction, username: str) -> None:
        await interaction.response.defer()
        status, data, _ = await self.gh.get(f"/users/{username}")
        if status == 404:
            await interaction.followup.send(f"❌ User `{username}` not found.")
            return
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ GitHub API error.")
            return
        await interaction.followup.send(embed=_make_user_embed(data))

    # ---- single issue / PR

    @github_group.command(name="issue", description="Look up a specific issue or PR by number.")
    @app_commands.describe(repo="owner/repo", number="Issue or PR number")
    async def gh_issue(self, interaction: discord.Interaction, repo: str, number: int) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format.", ephemeral=True)
            return
        await interaction.response.defer()
        status, data, _ = await self.gh.get(f"/repos/{repo}/issues/{number}")
        if status == 404:
            await interaction.followup.send(f"❌ Issue #{number} not found in `{repo}`.")
            return
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ GitHub API error.")
            return
        is_pr = "pull_request" in data
        kind = "PR" if is_pr else "Issue"
        state = data.get("state", "")
        state_icon = {"open": "🟢", "closed": "🔴"}.get(state, "⚪")
        em = discord.Embed(
            title=f"{state_icon} {kind} #{data.get('number')}: {_trunc(data.get('title', ''), 80)}",
            url=data.get("html_url", ""),
            description=_trunc(data.get("body") or "", 400),
            color=0x2DA44E if state == "open" else 0xCF222E,
            timestamp=datetime.now(timezone.utc),
        )
        user = data.get("user") or {}
        em.set_author(name=user.get("login", ""), url=user.get("html_url", ""),
                      icon_url=user.get("avatar_url"))
        labels = [lbl.get("name", "") for lbl in (data.get("labels") or [])]
        if labels:
            em.add_field(name="Labels", value=" ".join(f"`{l}`" for l in labels[:8]), inline=False)
        if data.get("assignees"):
            assignees = ", ".join(a["login"] for a in data["assignees"][:5])
            em.add_field(name="Assignees", value=assignees, inline=True)
        em.set_footer(text=f"{repo}  •  {data.get('comments', 0)} comment(s)")
        await interaction.followup.send(embed=em)

    # ---- list open issues

    @github_group.command(name="issues", description="List open issues for a repository.")
    @app_commands.describe(repo="owner/repo", label="Filter by label (optional)")
    async def gh_issues(self, interaction: discord.Interaction, repo: str, label: str | None = None) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format.", ephemeral=True)
            return
        await interaction.response.defer()
        path = f"/repos/{repo}/issues?state=open&per_page={MAX_ISSUES_LISTED}&pulls=false"
        if label:
            path += f"&labels={label}"
        status, data, _ = await self.gh.get(path)
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitHub API error.")
            return
        # Filter out pull requests (GitHub issues endpoint returns both)
        issues = [i for i in data if "pull_request" not in i][:MAX_ISSUES_LISTED]
        if not issues:
            await interaction.followup.send(f"✅ No open issues in `{repo}`" + (f" with label `{label}`" if label else "") + ".")
            return
        em = discord.Embed(
            title=f"🐛 Open Issues — {repo}",
            url=f"https://github.com/{repo}/issues",
            color=GITHUB_COLOR,
        )
        for issue in issues:
            labels = ", ".join(lbl["name"] for lbl in (issue.get("labels") or [])[:3])
            label_str = f"  `{labels}`" if labels else ""
            em.add_field(
                name=f"#{issue.get('number')} — {_trunc(issue.get('title', ''), 55)}",
                value=f"[View]({issue.get('html_url', '')}){label_str}  •  by `{(issue.get('user') or {}).get('login', '?')}`",
                inline=False,
            )
        em.set_footer(text=f"Showing up to {MAX_ISSUES_LISTED} open issues")
        await interaction.followup.send(embed=em)

    # ---- list open PRs

    @github_group.command(name="prs", description="List open pull requests for a repository.")
    @app_commands.describe(repo="owner/repo")
    async def gh_prs(self, interaction: discord.Interaction, repo: str) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format.", ephemeral=True)
            return
        await interaction.response.defer()
        status, data, _ = await self.gh.get(f"/repos/{repo}/pulls?state=open&per_page={MAX_ISSUES_LISTED}")
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitHub API error.")
            return
        if not data:
            await interaction.followup.send(f"✅ No open pull requests in `{repo}`.")
            return
        em = discord.Embed(
            title=f"🔀 Open Pull Requests — {repo}",
            url=f"https://github.com/{repo}/pulls",
            color=0x2DA44E,
        )
        for pr in data[:MAX_ISSUES_LISTED]:
            base = (pr.get("base") or {}).get("label", "?")
            head = (pr.get("head") or {}).get("label", "?")
            em.add_field(
                name=f"#{pr.get('number')} — {_trunc(pr.get('title', ''), 55)}",
                value=f"[View]({pr.get('html_url', '')})  •  `{head}` → `{base}`  •  by `{(pr.get('user') or {}).get('login', '?')}`",
                inline=False,
            )
        em.set_footer(text=f"Showing up to {MAX_ISSUES_LISTED} open PRs")
        await interaction.followup.send(embed=em)

    # ---- releases

    @github_group.command(name="releases", description="Show the latest releases for a repository.")
    @app_commands.describe(repo="owner/repo")
    async def gh_releases(self, interaction: discord.Interaction, repo: str) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format.", ephemeral=True)
            return
        await interaction.response.defer()
        status, data, _ = await self.gh.get(f"/repos/{repo}/releases?per_page={MAX_RELEASES_LISTED}")
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitHub API error.")
            return
        if not data:
            await interaction.followup.send(f"ℹ️ No releases found for `{repo}`.")
            return
        em = discord.Embed(
            title=f"🚀 Releases — {repo}",
            url=f"https://github.com/{repo}/releases",
            color=0xFBCA04,
        )
        for rel in data[:MAX_RELEASES_LISTED]:
            tag = rel.get("tag_name", "?")
            name = rel.get("name") or tag
            pre = " · pre-release" if rel.get("prerelease") else ""
            assets = len(rel.get("assets") or [])
            em.add_field(
                name=f"{name}",
                value=f"[`{tag}`]({rel.get('html_url', '')})  •  {_ts(rel.get('published_at'))}  •  {assets} asset(s){pre}",
                inline=False,
            )
        await interaction.followup.send(embed=em)

    # ---- search repos

    @github_group.command(name="search", description="Search GitHub repositories.")
    @app_commands.describe(query="Search query, e.g. 'discord bot python'", sort="Sort order")
    @app_commands.choices(sort=[
        app_commands.Choice(name="Best Match", value=""),
        app_commands.Choice(name="Stars", value="stars"),
        app_commands.Choice(name="Forks", value="forks"),
        app_commands.Choice(name="Recently Updated", value="updated"),
    ])
    async def gh_search(self, interaction: discord.Interaction, query: str, sort: str = "") -> None:
        await interaction.response.defer()
        path = f"/search/repositories?q={query}&per_page={MAX_SEARCH_RESULTS}"
        if sort:
            path += f"&sort={sort}&order=desc"
        status, data, _ = await self.gh.get(path)
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ GitHub API error.")
            return
        items = data.get("items") or []
        total = data.get("total_count", 0)
        if not items:
            await interaction.followup.send(f"🔍 No repositories found for `{query}`.")
            return
        em = discord.Embed(
            title=f"🔍 GitHub Repo Search: {_trunc(query, 50)}",
            description=f"{total:,} total results",
            color=GITHUB_COLOR,
        )
        for item in items[:MAX_SEARCH_RESULTS]:
            lang = item.get("language") or "—"
            stars = item.get("stargazers_count", 0)
            em.add_field(
                name=item.get("full_name", "?"),
                value=f"[View]({item.get('html_url', '')})  •  ⭐ {stars:,}  •  {lang}\n{_trunc(item.get('description') or '', 80)}",
                inline=False,
            )
        await interaction.followup.send(embed=em)

    # ---- rate limit

    @github_group.command(name="ratelimit", description="Show GitHub API rate-limit status.")
    async def gh_ratelimit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        status, data, _ = await self.gh.get("/rate_limit")
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ Could not fetch rate-limit info.", ephemeral=True)
            return
        core = (data.get("resources") or {}).get("core") or data.get("rate") or {}
        search = (data.get("resources") or {}).get("search") or {}
        remaining = core.get("remaining", "?")
        limit = core.get("limit", "?")
        reset_ts = core.get("reset")
        reset_str = f"<t:{reset_ts}:R>" if reset_ts else "unknown"
        em = discord.Embed(title="🐙 GitHub API Rate Limit", color=GITHUB_COLOR)
        em.add_field(name="Core", value=f"{remaining} / {limit} remaining\nResets {reset_str}", inline=True)
        if search:
            em.add_field(
                name="Search",
                value=f"{search.get('remaining', '?')} / {search.get('limit', '?')} remaining",
                inline=True,
            )
        token_status = "✅ Authenticated" if self.gh._token else "⚠️ Unauthenticated (60 req/hr)"
        em.set_footer(text=token_status)
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------ subscription commands

    @github_group.command(name="subscribe", description="Subscribe a channel to GitHub repo notifications.")
    @app_commands.describe(
        repo="Repository in owner/repo format",
        channel="Channel to post notifications in (default: current channel)",
        events="Comma-separated event types: push,pull_request,issues,release",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gh_subscribe(
        self,
        interaction: discord.Interaction,
        repo: str,
        channel: discord.TextChannel | None = None,
        events: str = _DEFAULT_EVENTS,
    ) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format. Use `owner/repo`.", ephemeral=True)
            return

        # Validate event names
        parsed_events = {e.strip().lower() for e in events.split(",")}
        invalid = parsed_events - _VALID_EVENTS
        if invalid:
            await interaction.response.send_message(
                f"❌ Unknown event type(s): {', '.join(invalid)}.\nValid: `push`, `pull_request`, `issues`, `release`.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ Target must be a text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Verify repo exists on GitHub
        status, _, _ = await self.gh.get(f"/repos/{repo}")
        if status == 404:
            await interaction.followup.send(f"❌ Repository `{repo}` not found on GitHub.", ephemeral=True)
            return
        if status not in (200, 301):
            await interaction.followup.send("❌ Could not verify repository — GitHub API error.", ephemeral=True)
            return

        events_str = ",".join(sorted(parsed_events))
        added = await self.db.add_github_subscription(
            guild_id=interaction.guild_id,  # type: ignore[arg-type]
            channel_id=target.id,
            repo=repo,
            events=events_str,
            added_by=interaction.user.id,
        )
        if not added:
            # Update existing subscription's event list
            await self.db.update_github_subscription_events(
                guild_id=interaction.guild_id,  # type: ignore[arg-type]
                channel_id=target.id,
                repo=repo,
                events=events_str,
            )
            await interaction.followup.send(
                f"✅ Updated subscription for `{repo}` in {target.mention} — events: `{events_str}`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Subscribed {target.mention} to `{repo}` — watching: `{events_str}`.",
                ephemeral=True,
            )

    @github_group.command(name="unsubscribe", description="Remove a GitHub repo subscription from a channel.")
    @app_commands.describe(
        repo="Repository in owner/repo format",
        channel="Channel the subscription is in (default: current channel)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gh_unsubscribe(
        self,
        interaction: discord.Interaction,
        repo: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ Target must be a text channel.", ephemeral=True)
            return
        removed = await self.db.remove_github_subscription(
            guild_id=interaction.guild_id,  # type: ignore[arg-type]
            channel_id=target.id,
            repo=repo,
        )
        if removed:
            await interaction.response.send_message(
                f"✅ Unsubscribed {target.mention} from `{repo}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ No subscription found for `{repo}` in {target.mention}.", ephemeral=True
            )

    @github_group.command(name="subscriptions", description="List all GitHub subscriptions in this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def gh_subscriptions(self, interaction: discord.Interaction) -> None:
        subs = await self.db.get_github_subscriptions(interaction.guild_id)  # type: ignore[arg-type]
        if not subs:
            await interaction.response.send_message("ℹ️ No GitHub subscriptions configured.", ephemeral=True)
            return
        em = discord.Embed(title="🐙 GitHub Subscriptions", color=GITHUB_COLOR)
        for sub in subs:
            ch = self.bot.get_channel(sub["channel_id"])
            ch_str = ch.mention if ch else f"<#{sub['channel_id']}>"
            em.add_field(
                name=sub["repo"],
                value=f"Channel: {ch_str}\nEvents: `{sub['events']}`\nAdded: {_ts(sub['created_at'])}",
                inline=True,
            )
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ------------------------------------------------------------------ RAG ingestion

    @github_group.command(name="ingest", description="Ingest a GitHub repo's README/docs into the AI knowledge base.")
    @app_commands.describe(
        repo="Repository in owner/repo format",
        branch="Branch to ingest from (default: main)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gh_ingest(
        self,
        interaction: discord.Interaction,
        repo: str,
        branch: str = "main",
    ) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format.", ephemeral=True)
            return

        support_cog = self.bot.get_cog("Support")
        if support_cog is None:
            await interaction.response.send_message(
                "❌ The Support/AI cog is not loaded — cannot ingest embeddings.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Collect docs to ingest: README + files in docs/ at root level
        docs_to_fetch: list[tuple[str, str]] = []  # (label, raw_url)

        # README
        for readme_name in ("README.md", "README.rst", "README.txt", "README"):
            raw_url = f"{GITHUB_RAW}/{repo}/{branch}/{readme_name}"
            docs_to_fetch.append((f"{repo}/README", raw_url))
            break  # try only the most common one first; we check status below

        # docs/ tree
        status, tree_data, _ = await self.gh.get(
            f"/repos/{repo}/git/trees/{branch}?recursive=1"
        )
        if status == 200 and isinstance(tree_data, dict):
            for item in (tree_data.get("tree") or []):
                path: str = item.get("path", "")
                if not isinstance(path, str):
                    continue
                if item.get("type") != "blob":
                    continue
                lower = path.lower()
                # Include markdown/text files from docs/, wiki/, .github/
                if any(lower.startswith(p) for p in ("docs/", "wiki/", ".github/", "doc/")):
                    if lower.endswith((".md", ".rst", ".txt")):
                        raw_url = f"{GITHUB_RAW}/{repo}/{branch}/{path}"
                        label = f"{repo}/{path}"
                        docs_to_fetch.append((label, raw_url))
                        if len(docs_to_fetch) >= 30:  # safety cap
                            break

        if not docs_to_fetch:
            await interaction.followup.send("❌ No README or docs files found in that repo.", ephemeral=True)
            return

        # Use the LLM service and DB from the support cog
        llm = getattr(support_cog, "llm", None)
        guild_id = interaction.guild_id

        ingested = 0
        skipped = 0
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            for label, raw_url in docs_to_fetch:
                try:
                    async with session.get(
                        raw_url,
                        timeout=aiohttp.ClientTimeout(total=15),
                        headers={"User-Agent": "DiscordBot-GitHubCog/1.0"},
                    ) as resp:
                        if resp.status != 200:
                            skipped += 1
                            continue
                        text = await resp.text(errors="replace")
                except Exception:
                    skipped += 1
                    continue

                if not text.strip():
                    skipped += 1
                    continue

                # Generate embedding if LLM service is available
                embedding_bytes: bytes | None = None
                model: str | None = None
                if llm is not None:
                    try:
                        from bot.llm_service import LLMService  # local import to avoid circular
                        vec = await llm.get_embedding(text[:8000])
                        if vec:
                            import struct
                            embedding_bytes = struct.pack(f"{len(vec)}f", *vec)
                            model = getattr(llm, "_embedding_model", None)
                    except Exception as emb_exc:
                        logger.debug("Embedding failed for %s: %s", label, emb_exc)

                # Upsert into embeddings table
                added = await self.db.add_embedding(
                    guild_id=guild_id,  # type: ignore[arg-type]
                    name=label,
                    text=text[:12000],
                    embedding=embedding_bytes,
                    model=model,
                    source_url=raw_url,
                )
                if not added:
                    await self.db.update_embedding(
                        guild_id=guild_id,  # type: ignore[arg-type]
                        name=label,
                        text=text[:12000],
                        embedding=embedding_bytes,
                        model=model,
                        source_url=raw_url,
                    )
                ingested += 1

        msg = f"✅ Ingested **{ingested}** file(s) from `{repo}` into the knowledge base."
        if skipped:
            msg += f" ({skipped} skipped / not found)"
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Called by discord.py if loading via load_extension."""
    pass  # Loaded manually in main.py with db and config injected
