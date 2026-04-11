"""Knowledge/embeddings/crawl/facts/feedback routes."""

from __future__ import annotations

import logging
import os
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import dashboard.helpers as _helpers
from dashboard.helpers import (
    _safe_int,
    auth_redirect,
    ctx,
    db_execute,
    db_fetchall,
    db_fetchone,
    get_authorized_guilds,
    get_crawl_sources_with_metadata,
    get_knowledge_entries,
    get_session_user_id,
    is_authenticated,
    is_master_session,
    require_guild_access,
    upsert_crawl_source,
    upsert_crawled_embedding,
)

logger = logging.getLogger("dashboard")
router = APIRouter()

_crawl_jobs: dict[str, dict] = {}


def init(templates: Jinja2Templates) -> APIRouter:
    @router.get("/knowledge", response_class=HTMLResponse)
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

    @router.post("/knowledge/delete")
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

    @router.post("/knowledge/delete-source")
    async def knowledge_delete_source(request: Request, source_url: str = Form(...), guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM embeddings WHERE guild_id = ? AND source_url = ?", (guild_id, source_url))
        await db_execute("DELETE FROM crawl_sources WHERE guild_id = ? AND url = ?", (guild_id, source_url))
        from bot.qdrant_service import QdrantService
        await QdrantService().delete_embeddings_by_source(guild_id, source_url)
        return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=crawl", status_code=302)

    @router.post("/knowledge/repair-crawl-metadata")
    async def knowledge_repair_crawl_metadata(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        summary = await _helpers.repair_legacy_crawl_metadata(guild_id)
        query = urlencode({
            "guild_id": guild_id,
            "tab": "crawl",
            "repair": 1,
            "sources_repaired": summary["sources_repaired"],
            "duplicates_removed": summary["duplicates_removed"],
            "models_filled": summary["models_filled"],
        })
        return RedirectResponse(f"/knowledge?{query}", status_code=302)

    @router.post("/knowledge/reset")
    async def knowledge_reset(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        summary = await _helpers.clear_knowledge_base(guild_id)
        query = urlencode({
            "guild_id": guild_id,
            "tab": "embeddings",
            "cleared": 1,
            "embeddings_cleared": summary["embeddings_cleared"],
            "crawled_chunks_cleared": summary["crawled_chunks_cleared"],
            "sources_cleared": summary["sources_cleared"],
        })
        return RedirectResponse(f"/knowledge?{query}", status_code=302)

    @router.post("/knowledge/add-fact")
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

    @router.post("/knowledge/delete-fact")
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

    @router.post("/knowledge/toggle-fact")
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

    @router.post("/knowledge/reset-facts")
    async def knowledge_reset_facts(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM learned_facts WHERE guild_id = ?", (guild_id,))
        from bot.qdrant_service import QdrantService
        await QdrantService().reset_facts(guild_id)
        return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=training", status_code=302)

    @router.post("/knowledge/reset-feedback")
    async def knowledge_reset_feedback(request: Request, guild_id: int = Form(...)):
        if r := auth_redirect(request):
            return r
        await require_guild_access(request, guild_id)
        await db_execute("DELETE FROM response_feedback WHERE guild_id = ?", (guild_id,))
        return RedirectResponse(f"/knowledge?guild_id={guild_id}&tab=feedback", status_code=302)

    # ── Async crawl API ───────────────────────────────────────────────

    async def _run_crawl(job_id: str, guild_id: int, url: str, max_pages: int, chunk_size: int, replace: bool) -> None:
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

        llm_client = AsyncOpenAI(
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.getenv("LLM_API_KEY", ""),
        )
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
                            guild_id, entry_name, chunk, emb_model, result.url, point_id,
                        )
                        stored += 1
                    except Exception as exc:
                        logger.warning("DB insert failed for chunk %d: %s", i, exc)
                        continue
                    vec = await _embed(chunk)
                    if vec:
                        await qdrant.upsert_embedding(guild_id, point_id, vec, entry_name, chunk, emb_model, source_url=result.url)
                try:
                    await upsert_crawl_source(guild_id, result.url, result.title or "", len(result.chunks))
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

    @router.post("/api/crawl/start")
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

    @router.get("/api/crawl/status/{job_id}")
    async def api_crawl_status(request: Request, job_id: str):
        from fastapi import HTTPException
        if not is_authenticated(request):
            raise HTTPException(401)
        job = _crawl_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        current_user_id = get_session_user_id(request)
        if not is_master_session(request) and job.get("user_id") != current_user_id:
            raise HTTPException(403, "You do not have access to this crawl job")
        return JSONResponse(job)

    return router
