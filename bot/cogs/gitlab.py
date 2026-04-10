"""GitLab integration cog.

Features
--------
Project Monitoring (polling every 60 s)
  - Pushes, Merge Requests, Issues, Releases posted as rich Discord embeds.
  - Per-guild, per-channel subscriptions stored in the DB.
  - Deduplication via last-seen event ID.

GitLab API Slash Commands (/gitlab …)
  - project    — rich project overview embed.
  - user       — GitLab user profile.
  - issue      — look up a specific issue by IID.
  - issues     — list open issues for a project.
  - mrs        — list open merge requests for a project.
  - releases   — latest releases for a project.
  - search     — search projects on GitLab.

Subscription Management (/gitlab subscribe/unsubscribe/subscriptions)

RAG Ingestion (/gitlab ingest)
  - Fetches README + docs/ tree of a project and ingests into the RAG knowledge
    base (requires the SupportCog / embeddings system to be present).

Self-hosted GitLab support via GITLAB_URL env var (default: https://gitlab.com).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus, quote

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

POLL_INTERVAL_SECONDS = 60
MAX_ISSUES_LISTED = 8
MAX_RELEASES_LISTED = 5
MAX_SEARCH_RESULTS = 6
MAX_MRS_LISTED = 8
GITLAB_COLOR = 0xFC6D26  # GitLab orange
_VALID_EVENTS = {"push", "merge_request", "issues", "release"}
_DEFAULT_EVENTS = "push,merge_request,issues,release"

# Regex: "namespace/project" — basic validation (allows subgroups)
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(iso: str | None) -> str:
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


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _encoded_project(project: str) -> str:
    """URL-encode a 'namespace/project' path for GitLab API path segments."""
    return quote(project, safe="")


def _make_project_embed(data: dict, base_url: str) -> discord.Embed:
    em = discord.Embed(
        title=data.get("name_with_namespace") or data.get("path_with_namespace", ""),
        url=data.get("web_url", ""),
        description=_trunc(data.get("description") or "", 300),
        color=GITLAB_COLOR,
    )
    lang = data.get("predominant_language") or "—"
    stars = data.get("star_count", 0)
    forks = data.get("forks_count", 0)
    issues = data.get("open_issues_count", 0)
    em.add_field(name="Language", value=lang, inline=True)
    em.add_field(name="⭐ Stars", value=f"{stars:,}", inline=True)
    em.add_field(name="🍴 Forks", value=f"{forks:,}", inline=True)
    em.add_field(name="🐛 Open Issues", value=f"{issues:,}", inline=True)
    visibility = data.get("visibility", "—")
    em.add_field(name="Visibility", value=visibility.capitalize(), inline=True)
    topics = data.get("topics") or data.get("tag_list") or []
    if topics:
        em.add_field(name="Topics", value=" · ".join(f"`{t}`" for t in topics[:10]), inline=False)
    ns = data.get("namespace") or {}
    avatar = data.get("avatar_url") or ns.get("avatar_url")
    if avatar:
        if avatar.startswith("/"):
            avatar = base_url.rstrip("/") + avatar
        em.set_thumbnail(url=avatar)
    em.set_footer(text=f"Created {_ts(data.get('created_at'))}  |  Updated {_ts(data.get('last_activity_at'))}")
    return em


def _make_user_embed(data: dict, base_url: str) -> discord.Embed:
    web_url = data.get("web_url") or f"{base_url}/{data.get('username', '')}"
    em = discord.Embed(
        title=data.get("name") or data.get("username", ""),
        url=web_url,
        description=_trunc(data.get("bio") or "", 300),
        color=GITLAB_COLOR,
    )
    avatar = data.get("avatar_url", "")
    if avatar:
        em.set_thumbnail(url=avatar)
    em.add_field(name="Username", value=f"`{data.get('username', '')}`", inline=True)
    if data.get("organization"):
        em.add_field(name="Organization", value=data["organization"], inline=True)
    if data.get("location"):
        em.add_field(name="Location", value=data["location"], inline=True)
    if data.get("website_url"):
        em.add_field(name="Website", value=data["website_url"], inline=False)
    em.set_footer(text=f"Member since {_ts(data.get('created_at'))}")
    return em


# ---------------------------------------------------------------------------
# Notification embed builders
# ---------------------------------------------------------------------------

def _push_embed(project: str, payload: dict, base_url: str) -> discord.Embed:
    ref = payload.get("ref", "")
    branch = ref.split("/")[-1] if "/" in ref else ref
    commits = payload.get("commits") or []
    pusher = (payload.get("user_username") or payload.get("user_name") or "someone")
    project_url = payload.get("project", {}).get("web_url") or f"{base_url}/{project}"
    em = discord.Embed(
        title=f"📦 Push to `{project}` on `{branch}`",
        url=f"{project_url}/-/tree/{branch}",
        color=0xFC6D26,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=pusher)
    lines = []
    for c in commits[:5]:
        sha = (c.get("id") or "")[:7]
        msg = _trunc((c.get("message") or "").splitlines()[0], 72)
        url = c.get("url", "")
        lines.append(f"[`{sha}`]({url}) {msg}")
    if len(commits) > 5:
        lines.append(f"…and {len(commits) - 5} more")
    em.description = "\n".join(lines)
    em.set_footer(text=f"{project}  •  {len(commits)} commit(s)")
    return em


async def _generate_commit_embeddings(cog: "GitLabCog", project: str, commits: list[dict], branch: str, pusher: str, base_url: str) -> None:
    """Generate detailed embeddings for commits in a push event."""
    support_cog = cog.bot.get_cog("Support")
    if support_cog is None:
        return
    
    llm = getattr(support_cog, "llm", None)
    if llm is None:
        return
    
    project_url = f"{base_url}/{project}"
    
    for commit in commits:
        sha = commit.get("id", "")
        if not sha:
            continue
            
        # Extract detailed commit information
        author = commit.get("author", {})
        author_name = author.get("name", "Unknown")
        author_email = author.get("email", "")
        
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
        commit_details.append(f"Project: {project}")
        commit_details.append(f"Branch: {branch}")
        commit_details.append(f"Author: {author_name} ({author_email})")
        if timestamp:
            commit_details.append(f"Timestamp: {timestamp}")
        commit_details.append(f"Pushed by: {pusher}")
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
        label = f"commit:{project}:{sha[:7]}"
        
        try:
            from bot.llm_service import LLMService
            vec = await llm.get_embedding(text[:8000])
            if vec:
                import struct
                embedding_bytes = struct.pack(f"{len(vec)}f", *vec)
                model = getattr(llm, "_embedding_model", None)
                
                # Store embedding for each guild that has this project subscribed
                subs = await cog.db.get_all_gitlab_subscriptions()
                guild_ids = {sub["guild_id"] for sub in subs if sub["project"] == project}
                
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


def _mr_embed(project: str, payload: dict) -> discord.Embed | None:
    attrs = payload.get("object_attributes") or {}
    action = attrs.get("action", "")
    if action not in ("open", "close", "reopen", "merge"):
        return None
    color_map = {"open": 0x1AAA55, "close": 0xDD2B0E, "reopen": 0x1AAA55, "merge": 0x6E49CB}
    icon_map  = {"open": "🟢", "close": "🔴", "reopen": "🟢", "merge": "🟣"}
    color = color_map.get(action, GITLAB_COLOR)
    icon  = icon_map.get(action, "⚪")
    user  = payload.get("user") or {}
    sender = user.get("username") or user.get("name") or ""
    em = discord.Embed(
        title=f"{icon} MR !{attrs.get('iid')} {action}: {_trunc(attrs.get('title', ''), 80)}",
        url=attrs.get("url", ""),
        description=_trunc(attrs.get("description") or "", 300),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=sender)
    source = attrs.get("source_branch", "?")
    target = attrs.get("target_branch", "?")
    em.add_field(name="Branch", value=f"`{source}` → `{target}`", inline=True)
    em.set_footer(text=project)
    return em


def _issue_embed(project: str, payload: dict) -> discord.Embed | None:
    attrs = payload.get("object_attributes") or {}
    action = attrs.get("action", "")
    if action not in ("open", "close", "reopen"):
        return None
    color_map = {"open": 0x1AAA55, "close": 0xDD2B0E, "reopen": 0x1AAA55}
    icon_map  = {"open": "🟢", "close": "🔴", "reopen": "🟢"}
    user   = payload.get("user") or {}
    sender = user.get("username") or user.get("name") or ""
    em = discord.Embed(
        title=f"{icon_map.get(action, '⚪')} Issue #{attrs.get('iid')} {action}: {_trunc(attrs.get('title', ''), 80)}",
        url=attrs.get("url", ""),
        description=_trunc(attrs.get("description") or "", 300),
        color=color_map.get(action, GITLAB_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    em.set_author(name=sender)
    labels = [lbl.get("title", "") for lbl in (payload.get("labels") or [])]
    if labels:
        em.add_field(name="Labels", value=", ".join(f"`{l}`" for l in labels[:6]), inline=False)
    em.set_footer(text=project)
    return em


def _release_embed(project: str, payload: dict) -> discord.Embed | None:
    action = payload.get("action", "")
    if action not in ("create", "update"):
        return None
    name = payload.get("name") or payload.get("tag", "")
    tag  = payload.get("tag", "")
    url  = payload.get("url", "")
    desc = payload.get("description") or ""
    em = discord.Embed(
        title=f"🚀 Release: {_trunc(name, 80)}",
        url=url,
        description=_trunc(desc, 400),
        color=0xFBCA04,
        timestamp=datetime.now(timezone.utc),
    )
    em.add_field(name="Tag", value=f"`{tag}`", inline=True)
    em.set_footer(text=project)
    return em


# ---------------------------------------------------------------------------
# GitLab API client
# ---------------------------------------------------------------------------

class GitLabClient:
    """Minimal async GitLab REST API v4 client."""

    def __init__(self, token: str | None = None, base_url: str = "https://gitlab.com") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._api_base = f"{self._base_url}/api/v4"
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict:
        h: dict = {"User-Agent": "DiscordBot-GitLabCog/1.0"}
        if self._token:
            h["PRIVATE-TOKEN"] = self._token
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
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, dict | list | str | None]:
        url = path if path.startswith("http") else f"{self._api_base}{path}"
        session = await self._session_get()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
                return resp.status, body
        except Exception as exc:
            logger.warning("GitLab API error for %s: %s", url, exc)
            return 0, None

    async def get(self, path: str) -> tuple[int, dict | list | str | None]:
        return await self.request("GET", path)

    async def post(self, path: str, *, json_body: dict[str, Any]) -> tuple[int, dict | list | str | None]:
        return await self.request("POST", path, json_body=json_body)

    async def put(self, path: str, *, json_body: dict[str, Any]) -> tuple[int, dict | list | str | None]:
        return await self.request("PUT", path, json_body=json_body)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class GitLabCog(commands.Cog, name="GitLab"):
    """Full GitLab integration: monitoring, API queries, and RAG ingestion."""

    def __init__(self, bot: commands.Bot, db: "Database", config: "Config") -> None:
        self.bot = bot
        self.db = db
        self.gl = GitLabClient(token=config.gitlab_token, base_url=config.gitlab_url)
        self._base_url = config.gitlab_url.rstrip("/")

    # ------------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self._poller.start()
        logger.info("GitLabCog loaded — poller started")

    async def cog_unload(self) -> None:
        self._poller.cancel()
        await self.gl.close()

    # ------------------------------------------------------------------ helpers

    def _require_token(self, interaction: discord.Interaction) -> bool:
        return bool(self.gl._token)

    async def _send_no_token(self, interaction: discord.Interaction) -> None:
        msg = "❌ A `GITLAB_TOKEN` with API access is required for this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    
    async def _fetch_commit_details_for_push(self, project: str, commit_to: str, commits_count: int) -> list[dict]:
        """Fetch detailed commit information for a push event."""
        try:
            encoded_project = _encoded_project(project)
            # Get commits leading up to and including the latest commit
            status, commits = await self.gl.get(f"/projects/{encoded_project}/repository/commits?ref_name={commit_to}&per_page={min(commits_count, 10)}")
            if status != 200 or not isinstance(commits, list):
                return []
            
            return commits
            
        except Exception as exc:
            logger.debug("Failed to fetch commit details for push to %s: %s", project, exc)
            return []
    
    async def _fetch_and_embed_commit_details(self, project: str, commit_sha: str, branch: str, pusher: str) -> None:
        """Fetch detailed commit information from GitLab API and generate embeddings."""
        try:
            encoded_project = _encoded_project(project)
            status, commit_data = await self.gl.get(f"/projects/{encoded_project}/repository/commits/{commit_sha}")
            if status != 200 or not isinstance(commit_data, dict):
                return
            
            # Convert to format expected by embedding function
            commits = [commit_data]
            await _generate_commit_embeddings(self, project, commits, branch, pusher, self._base_url)
            
        except Exception as exc:
            logger.debug("Failed to fetch commit details for %s: %s", commit_sha[:7], exc)

    async def _get_default_project(self, guild_id: int | None) -> str | None:
        if guild_id is None:
            return None
        val = await self.db.get_guild_config(guild_id, "gitlab_default_project")
        return val or None

    async def _resolve_project(self, interaction: discord.Interaction, project: str | None) -> str | None:
        resolved = (project or "").strip()
        if not resolved:
            resolved = (await self._get_default_project(interaction.guild_id)) or ""
        if not resolved:
            await interaction.response.send_message(
                "❌ No project provided and no default project configured. Use `/gitlab default_project <namespace/project>` first.",
                ephemeral=True,
            )
            return None
        if not _PROJECT_RE.match(resolved):
            await interaction.response.send_message(
                "❌ Invalid project format. Use `namespace/project` (e.g. `gitlab-org/gitlab`).",
                ephemeral=True,
            )
            return None
        return resolved

    # ------------------------------------------------------------------ poller

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poller(self) -> None:
        try:
            await self._poll_all()
        except Exception as exc:
            logger.exception("GitLab poller error: %s", exc)

    @_poller.before_loop
    async def _before_poller(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_all(self) -> None:
        subs = await self.db.get_all_gitlab_subscriptions()
        if not subs:
            return
        project_subs: dict[str, list] = {}
        for sub in subs:
            project_subs.setdefault(sub["project"], []).append(sub)
        for project, subscribers in project_subs.items():
            await self._poll_project(project, subscribers)

    async def _poll_project(self, project: str, subscribers: list) -> None:
        """Poll GitLab events API for a project and dispatch to subscribed channels."""
        state = await self.db.get_gitlab_poll_state(project, "events")
        last_id = state["last_id"] if state else None

        encoded = _encoded_project(project)
        status, body = await self.gl.get(
            f"/projects/{encoded}/events?per_page=30&sort=desc"
        )

        if status != 200 or not isinstance(body, list):
            return

        events: list[dict] = body
        if not events:
            return

        newest_id = str(events[0].get("id", ""))

        if state is None:
            await self.db.set_gitlab_poll_state(project, "events", newest_id)
            logger.info("GitLab poller bootstrap for %s — seeded latest event %s", project, newest_id)
            return

        new_events: list[dict] = []
        for ev in events:
            eid = str(ev.get("id", ""))
            if last_id and eid == last_id:
                break
            new_events.append(ev)

        await self.db.set_gitlab_poll_state(project, "events", newest_id)

        for ev in reversed(new_events):
            await self._dispatch_event(project, ev, subscribers)

    async def _dispatch_event(self, project: str, event: dict, subscribers: list) -> None:
        action_name = event.get("action_name", "")
        target_type = (event.get("target_type") or "").lower()

        embed: discord.Embed | None = None
        event_key: str | None = None

        if action_name == "pushed to" or action_name == "pushed new":
            push_data = event.get("push_data") or {}
            commits_count = push_data.get("commit_count", 0)
            ref = push_data.get("ref") or ""
            commit_title = push_data.get("commit_title") or ""
            commit_to = push_data.get("commit_to") or ""
            author = event.get("author") or {}
            pusher = author.get("username") or author.get("name") or "someone"
            project_url = f"{self._base_url}/{project}"
            
            # Fetch detailed commit information for the push
            commits = await self._fetch_commit_details_for_push(project, commit_to, commits_count)
            
            em = discord.Embed(
                title=f"📦 Push to `{project}` on `{ref}`",
                url=f"{project_url}/-/commits/{ref}",
                color=GITLAB_COLOR,
                timestamp=datetime.now(timezone.utc),
            )
            em.set_author(name=pusher)
            
            # Display commit details
            if commits:
                lines = []
                for commit in commits[:5]:  # Show up to 5 commits
                    sha = commit.get("id", "")[:7]
                    message = commit.get("message", "")
                    first_line = message.splitlines()[0] if message else "No message"
                    url = commit.get("web_url", "")
                    lines.append(f"[`{sha}`]({url}) {_trunc(first_line, 72)}")
                
                if len(commits) > 5:
                    lines.append(f"…and {len(commits) - 5} more")
                
                em.description = "\n".join(lines)
            else:
                # Fallback to basic info if commit fetch failed
                sha = commit_to[:7] if commit_to else ""
                em.description = f"[`{sha}`]({project_url}/-/commit/{commit_to}) {_trunc(commit_title, 72)}" if sha else _trunc(commit_title, 120)
            
            em.set_footer(text=f"{project}  •  {commits_count} commit(s)")
            embed = em
            event_key = "push"
            
            # Generate embeddings for all commits
            if commits:
                await _generate_commit_embeddings(self, project, commits, ref, pusher, self._base_url)

        elif target_type == "mergerequest":
            action = action_name
            iid = event.get("target_iid") or event.get("target_id")
            title = event.get("target_title") or ""
            author = event.get("author") or {}
            sender = author.get("username") or author.get("name") or ""
            project_url = f"{self._base_url}/{project}"
            color_map = {"opened": 0x1AAA55, "closed": 0xDD2B0E, "merged": 0x6E49CB}
            icon_map  = {"opened": "🟢", "closed": "🔴", "merged": "🟣"}
            color = color_map.get(action, GITLAB_COLOR)
            icon  = icon_map.get(action, "⚪")
            em = discord.Embed(
                title=f"{icon} MR !{iid} {action}: {_trunc(title, 80)}",
                url=f"{project_url}/-/merge_requests/{iid}",
                color=color,
                timestamp=datetime.now(timezone.utc),
            )
            em.set_author(name=sender)
            em.set_footer(text=project)
            embed = em
            event_key = "merge_request"

        elif target_type == "issue":
            action = action_name
            iid = event.get("target_iid") or event.get("target_id")
            title = event.get("target_title") or ""
            author = event.get("author") or {}
            sender = author.get("username") or author.get("name") or ""
            project_url = f"{self._base_url}/{project}"
            color_map = {"opened": 0x1AAA55, "closed": 0xDD2B0E}
            icon_map  = {"opened": "🟢", "closed": "🔴"}
            em = discord.Embed(
                title=f"{icon_map.get(action, '⚪')} Issue #{iid} {action}: {_trunc(title, 80)}",
                url=f"{project_url}/-/issues/{iid}",
                color=color_map.get(action, GITLAB_COLOR),
                timestamp=datetime.now(timezone.utc),
            )
            em.set_author(name=sender)
            em.set_footer(text=project)
            embed = em
            event_key = "issues"

        else:
            return

        if embed is None or event_key is None:
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
                logger.warning("GitLab: no permission to send in channel %d", sub["channel_id"])
            except Exception as exc:
                logger.warning("GitLab dispatch error: %s", exc)

    # ------------------------------------------------------------------ slash commands

    gitlab_group = app_commands.Group(name="gitlab", description="GitLab integration commands")

    # ---- project info

    @gitlab_group.command(name="project", description="Show information about a GitLab project.")
    @app_commands.describe(project="Project path, e.g. gitlab-org/gitlab")
    async def gl_project(self, interaction: discord.Interaction, project: str) -> None:
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message(
                "❌ Invalid project format. Use `namespace/project`.", ephemeral=True
            )
            return
        await interaction.response.defer()
        status, data = await self.gl.get(f"/projects/{_encoded_project(project)}")
        if status == 404:
            await interaction.followup.send(f"❌ Project `{project}` not found.")
            return
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ GitLab API error. Try again later.")
            return
        await interaction.followup.send(embed=_make_project_embed(data, self._base_url))

    # ---- user info

    @gitlab_group.command(name="user", description="Show a GitLab user's profile.")
    @app_commands.describe(username="GitLab username")
    async def gl_user(self, interaction: discord.Interaction, username: str) -> None:
        await interaction.response.defer()
        status, data = await self.gl.get(f"/users?username={quote_plus(username)}")
        if status != 200 or not isinstance(data, list) or not data:
            await interaction.followup.send(f"❌ User `{username}` not found.")
            return
        await interaction.followup.send(embed=_make_user_embed(data[0], self._base_url))

    # ---- single issue

    @gitlab_group.command(name="issue", description="Look up a specific issue by IID.")
    @app_commands.describe(project="namespace/project", number="Issue IID number")
    async def gl_issue(self, interaction: discord.Interaction, project: str, number: int) -> None:
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message("❌ Invalid project format.", ephemeral=True)
            return
        await interaction.response.defer()
        status, data = await self.gl.get(f"/projects/{_encoded_project(project)}/issues/{number}")
        if status == 404:
            await interaction.followup.send(f"❌ Issue #{number} not found in `{project}`.")
            return
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ GitLab API error.")
            return
        state = data.get("state", "")
        state_icon = {"opened": "🟢", "closed": "🔴"}.get(state, "⚪")
        em = discord.Embed(
            title=f"{state_icon} Issue #{data.get('iid')}: {_trunc(data.get('title', ''), 80)}",
            url=data.get("web_url", ""),
            description=_trunc(data.get("description") or "", 400),
            color=0x1AAA55 if state == "opened" else 0xDD2B0E,
            timestamp=datetime.now(timezone.utc),
        )
        author = data.get("author") or {}
        em.set_author(name=author.get("username", ""), url=author.get("web_url", ""),
                      icon_url=author.get("avatar_url"))
        labels = data.get("labels") or []
        if labels:
            em.add_field(name="Labels", value=" ".join(f"`{l}`" for l in labels[:8]), inline=False)
        assignees = data.get("assignees") or []
        if assignees:
            em.add_field(name="Assignees", value=", ".join(a["username"] for a in assignees[:5]), inline=True)
        em.set_footer(text=f"{project}  •  {data.get('user_notes_count', 0)} comment(s)")
        await interaction.followup.send(embed=em)

    # ---- list open issues

    @gitlab_group.command(name="issues", description="List open issues for a project.")
    @app_commands.describe(project="namespace/project", label="Filter by label (optional)")
    async def gl_issues(self, interaction: discord.Interaction, project: str, label: str | None = None) -> None:
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message("❌ Invalid project format.", ephemeral=True)
            return
        await interaction.response.defer()
        path = f"/projects/{_encoded_project(project)}/issues?state=opened&per_page={MAX_ISSUES_LISTED}"
        if label:
            path += f"&labels={quote_plus(label)}"
        status, data = await self.gl.get(path)
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitLab API error.")
            return
        issues = data[:MAX_ISSUES_LISTED]
        if not issues:
            await interaction.followup.send(
                f"✅ No open issues in `{project}`" + (f" with label `{label}`" if label else "") + "."
            )
            return
        em = discord.Embed(
            title=f"🐛 Open Issues — {project}",
            url=f"{self._base_url}/{project}/-/issues",
            color=GITLAB_COLOR,
        )
        for issue in issues:
            labels_str = ", ".join(f"`{l}`" for l in (issue.get("labels") or [])[:3])
            label_part = f"  {labels_str}" if labels_str else ""
            author = (issue.get("author") or {}).get("username", "?")
            em.add_field(
                name=f"#{issue.get('iid')} — {_trunc(issue.get('title', ''), 55)}",
                value=f"[View]({issue.get('web_url', '')}){label_part}  •  by `{author}`",
                inline=False,
            )
        em.set_footer(text=f"Showing up to {MAX_ISSUES_LISTED} open issues")
        await interaction.followup.send(embed=em)

    # ---- list open MRs

    @gitlab_group.command(name="mrs", description="List open merge requests for a project.")
    @app_commands.describe(project="namespace/project")
    async def gl_mrs(self, interaction: discord.Interaction, project: str) -> None:
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message("❌ Invalid project format.", ephemeral=True)
            return
        await interaction.response.defer()
        status, data = await self.gl.get(
            f"/projects/{_encoded_project(project)}/merge_requests?state=opened&per_page={MAX_MRS_LISTED}"
        )
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitLab API error.")
            return
        if not data:
            await interaction.followup.send(f"✅ No open merge requests in `{project}`.")
            return
        em = discord.Embed(
            title=f"🔀 Open Merge Requests — {project}",
            url=f"{self._base_url}/{project}/-/merge_requests",
            color=0x1AAA55,
        )
        for mr in data[:MAX_MRS_LISTED]:
            source = mr.get("source_branch", "?")
            target = mr.get("target_branch", "?")
            author = (mr.get("author") or {}).get("username", "?")
            em.add_field(
                name=f"!{mr.get('iid')} — {_trunc(mr.get('title', ''), 55)}",
                value=f"[View]({mr.get('web_url', '')})  •  `{source}` → `{target}`  •  by `{author}`",
                inline=False,
            )
        em.set_footer(text=f"Showing up to {MAX_MRS_LISTED} open MRs")
        await interaction.followup.send(embed=em)

    # ---- releases

    @gitlab_group.command(name="releases", description="Show the latest releases for a project.")
    @app_commands.describe(project="namespace/project")
    async def gl_releases(self, interaction: discord.Interaction, project: str) -> None:
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message("❌ Invalid project format.", ephemeral=True)
            return
        await interaction.response.defer()
        status, data = await self.gl.get(
            f"/projects/{_encoded_project(project)}/releases?per_page={MAX_RELEASES_LISTED}"
        )
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitLab API error.")
            return
        if not data:
            await interaction.followup.send(f"ℹ️ No releases found for `{project}`.")
            return
        em = discord.Embed(
            title=f"🚀 Releases — {project}",
            url=f"{self._base_url}/{project}/-/releases",
            color=0xFBCA04,
        )
        for rel in data[:MAX_RELEASES_LISTED]:
            tag  = rel.get("tag_name", "?")
            name = rel.get("name") or tag
            released_at = _ts(rel.get("released_at"))
            em.add_field(
                name=name,
                value=f"[`{tag}`]({rel.get('_links', {}).get('self', '')})  •  {released_at}",
                inline=False,
            )
        await interaction.followup.send(embed=em)

    # ---- search projects

    @gitlab_group.command(name="search", description="Search GitLab projects.")
    @app_commands.describe(query="Search query, e.g. 'discord bot'", order_by="Sort order")
    @app_commands.choices(order_by=[
        app_commands.Choice(name="Best Match", value=""),
        app_commands.Choice(name="Stars", value="star_count"),
        app_commands.Choice(name="Last Activity", value="last_activity_at"),
        app_commands.Choice(name="Name", value="name"),
    ])
    async def gl_search(self, interaction: discord.Interaction, query: str, order_by: str = "") -> None:
        await interaction.response.defer()
        path = f"/projects?search={quote_plus(query)}&per_page={MAX_SEARCH_RESULTS}"
        if order_by:
            path += f"&order_by={order_by}&sort=desc"
        status, data = await self.gl.get(path)
        if status != 200 or not isinstance(data, list):
            await interaction.followup.send("❌ GitLab API error.")
            return
        if not data:
            await interaction.followup.send(f"🔍 No projects found for `{query}`.")
            return
        em = discord.Embed(
            title=f"🔍 GitLab Project Search: {_trunc(query, 50)}",
            color=GITLAB_COLOR,
        )
        for item in data[:MAX_SEARCH_RESULTS]:
            stars = item.get("star_count", 0)
            lang  = item.get("predominant_language") or "—"
            em.add_field(
                name=item.get("name_with_namespace") or item.get("path_with_namespace", "?"),
                value=f"[View]({item.get('web_url', '')})  •  ⭐ {stars:,}  •  {lang}\n{_trunc(item.get('description') or '', 80)}",
                inline=False,
            )
        await interaction.followup.send(embed=em)

    # ---- default project

    @gitlab_group.command(name="default_project", description="Show or set the default GitLab project for this server.")
    @app_commands.describe(project="namespace/project to store as the default (leave blank to view current)")
    @app_commands.default_permissions(manage_guild=True)
    async def gl_default_project(self, interaction: discord.Interaction, project: str | None = None) -> None:
        if project is None:
            current = await self._get_default_project(interaction.guild_id)
            if current:
                await interaction.response.send_message(f"ℹ️ Default GitLab project: `{current}`.", ephemeral=True)
            else:
                await interaction.response.send_message("ℹ️ No default GitLab project configured.", ephemeral=True)
            return
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message(
                "❌ Invalid project format. Use `namespace/project`.", ephemeral=True
            )
            return
        await self.db.set_guild_config(interaction.guild_id, "gitlab_default_project", project)  # type: ignore[arg-type]
        await interaction.response.send_message(f"✅ Default GitLab project set to `{project}`.", ephemeral=True)

    @gitlab_group.command(name="clear_default_project", description="Clear the default GitLab project for this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def gl_clear_default_project(self, interaction: discord.Interaction) -> None:
        await self.db.set_guild_config(interaction.guild_id, "gitlab_default_project", "")  # type: ignore[arg-type]
        await interaction.response.send_message("✅ Cleared the default GitLab project.", ephemeral=True)

    # ---- issue create

    @gitlab_group.command(name="issue_create", description="Create an issue in a GitLab project.")
    @app_commands.describe(
        project="namespace/project (optional if a default is configured)",
        title="Issue title",
        description="Issue description",
        labels="Comma-separated labels to apply",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def gl_issue_create(
        self,
        interaction: discord.Interaction,
        title: str,
        project: str | None = None,
        description: str | None = None,
        labels: str | None = None,
    ) -> None:
        project = await self._resolve_project(interaction, project)
        if not project:
            return
        if not self._require_token(interaction):
            await self._send_no_token(interaction)
            return

        label_list = [l.strip() for l in (labels or "").split(",") if l.strip()]
        payload: dict[str, Any] = {"title": title.strip()}
        if description:
            payload["description"] = description.strip()
        if label_list:
            payload["labels"] = ",".join(label_list)

        await interaction.response.defer(ephemeral=True, thinking=True)
        status, data = await self.gl.post(
            f"/projects/{_encoded_project(project)}/issues",
            json_body=payload,
        )
        if status not in (200, 201) or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to create the GitLab issue.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Created issue [#{data.get('iid')} {data.get('title', 'issue')}]({data.get('web_url', '')}) in `{project}`.",
            ephemeral=True,
        )

    # ---- issue comment

    @gitlab_group.command(name="issue_comment", description="Add a comment to a GitLab issue.")
    @app_commands.describe(
        project="namespace/project (optional if a default is configured)",
        number="Issue IID",
        comment="Comment body",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def gl_issue_comment(
        self,
        interaction: discord.Interaction,
        number: int,
        comment: str,
        project: str | None = None,
    ) -> None:
        project = await self._resolve_project(interaction, project)
        if not project:
            return
        if not self._require_token(interaction):
            await self._send_no_token(interaction)
            return

        await interaction.response.defer(ephemeral=True)
        status, data = await self.gl.post(
            f"/projects/{_encoded_project(project)}/issues/{number}/notes",
            json_body={"body": comment.strip()},
        )
        if status not in (200, 201) or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to add the comment.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Added a comment to issue [#{number}]({data.get('noteable_url', '')}) in `{project}`.",
            ephemeral=True,
        )

    # ---- issue state

    @gitlab_group.command(name="issue_state", description="Open or close a GitLab issue.")
    @app_commands.describe(
        project="namespace/project (optional if a default is configured)",
        number="Issue IID",
        state="New state",
    )
    @app_commands.choices(state=[
        app_commands.Choice(name="Open", value="reopen"),
        app_commands.Choice(name="Closed", value="close"),
    ])
    @app_commands.default_permissions(manage_messages=True)
    async def gl_issue_state(
        self,
        interaction: discord.Interaction,
        number: int,
        state: str,
        project: str | None = None,
    ) -> None:
        project = await self._resolve_project(interaction, project)
        if not project:
            return
        if not self._require_token(interaction):
            await self._send_no_token(interaction)
            return

        await interaction.response.defer(ephemeral=True)
        status, data = await self.gl.put(
            f"/projects/{_encoded_project(project)}/issues/{number}",
            json_body={"state_event": state},
        )
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send("❌ Failed to update the issue state.", ephemeral=True)
            return
        new_state = data.get("state", state)
        await interaction.followup.send(
            f"✅ Issue [#{number} {data.get('title', '')}]({data.get('web_url', '')}) is now `{new_state}`.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ subscription commands

    @gitlab_group.command(name="subscribe", description="Subscribe a channel to GitLab project notifications.")
    @app_commands.describe(
        project="Project in namespace/project format",
        channel="Channel to post notifications in (default: current channel)",
        events="Comma-separated event types: push,merge_request,issues,release",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gl_subscribe(
        self,
        interaction: discord.Interaction,
        project: str,
        channel: discord.TextChannel | None = None,
        events: str = _DEFAULT_EVENTS,
    ) -> None:
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message(
                "❌ Invalid project format. Use `namespace/project`.", ephemeral=True
            )
            return

        parsed_events = {e.strip().lower() for e in events.split(",")}
        invalid = parsed_events - _VALID_EVENTS
        if invalid:
            await interaction.response.send_message(
                f"❌ Unknown event type(s): {', '.join(invalid)}.\nValid: `push`, `merge_request`, `issues`, `release`.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ Target must be a text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Verify project exists
        status, _ = await self.gl.get(f"/projects/{_encoded_project(project)}")
        if status == 404:
            await interaction.followup.send(f"❌ Project `{project}` not found on GitLab.", ephemeral=True)
            return
        if status not in (200, 201):
            await interaction.followup.send("❌ Could not verify project — GitLab API error.", ephemeral=True)
            return

        events_str = ",".join(sorted(parsed_events))
        added = await self.db.add_gitlab_subscription(
            guild_id=interaction.guild_id,  # type: ignore[arg-type]
            channel_id=target.id,
            project=project,
            events=events_str,
            added_by=interaction.user.id,
        )
        if not added:
            await self.db.update_gitlab_subscription_events(
                guild_id=interaction.guild_id,  # type: ignore[arg-type]
                channel_id=target.id,
                project=project,
                events=events_str,
            )
            await interaction.followup.send(
                f"✅ Updated subscription for `{project}` in {target.mention} — events: `{events_str}`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Subscribed {target.mention} to `{project}` — watching: `{events_str}`.",
                ephemeral=True,
            )

    @gitlab_group.command(name="unsubscribe", description="Remove a GitLab project subscription from a channel.")
    @app_commands.describe(
        project="Project in namespace/project format",
        channel="Channel the subscription is in (default: current channel)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gl_unsubscribe(
        self,
        interaction: discord.Interaction,
        project: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ Target must be a text channel.", ephemeral=True)
            return
        removed = await self.db.remove_gitlab_subscription(
            guild_id=interaction.guild_id,  # type: ignore[arg-type]
            channel_id=target.id,
            project=project,
        )
        if removed:
            await interaction.response.send_message(
                f"✅ Unsubscribed {target.mention} from `{project}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ No subscription found for `{project}` in {target.mention}.", ephemeral=True
            )

    @gitlab_group.command(name="subscriptions", description="List all GitLab subscriptions in this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def gl_subscriptions(self, interaction: discord.Interaction) -> None:
        subs = await self.db.get_gitlab_subscriptions(interaction.guild_id)  # type: ignore[arg-type]
        if not subs:
            await interaction.response.send_message("ℹ️ No GitLab subscriptions configured.", ephemeral=True)
            return
        em = discord.Embed(title="🦊 GitLab Subscriptions", color=GITLAB_COLOR)
        for sub in subs:
            ch = self.bot.get_channel(sub["channel_id"])
            ch_str = ch.mention if ch else f"<#{sub['channel_id']}>"
            em.add_field(
                name=sub["project"],
                value=f"Channel: {ch_str}\nEvents: `{sub['events']}`\nAdded: {_ts(sub['created_at'])}",
                inline=True,
            )
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ------------------------------------------------------------------ RAG ingestion

    @gitlab_group.command(name="ingest", description="Ingest a GitLab project's README/docs into the AI knowledge base.")
    @app_commands.describe(
        project="Project in namespace/project format",
        branch="Branch to ingest from (default: main)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gl_ingest(
        self,
        interaction: discord.Interaction,
        project: str,
        branch: str = "main",
    ) -> None:
        if not _PROJECT_RE.match(project):
            await interaction.response.send_message("❌ Invalid project format.", ephemeral=True)
            return

        support_cog = self.bot.get_cog("Support")
        if support_cog is None:
            await interaction.response.send_message(
                "❌ The Support/AI cog is not loaded — cannot ingest embeddings.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        encoded = _encoded_project(project)
        docs_to_fetch: list[tuple[str, str]] = []

        # README via API
        status, readme_data = await self.gl.get(
            f"/projects/{encoded}/repository/files/README.md?ref={quote_plus(branch)}"
        )
        if status == 200 and isinstance(readme_data, dict):
            raw_url = f"{self._base_url}/{project}/-/raw/{branch}/README.md"
            docs_to_fetch.append((f"{project}/README", raw_url))

        # Docs tree
        status, tree_data = await self.gl.get(
            f"/projects/{encoded}/repository/tree?ref={quote_plus(branch)}&recursive=true&per_page=100"
        )
        if status == 200 and isinstance(tree_data, list):
            for item in tree_data:
                path: str = item.get("path", "")
                if item.get("type") != "blob":
                    continue
                lower = path.lower()
                if any(lower.startswith(p) for p in ("docs/", "wiki/", "doc/")):
                    if lower.endswith((".md", ".rst", ".txt")):
                        raw_url = f"{self._base_url}/{project}/-/raw/{branch}/{path}"
                        docs_to_fetch.append((f"{project}/{path}", raw_url))
                        if len(docs_to_fetch) >= 30:
                            break

        if not docs_to_fetch:
            await interaction.followup.send("❌ No README or docs files found in that project.", ephemeral=True)
            return

        llm = getattr(support_cog, "llm", None)
        guild_id = interaction.guild_id

        ingested = 0
        skipped = 0
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            for label, raw_url in docs_to_fetch:
                headers: dict = {"User-Agent": "DiscordBot-GitLabCog/1.0"}
                if self.gl._token:
                    headers["PRIVATE-TOKEN"] = self.gl._token
                try:
                    async with session.get(
                        raw_url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
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

                embedding_bytes: bytes | None = None
                model: str | None = None
                if llm is not None:
                    try:
                        from bot.llm_service import LLMService  # noqa: F401
                        vec = await llm.get_embedding(text[:8000])
                        if vec:
                            import struct
                            embedding_bytes = struct.pack(f"{len(vec)}f", *vec)
                            model = getattr(llm, "_embedding_model", None)
                    except Exception as emb_exc:
                        logger.debug("Embedding failed for %s: %s", label, emb_exc)

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

        msg = f"✅ Ingested **{ingested}** file(s) from `{project}` into the knowledge base."
        if skipped:
            msg += f" ({skipped} skipped / not found)"
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Called by discord.py if loading via load_extension."""
    pass  # Loaded manually in main.py with db and config injected
