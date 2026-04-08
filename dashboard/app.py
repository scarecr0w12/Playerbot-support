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
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import FastAPI, Request, Response, HTTPException, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# Import configuration schemas
from dashboard.config_schema import get_config_categories
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
    guilds    = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")

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
# Moderation cases
# ---------------------------------------------------------------------------

@app.get("/moderation", response_class=HTMLResponse)
async def moderation_page(request: Request, guild_id: int | None = None, user_id: int | None = None, page: int = 1):
    if r := auth_redirect(request):
        return r

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM custom_commands ORDER BY guild_id")
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

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
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
# Knowledge base / embeddings
# ---------------------------------------------------------------------------

@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, guild_id: int | None = None):
    if r := auth_redirect(request):
        return r

    guilds = await db_fetchall("SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")
    entries = []
    sources = []
    if guild_id:
        entries = await db_fetchall(
            "SELECT id, name, model, source_url, created_at, LENGTH(text) as text_len FROM embeddings WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        sources = await db_fetchall(
            "SELECT * FROM crawl_sources WHERE guild_id = ? ORDER BY crawled_at DESC",
            (guild_id,),
        )

    return templates.TemplateResponse(request, "knowledge.html", ctx({
        "guilds": guilds,
        "guild_id": guild_id,
        "entries": entries,
        "sources": sources,
        "active_page": "knowledge",
    }))


@app.post("/knowledge/delete")
async def knowledge_delete(request: Request, entry_id: int = Form(...), guild_id: int = Form(...)):
    if r := auth_redirect(request):
        return r
    await db_execute("DELETE FROM embeddings WHERE id = ? AND guild_id = ?", (entry_id, guild_id))
    return RedirectResponse(f"/knowledge?guild_id={guild_id}", status_code=302)


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
    guilds = await db_fetchall(
        "SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id"
    )
    return [g["guild_id"] for g in guilds]
