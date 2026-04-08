"""Shared async SQLite database used by all cogs.

Tables
------
- guild_config      per-guild settings (mod-log channel, welcome channel, etc.)
- mod_cases         infraction / moderation case log
- warnings          active warnings per member
- tickets           support ticket metadata
- ticket_messages   messages inside a ticket (for transcript)
- automod_filters   per-guild word / link filter lists
- conversation_history  LLM conversation turns (migrated from old history.py)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bot.db")

_SCHEMA = """
-- Per-guild configuration (key/value style)
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id    INTEGER NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT,
    PRIMARY KEY (guild_id, key)
);

-- Moderation cases
CREATE TABLE IF NOT EXISTS mod_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action      TEXT    NOT NULL,   -- warn, mute, kick, ban, unban, unmute
    reason      TEXT,
    duration    INTEGER,            -- seconds, NULL = permanent
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cases_guild ON mod_cases (guild_id);
CREATE INDEX IF NOT EXISTS idx_cases_user  ON mod_cases (guild_id, user_id);

-- Active warnings
CREATE TABLE IF NOT EXISTS warnings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings (guild_id, user_id, active);

-- Tickets
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER UNIQUE,
    user_id     INTEGER NOT NULL,
    subject     TEXT,
    status      TEXT    NOT NULL DEFAULT 'open',   -- open, claimed, closed
    claimed_by  INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets (guild_id, status);

-- Ticket transcript messages
CREATE TABLE IF NOT EXISTS ticket_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    user_id     INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Auto-mod filter entries
CREATE TABLE IF NOT EXISTS automod_filters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    filter_type TEXT    NOT NULL,   -- word, link, regex
    pattern     TEXT    NOT NULL,
    UNIQUE(guild_id, filter_type, pattern)
);
CREATE INDEX IF NOT EXISTS idx_automod_guild ON automod_filters (guild_id, filter_type);

-- LLM conversation history (per-user, per-channel)
CREATE TABLE IF NOT EXISTS conversation_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_convo_user ON conversation_history (guild_id, channel_id, user_id);

-- Embeddings / RAG knowledge base
CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   BLOB,                   -- serialised float list
    model       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_embed_guild ON embeddings (guild_id);

-- Custom function definitions for function calling
CREATE TABLE IF NOT EXISTS custom_functions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL,
    parameters  TEXT    NOT NULL,        -- JSON schema string
    code        TEXT    NOT NULL,        -- Python code string
    enabled     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_funcs_guild ON custom_functions (guild_id);

-- Token usage tracking per guild
CREATE TABLE IF NOT EXISTS token_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_guild ON token_usage (guild_id);

-- Assistant trigger phrases per guild
CREATE TABLE IF NOT EXISTS assistant_triggers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    pattern     TEXT    NOT NULL,
    UNIQUE(guild_id, pattern)
);
CREATE INDEX IF NOT EXISTS idx_triggers_guild ON assistant_triggers (guild_id);

-- Economy / bank accounts
CREATE TABLE IF NOT EXISTS economy_accounts (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,
    last_payday TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- Custom commands
CREATE TABLE IF NOT EXISTS custom_commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    response    TEXT    NOT NULL,
    creator_id  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_cc_guild ON custom_commands (guild_id);

-- User reports
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    reporter_id     INTEGER NOT NULL,
    reported_user_id INTEGER NOT NULL,
    reason          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'open',   -- open, resolved, dismissed
    resolved_by     INTEGER,
    resolution_note TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_guild ON reports (guild_id, status);

-- Self-assignable roles
CREATE TABLE IF NOT EXISTS selfroles (
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

-- Command permission overrides (per-guild)
CREATE TABLE IF NOT EXISTS command_permissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    command     TEXT    NOT NULL,
    target_type TEXT    NOT NULL,   -- role, channel, user
    target_id   INTEGER NOT NULL,
    allowed     INTEGER NOT NULL DEFAULT 1,  -- 1 = allow, 0 = deny
    UNIQUE(guild_id, command, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_cmdperm_guild ON command_permissions (guild_id, command);

-- Moderator notes on users (not public infractions)
CREATE TABLE IF NOT EXISTS case_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    note        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON case_notes (guild_id, user_id);
"""


class Database:
    """Thin async wrapper around a shared SQLite database."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def setup(self) -> None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._db = await aiosqlite.connect(DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Database ready at %s", DB_PATH)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not initialised – call setup() first"
        return self._db

    # ------------------------------------------------------------------
    # Guild config helpers
    # ------------------------------------------------------------------

    async def get_guild_config(self, guild_id: int, key: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT value FROM guild_config WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_guild_config(self, guild_id: int, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, value),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Mod cases
    # ------------------------------------------------------------------

    async def add_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str | None = None,
        duration: int | None = None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO mod_cases (guild_id, user_id, moderator_id, action, reason, duration) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, action, reason, duration),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_cases(self, guild_id: int, user_id: int | None = None, limit: int = 25):
        if user_id:
            cur = await self.conn.execute(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, user_id, limit),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM mod_cases WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, limit),
            )
        return await cur.fetchall()

    async def get_case_by_id(self, guild_id: int, case_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM mod_cases WHERE id = ? AND guild_id = ?",
            (case_id, guild_id),
        )
        return await cur.fetchone()

    async def update_case_reason(self, guild_id: int, case_id: int, reason: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE mod_cases SET reason = ? WHERE id = ? AND guild_id = ?",
            (reason, case_id, guild_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def count_cases(self, guild_id: int, user_id: int | None = None) -> int:
        if user_id:
            cur = await self.conn.execute(
                "SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        else:
            cur = await self.conn.execute(
                "SELECT COUNT(*) FROM mod_cases WHERE guild_id = ?",
                (guild_id,),
            )
        row = await cur.fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Warnings
    # ------------------------------------------------------------------

    async def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str | None
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, reason),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_active_warnings(self, guild_id: int, user_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY id",
            (guild_id, user_id),
        )
        return await cur.fetchall()

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute(
            "UPDATE warnings SET active = 0 WHERE guild_id = ? AND user_id = ? AND active = 1",
            (guild_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount

    async def delete_warning(self, guild_id: int, warning_id: int) -> bool:
        cur = await self.conn.execute(
            "UPDATE warnings SET active = 0 WHERE id = ? AND guild_id = ? AND active = 1",
            (warning_id, guild_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Case notes
    # ------------------------------------------------------------------

    async def add_note(self, guild_id: int, user_id: int, moderator_id: int, note: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO case_notes (guild_id, user_id, moderator_id, note) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, note),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_notes(self, guild_id: int, user_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM case_notes WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (guild_id, user_id),
        )
        return await cur.fetchall()

    async def delete_note(self, guild_id: int, note_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM case_notes WHERE id = ? AND guild_id = ?",
            (note_id, guild_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    async def create_ticket(
        self, guild_id: int, user_id: int, channel_id: int, subject: str | None
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO tickets (guild_id, user_id, channel_id, subject) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, channel_id, subject),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_open_tickets(self, guild_id: int, user_id: int | None = None):
        if user_id:
            cur = await self.conn.execute(
                "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status != 'closed'",
                (guild_id, user_id),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM tickets WHERE guild_id = ? AND status != 'closed'",
                (guild_id,),
            )
        return await cur.fetchall()

    async def close_ticket(self, ticket_id: int) -> None:
        await self.conn.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), ticket_id),
        )
        await self.conn.commit()

    async def claim_ticket(self, ticket_id: int, moderator_id: int) -> None:
        await self.conn.execute(
            "UPDATE tickets SET status = 'claimed', claimed_by = ? WHERE id = ?",
            (moderator_id, ticket_id),
        )
        await self.conn.commit()

    async def add_ticket_message(self, ticket_id: int, user_id: int, content: str) -> None:
        await self.conn.execute(
            "INSERT INTO ticket_messages (ticket_id, user_id, content) VALUES (?, ?, ?)",
            (ticket_id, user_id, content),
        )
        await self.conn.commit()

    async def get_ticket_transcript(self, ticket_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
        return await cur.fetchall()

    async def get_ticket_by_channel(self, channel_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
        )
        return await cur.fetchone()

    # ------------------------------------------------------------------
    # Auto-mod filters
    # ------------------------------------------------------------------

    async def add_filter(self, guild_id: int, filter_type: str, pattern: str) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO automod_filters (guild_id, filter_type, pattern) VALUES (?, ?, ?)",
                (guild_id, filter_type, pattern),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_filter(self, guild_id: int, filter_type: str, pattern: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM automod_filters WHERE guild_id = ? AND filter_type = ? AND pattern = ?",
            (guild_id, filter_type, pattern),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_filters(self, guild_id: int, filter_type: str | None = None):
        if filter_type:
            cur = await self.conn.execute(
                "SELECT * FROM automod_filters WHERE guild_id = ? AND filter_type = ?",
                (guild_id, filter_type),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM automod_filters WHERE guild_id = ?", (guild_id,)
            )
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # Conversation history (per-user, per-channel)
    # ------------------------------------------------------------------

    async def add_conversation_message(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        role: str,
        content: str,
        token_count: int = 0,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO conversation_history (guild_id, channel_id, user_id, role, content, token_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, user_id, role, content, token_count),
        )
        await self.conn.commit()

    async def get_conversation_history(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        limit: int = 40,
    ):
        cur = await self.conn.execute(
            "SELECT role, content, token_count FROM conversation_history "
            "WHERE guild_id = ? AND channel_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, channel_id, user_id, limit),
        )
        rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def get_conversation_stats(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> dict:
        cur = await self.conn.execute(
            "SELECT COUNT(*) as msg_count, COALESCE(SUM(token_count), 0) as total_tokens "
            "FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        row = await cur.fetchone()
        return {"messages": row["msg_count"], "tokens": row["total_tokens"]}

    async def clear_conversation_history(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> int:
        cur = await self.conn.execute(
            "DELETE FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount

    async def pop_last_conversation_message(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM conversation_history WHERE id = ("
            "  SELECT id FROM conversation_history "
            "  WHERE guild_id = ? AND channel_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1"
            ")",
            (guild_id, channel_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def replace_conversation(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        messages: list[dict],
    ) -> None:
        """Replace all conversation history with compacted messages."""
        await self.conn.execute(
            "DELETE FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        for m in messages:
            await self.conn.execute(
                "INSERT INTO conversation_history (guild_id, channel_id, user_id, role, content, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, user_id, m["role"], m["content"], m.get("token_count", 0)),
            )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Embeddings / RAG knowledge base
    # ------------------------------------------------------------------

    async def add_embedding(
        self, guild_id: int, name: str, text: str, embedding: bytes | None, model: str | None
    ) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO embeddings (guild_id, name, text, embedding, model) VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, text, embedding, model),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_embedding(
        self, guild_id: int, name: str, text: str, embedding: bytes | None, model: str | None
    ) -> bool:
        cur = await self.conn.execute(
            "UPDATE embeddings SET text = ?, embedding = ?, model = ? WHERE guild_id = ? AND name = ?",
            (text, embedding, model, guild_id, name),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def delete_embedding(self, guild_id: int, name: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM embeddings WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_embedding(self, guild_id: int, name: str):
        cur = await self.conn.execute(
            "SELECT * FROM embeddings WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def get_all_embeddings(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM embeddings WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()

    async def reset_embeddings(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM embeddings WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Custom functions
    # ------------------------------------------------------------------

    async def add_custom_function(
        self, guild_id: int, name: str, description: str, parameters: str, code: str
    ) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO custom_functions (guild_id, name, description, parameters, code) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, description, parameters, code),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def delete_custom_function(self, guild_id: int, name: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM custom_functions WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def toggle_custom_function(self, guild_id: int, name: str) -> bool | None:
        row = await self.conn.execute(
            "SELECT enabled FROM custom_functions WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        existing = await row.fetchone()
        if not existing:
            return None
        new_val = 0 if existing["enabled"] else 1
        await self.conn.execute(
            "UPDATE custom_functions SET enabled = ? WHERE guild_id = ? AND name = ?",
            (new_val, guild_id, name),
        )
        await self.conn.commit()
        return bool(new_val)

    async def get_enabled_functions(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM custom_functions WHERE guild_id = ? AND enabled = 1",
            (guild_id,),
        )
        return await cur.fetchall()

    async def get_all_functions(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM custom_functions WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # Token usage tracking
    # ------------------------------------------------------------------

    async def log_token_usage(
        self, guild_id: int, user_id: int, prompt_tokens: int, completion_tokens: int
    ) -> None:
        await self.conn.execute(
            "INSERT INTO token_usage (guild_id, user_id, prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, user_id, prompt_tokens, completion_tokens),
        )
        await self.conn.commit()

    async def get_guild_usage(self, guild_id: int) -> dict:
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) as prompt, "
            "COALESCE(SUM(completion_tokens), 0) as completion "
            "FROM token_usage WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cur.fetchone()
        return {"prompt_tokens": row["prompt"], "completion_tokens": row["completion"]}

    async def get_user_usage(self, guild_id: int, user_id: int) -> dict:
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) as prompt, "
            "COALESCE(SUM(completion_tokens), 0) as completion "
            "FROM token_usage WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return {"prompt_tokens": row["prompt"], "completion_tokens": row["completion"]}

    async def reset_usage(self, guild_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM token_usage WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Assistant triggers
    # ------------------------------------------------------------------

    async def add_trigger(self, guild_id: int, pattern: str) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO assistant_triggers (guild_id, pattern) VALUES (?, ?)",
                (guild_id, pattern),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_trigger(self, guild_id: int, pattern: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM assistant_triggers WHERE guild_id = ? AND pattern = ?",
            (guild_id, pattern),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_triggers(self, guild_id: int) -> list[str]:
        cur = await self.conn.execute(
            "SELECT pattern FROM assistant_triggers WHERE guild_id = ?", (guild_id,)
        )
        rows = await cur.fetchall()
        return [r["pattern"] for r in rows]

    # ------------------------------------------------------------------
    # Economy
    # ------------------------------------------------------------------

    async def ensure_account(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO economy_accounts (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        await self.conn.commit()

    async def get_balance(self, guild_id: int, user_id: int) -> int:
        await self.ensure_account(guild_id, user_id)
        cur = await self.conn.execute(
            "SELECT balance FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return row["balance"] if row else 0

    async def set_balance(self, guild_id: int, user_id: int, amount: int) -> None:
        await self.ensure_account(guild_id, user_id)
        await self.conn.execute(
            "UPDATE economy_accounts SET balance = ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, user_id),
        )
        await self.conn.commit()

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        await self.ensure_account(guild_id, user_id)
        await self.conn.execute(
            "UPDATE economy_accounts SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, user_id),
        )
        await self.conn.commit()
        return await self.get_balance(guild_id, user_id)

    async def transfer_balance(
        self, guild_id: int, from_id: int, to_id: int, amount: int
    ) -> bool:
        bal = await self.get_balance(guild_id, from_id)
        if bal < amount:
            return False
        await self.ensure_account(guild_id, to_id)
        await self.conn.execute(
            "UPDATE economy_accounts SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, from_id),
        )
        await self.conn.execute(
            "UPDATE economy_accounts SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, to_id),
        )
        await self.conn.commit()
        return True

    async def get_last_payday(self, guild_id: int, user_id: int) -> str | None:
        cur = await self.conn.execute(
            "SELECT last_payday FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return row["last_payday"] if row else None

    async def set_last_payday(self, guild_id: int, user_id: int, ts: str) -> None:
        await self.conn.execute(
            "UPDATE economy_accounts SET last_payday = ? WHERE guild_id = ? AND user_id = ?",
            (ts, guild_id, user_id),
        )
        await self.conn.commit()

    async def get_leaderboard(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute(
            "SELECT user_id, balance FROM economy_accounts WHERE guild_id = ? ORDER BY balance DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # Custom commands
    # ------------------------------------------------------------------

    async def add_custom_command(
        self, guild_id: int, name: str, response: str, creator_id: int
    ) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO custom_commands (guild_id, name, response, creator_id) VALUES (?, ?, ?, ?)",
                (guild_id, name.lower(), response, creator_id),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def edit_custom_command(self, guild_id: int, name: str, response: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE custom_commands SET response = ? WHERE guild_id = ? AND name = ?",
            (response, guild_id, name.lower()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def delete_custom_command(self, guild_id: int, name: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM custom_commands WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_custom_command(self, guild_id: int, name: str):
        cur = await self.conn.execute(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )
        return await cur.fetchone()

    async def list_custom_commands(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT name, creator_id, created_at FROM custom_commands WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    async def create_report(
        self, guild_id: int, reporter_id: int, reported_user_id: int, reason: str
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO reports (guild_id, reporter_id, reported_user_id, reason) VALUES (?, ?, ?, ?)",
            (guild_id, reporter_id, reported_user_id, reason),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_open_reports(self, guild_id: int, limit: int = 25):
        cur = await self.conn.execute(
            "SELECT * FROM reports WHERE guild_id = ? AND status = 'open' ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def resolve_report(
        self, report_id: int, resolved_by: int, note: str | None, status: str = "resolved"
    ) -> bool:
        cur = await self.conn.execute(
            "UPDATE reports SET status = ?, resolved_by = ?, resolution_note = ?, resolved_at = ? WHERE id = ?",
            (status, resolved_by, note, datetime.now(timezone.utc).isoformat(), report_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_report(self, report_id: int):
        cur = await self.conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
        return await cur.fetchone()

    # ------------------------------------------------------------------
    # Self-roles
    # ------------------------------------------------------------------

    async def add_selfrole(self, guild_id: int, role_id: int) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO selfroles (guild_id, role_id) VALUES (?, ?)",
                (guild_id, role_id),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_selfrole(self, guild_id: int, role_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM selfroles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_selfroles(self, guild_id: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT role_id FROM selfroles WHERE guild_id = ?", (guild_id,)
        )
        rows = await cur.fetchall()
        return [r["role_id"] for r in rows]

    # ------------------------------------------------------------------
    # Command permissions
    # ------------------------------------------------------------------

    async def set_command_permission(
        self, guild_id: int, command: str, target_type: str, target_id: int, allowed: bool
    ) -> None:
        await self.conn.execute(
            "INSERT INTO command_permissions (guild_id, command, target_type, target_id, allowed) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, command, target_type, target_id) DO UPDATE SET allowed = excluded.allowed",
            (guild_id, command, target_type, target_id, int(allowed)),
        )
        await self.conn.commit()

    async def remove_command_permission(
        self, guild_id: int, command: str, target_type: str, target_id: int
    ) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM command_permissions WHERE guild_id = ? AND command = ? AND target_type = ? AND target_id = ?",
            (guild_id, command, target_type, target_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_command_permissions(self, guild_id: int, command: str):
        cur = await self.conn.execute(
            "SELECT * FROM command_permissions WHERE guild_id = ? AND command = ?",
            (guild_id, command),
        )
        return await cur.fetchall()

    async def check_command_allowed(
        self, guild_id: int, command: str, user_id: int, channel_id: int, role_ids: list[int]
    ) -> bool | None:
        """Return True/False if an explicit override exists, or None if no override."""
        perms = await self.get_command_permissions(guild_id, command)
        if not perms:
            return None
        for p in perms:
            if p["target_type"] == "user" and p["target_id"] == user_id:
                return bool(p["allowed"])
            if p["target_type"] == "channel" and p["target_id"] == channel_id:
                return bool(p["allowed"])
            if p["target_type"] == "role" and p["target_id"] in role_ids:
                return bool(p["allowed"])
        return None
