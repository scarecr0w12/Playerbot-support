"""Pure embed-builder functions and review/triage helpers for the GitHub cog.

All functions here are stateless and have no Discord.py cog dependencies.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord

from bot.github_client import GITHUB_COLOR, GITHUB_API, MAX_TRIAGE_ITEMS

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")

# ---------------------------------------------------------------------------
# Generic helpers
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


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Review queue helpers
# ---------------------------------------------------------------------------


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


def _reviewer_load_lines(queue: list[tuple[dict, list[dict]]], stale_cutoff: datetime) -> list[str]:
    return _review_load_lines(queue, stale_cutoff, teams=False)


def _team_load_lines(queue: list[tuple[dict, list[dict]]], stale_cutoff: datetime) -> list[str]:
    return _review_load_lines(queue, stale_cutoff, teams=True)


# ---------------------------------------------------------------------------
# Discord embed builders — review / triage / issue
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Discord embed builders — API response embeds
# ---------------------------------------------------------------------------


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
# Discord embed builders — event notification embeds (polling)
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
