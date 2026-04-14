"""Economy/levels/giveaways routes."""

from __future__ import annotations

import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.helpers import (
    DB_PATH,
    auth_redirect,
    ctx,
    db_execute,
    db_fetchall,
    db_fetchone,
    get_authorized_guilds,
    require_guild_access,
)

router = APIRouter()

logger = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

_DURATION_RE = re.compile(
    r"(?:(\d+)\s*d(?:ays?)?)?\s*"
    r"(?:(\d+)\s*h(?:ours?)?)?\s*"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?\s*"
    r"(?:(\d+)\s*s(?:ec(?:onds?)?)?)?",
    re.IGNORECASE,
)


def _parse_duration(text: str) -> timedelta | None:
    m = _DURATION_RE.fullmatch(text.strip())
    if not m or not any(m.groups()):
        return None
    td = timedelta(
        days=int(m.group(1) or 0),
        hours=int(m.group(2) or 0),
        minutes=int(m.group(3) or 0),
        seconds=int(m.group(4) or 0),
    )
    return td if td.total_seconds() >= 1 else None


def _giveaway_embed_payload(
    giveaway_id: int,
    prize: str,
    end_time: datetime,
    winner_count: int,
    host_id: int,
    entry_count: int = 0,
    ended: bool = False,
    cancelled: bool = False,
    winners: list[int] | None = None,
) -> dict:
    ts = int(end_time.timestamp())
    color = 0xED4245 if (ended or cancelled) else 0x57F287
    title = f"🎉 {'[ENDED] ' if ended else '[CANCELLED] ' if cancelled else ''}Giveaway #{giveaway_id}"
    description = f"**Prize:** {prize}"
    if cancelled:
        description += "\n\n*Giveaway cancelled.*"
    fields = []
    if ended and winners:
        fields.append({"name": "🏆 Winners", "value": "\n".join(f"<@{w}>" for w in winners) or "No winners", "inline": False})
    elif not ended and not cancelled:
        fields.append({"name": "Ends", "value": f"<t:{ts}:R> (<t:{ts}:f>)", "inline": True})
    fields.append({"name": "Winners", "value": str(winner_count), "inline": True})
    fields.append({"name": "Entries", "value": str(entry_count), "inline": True})
    fields.append({"name": "Hosted by", "value": f"<@{host_id}>", "inline": True})
    footer_text = f"Giveaway ID: {giveaway_id} · {'Ended' if ended or cancelled else 'Click 🎉 to enter!'}"
    return {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {"text": footer_text},
    }


async def _discord_post_message(channel_id: int, embed: dict, components: list | None = None) -> dict | None:
    if not _BOT_TOKEN:
        return None
    payload: dict = {"embeds": [embed]}
    if components:
        payload["components"] = components
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_DISCORD_API}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {_BOT_TOKEN}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            logger.error("Discord post failed %s: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.exception("Discord post exception: %s", exc)
    return None


async def _discord_edit_message(channel_id: int, message_id: int, embed: dict, remove_components: bool = False) -> None:
    if not _BOT_TOKEN or not message_id:
        return
    payload: dict = {"embeds": [embed]}
    if remove_components:
        payload["components"] = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                f"{_DISCORD_API}/channels/{channel_id}/messages/{message_id}",
                headers={"Authorization": f"Bot {_BOT_TOKEN}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception:
        pass


async def _discord_send_message(channel_id: int, content: str) -> None:
    if not _BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{_DISCORD_API}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {_BOT_TOKEN}", "Content-Type": "application/json"},
                json={"content": content},
            )
    except Exception:
        pass


def init(templates: Jinja2Templates) -> APIRouter:
    # ── Economy ───────────────────────────────────────────────────────

    @router.get("/economy", response_class=HTMLResponse)
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

    @router.post("/economy/set")
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

    @router.post("/economy/delete")
    async def economy_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM economy_accounts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return RedirectResponse(f"/economy?guild_id={guild_id}", status_code=302)

    # ── Levels ────────────────────────────────────────────────────────

    @router.get("/levels", response_class=HTMLResponse)
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

    @router.post("/levels/set")
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

    @router.post("/levels/delete")
    async def levels_delete(request: Request, guild_id: int = Form(...), user_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return RedirectResponse(f"/levels?guild_id={guild_id}", status_code=302)

    # ── Giveaways ─────────────────────────────────────────────────────

    @router.get("/giveaways", response_class=HTMLResponse)
    async def giveaways_page(request: Request, guild_id: int | None = None, status: str = "active"):
        if r := auth_redirect(request):
            return r

        guilds = await get_authorized_guilds(request, guild_id)
        giveaways = []
        entry_counts: dict[int, int] = {}
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
            for g in giveaways:
                row = await db_fetchone(
                    "SELECT COUNT(*) as c FROM giveaway_entries WHERE giveaway_id = ?", (g["id"],)
                )
                entry_counts[g["id"]] = row["c"] if row else 0

        flash_error = request.session.pop("flash_error", None)
        flash_ok = request.session.pop("flash_ok", None)
        return templates.TemplateResponse(request, "giveaways.html", ctx({
            "guilds": guilds,
            "guild_id": guild_id,
            "status": status,
            "giveaways": giveaways,
            "entry_counts": entry_counts,
            "flash_error": flash_error,
            "flash_ok": flash_ok,
            "active_page": "giveaways",
        }))

    @router.post("/giveaways/create")
    async def giveaways_create(
        request: Request,
        guild_id: int = Form(...),
        channel_id: int = Form(...),
        prize: str = Form(...),
        duration: str = Form(...),
        winner_count: int = Form(1),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)

        td = _parse_duration(duration)
        if td is None:
            request.session["flash_error"] = f"Invalid duration '{duration}'. Use formats like: 1d, 2h30m, 45m, 1d12h."
            return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=active", status_code=302)

        winner_count = max(1, min(winner_count, 20))
        end_dt = datetime.now(timezone.utc) + td
        user = request.session.get("user", {})
        host_id = int(user.get("id", 0))

        try:
            giveaway_id = None
            async with aiosqlite.connect(DB_PATH) as _db:
                _db.row_factory = aiosqlite.Row
                start_dt = datetime.now(timezone.utc)
                logger.info(
                    "giveaways_create inserting: guild_id=%s channel_id=%s prize=%s start_time=%s end_time=%s winner_count=%s host_id=%s",
                    guild_id, channel_id, prize, start_dt.isoformat(), end_dt.isoformat(), winner_count, host_id
                )
                cur = await _db.execute(
                    "INSERT INTO giveaways (guild_id, channel_id, prize, start_time, end_time, winner_count, host_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (guild_id, channel_id, prize, start_dt.isoformat(), end_dt.isoformat(), winner_count, host_id),
                )
                await _db.commit()
                giveaway_id = cur.lastrowid
                logger.info("giveaways_create inserted: giveaway_id=%s", giveaway_id)
                # Verify what was actually stored
                verify = await _db.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
                row = await verify.fetchone()
                logger.info("giveaways_create verify row: %s", dict(row))
        except Exception as exc:
            logger.exception("Failed to create giveaway: %s", exc)
            request.session["flash_error"] = f"Error creating giveaway: {exc}"
            return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=active", status_code=302)

        embed = _giveaway_embed_payload(
            giveaway_id=giveaway_id,
            prize=prize,
            end_time=end_dt,
            winner_count=winner_count,
            host_id=host_id,
        )
        components = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,
                        "label": "🎉 Enter",
                        "custom_id": f"giveaway:enter:{giveaway_id}",
                    }
                ],
            }
        ]
        msg = await _discord_post_message(channel_id, embed, components)
        if msg:
            try:
                await db_execute(
                    "UPDATE giveaways SET message_id = ? WHERE id = ?",
                    (int(msg["id"]), giveaway_id),
                )
            except Exception as exc:
                logger.warning("Failed to store message_id for giveaway %s: %s", giveaway_id, exc)
            request.session["flash_ok"] = f"Giveaway #{giveaway_id} launched in Discord!"
        else:
            request.session["flash_error"] = (
                f"Giveaway #{giveaway_id} saved but could not post to Discord "
                f"(check DISCORD_BOT_TOKEN and bot access to channel {channel_id})."
            )

        return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=active", status_code=302)

    @router.post("/giveaways/end")
    async def giveaways_end(
        request: Request,
        guild_id: int = Form(...),
        giveaway_id: int = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)

        row = await db_fetchone(
            "SELECT * FROM giveaways WHERE id = ? AND guild_id = ? AND status = 'active'",
            (giveaway_id, guild_id),
        )
        if not row:
            return RedirectResponse(f"/giveaways?guild_id={guild_id}", status_code=302)

        await db_execute("UPDATE giveaways SET status = 'ended' WHERE id = ?", (giveaway_id,))

        entry_rows = await db_fetchall(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,)
        )
        entries = [r["user_id"] for r in entry_rows]
        winner_count = min(row["winner_count"], len(entries))
        winners = random.sample(entries, winner_count) if entries else []

        end_time = datetime.fromisoformat(row["end_time"])
        embed = _giveaway_embed_payload(
            giveaway_id=giveaway_id,
            prize=row["prize"],
            end_time=end_time,
            winner_count=row["winner_count"],
            host_id=row["host_id"],
            entry_count=len(entries),
            ended=True,
            winners=winners,
        )
        await _discord_edit_message(row["channel_id"], row["message_id"], embed, remove_components=True)

        if winners:
            winner_mentions = " ".join(f"<@{w}>" for w in winners)
            await _discord_send_message(
                row["channel_id"],
                f"🎉 Giveaway **#{giveaway_id}** ended! Congratulations {winner_mentions}! You won **{row['prize']}**!",
            )
        else:
            await _discord_send_message(row["channel_id"], f"😢 Giveaway **#{giveaway_id}** ended with no valid entries.")

        return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=ended", status_code=302)

    @router.post("/giveaways/cancel")
    async def giveaways_cancel(
        request: Request,
        guild_id: int = Form(...),
        giveaway_id: int = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)

        row = await db_fetchone(
            "SELECT * FROM giveaways WHERE id = ? AND guild_id = ? AND status = 'active'",
            (giveaway_id, guild_id),
        )
        if not row:
            return RedirectResponse(f"/giveaways?guild_id={guild_id}", status_code=302)

        await db_execute("UPDATE giveaways SET status = 'ended' WHERE id = ?", (giveaway_id,))

        end_time = datetime.fromisoformat(row["end_time"])
        embed = _giveaway_embed_payload(
            giveaway_id=giveaway_id,
            prize=row["prize"],
            end_time=end_time,
            winner_count=row["winner_count"],
            host_id=row["host_id"],
            cancelled=True,
        )
        await _discord_edit_message(row["channel_id"], row["message_id"], embed, remove_components=True)

        return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=active", status_code=302)

    @router.post("/giveaways/reroll")
    async def giveaways_reroll(
        request: Request,
        guild_id: int = Form(...),
        giveaway_id: int = Form(...),
    ):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)

        row = await db_fetchone(
            "SELECT * FROM giveaways WHERE id = ? AND guild_id = ? AND status = 'ended'",
            (giveaway_id, guild_id),
        )
        if not row:
            return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=ended", status_code=302)

        entry_rows = await db_fetchall(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,)
        )
        entries = [r["user_id"] for r in entry_rows]
        if not entries:
            return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=ended", status_code=302)

        winner_count = min(row["winner_count"], len(entries))
        winners = random.sample(entries, winner_count)
        winner_mentions = " ".join(f"<@{w}>" for w in winners)
        await _discord_send_message(
            row["channel_id"],
            f"🔄 Reroll! New winners for giveaway **#{giveaway_id}** ({row['prize']}): {winner_mentions}!",
        )

        return RedirectResponse(f"/giveaways?guild_id={guild_id}&status=ended", status_code=302)

    @router.get("/api/guild-channels/{guild_id}")
    async def guild_channels_api(request: Request, guild_id: int):
        if r := auth_redirect(request):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        await require_guild_access(request, guild_id)
        if not _BOT_TOKEN:
            return JSONResponse({"channels": []})
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{_DISCORD_API}/guilds/{guild_id}/channels",
                    headers={"Authorization": f"Bot {_BOT_TOKEN}"},
                )
                resp.raise_for_status()
                channels = [
                    {"id": c["id"], "name": c["name"]}
                    for c in resp.json()
                    if c["type"] == 0
                ]
                channels.sort(key=lambda c: c["name"].lower())
                return JSONResponse({"channels": channels})
        except Exception:
            return JSONResponse({"channels": []})

    return router
