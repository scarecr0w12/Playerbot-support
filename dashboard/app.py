"""Dashboard web application for the Discord bot.

Serves a web UI backed by FastAPI + Jinja2 templates.
Authentication is handled via a secret token stored in DASHBOARD_SECRET env var.
"""

from __future__ import annotations

# Load .env before anything reads os.getenv
from dotenv import load_dotenv
load_dotenv()

import os
import time
import hmac
import hashlib
import secrets
import logging
import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Depends, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from dashboard.dynamic_config_schema import DynamicConfigSchema
from bot.config import Config
from bot.model_discovery import ModelDiscoveryService

logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "changeme")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bot.db")

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Bot Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# Initialize dynamic config schema
config = Config()
model_discovery = ModelDiscoveryService(config)
dynamic_schema = DynamicConfigSchema(model_discovery)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ctx(extra: dict) -> dict:
    """Build template context with common fields (now timestamp)."""
    return {"now": _now(), **extra}


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
    "SELECT DISTINCT guild_id FROM learned_facts WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM response_feedback WHERE guild_id IS NOT NULL",
    "SELECT DISTINCT guild_id FROM crawl_sources WHERE guild_id IS NOT NULL",
]

GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
VALID_GITHUB_EVENTS = {"push", "pull_request", "issues", "release"}
LEGACY_UNKNOWN_EMBEDDING_MODEL = "legacy-unknown"


async def get_all_guilds() -> list[dict[str, Any]]:
    guild_union = " UNION ".join(GUILD_ID_QUERIES)
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


def normalise_github_events(raw: str) -> str:
    events = {event.strip().lower() for event in raw.split(",") if event.strip()}
    valid = sorted(events & VALID_GITHUB_EVENTS)
    return ",".join(valid)


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


async def github_subscription_exists(guild_id: int, repo: str) -> bool:
    row = await db_fetchone(
        "SELECT 1 AS ok FROM github_subscriptions WHERE guild_id = ? AND repo = ? LIMIT 1",
        (guild_id, repo),
    )
    return bool(row)


async def reset_github_poll_state(repo: str) -> int:
    return await db_execute("DELETE FROM github_poll_state WHERE repo = ?", (repo,))


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_auth(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return True


def auth_redirect(request: Request):
    """Returns redirect if not authenticated, else None."""
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=302)
    return None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "now": _now()})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if hmac.compare_digest(password, DASHBOARD_SECRET):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid password", "now": _now()})


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

    # Gather top-level stats
    cases     = await db_fetchone("SELECT COUNT(*) as c FROM mod_cases")
    tickets   = await db_fetchone("SELECT COUNT(*) as c FROM tickets WHERE status != 'closed'")
    warnings  = await db_fetchone("SELECT COUNT(*) as c FROM warnings WHERE active = 1")
    guilds    = await get_all_guilds()
    reports   = await db_fetchone("SELECT COUNT(*) as c FROM reports WHERE status = 'open'")
    giveaways = await db_fetchone("SELECT COUNT(*) as c FROM giveaways WHERE status = 'active'")
    economy   = await db_fetchone("SELECT COUNT(*) as c FROM economy_accounts")
    levels    = await db_fetchone("SELECT COUNT(*) as c FROM levels")
    commands  = await db_fetchone("SELECT COUNT(*) as c FROM custom_commands")

    # Recent mod cases
    recent_cases = await db_fetchall(
        "SELECT * FROM mod_cases ORDER BY id DESC LIMIT 10"
    )

    stats = {
        "total_cases":     cases["c"] if cases else 0,
        "open_tickets":    tickets["c"] if tickets else 0,
        "active_warnings": warnings["c"] if warnings else 0,
        "guild_count":     len(guilds),
        "open_reports":    reports["c"] if reports else 0,
        "active_giveaways": giveaways["c"] if giveaways else 0,
        "economy_accounts": economy["c"] if economy else 0,
        "level_entries":   levels["c"] if levels else 0,
        "custom_commands": commands["c"] if commands else 0,
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

    guilds = await get_all_guilds()

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
        "active_page": "config",
    }))


@app.post("/config/set")
async def config_set(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    
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
    await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
    return RedirectResponse(f"/config?guild_id={guild_id}", status_code=302)


@app.post("/api/refresh-models")
async def refresh_models(request: Request):
    """API endpoint to refresh the model list."""
    if r := auth_redirect(request):
        return r
    
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

    guilds = await get_all_guilds()
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
    await db_execute(
        "UPDATE custom_functions SET enabled = ? WHERE id = ? AND guild_id = ?",
        (enabled, function_id, guild_id),
    )
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/functions/delete")
async def assistant_function_delete(request: Request, guild_id: int = Form(...), function_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM custom_functions WHERE id = ? AND guild_id = ?", (function_id, guild_id))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/listen/save")
async def assistant_listen_save(request: Request, guild_id: int = Form(...), channel_id: int = Form(...), enabled: int = Form(...)):
    if r := auth_redirect(request):
        return r
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
    await db_execute("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, f"channel_prompt_{channel_id}"))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/usage/reset")
async def assistant_usage_reset(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM token_usage WHERE guild_id = ?", (guild_id,))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


@app.post("/assistant/conversations/reset")
async def assistant_conversations_reset(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM conversation_history WHERE guild_id = ?", (guild_id,))
    return RedirectResponse(f"/assistant?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Community systems management
# ---------------------------------------------------------------------------

@app.get("/community", response_class=HTMLResponse)
async def community_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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
    await db_execute("INSERT OR IGNORE INTO selfroles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/selfroles/delete")
async def community_selfrole_delete(request: Request, guild_id: int = Form(...), role_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM selfroles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/highlights/add")
async def community_highlight_add(request: Request, guild_id: int = Form(...), user_id: int = Form(...), keyword: str = Form(...)):
    if r := auth_redirect(request):
        return r
    keyword = keyword.strip().lower()
    if keyword:
        await db_execute("INSERT OR IGNORE INTO highlights (user_id, guild_id, keyword) VALUES (?, ?, ?)", (user_id, guild_id, keyword))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/highlights/delete")
async def community_highlight_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...), keyword: str = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM highlights WHERE guild_id = ? AND user_id = ? AND keyword = ?", (guild_id, user_id, keyword))
    return RedirectResponse(f"/community?guild_id={guild_id}", status_code=302)


@app.post("/community/highlights/toggle-pause")
async def community_highlight_toggle_pause(request: Request, guild_id: int = Form(...), user_id: int = Form(...), paused: int = Form(...)):
    if r := auth_redirect(request):
        return r
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

@app.get("/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
    subscriptions: list[dict] = []
    poll_state: list[dict] = []

    if guild_id:
        subscriptions = await db_fetchall(
            "SELECT * FROM github_subscriptions WHERE guild_id = ? ORDER BY repo, channel_id",
            (guild_id,),
        )
        state_rows = await db_fetchall("SELECT * FROM github_poll_state ORDER BY updated_at DESC")
        repos = {row["repo"] for row in subscriptions}
        poll_state = [row for row in state_rows if row["repo"] in repos]

    return templates.TemplateResponse(request, "integrations.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "subscriptions": subscriptions,
        "poll_state": poll_state,
        "github_token_configured": bool(config.github_token),
        "active_page": "integrations",
    }))


@app.post("/integrations/github/save")
async def integrations_github_save(
    request: Request,
    guild_id: int = Form(...),
    channel_id: int = Form(...),
    repo: str = Form(...),
    events: str = Form(...),
):
    if r := auth_redirect(request):
        return r
    repo = repo.strip()
    if not GITHUB_REPO_RE.match(repo):
        raise HTTPException(status_code=400, detail="Invalid repo format")
    events_value = normalise_github_events(events) or "push,pull_request,issues,release"
    await db_execute(
        "INSERT INTO github_subscriptions (guild_id, channel_id, repo, events, added_by) VALUES (?, ?, ?, ?, 0) "
        "ON CONFLICT(guild_id, channel_id, repo) DO UPDATE SET events = excluded.events",
        (guild_id, channel_id, repo, events_value),
    )
    return RedirectResponse(f"/integrations?guild_id={guild_id}", status_code=302)


@app.post("/integrations/github/delete")
async def integrations_github_delete(request: Request, guild_id: int = Form(...), subscription_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    row = await db_fetchone(
        "SELECT repo FROM github_subscriptions WHERE id = ? AND guild_id = ?",
        (subscription_id, guild_id),
    )
    deleted = await db_execute(
        "DELETE FROM github_subscriptions WHERE id = ? AND guild_id = ?",
        (subscription_id, guild_id),
    )
    repo = row["repo"] if row else None
    if deleted and repo:
        remaining = await db_fetchone(
            "SELECT COUNT(*) AS c FROM github_subscriptions WHERE repo = ?",
            (repo,),
        )
        if not remaining or remaining["c"] == 0:
            await db_execute("DELETE FROM github_poll_state WHERE repo = ?", (repo,))
    return RedirectResponse(f"/integrations?guild_id={guild_id}", status_code=302)


@app.post("/integrations/github/reset_state")
async def integrations_github_reset_state(
    request: Request,
    guild_id: int = Form(...),
    repo: str = Form(...),
):
    if r := auth_redirect(request):
        return r
    repo = repo.strip()
    if not GITHUB_REPO_RE.match(repo):
        raise HTTPException(status_code=400, detail="Invalid repo format")
    if not await github_subscription_exists(guild_id, repo):
        raise HTTPException(status_code=404, detail="Subscription not found for repo in this guild")
    await reset_github_poll_state(repo)
    return RedirectResponse(f"/integrations?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Permission overrides
# ---------------------------------------------------------------------------

@app.get("/permissions", response_class=HTMLResponse)
async def permissions_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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
    await db_execute("DELETE FROM command_permissions WHERE id = ? AND guild_id = ?", (permission_id, guild_id))
    return RedirectResponse(f"/permissions?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Moderation cases
# ---------------------------------------------------------------------------

@app.get("/moderation", response_class=HTMLResponse)
async def moderation_page(request: Request, guild_id: int | None = None, user_id: int | None = None, page: int = 1):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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
    await db_execute("DELETE FROM mod_cases WHERE id = ? AND guild_id = ?", (case_id, guild_id))
    return RedirectResponse(f"/moderation?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

@app.get("/warnings", response_class=HTMLResponse)
async def warnings_page(request: Request, guild_id: int | None = None, user_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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
    await db_execute("UPDATE warnings SET active = 0 WHERE id = ? AND guild_id = ?", (warning_id, guild_id))
    return RedirectResponse(f"/warnings?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request, guild_id: int | None = None, status: str = "open"):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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

    guilds = await get_all_guilds()
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
    await db_execute("DELETE FROM automod_filters WHERE id = ? AND guild_id = ?", (filter_id, guild_id))
    return RedirectResponse(f"/automod?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------

@app.get("/economy", response_class=HTMLResponse)
async def economy_page(request: Request, guild_id: int | None = None, page: int = 1):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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
    await db_execute("DELETE FROM economy_accounts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    return RedirectResponse(f"/economy?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

@app.get("/levels", response_class=HTMLResponse)
async def levels_page(request: Request, guild_id: int | None = None, page: int = 1):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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
    await db_execute("DELETE FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    return RedirectResponse(f"/levels?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Giveaways
# ---------------------------------------------------------------------------

@app.get("/giveaways", response_class=HTMLResponse)
async def giveaways_page(request: Request, guild_id: int | None = None, status: str = "active"):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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
    await db_execute(
        "UPDATE reports SET status = 'resolved', resolution_note = ?, resolved_at = datetime('now') WHERE id = ? AND guild_id = ?",
        (note, report_id, guild_id),
    )
    return RedirectResponse(f"/reports?guild_id={guild_id}", status_code=302)


@app.post("/reports/dismiss")
async def reports_dismiss(request: Request, report_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
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

    guilds = await get_all_guilds()
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
    await db_execute("DELETE FROM custom_commands WHERE id = ? AND guild_id = ?", (cmd_id, guild_id))
    return RedirectResponse(f"/custom-commands?guild_id={guild_id}", status_code=302)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

@app.get("/reminders", response_class=HTMLResponse)
async def reminders_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await get_all_guilds()
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

    guilds = await get_all_guilds()
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
    await db_execute("DELETE FROM embeddings WHERE guild_id = ? AND source_url = ?", (guild_id, source_url))
    await db_execute("DELETE FROM crawl_sources WHERE guild_id = ? AND url = ?", (guild_id, source_url))
    from bot.qdrant_service import QdrantService
    await QdrantService().delete_embeddings_by_source(guild_id, source_url)
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=crawl", status_code=302)


@app.post("/knowledge/repair-crawl-metadata")
async def knowledge_repair_crawl_metadata(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    from urllib.parse import urlencode

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
    from urllib.parse import urlencode

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
    try:
        await db_execute(
            "INSERT OR IGNORE INTO learned_facts (guild_id, fact, source) VALUES (?, ?, ?)",
            (guild_id, fact.strip(), source),
        )
    except Exception:
        pass
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/delete-fact")
async def knowledge_delete_fact(request: Request, fact_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM learned_facts WHERE id = ? AND guild_id = ?", (fact_id, guild_id))
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/toggle-fact")
async def knowledge_toggle_fact(request: Request, fact_id: int = Form(...), guild_id: int = Form(...), approved: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute(
        "UPDATE learned_facts SET approved = ? WHERE id = ? AND guild_id = ?",
        (approved, fact_id, guild_id),
    )
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/reset-facts")
async def knowledge_reset_facts(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM learned_facts WHERE guild_id = ?", (guild_id,))
    return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)


@app.post("/knowledge/reset-feedback")
async def knowledge_reset_feedback(request: Request, guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
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
    import uuid
    job_id = str(uuid.uuid4())[:8]
    _crawl_jobs[job_id] = {"status": "queued", "pages": 0, "chunks": 0, "error": None}
    background_tasks.add_task(_run_crawl, job_id, guild_id, url, max_pages, chunk_size, replace)
    return JSONResponse({"job_id": job_id})


@app.get("/api/crawl/status/{job_id}")
async def api_crawl_status(request: Request, job_id: str):
    if not request.session.get("authenticated"):
        raise HTTPException(401)
    job = _crawl_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


# ---------------------------------------------------------------------------
# JSON API (for live stats / AJAX)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def api_stats(request: Request):
    require_auth(request)
    cases     = await db_fetchone("SELECT COUNT(*) as c FROM mod_cases")
    tickets   = await db_fetchone("SELECT COUNT(*) as c FROM tickets WHERE status != 'closed'")
    warnings  = await db_fetchone("SELECT COUNT(*) as c FROM warnings WHERE active = 1")
    reports   = await db_fetchone("SELECT COUNT(*) as c FROM reports WHERE status = 'open'")
    giveaways = await db_fetchone("SELECT COUNT(*) as c FROM giveaways WHERE status = 'active'")
    return {
        "total_cases":     cases["c"] if cases else 0,
        "open_tickets":    tickets["c"] if tickets else 0,
        "active_warnings": warnings["c"] if warnings else 0,
        "open_reports":    reports["c"] if reports else 0,
        "active_giveaways": giveaways["c"] if giveaways else 0,
    }


@app.get("/api/guilds")
async def api_guilds(request: Request):
    require_auth(request)
    guilds = await get_all_guilds()
    return [g["guild_id"] for g in guilds]
