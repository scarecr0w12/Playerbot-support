"""Repository: conversation_history, embeddings, crawl_sources, custom_functions,
token_usage, assistant_triggers, learned_facts, learned_message_marks,
prompt_templates, response_feedback tables."""

from __future__ import annotations

import aiosqlite


class SupportRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ── Conversation history ───────────────────────────────────────────

    async def add_conversation_message(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        role: str,
        content: str,
        token_count: int = 0,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO conversation_history (guild_id, channel_id, user_id, role, content, token_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, user_id, role, content, token_count),
        )
        await self._conn.commit()

    async def get_conversation_history(
        self, guild_id: int, channel_id: int, user_id: int, limit: int = 40
    ):
        cur = await self._conn.execute(
            "SELECT role, content, token_count FROM conversation_history "
            "WHERE guild_id = ? AND channel_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, channel_id, user_id, limit),
        )
        rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def get_conversation_stats(self, guild_id: int, channel_id: int, user_id: int) -> dict:
        cur = await self._conn.execute(
            "SELECT COUNT(*) as msg_count, COALESCE(SUM(token_count), 0) as total_tokens "
            "FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        row = await cur.fetchone()
        return {"messages": row["msg_count"], "tokens": row["total_tokens"]}

    async def clear_conversation_history(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> int:
        cur = await self._conn.execute(
            "DELETE FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        await self._conn.commit()
        return cur.rowcount

    async def pop_last_conversation_message(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM conversation_history WHERE id = ("
            "  SELECT id FROM conversation_history "
            "  WHERE guild_id = ? AND channel_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1"
            ")",
            (guild_id, channel_id, user_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def replace_conversation(
        self, guild_id: int, channel_id: int, user_id: int, messages: list[dict]
    ) -> None:
        await self._conn.execute(
            "DELETE FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        for m in messages:
            await self._conn.execute(
                "INSERT INTO conversation_history (guild_id, channel_id, user_id, role, content, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, user_id, m["role"], m["content"], m.get("token_count", 0)),
            )
        await self._conn.commit()

    # ── Embeddings / RAG knowledge base ───────────────────────────────

    async def add_embedding(
        self,
        guild_id: int,
        name: str,
        text: str,
        embedding: bytes | None,
        model: str | None,
        source_url: str | None = None,
        qdrant_id: str | None = None,
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, name, text, model, source_url, qdrant_id),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_embedding(
        self,
        guild_id: int,
        name: str,
        text: str,
        embedding: bytes | None,
        model: str | None,
        source_url: str | None = None,
        qdrant_id: str | None = None,
    ) -> bool:
        cur = await self._conn.execute(
            "UPDATE embeddings SET text = ?, model = ?, source_url = ?, qdrant_id = ? WHERE guild_id = ? AND name = ?",
            (text, model, source_url, qdrant_id, guild_id, name),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def delete_embedding(self, guild_id: int, name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM embeddings WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_embedding_by_name(self, guild_id: int, name: str):
        cur = await self._conn.execute(
            "SELECT * FROM embeddings WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def get_embedding(self, guild_id: int, name: str):
        return await self.get_embedding_by_name(guild_id, name)

    async def get_all_embeddings(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM embeddings WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()

    async def delete_embeddings_by_source(self, guild_id: int, source_url: str) -> int:
        cur = await self._conn.execute(
            "DELETE FROM embeddings WHERE guild_id = ? AND source_url = ?",
            (guild_id, source_url),
        )
        await self._conn.commit()
        return cur.rowcount

    async def reset_embeddings(self, guild_id: int) -> int:
        cur = await self._conn.execute(
            "DELETE FROM embeddings WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.commit()
        return cur.rowcount

    # ── Crawl sources ─────────────────────────────────────────────────

    async def upsert_crawl_source(
        self, guild_id: int, url: str, title: str, chunk_count: int
    ) -> None:
        await self._conn.execute(
            "INSERT INTO crawl_sources (guild_id, url, title, chunk_count, crawled_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(guild_id, url) DO UPDATE SET "
            "title = excluded.title, chunk_count = excluded.chunk_count, crawled_at = excluded.crawled_at",
            (guild_id, url, title, chunk_count),
        )
        await self._conn.commit()

    async def get_crawl_sources(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM crawl_sources WHERE guild_id = ? ORDER BY crawled_at DESC",
            (guild_id,),
        )
        return await cur.fetchall()

    async def delete_crawl_source(self, guild_id: int, url: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM crawl_sources WHERE guild_id = ? AND url = ?",
            (guild_id, url),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def reset_crawl_sources(self, guild_id: int) -> int:
        cur = await self._conn.execute(
            "DELETE FROM crawl_sources WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.commit()
        return cur.rowcount

    # ── Custom functions ──────────────────────────────────────────────

    async def add_custom_function(
        self, guild_id: int, name: str, description: str, parameters: str, code: str
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO custom_functions (guild_id, name, description, parameters, code) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, description, parameters, code),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def delete_custom_function(self, guild_id: int, name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM custom_functions WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def toggle_custom_function(self, guild_id: int, name: str) -> bool | None:
        row = await self._conn.execute(
            "SELECT enabled FROM custom_functions WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        existing = await row.fetchone()
        if not existing:
            return None
        new_val = 0 if existing["enabled"] else 1
        await self._conn.execute(
            "UPDATE custom_functions SET enabled = ? WHERE guild_id = ? AND name = ?",
            (new_val, guild_id, name),
        )
        await self._conn.commit()
        return bool(new_val)

    async def get_enabled_functions(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM custom_functions WHERE guild_id = ? AND enabled = 1",
            (guild_id,),
        )
        return await cur.fetchall()

    async def get_all_functions(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM custom_functions WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()

    # ── Token usage ───────────────────────────────────────────────────

    async def log_token_usage(
        self, guild_id: int, user_id: int, prompt_tokens: int, completion_tokens: int
    ) -> None:
        await self._conn.execute(
            "INSERT INTO token_usage (guild_id, user_id, prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, user_id, prompt_tokens, completion_tokens),
        )
        await self._conn.commit()

    async def get_guild_usage(self, guild_id: int) -> dict:
        cur = await self._conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) as prompt, "
            "COALESCE(SUM(completion_tokens), 0) as completion "
            "FROM token_usage WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cur.fetchone()
        return {"prompt_tokens": row["prompt"], "completion_tokens": row["completion"]}

    async def get_user_usage(self, guild_id: int, user_id: int) -> dict:
        cur = await self._conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) as prompt, "
            "COALESCE(SUM(completion_tokens), 0) as completion "
            "FROM token_usage WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return {"prompt_tokens": row["prompt"], "completion_tokens": row["completion"]}

    async def reset_usage(self, guild_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM token_usage WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.commit()

    # ── Assistant triggers ────────────────────────────────────────────

    async def add_trigger(self, guild_id: int, pattern: str) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO assistant_triggers (guild_id, pattern) VALUES (?, ?)",
                (guild_id, pattern),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_trigger(self, guild_id: int, pattern: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM assistant_triggers WHERE guild_id = ? AND pattern = ?",
            (guild_id, pattern),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_triggers(self, guild_id: int) -> list[str]:
        cur = await self._conn.execute(
            "SELECT pattern FROM assistant_triggers WHERE guild_id = ?", (guild_id,)
        )
        rows = await cur.fetchall()
        return [r["pattern"] for r in rows]

    # ── Learned facts ─────────────────────────────────────────────────

    async def add_learned_fact(
        self,
        guild_id: int,
        fact: str,
        embedding: bytes | None,
        model: str | None,
        qdrant_id: str | None = None,
        source: str = "conversation",
        confidence: float = 1.0,
        approved: bool = True,
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO learned_facts (guild_id, fact, embedding, model, qdrant_id, source, confidence, approved) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, fact, embedding, model, qdrant_id, source, confidence, int(approved)),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_learned_fact(self, guild_id: int, fact_id: int):
        cur = await self._conn.execute(
            "SELECT * FROM learned_facts WHERE guild_id = ? AND id = ?",
            (guild_id, fact_id),
        )
        return await cur.fetchone()

    async def get_learned_facts(self, guild_id: int, approved_only: bool = True):
        if approved_only:
            cur = await self._conn.execute(
                "SELECT * FROM learned_facts WHERE guild_id = ? AND approved = 1 ORDER BY id DESC",
                (guild_id,),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM learned_facts WHERE guild_id = ? ORDER BY id DESC",
                (guild_id,),
            )
        return await cur.fetchall()

    async def delete_learned_fact(self, guild_id: int, fact_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM learned_facts WHERE id = ? AND guild_id = ?",
            (fact_id, guild_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def set_fact_approval(self, guild_id: int, fact_id: int, approved: bool) -> bool:
        cur = await self._conn.execute(
            "UPDATE learned_facts SET approved = ? WHERE id = ? AND guild_id = ?",
            (int(approved), fact_id, guild_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def reset_learned_facts(self, guild_id: int) -> int:
        cur = await self._conn.execute(
            "DELETE FROM learned_facts WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.commit()
        return cur.rowcount

    async def count_learned_facts(self, guild_id: int) -> int:
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM learned_facts WHERE guild_id = ? AND approved = 1",
            (guild_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def has_learned_message_mark(self, guild_id: int, message_id: int) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM learned_message_marks WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        return await cur.fetchone() is not None

    async def add_learned_message_mark(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        marked_by: int,
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO learned_message_marks (guild_id, channel_id, message_id, author_id, marked_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, message_id, author_id, marked_by),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    # ── Response feedback ─────────────────────────────────────────────

    async def add_feedback(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        message_id: int,
        rating: int,
        user_input: str | None = None,
        bot_response: str | None = None,
    ) -> bool:
        try:
            await self._conn.execute(
                "INSERT INTO response_feedback "
                "(guild_id, channel_id, user_id, message_id, rating, user_input, bot_response) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, user_id, message_id, rating, user_input, bot_response),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_feedback_stats(self, guild_id: int) -> dict:
        cur = await self._conn.execute(
            "SELECT "
            "  COUNT(*) as total, "
            "  COALESCE(SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END), 0) as positive, "
            "  COALESCE(SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END), 0) as negative "
            "FROM response_feedback WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cur.fetchone()
        return {"total": row["total"], "positive": row["positive"], "negative": row["negative"]}

    async def get_negative_feedback(self, guild_id: int, limit: int = 20):
        cur = await self._conn.execute(
            "SELECT * FROM response_feedback WHERE guild_id = ? AND rating = -1 "
            "ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def reset_feedback(self, guild_id: int) -> int:
        cur = await self._conn.execute(
            "DELETE FROM response_feedback WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.commit()
        return cur.rowcount

    # ── Prompt templates ──────────────────────────────────────────────

    async def save_prompt_template(
        self, guild_id: int, name: str, content: str, created_by: int
    ) -> bool:
        cur = await self._conn.execute(
            "SELECT id FROM prompt_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        existing = await cur.fetchone()
        if existing:
            await self._conn.execute(
                "UPDATE prompt_templates SET content = ?, created_by = ?, "
                "created_at = datetime('now') WHERE guild_id = ? AND name = ?",
                (content, created_by, guild_id, name),
            )
            await self._conn.commit()
            return False
        await self._conn.execute(
            "INSERT INTO prompt_templates (guild_id, name, content, created_by) VALUES (?, ?, ?, ?)",
            (guild_id, name, content, created_by),
        )
        await self._conn.commit()
        return True

    async def get_prompt_template(self, guild_id: int, name: str):
        cur = await self._conn.execute(
            "SELECT * FROM prompt_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def list_prompt_templates(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, name, content, created_by, created_at FROM prompt_templates "
            "WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()

    async def delete_prompt_template(self, guild_id: int, name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM prompt_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self._conn.commit()
        return cur.rowcount > 0
