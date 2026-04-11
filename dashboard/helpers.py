"""Shared helpers used by all dashboard route modules.

Anything imported and used across multiple route files lives here:
session utilities, DB helpers, Discord OAuth helpers, guild queries, etc.
"""

from __future__ import annotations

import os
import re
import secrets
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import aiosqlite
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Bootstrap constants
# ---------------------------------------------------------------------------

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "").strip()
DISCORD_API_BASE = os.getenv("DISCORD_API_BASE", "https://discord.com/api/v10").rstrip("/")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bot.db")

DISCORD_OAUTH_SCOPES = ("identify", "guilds")
DISCORD_ADMINISTRATOR_PERMISSION = 0x8
DISCORD_MANAGE_GUILD_PERMISSION = 0x20

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


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


BOT_OWNER_DISCORD_ID = _safe_int(
    os.getenv("BOT_OWNER_DISCORD_ID") or os.getenv("MASTER_DISCORD_USER_ID")
)

# ---------------------------------------------------------------------------
# Template context helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ctx(extra: dict) -> dict:
    """Build template context with common fields (now timestamp)."""
    return {"now": _now(), **extra}


# ---------------------------------------------------------------------------
# Session / auth helpers
# ---------------------------------------------------------------------------


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


def require_auth(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return True


def auth_redirect(request: Request):
    """Returns redirect response if not authenticated, else None."""
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    return None


def require_master_user(request: Request) -> None:
    require_auth(request)
    if not is_master_session(request):
        raise HTTPException(status_code=403, detail="Only the bot owner can perform this action")


# ---------------------------------------------------------------------------
# Discord OAuth helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Guild access helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Raw DB helpers (dashboard uses aiosqlite directly, not the bot.db facade)
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


# ---------------------------------------------------------------------------
# Knowledge / crawl helpers
# ---------------------------------------------------------------------------


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
        await upsert_crawl_source(guild_id, source_url, title, len(kept_rows), crawled_at)
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
