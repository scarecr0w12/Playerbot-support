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

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

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
MAX_REVIEW_QUEUE_PRS = 10
MAX_TRIAGE_ITEMS = 5
DEFAULT_REVIEW_DIGEST_HOUR_UTC = 13
ISSUE_TEMPLATE_KEYS = ("bug", "feature", "docs")
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


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _requested_reviewer_names(pr_data: dict) -> list[str]:
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


def _summarize_reviews(reviews: list[dict]) -> tuple[int, bool]:
    latest_by_user: dict[str, tuple[datetime, str]] = {}
    for review in reviews:
        user = review.get("user") or {}
        login = user.get("login")
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


def _review_bucket(pr_data: dict, reviews: list[dict], stale_cutoff: datetime) -> str:
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


def _review_value(pr_data: dict, reviews: list[dict]) -> str:
    author = (pr_data.get("user") or {}).get("login", "?")
    updated_at = _ts(pr_data.get("updated_at"))
    requested = _requested_reviewer_names(pr_data)
    approvals, changes_requested = _summarize_reviews(reviews)
    parts = [f"[View]({pr_data.get('html_url', '')})  •  by `{author}`  •  updated {updated_at}"]
    if requested:
        parts.append(f"Requested: {', '.join(f'`{name}`' for name in requested[:4])}")
    if changes_requested:
        parts.append("Status: `changes requested`")
    elif approvals:
        parts.append(f"Approvals: `{approvals}`")
    return "\n".join(parts)


def _review_load_lines(
    queue: list[tuple[dict, list[dict]]],
    stale_cutoff: datetime,
    *,
    teams: bool = False,
) -> list[str]:
    review_load: dict[str, dict[str, Any]] = {}
    for pr_data, reviews in queue:
        if _review_bucket(pr_data, reviews, stale_cutoff) != "review_requested":
            continue
        updated_at = _parse_iso_dt(pr_data.get("updated_at")) or datetime.now(timezone.utc)
        number = pr_data.get("number")
        title = _trunc(pr_data.get("title", ""), 40)
        for reviewer in _requested_reviewer_names(pr_data):
            is_team = reviewer.startswith("team:")
            if is_team != teams:
                continue
            display_name = reviewer.removeprefix("team:") if is_team else reviewer
            info = review_load.setdefault(
                display_name,
                {"count": 0, "oldest": updated_at, "number": number, "title": title},
            )
            info["count"] += 1
            if updated_at <= info["oldest"]:
                info["oldest"] = updated_at
                info["number"] = number
                info["title"] = title

    lines = []
    for reviewer, info in sorted(review_load.items(), key=lambda item: (-item[1]["count"], item[1]["oldest"]))[:5]:
        lines.append(
            f"`{reviewer}`  •  {info['count']} pending  •  oldest #{info['number']} {_ts(info['oldest'].isoformat())}"
        )
    return lines


def _reviewer_load_lines(
    queue: list[tuple[dict, list[dict]]],
    stale_cutoff: datetime,
) -> list[str]:
    return _review_load_lines(queue, stale_cutoff, teams=False)


def _team_load_lines(
    queue: list[tuple[dict, list[dict]]],
    stale_cutoff: datetime,
) -> list[str]:
    return _review_load_lines(queue, stale_cutoff, teams=True)


def _build_review_queue_embed(
    repo: str,
    buckets: dict[str, list[tuple[dict, list[dict]]]],
    stale_hours: int,
    reviewer_load_lines: list[str] | None = None,
    team_load_lines: list[str] | None = None,
) -> discord.Embed:
    em = discord.Embed(
        title=f"🔎 PR Review Queue — {repo}",
        url=f"https://github.com/{repo}/pulls",
        description=f"Open PRs grouped by review status. Stale threshold: {stale_hours} hour(s).",
        color=0x2DA44E,
    )
    sections = [
        ("review_requested", "Needs Review"),
        ("changes_requested", "Changes Requested"),
        ("approved", "Approved"),
        ("stale", "Stale"),
        ("waiting", "Waiting"),
    ]
    for key, label in sections:
        items = buckets.get(key) or []
        if not items:
            continue
        value = "\n\n".join(_review_value(pr_data, reviews) for pr_data, reviews in items[:MAX_TRIAGE_ITEMS])
        em.add_field(name=f"{label} ({len(items)})", value=value, inline=False)
    if reviewer_load_lines:
        em.add_field(name="Reviewer Load", value="\n".join(reviewer_load_lines), inline=False)
    if team_load_lines:
        em.add_field(name="Team Load", value="\n".join(team_load_lines), inline=False)
    draft_count = len(buckets.get("draft") or [])
    if draft_count:
        em.set_footer(text=f"{draft_count} draft PR(s) hidden from the active queue")
    return em


def _issue_body(summary: str, reproduction: str | None = None, source_message: discord.Message | None = None) -> str:
    parts = ["## Summary", summary.strip() or "No summary provided."]
    if reproduction and reproduction.strip():
        parts.extend(["", "## Reproduction / Notes", reproduction.strip()])
    if source_message is not None:
        guild_id = source_message.guild.id if source_message.guild else "@me"
        source_link = f"https://discord.com/channels/{guild_id}/{source_message.channel.id}/{source_message.id}"
        excerpt = _trunc(source_message.content or "(no message content)", 500)
        parts.extend(
            [
                "",
                "## Discord Context",
                f"- Source message: {source_link}",
                f"- Author: @{getattr(source_message.author, 'display_name', getattr(source_message.author, 'name', 'unknown'))}",
                "",
                "> " + excerpt.replace("\n", "\n> "),
            ]
        )
    return "\n".join(parts).strip()


def _build_issue_triage_embed(repo: str, issues: list[dict], stale_days: int) -> discord.Embed:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    unassigned = [issue for issue in issues if not issue.get("assignees")]
    unlabeled = [issue for issue in issues if not issue.get("labels")]
    stale = [
        issue
        for issue in issues
        if (_parse_iso_dt(issue.get("updated_at")) or datetime.now(timezone.utc)) <= stale_cutoff
    ]

    em = discord.Embed(
        title=f"🧰 Issue Triage — {repo}",
        url=f"https://github.com/{repo}/issues",
        description=(
            f"Open issues: `{len(issues)}`  •  Unassigned: `{len(unassigned)}`  •  "
            f"Unlabeled: `{len(unlabeled)}`  •  Stale: `{len(stale)}`"
        ),
        color=0xFBCA04,
    )

    sections = [
        ("Unassigned", unassigned),
        ("Unlabeled", unlabeled),
        (f"Stale ({stale_days}d)", stale),
    ]
    for label, section_issues in sections:
        if not section_issues:
            continue
        lines = []
        for issue in section_issues[:MAX_TRIAGE_ITEMS]:
            author = (issue.get("user") or {}).get("login", "?")
            lines.append(
                f"[#{issue.get('number')} {_trunc(issue.get('title', ''), 55)}]({issue.get('html_url', '')})"
                f"  •  by `{author}`  •  updated {_ts(issue.get('updated_at'))}"
            )
        em.add_field(name=label, value="\n".join(lines), inline=False)
    return em


def _should_send_review_digest(now: datetime, hour_utc: int, last_sent_on: str | None) -> bool:
    if now.hour < hour_utc:
        return False
    return last_sent_on != now.date().isoformat()


def _default_issue_template(template_key: str) -> str:
    templates = {
        "bug": "Problem summary\n\nExpected behavior\n\nActual behavior\n\nImpact",
        "feature": "Requested change\n\nWhy it matters\n\nAcceptance criteria",
        "docs": "What is unclear\n\nSuggested documentation update\n\nWho is affected",
    }
    return templates.get(template_key, "")


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

    async def request(
        self,
        method: str,
        path: str,
        *,
        extra_headers: dict | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, dict | list | str | None, dict]:
        """Perform an HTTP request relative to the GitHub API."""
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        session = await self._session_get()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(extra_headers),
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp_headers = dict(resp.headers)
                if resp.status == 304:
                    return 304, None, resp_headers
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
                return resp.status, body, resp_headers
        except Exception as exc:
            logger.warning("GitHub API error for %s: %s", url, exc)
            return 0, None, {}

    async def get(self, path: str, *, extra_headers: dict | None = None) -> tuple[int, dict | list | str | None, dict]:
        """GET *path* (relative to GITHUB_API). Returns (status, body, response_headers)."""
        return await self.request("GET", path, extra_headers=extra_headers)

    async def post(self, path: str, *, json_body: dict[str, Any]) -> tuple[int, dict | list | str | None, dict]:
        return await self.request("POST", path, json_body=json_body)

    async def patch(self, path: str, *, json_body: dict[str, Any]) -> tuple[int, dict | list | str | None, dict]:
        return await self.request("PATCH", path, json_body=json_body)

    async def delete(self, path: str, *, json_body: dict[str, Any] | None = None) -> tuple[int, dict | list | str | None, dict]:
        return await self.request("DELETE", path, json_body=json_body)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Notification embed builders
# ---------------------------------------------------------------------------

def _push_embed(repo: str, payload: dict, actor: dict | None = None) -> discord.Embed:
    ref = payload.get("ref", "")
    branch = ref.split("/")[-1] if "/" in ref else ref
    commits = payload.get("commits") or []
    pusher_name = payload.get("pusher", {}).get("name") or (actor or {}).get("login", "someone")
    avatar_url = (actor or {}).get("avatar_url")
    head = payload.get("head_commit") or {}
    repo_url = f"https://github.com/{repo}"
    em = discord.Embed(
        title=f"📦 Push to `{repo}` on `{branch}`",
        url=f"{repo_url}/tree/{branch}",
        color=0x2DA44E,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=pusher_name, url=f"https://github.com/{pusher_name}", icon_url=avatar_url)
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


async def _generate_commit_embeddings(cog: "GitHubCog", repo: str, commits: list[dict], branch: str, pusher_name: str) -> None:
    """Generate detailed embeddings for commits in a push event."""
    support_cog = cog.bot.get_cog("Support")
    if support_cog is None:
        return
    
    llm = getattr(support_cog, "llm", None)
    if llm is None:
        return
    
    repo_url = f"https://github.com/{repo}"
    
    for commit in commits:
        sha = commit.get("id", "")
        if not sha:
            continue
            
        # Extract detailed commit information
        author = commit.get("author", {})
        author_name = author.get("name", "Unknown")
        author_email = author.get("email", "")
        committer = commit.get("committer", {})
        committer_name = committer.get("name", author_name)
        
        message = commit.get("message", "")
        url = commit.get("url", "")
        timestamp = commit.get("timestamp", "")
        
        # Get file changes if available
        added = commit.get("added", [])
        removed = commit.get("removed", [])
        modified = commit.get("modified", [])
        
        # Build detailed commit text for embedding
        commit_details = []
        commit_details.append(f"Commit: {sha[:7]}")
        commit_details.append(f"Repository: {repo}")
        commit_details.append(f"Branch: {branch}")
        commit_details.append(f"Author: {author_name} ({author_email})")
        commit_details.append(f"Committer: {committer_name}")
        if timestamp:
            commit_details.append(f"Timestamp: {timestamp}")
        commit_details.append(f"Pushed by: {pusher_name}")
        commit_details.append(f"URL: {url}")
        commit_details.append("")
        commit_details.append("Commit Message:")
        commit_details.append(message)
        
        if added or removed or modified:
            commit_details.append("")
            commit_details.append("File Changes:")
            if added:
                commit_details.append(f"Added: {', '.join(added)}")
            if removed:
                commit_details.append(f"Removed: {', '.join(removed)}")
            if modified:
                commit_details.append(f"Modified: {', '.join(modified)}")
        
        text = "\n".join(commit_details)
        label = f"commit:{repo}:{sha[:7]}"
        
        try:
            from bot.llm_service import LLMService
            vec = await llm.get_embedding(text[:8000])
            if vec:
                import struct
                embedding_bytes = struct.pack(f"{len(vec)}f", *vec)
                model = getattr(llm, "_embedding_model", None)
                
                # Store embedding for each guild that has this repo subscribed
                subs = await cog.db.get_all_github_subscriptions()
                guild_ids = {sub["guild_id"] for sub in subs if sub["repo"] == repo}
                
                for guild_id in guild_ids:
                    added = await cog.db.add_embedding(
                        guild_id=guild_id,
                        name=label,
                        text=text[:12000],
                        embedding=embedding_bytes,
                        model=model,
                        source_url=url,
                    )
                    if not added:
                        await cog.db.update_embedding(
                            guild_id=guild_id,
                            name=label,
                            text=text[:12000],
                            embedding=embedding_bytes,
                            model=model,
                            source_url=url,
                        )
                        
        except Exception as exc:
            logger.debug("Failed to generate embedding for commit %s: %s", sha[:7], exc)


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


class GitHubIssueModal(discord.ui.Modal):
    def __init__(
        self,
        cog: GitHubCog,
        *,
        repo: str | None = None,
        title_default: str = "",
        summary_default: str = "",
        reproduction_default: str = "",
        labels_default: str = "",
        template_key: str | None = None,
        source_message: discord.Message | None = None,
    ) -> None:
        super().__init__(title="Create GitHub Issue")
        self.cog = cog
        self.repo = repo
        self.template_key = template_key
        self.source_message = source_message

        self.repo_input: discord.ui.TextInput | None = None
        if repo is None:
            self.repo_input = discord.ui.TextInput(
                label="Repository",
                placeholder="owner/repo",
                max_length=120,
            )
            self.add_item(self.repo_input)

        self.title_input = discord.ui.TextInput(
            label="Issue Title",
            placeholder="Short summary of the problem or task",
            default=title_default[:100] if title_default else None,
            max_length=120,
        )
        self.summary_input = discord.ui.TextInput(
            label="Summary",
            style=discord.TextStyle.paragraph,
            placeholder="What needs to be fixed or tracked?",
            default=summary_default[:4000] if summary_default else None,
            max_length=4000,
        )
        self.repro_input = discord.ui.TextInput(
            label="Reproduction / Notes",
            style=discord.TextStyle.paragraph,
            placeholder="Steps, context, links, or debugging notes",
            required=False,
            default=reproduction_default[:4000] if reproduction_default else None,
            max_length=4000,
        )
        self.labels_input = discord.ui.TextInput(
            label="Labels",
            placeholder="bug, docs, backend",
            required=False,
            default=labels_default[:200] if labels_default else None,
            max_length=200,
        )

        self.add_item(self.title_input)
        self.add_item(self.summary_input)
        self.add_item(self.repro_input)
        self.add_item(self.labels_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        repo = self.repo or (self.repo_input.value.strip() if self.repo_input else "")
        await self.cog._submit_issue(
            interaction,
            repo=repo,
            title=self.title_input.value,
            summary=self.summary_input.value,
            reproduction=self.repro_input.value,
            labels_raw=self.labels_input.value,
            template_key=self.template_key,
            source_message=self.source_message,
        )


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
        self._issue_ctx = app_commands.ContextMenu(name="Create GitHub Issue", callback=self._issue_context_menu)
        self.bot.tree.add_command(self._issue_ctx)

    # ------------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self._poller.start()
        self._poll_task_started = True
        logger.info("GitHubCog loaded — poller started")

    async def cog_unload(self) -> None:
        self._poller.cancel()
        self.bot.tree.remove_command(self._issue_ctx.name, type=self._issue_ctx.type)
        await self.gh.close()

    async def _issue_context_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        title = (message.content or "New issue from Discord").splitlines()[0][:100]
        default_repo = await self._get_default_repo(interaction.guild_id)  # type: ignore[arg-type]
        default_template = await self._get_default_issue_template_key(interaction.guild_id)  # type: ignore[arg-type]
        summary_default, reproduction_default = await self._get_issue_template_defaults(
            interaction.guild_id,  # type: ignore[arg-type]
            default_template,
            summary_default=message.content[:4000],
            reproduction_default="",
        )
        labels_default = await self._get_issue_template_labels_text(interaction.guild_id, default_template)  # type: ignore[arg-type]
        await interaction.response.send_modal(
            GitHubIssueModal(
                self,
                repo=default_repo,
                title_default=title,
                summary_default=summary_default,
                reproduction_default=reproduction_default,
                labels_default=labels_default,
                template_key=default_template,
                source_message=message,
            )
        )

    async def _require_github_write_token(self, interaction: discord.Interaction) -> bool:
        if self.gh._token:
            return True
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ A `GITHUB_TOKEN` with repo issue permissions is required for this command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ A `GITHUB_TOKEN` with repo issue permissions is required for this command.",
                ephemeral=True,
            )
        return False

    async def _fetch_pr_reviews(self, repo: str, number: int) -> list[dict]:
        status, data, _ = await self.gh.get(f"/repos/{repo}/pulls/{number}/reviews?per_page=30")
        if status != 200 or not isinstance(data, list):
            return []
        return data

    async def _fetch_review_queue(self, repo: str) -> list[tuple[dict, list[dict]]]:
        status, data, _ = await self.gh.get(
            f"/repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page={MAX_REVIEW_QUEUE_PRS}"
        )
        if status != 200 or not isinstance(data, list):
            return []
        queue: list[tuple[dict, list[dict]]] = []
        for pr_data in data[:MAX_REVIEW_QUEUE_PRS]:
            reviews = await self._fetch_pr_reviews(repo, pr_data.get("number"))
            queue.append((pr_data, reviews))
        return queue

    async def _submit_issue(
        self,
        interaction: discord.Interaction,
        *,
        repo: str,
        title: str,
        summary: str,
        reproduction: str | None,
        labels_raw: str | None,
        template_key: str | None = None,
        source_message: discord.Message | None = None,
    ) -> None:
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format. Use `owner/repo`.", ephemeral=True)
            return
        if not await self._require_github_write_token(interaction):
            return

        labels = sorted({label.strip() for label in (labels_raw or "").split(",") if label.strip()})
        assignees = await self._get_issue_template_assignees(interaction.guild_id, template_key)
        milestone = await self._get_issue_template_milestone(interaction.guild_id, template_key)
        payload = {
            "title": title.strip(),
            "body": _issue_body(summary, reproduction, source_message),
        }
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        if milestone is not None:
            payload["milestone"] = milestone

        await interaction.response.defer(ephemeral=True, thinking=True)
        status, data, _ = await self.gh.post(f"/repos/{repo}/issues", json_body=payload)
        if status not in (200, 201) or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to create the GitHub issue.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Created issue [#{data.get('number')} {data.get('title', 'issue')}]({data.get('html_url', '')}) in `{repo}`.",
            ephemeral=True,
        )

    async def _get_linked_github_username(self, guild_id: int, user_id: int) -> str | None:
        return await self.db.get_guild_config(guild_id, f"github_username_{user_id}")

    async def _get_default_repo(self, guild_id: int | None) -> str | None:
        if guild_id is None:
            return None
        repo = await self.db.get_guild_config(guild_id, "github_default_repo")
        return repo or None

    async def _get_default_issue_template_key(self, guild_id: int | None) -> str | None:
        if guild_id is None:
            return None
        template_key = await self.db.get_guild_config(guild_id, "github_issue_default_template")
        if template_key in ISSUE_TEMPLATE_KEYS:
            return template_key
        return None

    async def _get_issue_template_text(self, guild_id: int | None, template_key: str | None) -> str:
        if guild_id is None or template_key not in ISSUE_TEMPLATE_KEYS:
            return ""
        stored = await self.db.get_guild_config(guild_id, f"github_issue_template_{template_key}")
        return stored or _default_issue_template(template_key)

    async def _get_issue_template_defaults(
        self,
        guild_id: int | None,
        template_key: str | None,
        *,
        summary_default: str = "",
        reproduction_default: str = "",
    ) -> tuple[str, str]:
        template_text = await self._get_issue_template_text(guild_id, template_key)
        if not template_text:
            return summary_default, reproduction_default

        if summary_default.strip():
            return summary_default, template_text
        return template_text, reproduction_default

    async def _get_issue_template_labels_text(self, guild_id: int | None, template_key: str | None) -> str:
        if guild_id is None or template_key not in ISSUE_TEMPLATE_KEYS:
            return ""
        stored = await self.db.get_guild_config(guild_id, f"github_issue_template_labels_{template_key}")
        return stored or ""

    async def _get_issue_template_assignees(self, guild_id: int | None, template_key: str | None) -> list[str]:
        if guild_id is None or template_key not in ISSUE_TEMPLATE_KEYS:
            return []
        stored = await self.db.get_guild_config(guild_id, f"github_issue_template_assignees_{template_key}")
        if not stored:
            return []
        return sorted({assignee.strip() for assignee in stored.split(",") if assignee.strip()})

    async def _get_issue_template_milestone(self, guild_id: int | None, template_key: str | None) -> int | None:
        if guild_id is None or template_key not in ISSUE_TEMPLATE_KEYS:
            return None
        stored = await self.db.get_guild_config(guild_id, f"github_issue_template_milestone_{template_key}")
        if not stored or not stored.strip().isdigit():
            return None
        return int(stored.strip())

    async def _resolve_repo(self, interaction: discord.Interaction, repo: str | None) -> str | None:
        resolved = (repo or "").strip()
        if not resolved:
            resolved = (await self._get_default_repo(interaction.guild_id)) or ""
        if not resolved:
            await interaction.response.send_message(
                "❌ No repo provided and no default repo is configured. Use `/github default_repo <owner/repo>` first.",
                ephemeral=True,
            )
            return None
        if not _REPO_RE.match(resolved):
            await interaction.response.send_message("❌ Invalid repo format. Use `owner/repo`.", ephemeral=True)
            return None
        return resolved

    async def _fetch_issue(self, repo: str, number: int) -> dict | None:
        status, data, _ = await self.gh.get(f"/repos/{repo}/issues/{number}")
        if status != 200 or not isinstance(data, dict):
            return None
        return data

    async def _send_review_digest(self, guild: discord.Guild, channel: discord.TextChannel, repo: str, stale_hours: int) -> bool:
        queue = await self._fetch_review_queue(repo)
        if not queue:
            return False

        stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
        buckets: dict[str, list[tuple[dict, list[dict]]]] = {}
        for pr_data, reviews in queue:
            bucket = _review_bucket(pr_data, reviews, stale_cutoff)
            buckets.setdefault(bucket, []).append((pr_data, reviews))

        reviewer_lines = _reviewer_load_lines(queue, stale_cutoff)
        team_lines = _team_load_lines(queue, stale_cutoff)
        embed = _build_review_queue_embed(repo, buckets, stale_hours, reviewer_lines, team_lines)
        embed.title = f"🗓️ Daily Review Digest — {repo}"
        embed.description = (
            f"Daily PR review summary for **{guild.name}**. "
            f"Stale threshold: {stale_hours} hour(s)."
        )
        await channel.send(embed=embed)
        return True

    async def _maybe_send_review_digests(self) -> None:
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            channel_raw = await self.db.get_guild_config(guild.id, "github_review_digest_channel")
            if not channel_raw:
                continue
            repo = (
                await self.db.get_guild_config(guild.id, "github_review_digest_repo")
                or await self._get_default_repo(guild.id)
            )
            if not repo or not _REPO_RE.match(repo):
                continue

            hour_raw = await self.db.get_guild_config(guild.id, "github_review_digest_hour_utc")
            stale_raw = await self.db.get_guild_config(guild.id, "github_review_digest_stale_hours")
            last_sent_on = await self.db.get_guild_config(guild.id, "github_review_digest_last_sent")
            hour_utc = int(hour_raw) if hour_raw and hour_raw.isdigit() else DEFAULT_REVIEW_DIGEST_HOUR_UTC
            stale_hours = int(stale_raw) if stale_raw and stale_raw.isdigit() else 24
            if not _should_send_review_digest(now, hour_utc, last_sent_on):
                continue

            channel = guild.get_channel(int(channel_raw))
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                sent = await self._send_review_digest(guild, channel, repo, stale_hours)
            except discord.Forbidden:
                logger.warning("GitHub: no permission to send review digest in channel %s", channel_raw)
                continue
            except Exception as exc:
                logger.warning("GitHub review digest error for guild %s: %s", guild.id, exc)
                continue

            if sent:
                await self.db.set_guild_config(guild.id, "github_review_digest_last_sent", now.date().isoformat())

    # ------------------------------------------------------------------ poller

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poller(self) -> None:
        try:
            await self._poll_all()
            await self._maybe_send_review_digests()
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

        if state is None:
            await self.db.set_github_poll_state(repo, "events", newest_id, new_etag)
            logger.info("GitHub poller bootstrap for %s — seeded latest event %s", repo, newest_id)
            return

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
            embed = _push_embed(repo, payload, actor=event.get("actor"))
            event_key = "push"
            
            # Generate embeddings for commit details
            commits = payload.get("commits") or []
            if commits:
                ref = payload.get("ref", "")
                branch = ref.split("/")[-1] if "/" in ref else ref
                pusher_name = payload.get("pusher", {}).get("name") or (event.get("actor") or {}).get("login", "someone")
                await _generate_commit_embeddings(self, repo, commits, branch, pusher_name)
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
        path = f"/search/repositories?q={quote_plus(query)}&per_page={MAX_SEARCH_RESULTS}"
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

    @github_group.command(name="link", description="Link your Discord user to a GitHub username for review commands.")
    @app_commands.describe(username="Your GitHub username")
    async def gh_link(self, interaction: discord.Interaction, username: str) -> None:
        await self.db.set_guild_config(interaction.guild_id, f"github_username_{interaction.user.id}", username)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Linked your account to GitHub user `{username}`.",
            ephemeral=True,
        )

    @github_group.command(name="default_repo", description="Show or set the default GitHub repo for this server.")
    @app_commands.describe(repo="owner/repo to store as the default repo (leave blank to view current)")
    @app_commands.default_permissions(manage_guild=True)
    async def gh_default_repo(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        if repo is None:
            current = await self._get_default_repo(interaction.guild_id)  # type: ignore[arg-type]
            if current:
                await interaction.response.send_message(f"ℹ️ Default GitHub repo: `{current}`.", ephemeral=True)
            else:
                await interaction.response.send_message("ℹ️ No default GitHub repo is configured.", ephemeral=True)
            return
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format. Use `owner/repo`.", ephemeral=True)
            return
        await self.db.set_guild_config(interaction.guild_id, "github_default_repo", repo)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Default GitHub repo set to `{repo}`.", ephemeral=True)

    @github_group.command(name="clear_default_repo", description="Clear the default GitHub repo for this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def gh_clear_default_repo(self, interaction: discord.Interaction) -> None:
        await self.db.set_guild_config(interaction.guild_id, "github_default_repo", "")  # type: ignore[arg-type]
        await interaction.response.send_message("✅ Cleared the default GitHub repo.", ephemeral=True)

    @github_group.command(name="review_queue", description="Show open PRs grouped by review status.")
    @app_commands.describe(repo="owner/repo (optional if a default repo is configured)", stale_hours="How old a PR must be before it is considered stale")
    async def gh_review_queue(
        self,
        interaction: discord.Interaction,
        repo: str | None = None,
        stale_hours: app_commands.Range[int, 1, 336] = 24,
    ) -> None:
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return

        await interaction.response.defer()
        queue = await self._fetch_review_queue(repo)
        if not queue:
            await interaction.followup.send(f"✅ No open pull requests found in `{repo}`.")
            return

        stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
        buckets: dict[str, list[tuple[dict, list[dict]]]] = {}
        for pr_data, reviews in queue:
            bucket = _review_bucket(pr_data, reviews, stale_cutoff)
            buckets.setdefault(bucket, []).append((pr_data, reviews))

        reviewer_lines = _reviewer_load_lines(queue, stale_cutoff)
        team_lines = _team_load_lines(queue, stale_cutoff)
        await interaction.followup.send(embed=_build_review_queue_embed(repo, buckets, stale_hours, reviewer_lines, team_lines))

    @github_group.command(name="my_reviews", description="Show PRs in a repo that are requesting your review.")
    @app_commands.describe(repo="owner/repo (optional if a default repo is configured)", username="GitHub username (optional if linked with /github link)")
    async def gh_my_reviews(
        self,
        interaction: discord.Interaction,
        repo: str | None = None,
        username: str | None = None,
    ) -> None:
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return

        github_username = username
        if not github_username:
            github_username = await self._get_linked_github_username(interaction.guild_id, interaction.user.id)  # type: ignore[arg-type]
        if not github_username:
            await interaction.response.send_message(
                "❌ No linked GitHub username found. Use `/github link <username>` or provide `username`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        queue = await self._fetch_review_queue(repo)
        mine = []
        for pr_data, reviews in queue:
            requested = {name.lower() for name in _requested_reviewer_names(pr_data)}
            if github_username.lower() in requested:
                mine.append((pr_data, reviews))

        if not mine:
            await interaction.followup.send(
                f"✅ No open PRs in `{repo}` are currently requesting review from `{github_username}`.",
                ephemeral=True,
            )
            return

        em = discord.Embed(
            title=f"👀 Requested Reviews — {repo}",
            description=f"Open PRs requesting review from `{github_username}`.",
            url=f"https://github.com/{repo}/pulls",
            color=0x0969DA,
        )
        for pr_data, reviews in mine[:MAX_REVIEW_QUEUE_PRS]:
            em.add_field(
                name=f"#{pr_data.get('number')} — {_trunc(pr_data.get('title', ''), 60)}",
                value=_review_value(pr_data, reviews),
                inline=False,
            )
        await interaction.followup.send(embed=em, ephemeral=True)

    @github_group.command(name="issue_create", description="Open a modal to create a GitHub issue.")
    @app_commands.describe(
        repo="owner/repo (optional if a default repo is configured)",
        template="Optional issue template",
    )
    @app_commands.choices(template=[
        app_commands.Choice(name="Bug", value="bug"),
        app_commands.Choice(name="Feature", value="feature"),
        app_commands.Choice(name="Docs", value="docs"),
    ])
    @app_commands.default_permissions(manage_messages=True)
    async def gh_issue_create(
        self,
        interaction: discord.Interaction,
        repo: str | None = None,
        template: str | None = None,
    ) -> None:
        if not await self._require_github_write_token(interaction):
            return
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return
        summary_default, reproduction_default = await self._get_issue_template_defaults(
            interaction.guild_id,
            template or await self._get_default_issue_template_key(interaction.guild_id),
        )
        labels_default = await self._get_issue_template_labels_text(
            interaction.guild_id,
            template or await self._get_default_issue_template_key(interaction.guild_id),
        )
        await interaction.response.send_modal(
            GitHubIssueModal(
                self,
                repo=repo,
                summary_default=summary_default,
                reproduction_default=reproduction_default,
                labels_default=labels_default,
                template_key=template or await self._get_default_issue_template_key(interaction.guild_id),
            )
        )

    @github_group.command(name="triage", description="Show open issues that need triage attention.")
    @app_commands.describe(repo="owner/repo (optional if a default repo is configured)", stale_days="How old an issue must be before it is considered stale")
    async def gh_triage(
        self,
        interaction: discord.Interaction,
        repo: str | None = None,
        stale_days: app_commands.Range[int, 1, 90] = 7,
    ) -> None:
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return

        await interaction.response.defer()
        status, data, _ = await self.gh.get(
            f"/repos/{repo}/issues?state=open&sort=updated&direction=asc&per_page=30"
        )
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitHub API error.")
            return

        issues = [issue for issue in data if "pull_request" not in issue]
        if not issues:
            await interaction.followup.send(f"✅ No open issues in `{repo}`.")
            return

        await interaction.followup.send(embed=_build_issue_triage_embed(repo, issues, stale_days))

    @github_group.command(name="review_digest", description="Configure the daily PR review digest channel and time.")
    @app_commands.describe(
        channel="Channel to post the digest in",
        hour_utc="Hour in UTC when the digest should post",
        repo="Repo to digest (defaults to the configured default repo)",
        stale_hours="PR age in hours to treat as stale in the digest",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gh_review_digest(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        hour_utc: app_commands.Range[int, 0, 23] = DEFAULT_REVIEW_DIGEST_HOUR_UTC,
        repo: str | None = None,
        stale_hours: app_commands.Range[int, 1, 336] = 24,
    ) -> None:
        repo = repo or await self._get_default_repo(interaction.guild_id)  # type: ignore[arg-type]
        if not repo:
            await interaction.response.send_message(
                "❌ Provide a repo or configure `/github default_repo <owner/repo>` first.",
                ephemeral=True,
            )
            return
        if not _REPO_RE.match(repo):
            await interaction.response.send_message("❌ Invalid repo format. Use `owner/repo`.", ephemeral=True)
            return
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_channel", str(channel.id))  # type: ignore[arg-type]
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_hour_utc", str(hour_utc))  # type: ignore[arg-type]
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_repo", repo)  # type: ignore[arg-type]
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_stale_hours", str(stale_hours))  # type: ignore[arg-type]
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_last_sent", "")  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"✅ Daily review digest configured for `{repo}` in {channel.mention} at `{hour_utc}:00 UTC`.",
            ephemeral=True,
        )

    @github_group.command(name="review_digest_disable", description="Disable the daily PR review digest.")
    @app_commands.default_permissions(manage_guild=True)
    async def gh_review_digest_disable(self, interaction: discord.Interaction) -> None:
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_channel", "")  # type: ignore[arg-type]
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_repo", "")  # type: ignore[arg-type]
        await self.db.set_guild_config(interaction.guild_id, "github_review_digest_last_sent", "")  # type: ignore[arg-type]
        await interaction.response.send_message("✅ Disabled the daily review digest.", ephemeral=True)

    @github_group.command(name="issue_comment", description="Add a comment to an issue or pull request.")
    @app_commands.describe(repo="owner/repo (optional if a default repo is configured)", number="Issue or PR number", comment="Comment body")
    @app_commands.default_permissions(manage_messages=True)
    async def gh_issue_comment(
        self,
        interaction: discord.Interaction,
        number: int,
        comment: str,
        repo: str | None = None,
    ) -> None:
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return
        if not await self._require_github_write_token(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        status, data, _ = await self.gh.post(
            f"/repos/{repo}/issues/{number}/comments",
            json_body={"body": comment.strip()},
        )
        if status not in (200, 201) or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to add the issue comment.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Added a comment to [#{number}]({data.get('html_url', '')}) in `{repo}`.",
            ephemeral=True,
        )

    @github_group.command(name="issue_state", description="Open or close an issue.")
    @app_commands.describe(repo="owner/repo (optional if a default repo is configured)", number="Issue number", state="New state")
    @app_commands.choices(state=[
        app_commands.Choice(name="Open", value="open"),
        app_commands.Choice(name="Closed", value="closed"),
    ])
    @app_commands.default_permissions(manage_messages=True)
    async def gh_issue_state(
        self,
        interaction: discord.Interaction,
        number: int,
        state: str,
        repo: str | None = None,
    ) -> None:
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return
        if not await self._require_github_write_token(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        status, data, _ = await self.gh.patch(
            f"/repos/{repo}/issues/{number}",
            json_body={"state": state},
        )
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to update the issue state.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Updated [#{number} {data.get('title', 'issue')}]({data.get('html_url', '')}) to `{state}`.",
            ephemeral=True,
        )

    @github_group.command(name="issue_labels", description="Add or remove labels on an issue.")
    @app_commands.describe(
        repo="owner/repo (optional if a default repo is configured)",
        number="Issue number",
        add="Comma-separated labels to add",
        remove="Comma-separated labels to remove",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def gh_issue_labels(
        self,
        interaction: discord.Interaction,
        number: int,
        add: str | None = None,
        remove: str | None = None,
        repo: str | None = None,
    ) -> None:
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return
        if not await self._require_github_write_token(interaction):
            return
        add_labels = {label.strip() for label in (add or "").split(",") if label.strip()}
        remove_labels = {label.strip() for label in (remove or "").split(",") if label.strip()}
        if not add_labels and not remove_labels:
            await interaction.response.send_message("❌ Provide at least one label to add or remove.", ephemeral=True)
            return

        issue = await self._fetch_issue(repo, number)
        if issue is None:
            await interaction.response.send_message(f"❌ Issue #{number} not found in `{repo}`.", ephemeral=True)
            return
        current_labels = {label.get("name", "") for label in issue.get("labels") or [] if label.get("name")}
        next_labels = sorted((current_labels | add_labels) - remove_labels)

        await interaction.response.defer(ephemeral=True)
        status, data, _ = await self.gh.patch(
            f"/repos/{repo}/issues/{number}",
            json_body={"labels": next_labels},
        )
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to update issue labels.", ephemeral=True)
            return
        label_text = ", ".join(f"`{label}`" for label in next_labels) if next_labels else "no labels"
        await interaction.followup.send(
            f"✅ Updated labels for [#{number} {data.get('title', 'issue')}]({data.get('html_url', '')}): {label_text}.",
            ephemeral=True,
        )

    @github_group.command(name="issue_assign", description="Assign or unassign a GitHub user on an issue.")
    @app_commands.describe(
        repo="owner/repo (optional if a default repo is configured)",
        number="Issue number",
        username="GitHub username to assign or unassign",
        remove="Remove the assignee instead of adding them",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def gh_issue_assign(
        self,
        interaction: discord.Interaction,
        number: int,
        username: str,
        remove: bool = False,
        repo: str | None = None,
    ) -> None:
        repo = await self._resolve_repo(interaction, repo)
        if not repo:
            return
        if not await self._require_github_write_token(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        path = f"/repos/{repo}/issues/{number}/assignees"
        payload = {"assignees": [username]}
        if remove:
            status, data, _ = await self.gh.delete(path, json_body=payload)
        else:
            status, data, _ = await self.gh.post(path, json_body=payload)
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to update issue assignees.", ephemeral=True)
            return
        verb = "Removed" if remove else "Added"
        await interaction.followup.send(
            f"✅ {verb} `{username}` {'from' if remove else 'to'} [#{number} {data.get('title', 'issue')}]({data.get('html_url', '')}).",
            ephemeral=True,
        )

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
