"""Dashboard web application for the Discord bot.

Serves a web UI backed by FastAPI + Jinja2 templates.
Authentication is handled with Discord OAuth and per-guild access control.
"""

from __future__ import annotations

# Load .env before anything reads os.getenv
from dotenv import load_dotenv
load_dotenv()

import os
import time
import secrets
import logging
import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import aiosqlite
import httpx
from fastapi import FastAPI, Request, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from dashboard.dynamic_config_schema import DynamicConfigSchema
from dashboard.routes.github_integrations import GitHubIntegrationsModule
from dashboard.routes.gitlab_integrations import GitLabIntegrationsModule
from bot.config import Config
from bot.model_discovery import ModelDiscoveryService

logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "").strip()
DISCORD_API_BASE = os.getenv("DISCORD_API_BASE", "https://discord.com/api/v10").rstrip("/")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bot.db")

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Bot Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

DISCORD_OAUTH_SCOPES = ("identify", "guilds")
DISCORD_ADMINISTRATOR_PERMISSION = 0x8
DISCORD_MANAGE_GUILD_PERMISSION = 0x20


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


BOT_OWNER_DISCORD_ID = _safe_int(
    os.getenv("BOT_OWNER_DISCORD_ID") or os.getenv("MASTER_DISCORD_USER_ID")
)

# Initialize dynamic config schema
config = Config()
model_discovery = ModelDiscoveryService(config)
dynamic_schema = DynamicConfigSchema(model_discovery)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ctx(extra: dict) -> dict:
    """Build template context with common fields (now timestamp)."""
    return {"now": _now(), **extra}


def discord_oauth_configured() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI)


def build_discord_login_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "redirect_uri": DISCORD_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(DISCORD_OAUTH_SCOPES),
            "prompt": "none",
            "state": state,
        }
    )
    return f"https://discord.com/oauth2/authorize?{query}"


def build_login_context(request: Request, error: str | None = None) -> dict[str, Any]:
    oauth_ready = discord_oauth_configured()
    discord_login_url = None
    if oauth_ready:
        state = secrets.token_urlsafe(24)
        request.session["discord_oauth_state"] = state
        discord_login_url = build_discord_login_url(state)

    return ctx(
        {
            "error": error,
            "oauth_ready": oauth_ready,
            "discord_login_url": discord_login_url,
            "bot_owner_discord_id": BOT_OWNER_DISCORD_ID,
        }
    )


def get_session_user_id(request: Request) -> int | None:
    return _safe_int(request.session.get("discord_user_id"))


def is_master_user_id(user_id: int | None) -> bool:
    return bool(BOT_OWNER_DISCORD_ID is not None and user_id == BOT_OWNER_DISCORD_ID)


def is_master_session(request: Request) -> bool:
    return is_master_user_id(get_session_user_id(request))


def get_session_guild_ids(request: Request) -> list[int]:
    raw_ids = request.session.get("guild_access_ids") or []
    guild_ids: list[int] = []
    for raw_id in raw_ids:
        guild_id = _safe_int(raw_id)
        if guild_id is not None:
            guild_ids.append(guild_id)
    return guild_ids


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated") and get_session_user_id(request) is not None)


def discord_avatar_url(user: dict[str, Any]) -> str | None:
    user_id = user.get("id")
    avatar = user.get("avatar")
    if not user_id or not avatar:
        return None
    image_format = "gif" if str(avatar).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{image_format}?size=128"


def guild_is_manageable(guild: dict[str, Any]) -> bool:
    if guild.get("owner"):
        return True
    permissions = _safe_int(guild.get("permissions") or guild.get("permissions_new")) or 0
    required_permissions = DISCORD_ADMINISTRATOR_PERMISSION | DISCORD_MANAGE_GUILD_PERMISSION
    return bool(permissions & required_permissions)


async def fetch_discord_oauth_token(code: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail="Discord OAuth did not return an access token")
    return token


async def fetch_discord_identity(access_token: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        user_response = await client.get(f"{DISCORD_API_BASE}/users/@me")
        user_response.raise_for_status()
        guilds_response = await client.get(f"{DISCORD_API_BASE}/users/@me/guilds")
        guilds_response.raise_for_status()
    return user_response.json(), guilds_response.json()


async def get_accessible_guilds(request: Request) -> list[dict[str, Any]]:
    require_auth(request)
    guilds = await get_all_guilds()
    if is_master_session(request):
        return guilds

    allowed_guild_ids = set(get_session_guild_ids(request))
    return [guild for guild in guilds if _safe_int(guild.get("guild_id")) in allowed_guild_ids]


async def get_authorized_guilds(request: Request, guild_id: int | None = None) -> list[dict[str, Any]]:
    guilds = await get_accessible_guilds(request)
    if guild_id is None:
        return guilds

    if is_master_session(request):
        if guild_id not in {_safe_int(guild.get("guild_id")) for guild in guilds}:
            guilds = [*guilds, {"guild_id": guild_id, "guild_name": f"Guild {guild_id}"}]
        return guilds

    authorized_guild_ids = set(get_session_guild_ids(request))
    if guild_id not in authorized_guild_ids:
        raise HTTPException(status_code=403, detail="You do not have access to this guild")

    if guild_id not in {_safe_int(guild.get("guild_id")) for guild in guilds}:
        guilds = [*guilds, {"guild_id": guild_id, "guild_name": f"Guild {guild_id}"}]
    return guilds


async def require_guild_access(request: Request, guild_id: int) -> None:
    await get_authorized_guilds(request, guild_id)


def require_master_user(request: Request) -> None:
    require_auth(request)
    if not is_master_session(request):
        raise HTTPException(status_code=403, detail="Only the bot owner can perform this action")


def build_guild_scope_clause(guild_ids: list[int], column: str = "guild_id") -> tuple[str, tuple[Any, ...]]:
    if not guild_ids:
        return "1 = 0", ()
    placeholders = ", ".join("?" for _ in guild_ids)
    return f"{column} IN ({placeholders})", tuple(guild_ids)


async def count_scoped_rows(table: str, guild_ids: list[int], where: str | None = None, params: tuple[Any, ...] = ()) -> int:
    scope_clause, scope_params = build_guild_scope_clause(guild_ids)
    query = f"SELECT COUNT(*) AS c FROM {table} WHERE {scope_clause}"
    if where:
        query = f"{query} AND {where}"
    row = await db_fetchone(query, scope_params + params)
    return int(row["c"]) if row else 0


GUILD_ID_QUERIES = [
    "SELECT DISTINCT guild_id FROM guild_config WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM mod_cases WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM warnings WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM tickets WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM automod_filters WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM conversation_history WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM embeddings WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM custom_functions WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM token_usage WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM assistant_triggers WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM economy_accounts WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM custom_commands WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM reports WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM selfroles WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM command_permissions WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM levels WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM giveaways WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM reminders WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM starboard_messages WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM highlights WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM github_subscriptions WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM gitlab_subscriptions WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM learned_facts WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM response_feedback WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM crawl_sources WHERE guild_id IS NOT NULL",
]

LEGACY_UNKNOWN_EMBEDDING_MODEL = "legacy-unknown"
SQLITE_SOURCE_TABLE_RE = re.compile(r"FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


async def get_all_guilds() -> list[dict[str, Any]]:
    table_rows = await db_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    existing_tables = {str(row["name"]) for row in table_rows}

    available_queries = []
    for query in GUILD_ID_QUERIES:
        match = SQLITE_SOURCE_TABLE_RE.search(query)
        if not match:
            continue
        if match.group(1) in existing_tables:
            available_queries.append(query)

    if not available_queries:
        return []

    guild_union = " UNION ".join(available_queries)
    if "guild_config" in existing_tables:
        query = f"""
            WITH guilds AS (
                {guild_union}
            )
            SELECT
                guilds.guild_id,
                COALESCE(NULLIF(TRIM(meta.value), ''), 'Guild ' || guilds.guild_id) AS guild_name
            FROM guilds
            LEFT JOIN guild_config AS meta
                ON meta.guild_id = guilds.guild_id
               AND meta.key = 'guild_name'
            ORDER BY LOWER(COALESCE(NULLIF(TRIM(meta.value), ''), 'Guild ' || guilds.guild_id)), guilds.guild_id
        """
    else:
        query = f"""
            WITH guilds AS (
                {guild_union}
            )
            SELECT guild_id, 'Guild ' || guild_id AS guild_name
            FROM guilds
            ORDER BY guild_id
        """
    return await db_fetchall(query)


async def get_guild_config_map(guild_id: int) -> dict[str, str]:
    rows = await db_fetchall(
        "SELECT key, value FROM guild_config WHERE guild_id = ? ORDER BY key",
        (guild_id,),
    )
    return {row["key"]: row["value"] for row in rows}


def parse_csv_ids(value: str | None) -> list[int]:
    if not value:
        return []
    ids: list[int] = []
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError:
            continue
    return ids

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def db_fetchall(query: str, params: tuple = ()) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def db_fetchone(query: str, params: tuple = ()) -> dict | None:
    db = await get_db()
    try:
        cur = await db.execute(query, params)
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def db_execute(query: str, params: tuple = ()) -> int:
    db = await get_db()
    try:
        cur = await db.execute(query, params)
        await db.commit()
        return cur.rowcount
    finally:
        await db.close()


async def upsert_crawled_embedding(
    guild_id: int,
    name: str,
    text: str,
    model: str,
    source_url: str,
    qdrant_id: str,
) -> int:
    existing = await db_fetchone(
        "SELECT id FROM embeddings WHERE guild_id = ? AND name = ? ORDER BY id LIMIT 1",
        (guild_id, name),
    )
    if existing:
        return await db_execute(
            "UPDATE embeddings SET text = ?, model = ?, source_url = ?, qdrant_id = ? WHERE id = ?",
            (text, model, source_url, qdrant_id, existing["id"]),
        )
    return await db_execute(
        "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, name, text, model, source_url, qdrant_id),
    )


async def upsert_crawl_source(
    guild_id: int,
    url: str,
    title: str,
    chunk_count: int,
    crawled_at: str | None = None,
) -> int:
    existing = await db_fetchone(
        "SELECT id FROM crawl_sources WHERE guild_id = ? AND url = ? ORDER BY id LIMIT 1",
        (guild_id, url),
    )
    if existing:
        if crawled_at is None:
            return await db_execute(
                "UPDATE crawl_sources SET title = ?, chunk_count = ?, crawled_at = datetime('now') WHERE id = ?",
                (title, chunk_count, existing["id"]),
            )
        return await db_execute(
            "UPDATE crawl_sources SET title = ?, chunk_count = ?, crawled_at = ? WHERE id = ?",
            (title, chunk_count, crawled_at, existing["id"]),
        )
    if crawled_at is None:
        return await db_execute(
            "INSERT INTO crawl_sources (guild_id, url, title, chunk_count) VALUES (?, ?, ?, ?)",
            (guild_id, url, title, chunk_count),
        )
    return await db_execute(
        "INSERT INTO crawl_sources (guild_id, url, title, chunk_count, crawled_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, url, title, chunk_count, crawled_at),
    )


async def get_knowledge_entries(guild_id: int) -> list[dict]:
    return await db_fetchall(
        """
        WITH grouped AS (
            SELECT
                CASE
                    WHEN COALESCE(TRIM(source_url), '') != '' THEN 'url:' || source_url
                    ELSE 'name:' || name
                END AS entry_key,
                CASE
                    WHEN COALESCE(TRIM(source_url), '') != '' THEN 1
                    ELSE 0
                END AS is_crawled,
                source_url,
                MIN(id) AS id,
                MAX(name) AS fallback_name,
                MAX(NULLIF(model, '')) AS model,
                MIN(created_at) AS created_at,
                COUNT(*) AS chunk_count,
                SUM(LENGTH(text)) AS text_len
            FROM embeddings
            WHERE guild_id = ?
            GROUP BY entry_key, is_crawled, source_url
        )
        SELECT
            g.id,
            CASE
                WHEN g.is_crawled = 1 THEN COALESCE(NULLIF(cs.title, ''), g.source_url)
                ELSE g.fallback_name
            END AS name,
            g.model,
            g.source_url,
            g.created_at,
            g.chunk_count,
            g.text_len,
            g.is_crawled
        FROM grouped g
        LEFT JOIN crawl_sources cs
            ON cs.guild_id = ?
           AND cs.url = g.source_url
        ORDER BY LOWER(
            CASE
                WHEN g.is_crawled = 1 THEN COALESCE(NULLIF(cs.title, ''), g.source_url)
                ELSE g.fallback_name
            END
        )
        """,
        (guild_id, guild_id),
    )


async def get_crawl_sources_with_metadata(guild_id: int) -> list[dict]:
    return await db_fetchall(
        """
        SELECT
            cs.*,
            MIN(e.created_at) AS added_at,
            MAX(NULLIF(e.model, '')) AS model
        FROM crawl_sources cs
        LEFT JOIN embeddings e
            ON e.guild_id = cs.guild_id
           AND e.source_url = cs.url
        WHERE cs.guild_id = ?
        GROUP BY cs.id, cs.guild_id, cs.url, cs.title, cs.chunk_count, cs.crawled_at
        ORDER BY cs.crawled_at DESC
        """,
        (guild_id,),
    )


def _infer_crawl_title(source_url: str, rows: list[dict]) -> str:
    for row in rows:
        base = re.sub(r"\s*\[\d+\]$", "", (row.get("name") or "").strip())
        if base:
            return base
    return source_url


async def repair_legacy_crawl_metadata(guild_id: int, qdrant: Any | None = None) -> dict[str, int]:
    rows = await db_fetchall(
        "SELECT id, name, text, model, source_url, qdrant_id, created_at "
        "FROM embeddings WHERE guild_id = ? AND COALESCE(TRIM(source_url), '') != '' "
        "ORDER BY source_url, created_at, id",
        (guild_id,),
    )
    if not rows:
        return {"sources_repaired": 0, "duplicates_removed": 0, "models_filled": 0}

    if qdrant is None:
        from bot.qdrant_service import QdrantService
        qdrant = QdrantService()

    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row["source_url"], []).append(row)

    sources_repaired = 0
    duplicates_removed = 0
    models_filled = 0

    for source_url, source_rows in by_source.items():
        kept_rows: list[dict] = []
        duplicate_rows: list[dict] = []
        seen_texts: set[str] = set()
        for row in source_rows:
            text_key = (row.get("text") or "").strip()
            if text_key in seen_texts:
                duplicate_rows.append(row)
                continue
            seen_texts.add(text_key)
            kept_rows.append(row)

        if duplicate_rows:
            placeholders = ", ".join("?" for _ in duplicate_rows)
            params = (guild_id, *(row["id"] for row in duplicate_rows))
            await db_execute(
                f"DELETE FROM embeddings WHERE guild_id = ? AND id IN ({placeholders})",
                params,
            )
            for row in duplicate_rows:
                if row.get("qdrant_id"):
                    await qdrant.delete_embedding(guild_id, row["qdrant_id"])
            duplicates_removed += len(duplicate_rows)

        model_to_use = next(
            ((row.get("model") or "").strip() for row in kept_rows if (row.get("model") or "").strip()),
            LEGACY_UNKNOWN_EMBEDDING_MODEL,
        )
        for row in kept_rows:
            if not (row.get("model") or "").strip():
                await db_execute(
                    "UPDATE embeddings SET model = ? WHERE guild_id = ? AND id = ?",
                    (model_to_use, guild_id, row["id"]),
                )
                row["model"] = model_to_use
                models_filled += 1

        existing_source = await db_fetchone(
            "SELECT title, crawled_at FROM crawl_sources WHERE guild_id = ? AND url = ?",
            (guild_id, source_url),
        )
        title = (
            (existing_source or {}).get("title")
            or _infer_crawl_title(source_url, kept_rows)
        )
        crawled_at = (
            (existing_source or {}).get("crawled_at")
            or max((row.get("created_at") or "") for row in kept_rows)
            or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        )
        await upsert_crawl_source(
            guild_id,
            source_url,
            title,
            len(kept_rows),
            crawled_at,
        )
        sources_repaired += 1

    return {
        "sources_repaired": sources_repaired,
        "duplicates_removed": duplicates_removed,
        "models_filled": models_filled,
    }


async def clear_knowledge_base(guild_id: int, qdrant: Any | None = None) -> dict[str, int]:
    row = await db_fetchone(
        "SELECT COUNT(*) AS embeddings_count, "
        "COALESCE(SUM(CASE WHEN COALESCE(TRIM(source_url), '') != '' THEN 1 ELSE 0 END), 0) AS crawled_count "
        "FROM embeddings WHERE guild_id = ?",
        (guild_id,),
    ) or {"embeddings_count": 0, "crawled_count": 0}
    source_row = await db_fetchone(
        "SELECT COUNT(*) AS source_count FROM crawl_sources WHERE guild_id = ?",
        (guild_id,),
    ) or {"source_count": 0}

    await db_execute("DELETE FROM embeddings WHERE guild_id = ?", (guild_id,))
    await db_execute("DELETE FROM crawl_sources WHERE guild_id = ?", (guild_id,))

    if qdrant is None:
        from bot.qdrant_service import QdrantService
        qdrant = QdrantService()
    await qdrant.reset_embeddings(guild_id)

    return {
        "embeddings_cleared": int(row["embeddings_count"]),
        "crawled_chunks_cleared": int(row["crawled_count"]),
        "sources_cleared": int(source_row["source_count"]),
    }


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_auth(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return True


def auth_redirect(request: Request):
    """Returns redirect if not authenticated, else None."""
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    return None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", build_login_context(request))


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        build_login_context(request, "Password login is disabled. Sign in with Discord."),
    )


@app.get("/auth/discord/callback")
async def discord_auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return templates.TemplateResponse(
            request,
            "login.html",
            build_login_context(request, f"Discord login failed: {error}"),
        )
    if not discord_oauth_configured():
        return templates.TemplateResponse(
            request,
            "login.html",
            build_login_context(request, "Discord OAuth is not configured on the server."),
        )

    expected_state = request.session.pop("discord_oauth_state", None)
    if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return templates.TemplateResponse(
            request,
            "login.html",
            build_login_context(request, "Discord login session expired. Please try again."),
        )

    try:
        access_token = await fetch_discord_oauth_token(code)
        user, guilds = await fetch_discord_identity(access_token)
    except httpx.HTTPError as exc:
        logger.warning("Discord OAuth request failed: %s", exc)
        return templates.TemplateResponse(
            request,
            "login.html",
            build_login_context(request, "Unable to complete Discord login right now."),
        )

    user_id = _safe_int(user.get("id"))
    if user_id is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            build_login_context(request, "Discord login returned an invalid user profile."),
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


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# Overview / index
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request)
    guild_ids = [_safe_int(guild["guild_id"]) for guild in guilds]
    guild_ids = [guild_id for guild_id in guild_ids if guild_id is not None]

    # Gather top-level stats
    total_cases = await count_scoped_rows("mod_cases", guild_ids)
    open_tickets = await count_scoped_rows("tickets", guild_ids, "status != 'closed'")
    active_warnings = await count_scoped_rows("warnings", guild_ids, "active = 1")
    open_reports = await count_scoped_rows("reports", guild_ids, "status = 'open'")
    active_giveaways = await count_scoped_rows("giveaways", guild_ids, "status = 'active'")
    economy_accounts = await count_scoped_rows("economy_accounts", guild_ids)
    level_entries = await count_scoped_rows("levels", guild_ids)
    custom_commands = await count_scoped_rows("custom_commands", guild_ids)

    # Recent mod cases
    if guild_ids:
        scope_clause, scope_params = build_guild_scope_clause(guild_ids)
        recent_cases = await db_fetchall(
            f"SELECT * FROM mod_cases WHERE {scope_clause} ORDER BY id DESC LIMIT 10",
            scope_params,
        )
    else:
        recent_cases = []

    stats = {
        "total_cases": total_cases,
        "open_tickets": open_tickets,
        "active_warnings": active_warnings,
        "guild_count": len(guilds),
        "open_reports": open_reports,
        "active_giveaways": active_giveaways,
        "economy_accounts": economy_accounts,
        "level_entries": level_entries,
        "custom_commands": custom_commands,
    }

    return templates.TemplateResponse(request, "index.html", ctx({
        "stats": stats,
        "recent_cases": recent_cases,
        "guilds": guilds,
        "active_page": "overview",
    }))


# ---------------------------------------------------------------------------
# Guild config
# ---------------------------------------------------------------------------

@app.get("/config", response_class=HTMLResponse)
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
        # Convert to dict for easier lookup
        config_values = {row["key"]: row["value"] for row in config_rows}

    # Get dynamic configuration schema
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


@app.post("/config/set")
async def config_set(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    
    # Parse form data to extract config values
    form_data = await request.form()
    # Get current schema to validate keys
    config_schema = await dynamic_schema.get_config_schema()
    
    # Process each config key that was submitted
    for key, value in form_data.items():
        if key.startswith("config_"):
            config_key = key[7:]  # Remove "config_" prefix
            
            # Only process keys that are in our schema
            if config_key in config_schema:
                # Handle empty values for select fields (set to default)
                if value.strip() == "":
                    default_value = config_schema[config_key].get("default", "")
                    if default_value:
                        logger.debug(f"Setting {config_key} to default value: {default_value}")
                        await db_execute(
                            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                            (guild_id, config_key, default_value),
                        )
                    else:
                        # Remove the config if no default and empty value
                        logger.debug(f"Removing {config_key} (empty value, no default)")
                        await db_execute(
                            "DELETE FROM guild_config WHERE guild_id = ? AND key = ?",
                            (guild_id, config_key)
                        )
                else:
                    logger.debug(f"Setting {config_key} to value: {value}")
                    await db_execute(
                        "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                        "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                        (guild_id, config_key, str(value)),
                    )
    
    return RedirectResponse(f"/config?guild_id={guild_id}", status_code=302)


@app.post("/config/delete")
async def config_delete(request: Request, guild_id: int = Form(...), key: str = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
    return RedirectResponse(f"/config?guild_id={guild_id}", status_code=302)


@app.post("/api/refresh-models")
async def refresh_models(request: Request):
    """API endpoint to refresh the model list."""
    if r := auth_redirect(request):
        return r
    require_master_user(request)
    
    try:
        await dynamic_schema.refresh_models()
        return JSONResponse({"success": True, "message": "Model list refreshed successfully"})
    except Exception as e:
        logger.error(f"Failed to refresh models: {e}")
        return JSONResponse({"success": False, "message": f"Failed to refresh models: {str(e)}"}, status_code=500)


# ---------------------------------------------------------------------------
# Assistant management
# ---------------------------------------------------------------------------

@app.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    config_values: dict[str, str] = {}
    triggers: list[dict] = []
    custom_functions: list[dict] = []
    listen_channels: list[dict[str, int | str]] = []
    channel_prompts: list[dict[str, int | str]] = []
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

    return templates.TemplateResponse(request, "assistant.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "config_values": config_values,
        "triggers": triggers,
        "custom_functions": custom_functions,
        "listen_channels": listen_channels,
        "channel_prompts": channel_prompts,
        "usage": usage,
        "conversations": conversations,
        "active_page": "assistant",
    }))


@app.post("/assistant/triggers/add")
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


@app.post("/assistant/triggers/delete")
async def assistant_trigger_delete(request: Request, guild_id: int = Form(...), trigger_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM assistant_triggers WHERE id = ? AND guild_id = ?", (trigger_id, guild_id))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/functions/save")
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


@app.post("/assistant/functions/toggle")
async def assistant_function_toggle(request: Request, guild_id: int = Form(...), function_id: int = Form(...), enabled: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute(
        "UPDATE custom_functions SET enabled = ? WHERE id = ? AND guild_id = ?",
        (enabled, function_id, guild_id),
    )
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/functions/delete")
async def assistant_function_delete(request: Request, guild_id: int = Form(...), function_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM custom_functions WHERE id = ? AND guild_id = ?", (function_id, guild_id))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/listen/save")
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


@app.post("/assistant/channel-prompts/save")
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


@app.post("/assistant/channel-prompts/delete")
async def assistant_channel_prompt_delete(request: Request, guild_id: int = Form(...), channel_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, f"channel_prompt_{channel_id}"))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/usage/reset")
async def assistant_usage_reset(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM token_usage WHERE guild_id = ?", (guild_id,))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/conversations/reset")
async def assistant_conversations_reset(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM conversation_history WHERE guild_id = ?", (guild_id,))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Community systems management
# ---------------------------------------------------------------------------

@app.get("/community", response_class=HTMLResponse)
async def community_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    config_values: dict[str, str] = {}
    selfroles: list[dict] = []
    highlights: list[dict] = []
    starboard_entries: list[dict] = []

    if guild_id:
        config_values = await get_guild_config_map(guild_id)
        selfroles = await db_fetchall(
            "SELECT role_id FROM selfroles WHERE guild_id = ? ORDER BY role_id",
            (guild_id,),
        )
        highlights = await db_fetchall(
            "SELECT h.user_id, h.keyword, COALESCE((SELECT gc.value FROM guild_config gc WHERE gc.guild_id = h.guild_id AND gc.key = 'highlight_pause_' || h.user_id), '0') AS paused "
            "FROM highlights h WHERE h.guild_id = ? ORDER BY h.user_id, h.keyword",
            (guild_id,),
        )
        starboard_entries = await db_fetchall(
            "SELECT * FROM starboard_messages WHERE guild_id = ? ORDER BY star_count DESC, created_at DESC LIMIT 50",
            (guild_id,),
        )

    return templates.TemplateResponse(request, "community.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "config_values": config_values,
        "selfroles": selfroles,
        "highlights": highlights,
        "starboard_entries": starboard_entries,
        "active_page": "community",
    }))


@app.post("/community/selfroles/add")
async def community_selfrole_add(request: Request, guild_id: int = Form(...), role_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("INSERT OR IGNORE INTO selfroles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/selfroles/delete")
async def community_selfrole_delete(request: Request, guild_id: int = Form(...), role_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM selfroles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/highlights/add")
async def community_highlight_add(request: Request, guild_id: int = Form(...), user_id: int = Form(...), keyword: str = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    keyword = keyword.strip().lower()
    if keyword:
        await db_execute("INSERT OR IGNORE INTO highlights (user_id, guild_id, keyword) VALUES (?, ?, ?)", (user_id, guild_id, keyword))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/highlights/delete")
async def community_highlight_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...), keyword: str = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM highlights WHERE guild_id = ? AND user_id = ? AND keyword = ?", (guild_id, user_id, keyword))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/highlights/toggle-pause")
async def community_highlight_toggle_pause(request: Request, guild_id: int = Form(...), user_id: int = Form(...), paused: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    key = f"highlight_pause_{user_id}"
    await db_execute(
        "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
        (guild_id, key, "1" if paused else "0"),
    )
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/starboard/save")
async def community_starboard_save(
    request: Request,
    guild_id: int = Form(...),
    starboard_enabled: str = Form("1"),
    starboard_channel: str = Form(""),
    starboard_threshold: str = Form("3"),
    starboard_emoji: str = Form("⭐"),
    starboard_ignore_channels: str = Form(""),
):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    settings = {
        "starboard_enabled": starboard_enabled,
        "starboard_channel": starboard_channel.strip(),
        "starboard_threshold": starboard_threshold.strip() or "3",
        "starboard_emoji": starboard_emoji.strip() or "⭐",
        "starboard_ignore_channels": starboard_ignore_channels.strip(),
    }
    for key, value in settings.items():
        if value:
            await db_execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, value),
            )
        else:
            await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Integrations management
# ---------------------------------------------------------------------------

github_integrations = GitHubIntegrationsModule(
    templates=templates,
    ctx=ctx,
    auth_redirect=auth_redirect,
    get_authorized_guilds=get_authorized_guilds,
    get_guild_config_map=get_guild_config_map,
    require_guild_access=require_guild_access,
    db_fetchall=db_fetchall,
    db_fetchone=db_fetchone,
    db_execute=db_execute,
    github_token=config.github_token,
    github_token_configured=bool(config.github_token),
)
app.include_router(github_integrations.router)

gitlab_integrations = GitLabIntegrationsModule(
    templates=templates,
    ctx=ctx,
    auth_redirect=auth_redirect,
    get_authorized_guilds=get_authorized_guilds,
    get_guild_config_map=get_guild_config_map,
    require_guild_access=require_guild_access,
    db_fetchall=db_fetchall,
    db_fetchone=db_fetchone,
    db_execute=db_execute,
    gitlab_token=config.gitlab_token,
    gitlab_token_configured=bool(config.gitlab_token),
    gitlab_url=config.gitlab_url,
)
app.include_router(gitlab_integrations.router)

# Backward-compatible aliases for tests and existing imports.
integrations_page = github_integrations.integrations_page
integrations_github_save = github_integrations.integrations_github_save
integrations_github_delete = github_integrations.integrations_github_delete
integrations_github_reset_state = github_integrations.integrations_github_reset_state
integrations_github_workflow_save = github_integrations.integrations_github_workflow_save
integrations_github_user_link_save = github_integrations.integrations_github_user_link_save
integrations_github_user_link_delete = github_integrations.integrations_github_user_link_delete


# ---------------------------------------------------------------------------
# Permission overrides
# ---------------------------------------------------------------------------

@app.get("/permissions", response_class=HTMLResponse)
async def permissions_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    permission_rows: list[dict] = []

    if guild_id:
        permission_rows = await db_fetchall(
            "SELECT * FROM command_permissions WHERE guild_id = ? ORDER BY command, target_type, target_id",
            (guild_id,),
        )

    return templates.TemplateResponse(request, "permissions.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "permission_rows": permission_rows,
        "active_page": "permissions",
    }))


@app.post("/permissions/save")
async def permissions_save(
    request: Request,
    guild_id: int = Form(...),
    command: str = Form(...),
    target_type: str = Form(...),
    target_id: int = Form(...),
    allowed: int = Form(...),
):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    if target_type not in {"role", "channel", "user"}:
        raise HTTPException(status_code=400, detail="Invalid target type")
    await db_execute(
        "INSERT INTO command_permissions (guild_id, command, target_type, target_id, allowed) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, command, target_type, target_id) DO UPDATE SET allowed = excluded.allowed",
        (guild_id, command.strip().lstrip("/"), target_type, target_id, allowed),
    )
    return RedirectResponse(f"/permissions?guild_id={guild_id}", status_code=302)


@app.post("/permissions/delete")
async def permissions_delete(request: Request, guild_id: int = Form(...), permission_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM command_permissions WHERE id = ? AND guild_id = ?", (permission_id, guild_id))
    return RedirectResponse(f"/permissions?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Moderation cases
# ---------------------------------------------------------------------------

@app.get("/moderation", response_class=HTMLResponse)
async def moderation_page(request: Request, guild_id: int | None = None, user_id: int | None = None, page: int = 1):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    per_page = 25
    offset = (page - 1) * per_page

    cases = []
    total = 0
    if guild_id:
        if user_id:
            total_row = await db_fetchone("SELECT COUNT(*) as c FROM mod_cases WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            total = total_row["c"] if total_row else 0
            cases = await db_fetchall(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (guild_id, user_id, per_page, offset),
            )
        else:
            total_row = await db_fetchone("SELECT COUNT(*) as c FROM mod_cases WHERE guild_id = ?", (guild_id,))
            total = total_row["c"] if total_row else 0
            cases = await db_fetchall(
                "SELECT * FROM mod_cases WHERE guild_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (guild_id, per_page, offset),
            )

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "moderation.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "user_id": user_id,
        "cases": cases,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "active_page": "moderation",
    }))


@app.post("/moderation/delete")
async def moderation_delete(request: Request, case_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM mod_cases WHERE id = ? AND guild_id = ?", (case_id, guild_id))
    return RedirectResponse(f"/moderation?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

@app.get("/warnings", response_class=HTMLResponse)
async def warnings_page(request: Request, guild_id: int | None = None, user_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    warnings = []
    if guild_id:
        if user_id:
            warnings = await db_fetchall(
                "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY id DESC",
                (guild_id, user_id),
            )
        else:
            warnings = await db_fetchall(
                "SELECT * FROM warnings WHERE guild_id = ? AND active = 1 ORDER BY id DESC",
                (guild_id,),
            )

    return templates.TemplateResponse(request, "warnings.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "user_id": user_id,
        "warnings": warnings,
        "active_page": "warnings",
    }))


@app.post("/warnings/delete")
async def warning_delete(request: Request, warning_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("UPDATE warnings SET active = 0 WHERE id = ? AND guild_id = ?", (warning_id, guild_id))
    return RedirectResponse(f"/warnings?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request, guild_id: int | None = None, status: str = "open"):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    tickets = []
    if guild_id:
        if status == "all":
            tickets = await db_fetchall(
                "SELECT * FROM tickets WHERE guild_id = ? ORDER BY id DESC",
                (guild_id,),
            )
        else:
            tickets = await db_fetchall(
                "SELECT * FROM tickets WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                (guild_id, status),
            )

    return templates.TemplateResponse(request, "tickets.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "status": status,
        "tickets": tickets,
        "active_page": "tickets",
    }))


@app.get("/tickets/{ticket_id}/transcript", response_class=HTMLResponse)
async def ticket_transcript(request: Request, ticket_id: int):
    if r := auth_redirect(request):
        return r

    ticket = await db_fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    await require_guild_access(request, int(ticket["guild_id"]))
    messages = await db_fetchall(
        "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id",
        (ticket_id,),
    )

    return templates.TemplateResponse(request, "ticket_transcript.html", ctx({
        "ticket": ticket,
        "messages": messages,
        "active_page": "tickets",
    }))


# ---------------------------------------------------------------------------
# Auto-mod filters
# ---------------------------------------------------------------------------

@app.get("/automod", response_class=HTMLResponse)
async def automod_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    filters = []
    if guild_id:
        filters = await db_fetchall(
            "SELECT * FROM automod_filters WHERE guild_id = ? ORDER BY filter_type, pattern",
            (guild_id,),
        )

    return templates.TemplateResponse(request, "automod.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "filters": filters,
        "active_page": "automod",
    }))


@app.post("/automod/add")
async def automod_add(request: Request, guild_id: int = Form(...), filter_type: str = Form(...), pattern: str = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    try:
        await db_execute(
            "INSERT OR IGNORE INTO automod_filters (guild_id, filter_type, pattern) VALUES (?, ?, ?)",
            (guild_id, filter_type, pattern),
        )
    except Exception:
        pass
    return RedirectResponse(f"/automod?guild_id={guild_id}", status_code=302)


@app.post("/automod/delete")
async def automod_delete(request: Request, filter_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM automod_filters WHERE id = ? AND guild_id = ?", (filter_id, guild_id))
    return RedirectResponse(f"/automod?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------

@app.get("/economy", response_class=HTMLResponse)
async def economy_page(request: Request, guild_id: int | None = None, page: int = 1):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    per_page = 25
    offset = (page - 1) * per_page
    accounts = []
    total = 0

    if guild_id:
        total_row = await db_fetchone("SELECT COUNT(*) as c FROM economy_accounts WHERE guild_id = ?", (guild_id,))
        total = total_row["c"] if total_row else 0
        accounts = await db_fetchall(
            "SELECT * FROM economy_accounts WHERE guild_id = ? ORDER BY balance DESC LIMIT ? OFFSET ?",
            (guild_id, per_page, offset),
        )

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "economy.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "accounts": accounts,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "active_page": "economy",
    }))


@app.post("/economy/set")
async def economy_set(request: Request, guild_id: int = Form(...), user_id: int = Form(...), balance: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute(
        "INSERT INTO economy_accounts (guild_id, user_id, balance) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = excluded.balance",
        (guild_id, user_id, balance),
    )
    return RedirectResponse(f"/economy?guild_id={guild_id}", status_code=302)


@app.post("/economy/delete")
async def economy_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM economy_accounts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    return RedirectResponse(f"/economy?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

@app.get("/levels", response_class=HTMLResponse)
async def levels_page(request: Request, guild_id: int | None = None, page: int = 1):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    per_page = 25
    offset = (page - 1) * per_page
    entries = []
    total = 0

    if guild_id:
        total_row = await db_fetchone("SELECT COUNT(*) as c FROM levels WHERE guild_id = ?", (guild_id,))
        total = total_row["c"] if total_row else 0
        entries = await db_fetchall(
            "SELECT * FROM levels WHERE guild_id = ? ORDER BY xp DESC LIMIT ? OFFSET ?",
            (guild_id, per_page, offset),
        )

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "levels.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "entries": entries,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "active_page": "levels",
    }))


@app.post("/levels/set")
async def levels_set(request: Request, guild_id: int = Form(...), user_id: int = Form(...), xp: int = Form(...), level: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute(
        "INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level",
        (guild_id, user_id, xp, level),
    )
    return RedirectResponse(f"/levels?guild_id={guild_id}", status_code=302)


@app.post("/levels/delete")
async def levels_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    return RedirectResponse(f"/levels?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Giveaways
# ---------------------------------------------------------------------------

@app.get("/giveaways", response_class=HTMLResponse)
async def giveaways_page(request: Request, guild_id: int | None = None, status: str = "active"):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    giveaways = []
    if guild_id:
        if status == "all":
            giveaways = await db_fetchall(
                "SELECT * FROM giveaways WHERE guild_id = ? ORDER BY id DESC",
                (guild_id,),
            )
        else:
            giveaways = await db_fetchall(
                "SELECT * FROM giveaways WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                (guild_id, status),
            )

    return templates.TemplateResponse(request, "giveaways.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "status": status,
        "giveaways": giveaways,
        "active_page": "giveaways",
    }))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, guild_id: int | None = None, status: str = "open"):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    reports = []
    if guild_id:
        if status == "all":
            reports = await db_fetchall(
                "SELECT * FROM reports WHERE guild_id = ? ORDER BY id DESC",
                (guild_id,),
            )
        else:
            reports = await db_fetchall(
                "SELECT * FROM reports WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                (guild_id, status),
            )

    return templates.TemplateResponse(request, "reports.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "status": status,
        "reports": reports,
        "active_page": "reports",
    }))


@app.post("/reports/resolve")
async def reports_resolve(request: Request, report_id: int = Form(...), guild_id: int = Form(...), note: str = Form("")):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute(
        "UPDATE reports SET status = 'resolved', resolution_note = ?, resolved_at = datetime('now') WHERE id = ? AND guild_id = ?",
        (note, report_id, guild_id),
    )
    return RedirectResponse(f"/reports?guild_id={guild_id}", status_code=302)


@app.post("/reports/dismiss")
async def reports_dismiss(request: Request, report_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute(
        "UPDATE reports SET status = 'dismissed', resolved_at = datetime('now') WHERE id = ? AND guild_id = ?",
        (report_id, guild_id),
    )
    return RedirectResponse(f"/reports?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Custom commands
# ---------------------------------------------------------------------------

@app.get("/custom-commands", response_class=HTMLResponse)
async def custom_commands_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    commands = []
    if guild_id:
        commands = await db_fetchall(
            "SELECT * FROM custom_commands WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )

    return templates.TemplateResponse(request, "custom_commands.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "commands": commands,
        "active_page": "custom_commands",
    }))


@app.post("/custom-commands/add")
async def custom_commands_add(request: Request, guild_id: int = Form(...), name: str = Form(...), response: str = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    try:
        await db_execute(
            "INSERT OR REPLACE INTO custom_commands (guild_id, name, response, creator_id) VALUES (?, ?, ?, 0)",
            (guild_id, name.lower().strip(), response),
        )
    except Exception:
        pass
    return RedirectResponse(f"/custom-commands?guild_id={guild_id}", status_code=302)


@app.post("/custom-commands/delete")
async def custom_commands_delete(request: Request, cmd_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM custom_commands WHERE id = ? AND guild_id = ?", (cmd_id, guild_id))
    return RedirectResponse(f"/custom-commands?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

@app.get("/reminders", response_class=HTMLResponse)
async def reminders_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    reminders = []
    if guild_id:
        reminders = await db_fetchall(
            "SELECT * FROM reminders WHERE guild_id = ? ORDER BY end_time ASC",
            (guild_id,),
        )

    return templates.TemplateResponse(request, "reminders.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "reminders": reminders,
        "active_page": "reminders",
    }))


@app.post("/reminders/delete")
async def reminders_delete(request: Request, reminder_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    return RedirectResponse(f"/reminders?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Knowledge base / embeddings + Training + Learned facts + Feedback
# ---------------------------------------------------------------------------

@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(
    request: Request,
    guild_id: int | None = None,
    tab: str = "crawl",
    repair: int = 0,
    cleared: int = 0,
    sources_repaired: int = 0,
    duplicates_removed: int = 0,
    models_filled: int = 0,
    embeddings_cleared: int = 0,
    crawled_chunks_cleared: int = 0,
    sources_cleared: int = 0,
):
    if r := auth_redirect(request):
        return r

    guilds = await get_authorized_guilds(request, guild_id)
    entries = []
    sources = []
    learned_facts = []
    feedback_stats: dict = {"total": 0, "positive": 0, "negative": 0}
    recent_negative: list = []
    repair_summary = None
    clear_summary = None

    if guild_id:
        entries = await get_knowledge_entries(guild_id)
        sources = await get_crawl_sources_with_metadata(guild_id)
        try:
            learned_facts = await db_fetchall(
                "SELECT id, fact, source, confidence, approved, created_at "
                "FROM learned_facts WHERE guild_id = ? ORDER BY id DESC",
                (guild_id,),
            )
        except Exception:
            learned_facts = []
        try:
            row = await db_fetchone(
                "SELECT COUNT(*) as total, "
                "COALESCE(SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END),0) as positive, "
                "COALESCE(SUM(CASE WHEN rating=-1 THEN 1 ELSE 0 END),0) as negative "
                "FROM response_feedback WHERE guild_id = ?",
                (guild_id,),
            )
            if row:
                feedback_stats = dict(row)
        except Exception:
            pass
        try:
            recent_negative = await db_fetchall(
                "SELECT user_input, bot_response, created_at FROM response_feedback "
                "WHERE guild_id = ? AND rating = -1 ORDER BY created_at DESC LIMIT 10",
                (guild_id,),
            )
        except Exception:
            recent_negative = []

    if repair:
        repair_summary = {
            "sources_repaired": sources_repaired,
            "duplicates_removed": duplicates_removed,
            "models_filled": models_filled,
        }
    if cleared:
        clear_summary = {
            "embeddings_cleared": embeddings_cleared,
            "crawled_chunks_cleared": crawled_chunks_cleared,
            "sources_cleared": sources_cleared,
        }

    return templates.TemplateResponse(request, "knowledge.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "entries": entries,
        "sources": sources,
        "learned_facts": learned_facts,
        "feedback_stats": feedback_stats,
        "recent_negative": recent_negative,
        "repair_summary": repair_summary,
        "clear_summary": clear_summary,
        "active_tab": tab,
        "active_page": "knowledge",
    }))


@app.post("/knowledge/delete")
async def knowledge_delete(request: Request, entry_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    row = await db_fetchone("SELECT qdrant_id FROM embeddings WHERE id = ? AND guild_id = ?", (entry_id, guild_id))
    await db_execute("DELETE FROM embeddings WHERE id = ? AND guild_id = ?", (entry_id, guild_id))
    if row and row["qdrant_id"]:
        from bot.qdrant_service import QdrantService
        await QdrantService().delete_embedding(guild_id, row["qdrant_id"])
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=embeddings", status_code=302)


@app.post("/knowledge/delete-source")
async def knowledge_delete_source(request: Request, source_url: str = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM embeddings WHERE guild_id = ? AND source_url = ?", (guild_id, source_url))
    await db_execute("DELETE FROM crawl_sources WHERE guild_id = ? AND url = ?", (guild_id, source_url))
    from bot.qdrant_service import QdrantService
    await QdrantService().delete_embeddings_by_source(guild_id, source_url)
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=crawl", status_code=302)


@app.post("/knowledge/repair-crawl-metadata")
async def knowledge_repair_crawl_metadata(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)

    summary = await repair_legacy_crawl_metadata(guild_id)
    query = urlencode({
        "guild_id": guild_id,
        "tab": "crawl",
        "repair": 1,
        "sources_repaired": summary["sources_repaired"],
        "duplicates_removed": summary["duplicates_removed"],
        "models_filled": summary["models_filled"],
    })
    return RedirectResponse(f"/knowledge?{query}", status_code=302)


@app.post("/knowledge/reset")
async def knowledge_reset(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)

    summary = await clear_knowledge_base(guild_id)
    query = urlencode({
        "guild_id": guild_id,
        "tab": "embeddings",
        "cleared": 1,
        "embeddings_cleared": summary["embeddings_cleared"],
        "crawled_chunks_cleared": summary["crawled_chunks_cleared"],
        "sources_cleared": summary["sources_cleared"],
    })
    return RedirectResponse(f"/knowledge?{query}", status_code=302)


@app.post("/knowledge/add-fact")
async def knowledge_add_fact(request: Request, guild_id: int = Form(...), fact: str = Form(...), source: str = Form("training")):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    try:
        await db_execute(
            "INSERT OR IGNORE INTO learned_facts (guild_id, fact, source, approved) VALUES (?, ?, ?, 1)",
            (guild_id, fact.strip(), source),
        )
    except Exception:
        pass
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/delete-fact")
async def knowledge_delete_fact(request: Request, fact_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    row = await db_fetchone("SELECT qdrant_id FROM learned_facts WHERE id = ? AND guild_id = ?", (fact_id, guild_id))
    await db_execute("DELETE FROM learned_facts WHERE id = ? AND guild_id = ?", (fact_id, guild_id))
    if row and row["qdrant_id"]:
        from bot.qdrant_service import QdrantService
        await QdrantService().delete_fact(guild_id, row["qdrant_id"])
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/toggle-fact")
async def knowledge_toggle_fact(request: Request, fact_id: int = Form(...), guild_id: int = Form(...), approved: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    row = await db_fetchone("SELECT qdrant_id FROM learned_facts WHERE id = ? AND guild_id = ?", (fact_id, guild_id))
    await db_execute(
        "UPDATE learned_facts SET approved = ? WHERE id = ? AND guild_id = ?",
        (approved, fact_id, guild_id),
    )
    if row and row["qdrant_id"]:
        from bot.qdrant_service import QdrantService
        await QdrantService().set_fact_approved(guild_id, row["qdrant_id"], int(approved))
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/reset-facts")
async def knowledge_reset_facts(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM learned_facts WHERE guild_id = ?", (guild_id,))
    from bot.qdrant_service import QdrantService
    await QdrantService().reset_facts(guild_id)
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/reset-feedback")
async def knowledge_reset_feedback(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    await db_execute("DELETE FROM response_feedback WHERE guild_id = ?", (guild_id,))
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=feedback", status_code=302)


# ---------------------------------------------------------------------------
# API: async crawl (JSON — called by dashboard JS)
# ---------------------------------------------------------------------------

# In-memory job store for crawl progress (keyed by job_id)
_crawl_jobs: dict[str, dict] = {}


async def _run_crawl(job_id: str, guild_id: int, url: str, max_pages: int, chunk_size: int, replace: bool) -> None:
    """Background crawl task: fetches pages, embeds chunks, stores in Qdrant + SQLite metadata."""
    import re as _re
    import uuid as _uuid
    from urllib.parse import urlparse as _up
    from bot.crawler import WebCrawler
    from bot.qdrant_service import QdrantService
    from openai import AsyncOpenAI

    job = _crawl_jobs[job_id]
    job["status"] = "running"
    stored = 0
    pages = 0

    # LLM client for embeddings
    llm_client = AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("LLM_API_KEY", ""),
    )
    # Use the guild's configured embedding model (fallback to a sensible default)
    _emb_row = await db_fetchone(
        "SELECT value FROM guild_config WHERE guild_id = ? AND key = 'assistant_embedding_model'",
        (guild_id,),
    )
    emb_model = (_emb_row["value"] if _emb_row else None) or "qwen3-embedding-8b"
    qdrant = QdrantService()

    async def _embed(text: str) -> list[float] | None:
        try:
            resp = await llm_client.embeddings.create(model=emb_model, input=text)
            return resp.data[0].embedding
        except Exception:
            return None

    try:
        crawler = WebCrawler(chunk_size=max(200, min(chunk_size, 4000)), max_pages=max_pages)
        async for result in crawler.crawl_site(url, max_pages=max_pages, same_origin_only=True):
            pages += 1
            job["pages"] = pages
            if replace:
                await db_execute(
                    "DELETE FROM embeddings WHERE guild_id = ? AND source_url = ?",
                    (guild_id, result.url),
                )
                await db_execute(
                    "DELETE FROM crawl_sources WHERE guild_id = ? AND url = ?",
                    (guild_id, result.url),
                )
                await qdrant.delete_embeddings_by_source(guild_id, result.url)

            _slug = _re.sub(r"[^a-z0-9]+", "-", _up(result.url).netloc + _up(result.url).path, flags=_re.IGNORECASE).strip("-")[:50]
            prefix = f"{(result.title or '')[:30]}|{_slug}".strip("|") or _slug or "page"
            for i, chunk in enumerate(result.chunks):
                entry_name = f"{prefix} [{i+1}]"
                point_id = str(_uuid.uuid4())
                try:
                    await upsert_crawled_embedding(
                        guild_id,
                        entry_name,
                        chunk,
                        emb_model,
                        result.url,
                        point_id,
                    )
                    stored += 1
                except Exception as exc:
                    logger.warning("DB insert failed for chunk %d: %s", i, exc)
                    continue
                vec = await _embed(chunk)
                if vec:
                    await qdrant.upsert_embedding(guild_id, point_id, vec, entry_name, chunk, emb_model, source_url=result.url)
            # Upsert crawl_source record
            try:
                await upsert_crawl_source(
                    guild_id,
                    result.url,
                    result.title or "",
                    len(result.chunks),
                )
            except Exception:
                pass
            job["chunks"] = stored
        job["status"] = "done"
        job["chunks"] = stored
        job["pages"] = pages
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        logger.exception("Crawl job %s failed", job_id)


@app.post("/api/crawl/start")
async def api_crawl_start(
    request: Request,
    background_tasks: BackgroundTasks,
    guild_id: int = Form(...),
    url: str = Form(...),
    max_pages: int = Form(10),
    chunk_size: int = Form(800),
    replace: bool = Form(True),
):
    if r := auth_redirect(request):
        return r
    await require_guild_access(request, guild_id)
    import uuid
    job_id = str(uuid.uuid4())[:8]
    _crawl_jobs[job_id] = {
        "status": "queued",
        "pages": 0,
        "chunks": 0,
        "error": None,
        "guild_id": guild_id,
        "user_id": get_session_user_id(request),
    }
    background_tasks.add_task(_run_crawl, job_id, guild_id, url, max_pages, chunk_size, replace)
    return JSONResponse({"job_id": job_id})


@app.get("/api/crawl/status/{job_id}")
async def api_crawl_status(request: Request, job_id: str):
    if not is_authenticated(request):
        raise HTTPException(401)
    job = _crawl_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    current_user_id = get_session_user_id(request)
    if not is_master_session(request) and job.get("user_id") != current_user_id:
        raise HTTPException(403, "You do not have access to this crawl job")
    return JSONResponse(job)


# ---------------------------------------------------------------------------
# JSON API (for live stats / AJAX)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def api_stats(request: Request):
    require_auth(request)
    guilds = await get_accessible_guilds(request)
    guild_ids = [_safe_int(guild["guild_id"]) for guild in guilds]
    guild_ids = [guild_id for guild_id in guild_ids if guild_id is not None]
    return {
        "total_cases": await count_scoped_rows("mod_cases", guild_ids),
        "open_tickets": await count_scoped_rows("tickets", guild_ids, "status != 'closed'"),
        "active_warnings": await count_scoped_rows("warnings", guild_ids, "active = 1"),
        "open_reports": await count_scoped_rows("reports", guild_ids, "status = 'open'"),
        "active_giveaways": await count_scoped_rows("giveaways", guild_ids, "status = 'active'"),
    }


@app.get("/api/guilds")
async def api_guilds(request: Request):
    require_auth(request)
    guilds = await get_accessible_guilds(request)
    return [g["guild_id"] for g in guilds]
