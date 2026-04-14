"""Dashboard routes for Discord interactive polls (PollsCog / polls + poll_votes tables)."""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bot.dashboard_bridge import get_discord_bot, run_coroutine_on_bot_loop
from dashboard.helpers import (
    auth_redirect,
    ctx,
    db_execute,
    db_fetchall,
    db_fetchone,
    get_authorized_guilds,
    get_session_user_id,
    require_guild_access,
)
from dashboard.routes.economy import _discord_edit_message, _discord_post_message

router = APIRouter()
logger = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# Match bot/cogs/polls.py (for result field labels)
NUMBER_EMOJIS = [
    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
    "🟦", "🟩", "🟨", "🟧", "🟪", "🟫", "⬛", "⬜", "🟥", "🟦",
]


def _norm_list_status(value: str) -> str:
    if value in ("all", "past_deadline", "active"):
        return value
    return "active"


def _parse_options(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (TypeError, json.JSONDecodeError):
        pass
    return []


def _results_by_option(results_rows: list[dict[str, Any]], num_options: int) -> list[int]:
    counts = [0] * num_options
    for r in results_rows:
        idx = int(r["option_index"])
        if 0 <= idx < num_options:
            counts[idx] = int(r["votes"])
    return counts


def _active_poll_embed(question: str, option_list: list[str], footer_text: str) -> dict[str, Any]:
    ts = datetime.now(timezone.utc).isoformat()
    fields: list[dict[str, Any]] = []
    for i, opt in enumerate(option_list):
        if i < len(NUMBER_EMOJIS):
            fields.append(
                {
                    "name": f"{NUMBER_EMOJIS[i]} {opt}",
                    "value": "0 votes (0%)",
                    "inline": True,
                }
            )
    return {
        "title": f"📊 {question}",
        "description": "Vote using the buttons below!",
        "color": 0x3498DB,
        "fields": fields,
        "footer": {"text": footer_text},
        "timestamp": ts,
    }


def _final_results_embed(question: str, options: list[str], results_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = _results_by_option(results_rows, len(options))
    total = sum(counts)
    fields: list[dict[str, Any]] = []
    for i, option in enumerate(options):
        vote_count = counts[i]
        pct = (vote_count / total * 100) if total > 0 else 0.0
        label = f"{NUMBER_EMOJIS[i]} {option}" if i < len(NUMBER_EMOJIS) else f"📋 {option}"
        fields.append(
            {
                "name": label,
                "value": f"{vote_count} votes ({pct:.1f}%)",
                "inline": True,
            }
        )
    fields.append({"name": "Total Votes", "value": str(total), "inline": False})
    return {
        "title": f"📊 Final Results: {question}",
        "description": "This poll has ended.",
        "color": 0x57F287,
        "fields": fields,
        "footer": {"text": "Poll ended from dashboard"},
    }


async def _discord_delete_message(channel_id: int, message_id: int) -> None:
    if not _BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.delete(
                f"{_DISCORD_API}/channels/{channel_id}/messages/{message_id}",
                headers={"Authorization": f"Bot {_BOT_TOKEN}"},
            )
    except Exception:
        logger.debug("Discord delete message failed", exc_info=True)


async def _delete_poll_db(guild_id: int, message_id: int) -> bool:
    poll = await db_fetchone(
        "SELECT id FROM polls WHERE guild_id = ? AND message_id = ?",
        (guild_id, message_id),
    )
    if not poll:
        return False
    await db_execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
    n = await db_execute("DELETE FROM polls WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))
    return n > 0


def init(templates: Jinja2Templates) -> APIRouter:
    @router.get("/polls", response_class=HTMLResponse)
    async def polls_page(request: Request, guild_id: int | None = None, status: str = "active"):
        if r := auth_redirect(request):
            return r

        status = _norm_list_status(status)
        guilds = await get_authorized_guilds(request, guild_id)
        polls_list: list[dict[str, Any]] = []
        if guild_id:
            if status == "all":
                polls_list = await db_fetchall(
                    "SELECT * FROM polls WHERE guild_id = ? ORDER BY created_at DESC",
                    (guild_id,),
                )
            elif status == "past_deadline":
                polls_list = await db_fetchall(
                    "SELECT * FROM polls WHERE guild_id = ? AND ends_at IS NOT NULL "
                    "AND ends_at <= datetime('now') ORDER BY created_at DESC",
                    (guild_id,),
                )
            else:
                polls_list = await db_fetchall(
                    "SELECT * FROM polls WHERE guild_id = ? AND (ends_at IS NULL OR ends_at > datetime('now')) "
                    "ORDER BY created_at DESC",
                    (guild_id,),
                )

        vote_totals: dict[int, int] = {}
        for p in polls_list:
            row = await db_fetchone("SELECT COUNT(*) AS c FROM poll_votes WHERE poll_id = ?", (p["id"],))
            vote_totals[p["id"]] = int(row["c"]) if row else 0

        flash_error = request.session.pop("flash_error", None)
        flash_ok = request.session.pop("flash_ok", None)
        return templates.TemplateResponse(
            request,
            "polls.html",
            ctx(
                {
                    "guilds": guilds,
                    "guild_id": guild_id,
                    "poll_status": status,
                    "polls": polls_list,
                    "vote_totals": vote_totals,
                    "flash_error": flash_error,
                    "flash_ok": flash_ok,
                    "active_page": "polls",
                    "discord_configured": bool(_BOT_TOKEN),
                    "bot_wired": get_discord_bot() is not None,
                }
            ),
        )

    @router.post("/polls/create")
    async def polls_create(
        request: Request,
        guild_id: int = Form(...),
        channel_id: int = Form(...),
        question: str = Form(...),
        options: str = Form(...),
        duration_hours: str = Form(""),
        poll_status: str = Form("active"),
        multiple_choice: str | None = Form(None),
        anonymous: str | None = Form(None),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        poll_status = _norm_list_status(poll_status)
        creator_id = get_session_user_id(request)
        if creator_id is None:
            return RedirectResponse("/login", status_code=302)

        q = question.strip()
        if not q or len(q) > 240:
            request.session["flash_error"] = "Question is required (max 240 characters)."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        option_list = [o.strip() for o in options.replace("\n", ",").split(",") if o.strip()]
        option_list = option_list[:20]
        if len(option_list) < 2:
            request.session["flash_error"] = "Provide at least two options (comma-separated)."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        ends_at: str | None = None
        dh = (duration_hours or "").strip()
        if dh:
            try:
                h = int(dh)
                if h < 1 or h > 8760:
                    raise ValueError
                ends_at = (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat()
            except ValueError:
                request.session["flash_error"] = "Duration must be empty or an integer hour count between 1 and 8760."
                return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        is_multi = bool(multiple_choice)
        is_anon = bool(anonymous)
        prof = request.session.get("user") or {}
        creator_label = (prof.get("global_name") or prof.get("username") or "Dashboard").strip()
        footer = f"Created by {creator_label}"
        if is_multi:
            footer += " • Multiple choice"
        if is_anon:
            footer += " • Anonymous"
        if dh:
            footer += f" • Ends in {dh}h"

        if not _BOT_TOKEN:
            request.session["flash_error"] = "DISCORD_BOT_TOKEN is not configured."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        embed = _active_poll_embed(q, option_list, footer)
        msg = await _discord_post_message(channel_id, embed, None)
        if not msg:
            request.session["flash_error"] = (
                f"Could not post to Discord (check bot permissions and channel ID {channel_id})."
            )
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        msg_id = int(msg["id"])
        multi = 1 if is_multi else 0
        anon = 1 if is_anon else 0

        try:
            await db_execute(
                "INSERT INTO polls (guild_id, channel_id, message_id, creator_id, question, options, "
                "multiple_choice, anonymous, ends_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, msg_id, creator_id, q, json.dumps(option_list), multi, anon, ends_at),
            )
        except aiosqlite.IntegrityError:
            await _discord_delete_message(channel_id, msg_id)
            request.session["flash_error"] = "Could not save the poll (database conflict). The draft message was removed."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        jump = f"https://discord.com/channels/{guild_id}/{channel_id}/{msg_id}"
        attach_ok = False
        bot = get_discord_bot()
        if bot is not None:
            from bot.cogs.polls import PollsCog

            cog = bot.get_cog("Polls")
            if isinstance(cog, PollsCog):
                try:
                    attach_ok = bool(
                        run_coroutine_on_bot_loop(cog.attach_persistent_view_for_message(guild_id, msg_id))
                    )
                except (FuturesTimeoutError, RuntimeError, OSError) as exc:
                    logger.warning("Poll dashboard attach: %s", exc)
                except Exception:
                    logger.exception("Poll dashboard attach failed")

        if attach_ok:
            request.session["flash_ok"] = f"Poll launched with voting buttons. {jump}"
        else:
            request.session["flash_ok"] = (
                f"Poll saved and posted. If voting buttons are missing, ensure the bot is running from main.py "
                f"(not uvicorn alone) so views can register; restart the bot if needed. {jump}"
            )

        return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

    @router.post("/polls/end")
    async def polls_end(
        request: Request,
        guild_id: int = Form(...),
        message_id: int = Form(...),
        poll_status: str = Form("active"),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        poll_status = _norm_list_status(poll_status)

        poll = await db_fetchone(
            "SELECT * FROM polls WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        if not poll:
            request.session["flash_error"] = "Poll not found for that message."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        results = await db_fetchall(
            "SELECT option_index, COUNT(*) AS votes FROM poll_votes WHERE poll_id = ? GROUP BY option_index",
            (poll["id"],),
        )
        options = _parse_options(poll["options"])
        if len(options) < 2:
            request.session["flash_error"] = "Poll has invalid options data."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        embed = _final_results_embed(poll["question"], options, results)
        await _discord_edit_message(int(poll["channel_id"]), int(poll["message_id"]), embed, remove_components=True)

        await _delete_poll_db(guild_id, message_id)
        request.session["flash_ok"] = "Poll ended; results were posted to Discord (if the message still exists)."
        return RedirectResponse(f"/polls?guild_id={guild_id}&status=all", status_code=302)

    @router.post("/polls/delete")
    async def polls_delete_record(
        request: Request,
        guild_id: int = Form(...),
        message_id: int = Form(...),
        poll_status: str = Form("active"),
    ):
        """Remove poll row and votes from SQLite only (e.g. orphaned after a deleted Discord message)."""
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        poll_status = _norm_list_status(poll_status)

        if await _delete_poll_db(guild_id, message_id):
            request.session["flash_ok"] = "Poll record removed from the database."
        else:
            request.session["flash_error"] = "No matching poll in the database."
        return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

    @router.post("/polls/clear-components")
    async def polls_clear_components(
        request: Request,
        guild_id: int = Form(...),
        message_id: int = Form(...),
        poll_status: str = Form("active"),
    ):
        """Strip buttons from the Discord message without deleting the poll (repair stale UI)."""
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        poll_status = _norm_list_status(poll_status)

        poll = await db_fetchone(
            "SELECT channel_id, message_id FROM polls WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        if not poll:
            request.session["flash_error"] = "Poll not found for that message."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        if not _BOT_TOKEN:
            request.session["flash_error"] = "Discord bot token is not configured."
            return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.patch(
                    f"{_DISCORD_API}/channels/{int(poll['channel_id'])}/messages/{int(poll['message_id'])}",
                    headers={"Authorization": f"Bot {_BOT_TOKEN}", "Content-Type": "application/json"},
                    json={"components": []},
                )
                if r.status_code not in (200, 201):
                    request.session["flash_error"] = f"Discord API returned {r.status_code}."
                else:
                    request.session["flash_ok"] = "Buttons removed from the poll message."
        except Exception as exc:
            logger.exception("polls_clear_components: %s", exc)
            request.session["flash_error"] = "Failed to reach Discord API."

        return RedirectResponse(f"/polls?guild_id={guild_id}&status={poll_status}", status_code=302)

    return router
