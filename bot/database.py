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
    embedding   BLOB,                   -- kept for backwards compat (unused)
    model       TEXT,
    source_url  TEXT,                   -- origin URL if ingested via crawler
    qdrant_id   TEXT,                   -- UUID key in Qdrant collection
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_embed_guild ON embeddings (guild_id);
CREATE INDEX IF NOT EXISTS idx_embed_source ON embeddings (guild_id, source_url);

-- Crawl sources — tracks which URLs have been ingested per guild
CREATE TABLE IF NOT EXISTS crawl_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    url         TEXT    NOT NULL,
    title       TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    crawled_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, url)
);
CREATE INDEX IF NOT EXISTS idx_crawl_guild ON crawl_sources (guild_id);

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

-- Reaction roles (emoji-based role assignment)
CREATE TABLE IF NOT EXISTS reaction_roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    emoji       TEXT    NOT NULL,
    role_id     INTEGER NOT NULL,
    unique_role INTEGER NOT NULL DEFAULT 0,  -- 1 = remove other roles from this message when adding
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, message_id, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reaction_msg ON reaction_roles (message_id);

-- Polls system
CREATE TABLE IF NOT EXISTS polls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER UNIQUE,
    creator_id      INTEGER NOT NULL,
    question        TEXT    NOT NULL,
    options         TEXT    NOT NULL,    -- JSON array of options
    multiple_choice INTEGER NOT NULL DEFAULT 0,  -- 0 = single choice, 1 = multiple choice
    anonymous       INTEGER NOT NULL DEFAULT 0,  -- 0 = public votes, 1 = anonymous
    ends_at         TEXT,                  -- NULL = no end time
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_polls_guild ON polls (guild_id);

-- Poll votes
CREATE TABLE IF NOT EXISTS poll_votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    option_index INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(poll_id, user_id, option_index)
);
CREATE INDEX IF NOT EXISTS idx_poll_votes_poll ON poll_votes (poll_id, user_id);

-- Raid protection system
CREATE TABLE IF NOT EXISTS raid_settings (
    guild_id        INTEGER PRIMARY KEY,
    enabled         INTEGER NOT NULL DEFAULT 0,
    join_threshold  INTEGER NOT NULL DEFAULT 5,      -- Number of joins in window to trigger
    join_window     INTEGER NOT NULL DEFAULT 60,     -- Time window in seconds
    account_age_min INTEGER NOT NULL DEFAULT 0,      -- Minimum account age in hours (0 = disabled)
    lockdown_duration INTEGER NOT NULL DEFAULT 300,  -- Lockdown duration in seconds
    alert_channel_id INTEGER,                        -- Channel to send raid alerts to
    auto_ban        INTEGER NOT NULL DEFAULT 0,      -- Auto-ban raiders (1 = yes)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Raid events log
CREATE TABLE IF NOT EXISTS raid_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    triggered_at    TEXT NOT NULL DEFAULT (datetime('now')),
    join_count      INTEGER NOT NULL,
    window_seconds  INTEGER NOT NULL,
    actions_taken   TEXT,                           -- JSON array of actions taken
    resolved_at     TEXT,
    resolved_by     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_raid_events_guild ON raid_events (guild_id, triggered_at DESC);

-- Join tracking for raid detection
CREATE TABLE IF NOT EXISTS join_tracking (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    joined_at       TEXT NOT NULL DEFAULT (datetime('now')),
    account_created TEXT,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_join_tracking_guild_time ON join_tracking (guild_id, joined_at DESC);

-- Invite tracking system
CREATE TABLE IF NOT EXISTS invite_codes (
    guild_id        INTEGER NOT NULL,
    invite_code     TEXT    NOT NULL,
    inviter_id      INTEGER NOT NULL,
    channel_id      INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    uses            INTEGER NOT NULL DEFAULT 0,
    max_uses        INTEGER,                -- NULL = unlimited
    temporary       INTEGER NOT NULL DEFAULT 0,  -- 1 = temporary invite
    expires_at      TEXT,                   -- NULL = never expires
    UNIQUE(guild_id, invite_code)
);
CREATE INDEX IF NOT EXISTS idx_invite_codes_guild ON invite_codes (guild_id, inviter_id);

-- Invite uses tracking
CREATE TABLE IF NOT EXISTS invite_uses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    invite_code     TEXT    NOT NULL,
    user_id         INTEGER NOT NULL,
    inviter_id      INTEGER NOT NULL,
    used_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    left_at         TEXT,                   -- When user left (if tracked)
    account_created TEXT,                   -- User's account creation time
    UNIQUE(guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_invite_uses_guild ON invite_uses (guild_id, invite_code, used_at DESC);
CREATE INDEX IF NOT EXISTS idx_invite_uses_inviter ON invite_uses (guild_id, inviter_id, used_at DESC);

-- Birthday tracking system
CREATE TABLE IF NOT EXISTS birthdays (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    birthday    TEXT    NOT NULL,  -- Format: MM-DD
    timezone    TEXT    DEFAULT 'UTC',
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_birthdays_guild ON birthdays (guild_id, birthday);

-- Birthday announcements sent (to prevent duplicate announcements in a day)
CREATE TABLE IF NOT EXISTS birthday_announcements (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    date_sent   TEXT    NOT NULL,  -- Format: YYYY-MM-DD
    PRIMARY KEY (guild_id, user_id, date_sent)
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

-- Leveling / XP system
CREATE TABLE IF NOT EXISTS levels (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    xp              INTEGER NOT NULL DEFAULT 0,
    level           INTEGER NOT NULL DEFAULT 0,
    last_xp_at      TEXT,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_levels_guild ON levels (guild_id, xp DESC);

-- Giveaways
CREATE TABLE IF NOT EXISTS giveaways (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    prize       TEXT    NOT NULL,
    end_time    TEXT    NOT NULL,
    winner_count INTEGER NOT NULL DEFAULT 1,
    host_id     INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active',  -- active, ended
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_giveaways_guild ON giveaways (guild_id, status);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    entered_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (giveaway_id, user_id)
);

-- Reminders
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER,
    channel_id  INTEGER,
    message     TEXT    NOT NULL,
    end_time    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders (end_time);

-- Starboard
CREATE TABLE IF NOT EXISTS starboard_messages (
    message_id      INTEGER PRIMARY KEY,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    author_id       INTEGER NOT NULL,
    star_count      INTEGER NOT NULL DEFAULT 0,
    starboard_msg_id INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_starboard_guild ON starboard_messages (guild_id);

-- Highlights / keyword notifications
CREATE TABLE IF NOT EXISTS highlights (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    keyword     TEXT    NOT NULL,
    PRIMARY KEY (user_id, guild_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_highlights_guild ON highlights (guild_id);

-- GitHub repo subscriptions (per guild)
CREATE TABLE IF NOT EXISTS github_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    repo            TEXT    NOT NULL,       -- "owner/repo"
    events          TEXT    NOT NULL DEFAULT 'push,pull_request,issues,release',
    added_by        INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, channel_id, repo)
);
CREATE INDEX IF NOT EXISTS idx_gh_subs_guild ON github_subscriptions (guild_id);

-- GitHub poll state — tracks last-seen etag/timestamp per repo per event type
CREATE TABLE IF NOT EXISTS github_poll_state (
    repo        TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,   -- push, pull_request, issues, release
    last_id     TEXT,               -- last processed event/item ID
    etag        TEXT,               -- HTTP ETag for conditional requests
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (repo, event_type)
);

-- GitLab project subscriptions (per guild)
CREATE TABLE IF NOT EXISTS gitlab_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    project         TEXT    NOT NULL,       -- "namespace/project" or numeric project ID as text
    events          TEXT    NOT NULL DEFAULT 'push,merge_request,issues,release',
    added_by        INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, channel_id, project)
);
CREATE INDEX IF NOT EXISTS idx_gl_subs_guild ON gitlab_subscriptions (guild_id);

-- GitLab poll state — tracks last-seen event ID per project
CREATE TABLE IF NOT EXISTS gitlab_poll_state (
    project     TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    last_id     TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project, event_type)
);

-- Adaptive learning: facts extracted from conversations and manual training
CREATE TABLE IF NOT EXISTS learned_facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    fact        TEXT    NOT NULL,
    embedding   BLOB,                   -- serialised float list (same format as embeddings)
    model       TEXT,
    qdrant_id   TEXT,
    source      TEXT    NOT NULL DEFAULT 'conversation',  -- conversation | training | qa_pair
    confidence  REAL    NOT NULL DEFAULT 1.0,
    approved    INTEGER NOT NULL DEFAULT 1,               -- 1 = active, 0 = hidden
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, fact)
);
CREATE INDEX IF NOT EXISTS idx_facts_guild ON learned_facts (guild_id, approved);

-- Explicit message training marks (brain emoji on Discord messages)
CREATE TABLE IF NOT EXISTS learned_message_marks (
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    author_id       INTEGER NOT NULL,
    marked_by       INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_learned_message_marks_guild ON learned_message_marks (guild_id, channel_id);

-- Named prompt templates per guild (saved presets)
CREATE TABLE IF NOT EXISTS prompt_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_by  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_templates_guild ON prompt_templates (guild_id);

-- Response feedback: per-message thumbs up/down ratings
CREATE TABLE IF NOT EXISTS response_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,   -- the bot's reply message_id
    rating          INTEGER NOT NULL,   -- 1 = positive, -1 = negative
    user_input      TEXT,               -- what the user asked
    bot_response    TEXT,               -- what the bot replied
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, message_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_guild ON response_feedback (guild_id);

-- MCP server configurations per guild
CREATE TABLE IF NOT EXISTS mcp_servers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,       -- unique label for this server within the guild
    transport   TEXT    NOT NULL DEFAULT 'stdio',  -- stdio | sse
    command     TEXT,                   -- full command for stdio (e.g. 'npx -y @mcp/server-fs')
    args        TEXT    NOT NULL DEFAULT '[]',  -- JSON array of extra args
    env         TEXT    NOT NULL DEFAULT '{}',  -- JSON object of env vars
    url         TEXT,                   -- SSE endpoint URL
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_guild ON mcp_servers (guild_id);
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
        await self._migrate()
        logger.info("Database ready at %s", DB_PATH)

    async def _migrate(self) -> None:
        """Apply incremental schema migrations for existing databases."""
        # Add source_url to embeddings if it doesn't exist yet
        cur = await self._db.execute("PRAGMA table_info(embeddings)")  # type: ignore[union-attr]
        cols = {row[1] for row in await cur.fetchall()}
        if "source_url" not in cols:
            await self._db.execute("ALTER TABLE embeddings ADD COLUMN source_url TEXT")  # type: ignore[union-attr]
            await self._db.commit()
            logger.info("Migration: added source_url column to embeddings")

        # Create learned_facts table if missing (pre-existing DB)
        await self._db.execute(  # type: ignore[union-attr]
            """CREATE TABLE IF NOT EXISTS learned_facts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                fact        TEXT    NOT NULL,
                embedding   BLOB,
                model       TEXT,
                qdrant_id   TEXT,
                source      TEXT    NOT NULL DEFAULT 'conversation',
                confidence  REAL    NOT NULL DEFAULT 1.0,
                approved    INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, fact)
            )"""
        )
        await self._db.execute(  # type: ignore[union-attr]
            "CREATE INDEX IF NOT EXISTS idx_facts_guild ON learned_facts (guild_id, approved)"
        )
        # Ensure embedding column exists (tables created before this column was added)
        try:
            await self._db.execute(  # type: ignore[union-attr]
                "ALTER TABLE learned_facts ADD COLUMN embedding BLOB"
            )
        except Exception:
            pass  # Column already exists
        try:
            await self._db.execute(  # type: ignore[union-attr]
                "ALTER TABLE learned_facts ADD COLUMN model TEXT"
            )
        except Exception:
            pass
        try:
            await self._db.execute(  # type: ignore[union-attr]
                "ALTER TABLE learned_facts ADD COLUMN qdrant_id TEXT"
            )
        except Exception:
            pass
        await self._db.commit()  # type: ignore[union-attr]

        # Add qdrant_id column to embeddings if missing
        try:
            await self._db.execute(  # type: ignore[union-attr]
                "ALTER TABLE embeddings ADD COLUMN qdrant_id TEXT"
            )
            await self._db.commit()  # type: ignore[union-attr]
        except Exception:
            pass

        # Create learned_message_marks table if missing
        await self._db.execute(  # type: ignore[union-attr]
            """CREATE TABLE IF NOT EXISTS learned_message_marks (
                guild_id        INTEGER NOT NULL,
                channel_id      INTEGER NOT NULL,
                message_id      INTEGER NOT NULL,
                author_id       INTEGER NOT NULL,
                marked_by       INTEGER NOT NULL,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (guild_id, message_id)
            )"""
        )
        await self._db.execute(  # type: ignore[union-attr]
            "CREATE INDEX IF NOT EXISTS idx_learned_message_marks_guild ON learned_message_marks (guild_id, channel_id)"
        )

        # Create response_feedback table if missing
        await self._db.execute(  # type: ignore[union-attr]
            """CREATE TABLE IF NOT EXISTS response_feedback (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        INTEGER NOT NULL,
                channel_id      INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                message_id      INTEGER NOT NULL,
                rating          INTEGER NOT NULL,
                user_input      TEXT,
                bot_response    TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, message_id, user_id)
            )"""
        )
        await self._db.execute(  # type: ignore[union-attr]
            "CREATE INDEX IF NOT EXISTS idx_feedback_guild ON response_feedback (guild_id)"
        )

        # Create prompt_templates table if missing
        await self._db.execute(  # type: ignore[union-attr]
            """CREATE TABLE IF NOT EXISTS prompt_templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                created_by  INTEGER NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, name)
            )"""
        )
        await self._db.execute(  # type: ignore[union-attr]
            "CREATE INDEX IF NOT EXISTS idx_templates_guild ON prompt_templates (guild_id)"
        )
        await self._db.commit()  # type: ignore[union-attr]

        # Create mcp_servers table if missing
        await self._db.execute(  # type: ignore[union-attr]
            """CREATE TABLE IF NOT EXISTS mcp_servers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                transport   TEXT    NOT NULL DEFAULT 'stdio',
                command     TEXT,
                args        TEXT    NOT NULL DEFAULT '[]',
                env         TEXT    NOT NULL DEFAULT '{}',
                url         TEXT,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, name)
            )"""
        )
        await self._db.execute(  # type: ignore[union-attr]
            "CREATE INDEX IF NOT EXISTS idx_mcp_servers_guild ON mcp_servers (guild_id)"
        )
        await self._db.commit()  # type: ignore[union-attr]

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

    async def get_setting(self, guild_id: int, key: str) -> str:
        """Return DB value for *key*, falling back to ``DEFAULTS[key]``."""
        from bot.config import DEFAULTS

        val = await self.get_guild_config(guild_id, key)
        return val if val is not None else DEFAULTS.get(key, "")

    async def get_setting_int(self, guild_id: int, key: str) -> int:
        """Like :meth:`get_setting` but coerced to int."""
        return int(await self.get_setting(guild_id, key))

    async def get_setting_float(self, guild_id: int, key: str) -> float:
        """Like :meth:`get_setting` but coerced to float."""
        return float(await self.get_setting(guild_id, key))

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
            await self.conn.execute(
                "INSERT INTO embeddings (guild_id, name, text, model, source_url, qdrant_id) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, name, text, model, source_url, qdrant_id),
            )
            await self.conn.commit()
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
        cur = await self.conn.execute(
            "UPDATE embeddings SET text = ?, model = ?, source_url = ?, qdrant_id = ? WHERE guild_id = ? AND name = ?",
            (text, model, source_url, qdrant_id, guild_id, name),
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

    async def get_embedding_by_name(self, guild_id: int, name: str):
        cur = await self.conn.execute(
            "SELECT * FROM embeddings WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return await cur.fetchone()

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

    async def delete_embeddings_by_source(self, guild_id: int, source_url: str) -> int:
        cur = await self.conn.execute(
            "DELETE FROM embeddings WHERE guild_id = ? AND source_url = ?",
            (guild_id, source_url),
        )
        await self.conn.commit()
        return cur.rowcount

    async def reset_embeddings(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM embeddings WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Crawl sources
    # ------------------------------------------------------------------

    async def upsert_crawl_source(
        self, guild_id: int, url: str, title: str, chunk_count: int
    ) -> None:
        await self.conn.execute(
            "INSERT INTO crawl_sources (guild_id, url, title, chunk_count, crawled_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(guild_id, url) DO UPDATE SET "
            "title = excluded.title, chunk_count = excluded.chunk_count, crawled_at = excluded.crawled_at",
            (guild_id, url, title, chunk_count),
        )
        await self.conn.commit()

    async def get_crawl_sources(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM crawl_sources WHERE guild_id = ? ORDER BY crawled_at DESC",
            (guild_id,),
        )
        return await cur.fetchall()

    async def delete_crawl_source(self, guild_id: int, url: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM crawl_sources WHERE guild_id = ? AND url = ?",
            (guild_id, url),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def reset_crawl_sources(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM crawl_sources WHERE guild_id = ?", (guild_id,)
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

    # ------------------------------------------------------------------
    # Leveling / XP
    # ------------------------------------------------------------------

    async def get_level_row(self, guild_id: int, user_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return await cur.fetchone()

    async def ensure_level_row(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO levels (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        await self.conn.commit()

    async def add_xp(self, guild_id: int, user_id: int, amount: int, last_xp_at: str) -> dict:
        """Add XP and return updated row as dict."""
        await self.ensure_level_row(guild_id, user_id)
        await self.conn.execute(
            "UPDATE levels SET xp = xp + ?, last_xp_at = ? WHERE guild_id = ? AND user_id = ?",
            (amount, last_xp_at, guild_id, user_id),
        )
        await self.conn.commit()
        row = await self.get_level_row(guild_id, user_id)
        return dict(row)

    async def set_level(self, guild_id: int, user_id: int, level: int) -> None:
        await self.conn.execute(
            "UPDATE levels SET level = ? WHERE guild_id = ? AND user_id = ?",
            (level, guild_id, user_id),
        )
        await self.conn.commit()

    async def set_xp(self, guild_id: int, user_id: int, xp: int, level: int) -> None:
        await self.ensure_level_row(guild_id, user_id)
        await self.conn.execute(
            "UPDATE levels SET xp = ?, level = ? WHERE guild_id = ? AND user_id = ?",
            (xp, level, guild_id, user_id),
        )
        await self.conn.commit()

    async def get_level_leaderboard(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute(
            "SELECT user_id, xp, level FROM levels WHERE guild_id = ? ORDER BY xp DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def get_level_rank(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM levels WHERE guild_id = ? AND xp > "
            "(SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?)",
            (guild_id, guild_id, user_id),
        )
        row = await cur.fetchone()
        return (row[0] + 1) if row else 1

    async def reset_levels(self, guild_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM levels WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Giveaways
    # ------------------------------------------------------------------

    async def create_giveaway(
        self,
        guild_id: int,
        channel_id: int,
        prize: str,
        end_time: str,
        winner_count: int,
        host_id: int,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO giveaways (guild_id, channel_id, prize, end_time, winner_count, host_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, prize, end_time, winner_count, host_id),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def set_giveaway_message(self, giveaway_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE giveaways SET message_id = ? WHERE id = ?",
            (message_id, giveaway_id),
        )
        await self.conn.commit()

    async def get_giveaway(self, giveaway_id: int):
        cur = await self.conn.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
        return await cur.fetchone()

    async def get_active_giveaways(self, guild_id: int | None = None):
        if guild_id:
            cur = await self.conn.execute(
                "SELECT * FROM giveaways WHERE status = 'active' AND guild_id = ?", (guild_id,)
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM giveaways WHERE status = 'active'"
            )
        return await cur.fetchall()

    async def end_giveaway(self, giveaway_id: int) -> None:
        await self.conn.execute(
            "UPDATE giveaways SET status = 'ended' WHERE id = ?", (giveaway_id,)
        )
        await self.conn.commit()

    async def enter_giveaway(self, giveaway_id: int, user_id: int) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)",
                (giveaway_id, user_id),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def leave_giveaway(self, giveaway_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
            (giveaway_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_giveaway_entries(self, giveaway_id: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,)
        )
        rows = await cur.fetchall()
        return [r["user_id"] for r in rows]

    async def get_giveaway_entry_count(self, giveaway_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    async def create_reminder(
        self,
        user_id: int,
        message: str,
        end_time: str,
        guild_id: int | None = None,
        channel_id: int | None = None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO reminders (user_id, guild_id, channel_id, message, end_time) VALUES (?, ?, ?, ?, ?)",
            (user_id, guild_id, channel_id, message, end_time),
        )
        await self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_due_reminders(self, now: str):
        cur = await self.conn.execute(
            "SELECT * FROM reminders WHERE end_time <= ? ORDER BY end_time",
            (now,),
        )
        return await cur.fetchall()

    async def delete_reminder(self, reminder_id: int) -> None:
        await self.conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await self.conn.commit()

    async def get_user_reminders(self, user_id: int) -> list:
        cur = await self.conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? ORDER BY end_time",
            (user_id,),
        )
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # Starboard
    # ------------------------------------------------------------------

    async def get_starboard_message(self, message_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM starboard_messages WHERE message_id = ?", (message_id,)
        )
        return await cur.fetchone()

    async def upsert_starboard_message(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_id: int,
        star_count: int,
        starboard_msg_id: int | None = None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO starboard_messages (message_id, guild_id, channel_id, author_id, star_count, starboard_msg_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            "star_count = excluded.star_count, starboard_msg_id = COALESCE(excluded.starboard_msg_id, starboard_msg_id)",
            (message_id, guild_id, channel_id, author_id, star_count, starboard_msg_id),
        )
        await self.conn.commit()

    async def set_starboard_msg_id(self, message_id: int, starboard_msg_id: int) -> None:
        await self.conn.execute(
            "UPDATE starboard_messages SET starboard_msg_id = ? WHERE message_id = ?",
            (starboard_msg_id, message_id),
        )
        await self.conn.commit()

    async def delete_starboard_message(self, message_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM starboard_messages WHERE message_id = ?", (message_id,)
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Highlights / keyword notifications
    # ------------------------------------------------------------------

    async def add_highlight(self, user_id: int, guild_id: int, keyword: str) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO highlights (user_id, guild_id, keyword) VALUES (?, ?, ?)",
                (user_id, guild_id, keyword.lower()),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_highlight(self, user_id: int, guild_id: int, keyword: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM highlights WHERE user_id = ? AND guild_id = ? AND keyword = ?",
            (user_id, guild_id, keyword.lower()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_user_highlights(self, user_id: int, guild_id: int) -> list[str]:
        cur = await self.conn.execute(
            "SELECT keyword FROM highlights WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        rows = await cur.fetchall()
        return [r["keyword"] for r in rows]

    async def get_guild_highlights(self, guild_id: int):
        """Return all highlight rows for a guild — used for on_message scanning."""
        cur = await self.conn.execute(
            "SELECT user_id, keyword FROM highlights WHERE guild_id = ?", (guild_id,)
        )
        return await cur.fetchall()

    async def clear_user_highlights(self, user_id: int, guild_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM highlights WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # GitHub subscriptions
    # ------------------------------------------------------------------

    async def add_github_subscription(
        self,
        guild_id: int,
        channel_id: int,
        repo: str,
        events: str,
        added_by: int,
    ) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO github_subscriptions (guild_id, channel_id, repo, events, added_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, repo, events, added_by),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_github_subscription_events(
        self, guild_id: int, channel_id: int, repo: str, events: str
    ) -> bool:
        cur = await self.conn.execute(
            "UPDATE github_subscriptions SET events = ? "
            "WHERE guild_id = ? AND channel_id = ? AND repo = ?",
            (events, guild_id, channel_id, repo),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_github_subscription(
        self, guild_id: int, channel_id: int, repo: str
    ) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM github_subscriptions WHERE guild_id = ? AND channel_id = ? AND repo = ?",
            (guild_id, channel_id, repo),
        )
        if cur.rowcount > 0:
            remaining_cur = await self.conn.execute(
                "SELECT COUNT(*) AS c FROM github_subscriptions WHERE repo = ?",
                (repo,),
            )
            remaining = await remaining_cur.fetchone()
            if not remaining or remaining["c"] == 0:
                await self.conn.execute(
                    "DELETE FROM github_poll_state WHERE repo = ?",
                    (repo,),
                )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_github_subscriptions(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM github_subscriptions WHERE guild_id = ? ORDER BY repo",
            (guild_id,),
        )
        return await cur.fetchall()

    async def get_all_github_subscriptions(self):
        """Return every subscription across all guilds (used by poller)."""
        cur = await self.conn.execute("SELECT * FROM github_subscriptions")
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # GitHub poll state
    # ------------------------------------------------------------------

    async def get_github_poll_state(self, repo: str, event_type: str):
        cur = await self.conn.execute(
            "SELECT * FROM github_poll_state WHERE repo = ? AND event_type = ?",
            (repo, event_type),
        )
        return await cur.fetchone()

    async def set_github_poll_state(
        self, repo: str, event_type: str, last_id: str | None, etag: str | None
    ) -> None:
        await self.conn.execute(
            "INSERT INTO github_poll_state (repo, event_type, last_id, etag, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(repo, event_type) DO UPDATE SET "
            "last_id = excluded.last_id, etag = excluded.etag, updated_at = excluded.updated_at",
            (repo, event_type, last_id, etag),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # GitLab subscriptions
    # ------------------------------------------------------------------

    async def add_gitlab_subscription(
        self,
        guild_id: int,
        channel_id: int,
        project: str,
        events: str,
        added_by: int,
    ) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO gitlab_subscriptions (guild_id, channel_id, project, events, added_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, project, events, added_by),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_gitlab_subscription_events(
        self, guild_id: int, channel_id: int, project: str, events: str
    ) -> bool:
        cur = await self.conn.execute(
            "UPDATE gitlab_subscriptions SET events = ? "
            "WHERE guild_id = ? AND channel_id = ? AND project = ?",
            (events, guild_id, channel_id, project),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_gitlab_subscription(
        self, guild_id: int, channel_id: int, project: str
    ) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM gitlab_subscriptions WHERE guild_id = ? AND channel_id = ? AND project = ?",
            (guild_id, channel_id, project),
        )
        if cur.rowcount > 0:
            remaining_cur = await self.conn.execute(
                "SELECT COUNT(*) AS c FROM gitlab_subscriptions WHERE project = ?",
                (project,),
            )
            remaining = await remaining_cur.fetchone()
            if not remaining or remaining["c"] == 0:
                await self.conn.execute(
                    "DELETE FROM gitlab_poll_state WHERE project = ?",
                    (project,),
                )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_gitlab_subscriptions(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM gitlab_subscriptions WHERE guild_id = ? ORDER BY project",
            (guild_id,),
        )
        return await cur.fetchall()

    async def get_all_gitlab_subscriptions(self):
        """Return every subscription across all guilds (used by poller)."""
        cur = await self.conn.execute("SELECT * FROM gitlab_subscriptions")
        return await cur.fetchall()

    # ------------------------------------------------------------------
    # GitLab poll state
    # ------------------------------------------------------------------

    async def get_gitlab_poll_state(self, project: str, event_type: str):
        cur = await self.conn.execute(
            "SELECT * FROM gitlab_poll_state WHERE project = ? AND event_type = ?",
            (project, event_type),
        )
        return await cur.fetchone()

    async def set_gitlab_poll_state(
        self, project: str, event_type: str, last_id: str | None
    ) -> None:
        await self.conn.execute(
            "INSERT INTO gitlab_poll_state (project, event_type, last_id, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(project, event_type) DO UPDATE SET "
            "last_id = excluded.last_id, updated_at = excluded.updated_at",
            (project, event_type, last_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Learned facts (adaptive knowledge base)
    # ------------------------------------------------------------------

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
        """Insert a fact; silently ignore duplicates. Returns True if inserted."""
        try:
            await self.conn.execute(
                "INSERT INTO learned_facts (guild_id, fact, embedding, model, qdrant_id, source, confidence, approved) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, fact, embedding, model, qdrant_id, source, confidence, int(approved)),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_learned_fact(self, guild_id: int, fact_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM learned_facts WHERE guild_id = ? AND id = ?",
            (guild_id, fact_id),
        )
        return await cur.fetchone()

    async def get_learned_facts(self, guild_id: int, approved_only: bool = True):
        if approved_only:
            cur = await self.conn.execute(
                "SELECT * FROM learned_facts WHERE guild_id = ? AND approved = 1 ORDER BY id DESC",
                (guild_id,),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM learned_facts WHERE guild_id = ? ORDER BY id DESC",
                (guild_id,),
            )
        return await cur.fetchall()

    async def delete_learned_fact(self, guild_id: int, fact_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM learned_facts WHERE id = ? AND guild_id = ?",
            (fact_id, guild_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_fact_approval(self, guild_id: int, fact_id: int, approved: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE learned_facts SET approved = ? WHERE id = ? AND guild_id = ?",
            (int(approved), fact_id, guild_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def reset_learned_facts(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM learned_facts WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()
        return cur.rowcount

    async def count_learned_facts(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM learned_facts WHERE guild_id = ? AND approved = 1",
            (guild_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def has_learned_message_mark(self, guild_id: int, message_id: int) -> bool:
        cur = await self.conn.execute(
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
            await self.conn.execute(
                "INSERT INTO learned_message_marks (guild_id, channel_id, message_id, author_id, marked_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, message_id, author_id, marked_by),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    # ------------------------------------------------------------------
    # Response feedback (thumbs up / down)
    # ------------------------------------------------------------------

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
        """Record feedback. Returns False if already rated."""
        try:
            await self.conn.execute(
                "INSERT INTO response_feedback "
                "(guild_id, channel_id, user_id, message_id, rating, user_input, bot_response) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, user_id, message_id, rating, user_input, bot_response),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_feedback_stats(self, guild_id: int) -> dict:
        cur = await self.conn.execute(
            "SELECT "
            "  COUNT(*) as total, "
            "  COALESCE(SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END), 0) as positive, "
            "  COALESCE(SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END), 0) as negative "
            "FROM response_feedback WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cur.fetchone()
        return {
            "total": row["total"],
            "positive": row["positive"],
            "negative": row["negative"],
        }

    async def get_negative_feedback(self, guild_id: int, limit: int = 20):
        cur = await self.conn.execute(
            "SELECT * FROM response_feedback WHERE guild_id = ? AND rating = -1 "
            "ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def reset_feedback(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM response_feedback WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Prompt templates
    # ------------------------------------------------------------------

    async def save_prompt_template(
        self, guild_id: int, name: str, content: str, created_by: int
    ) -> bool:
        """Upsert a named prompt template. Returns True on insert, False on update."""
        cur = await self.conn.execute(
            "SELECT id FROM prompt_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        existing = await cur.fetchone()
        if existing:
            await self.conn.execute(
                "UPDATE prompt_templates SET content = ?, created_by = ?, "
                "created_at = datetime('now') WHERE guild_id = ? AND name = ?",
                (content, created_by, guild_id, name),
            )
            await self.conn.commit()
            return False
        await self.conn.execute(
            "INSERT INTO prompt_templates (guild_id, name, content, created_by) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, name, content, created_by),
        )
        await self.conn.commit()
        return True

    async def get_prompt_template(self, guild_id: int, name: str):
        cur = await self.conn.execute(
            "SELECT * FROM prompt_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def list_prompt_templates(self, guild_id: int):
        cur = await self.conn.execute(
            "SELECT id, name, content, created_by, created_at FROM prompt_templates "
            "WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return await cur.fetchall()

    async def delete_prompt_template(self, guild_id: int, name: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM prompt_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # MCP server configurations
    # ------------------------------------------------------------------

    async def add_mcp_server(
        self,
        guild_id: int,
        name: str,
        transport: str,
        command: str | None,
        args: str,
        env: str,
        url: str | None,
    ) -> bool:
        """Insert a new MCP server config.  Returns False if name already exists."""
        try:
            await self.conn.execute(
                "INSERT INTO mcp_servers (guild_id, name, transport, command, args, env, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, name, transport, command, args, env, url),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_mcp_server(self, guild_id: int, name: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM mcp_servers WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_mcp_servers(self, guild_id: int, enabled_only: bool = False):
        if enabled_only:
            cur = await self.conn.execute(
                "SELECT * FROM mcp_servers WHERE guild_id = ? AND enabled = 1 ORDER BY name",
                (guild_id,),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM mcp_servers WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            )
        return await cur.fetchall()

    async def get_mcp_server(self, guild_id: int, name: str):
        cur = await self.conn.execute(
            "SELECT * FROM mcp_servers WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def toggle_mcp_server(self, guild_id: int, name: str) -> bool | None:
        row = await self.get_mcp_server(guild_id, name)
        if row is None:
            return None
        new_val = 0 if row["enabled"] else 1
        await self.conn.execute(
            "UPDATE mcp_servers SET enabled = ? WHERE guild_id = ? AND name = ?",
            (new_val, guild_id, name),
        )
        await self.conn.commit()
        return bool(new_val)

    async def update_mcp_server(
        self,
        guild_id: int,
        name: str,
        *,
        transport: str | None = None,
        command: str | None = None,
        args: str | None = None,
        env: str | None = None,
        url: str | None = None,
    ) -> bool:
        """Update one or more fields of an existing MCP server config."""
        fields: list[str] = []
        values: list[object] = []
        if transport is not None:
            fields.append("transport = ?")
            values.append(transport)
        if command is not None:
            fields.append("command = ?")
            values.append(command)
        if args is not None:
            fields.append("args = ?")
            values.append(args)
        if env is not None:
            fields.append("env = ?")
            values.append(env)
        if url is not None:
            fields.append("url = ?")
            values.append(url)
        if not fields:
            return False
        values.extend([guild_id, name])
        cur = await self.conn.execute(
            f"UPDATE mcp_servers SET {', '.join(fields)} WHERE guild_id = ? AND name = ?",
            values,
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Reaction roles
    # ------------------------------------------------------------------

    async def add_reaction_role(
        self,
        guild_id: int,
        message_id: int,
        channel_id: int,
        emoji: str,
        role_id: int,
        unique_role: bool = False,
    ) -> bool:
        """Add a reaction role mapping. Returns False if emoji already exists for this message."""
        try:
            await self.conn.execute(
                "INSERT INTO reaction_roles (guild_id, message_id, channel_id, emoji, role_id, unique_role) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, message_id, channel_id, emoji, role_id, int(unique_role)),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> bool:
        """Remove a specific reaction role mapping."""
        cur = await self.conn.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            (guild_id, message_id, emoji),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_all_reaction_roles(self, guild_id: int, message_id: int) -> int:
        """Remove all reaction roles for a message. Returns number removed."""
        cur = await self.conn.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        await self.conn.commit()
        return cur.rowcount

    async def get_reaction_roles(self, guild_id: int, message_id: int = None):
        """Get reaction roles for a guild or specific message."""
        if message_id:
            cur = await self.conn.execute(
                "SELECT * FROM reaction_roles WHERE guild_id = ? AND message_id = ? ORDER BY emoji",
                (guild_id, message_id),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM reaction_roles WHERE guild_id = ? ORDER BY created_at DESC",
                (guild_id,),
            )
        return await cur.fetchall()

    async def get_reaction_role(self, guild_id: int, message_id: int, emoji: str):
        """Get a specific reaction role mapping."""
        cur = await self.conn.execute(
            "SELECT * FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            (guild_id, message_id, emoji),
        )
        return await cur.fetchone()

    # ------------------------------------------------------------------
    # Polls
    # ------------------------------------------------------------------

    async def create_poll(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        creator_id: int,
        question: str,
        options: list[str],
        multiple_choice: bool = False,
        anonymous: bool = False,
        ends_at: str | None = None,
    ) -> bool:
        """Create a new poll."""
        import json
        try:
            await self.conn.execute(
                "INSERT INTO polls (guild_id, channel_id, message_id, creator_id, question, options, multiple_choice, anonymous, ends_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, message_id, creator_id, question, json.dumps(options), int(multiple_choice), int(anonymous), ends_at),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_poll(self, guild_id: int, message_id: int):
        """Get a poll by message ID."""
        cur = await self.conn.execute(
            "SELECT * FROM polls WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        return await cur.fetchone()

    async def get_polls(self, guild_id: int, active_only: bool = False):
        """Get polls for a guild."""
        if active_only:
            cur = await self.conn.execute(
                "SELECT * FROM polls WHERE guild_id = ? AND (ends_at IS NULL OR ends_at > datetime('now')) ORDER BY created_at DESC",
                (guild_id,),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM polls WHERE guild_id = ? ORDER BY created_at DESC",
                (guild_id,),
            )
        return await cur.fetchall()

    async def delete_poll(self, guild_id: int, message_id: int) -> bool:
        """Delete a poll and its votes."""
        poll = await self.get_poll(guild_id, message_id)
        if not poll:
            return False
        
        await self.conn.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
        cur = await self.conn.execute("DELETE FROM polls WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def add_poll_vote(self, poll_id: int, user_id: int, option_index: int) -> bool:
        """Add a vote to a poll."""
        try:
            await self.conn.execute(
                "INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (?, ?, ?)",
                (poll_id, user_id, option_index),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_poll_vote(self, poll_id: int, user_id: int, option_index: int) -> bool:
        """Remove a vote from a poll."""
        cur = await self.conn.execute(
            "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ? AND option_index = ?",
            (poll_id, user_id, option_index),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_user_poll_votes(self, poll_id: int, user_id: int):
        """Get all votes by a user for a specific poll."""
        cur = await self.conn.execute(
            "SELECT option_index FROM poll_votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        )
        rows = await cur.fetchall()
        return [row["option_index"] for row in rows]

    async def get_poll_results(self, poll_id: int):
        """Get vote counts for all options in a poll."""
        cur = await self.conn.execute(
            "SELECT option_index, COUNT(*) as votes FROM poll_votes WHERE poll_id = ? GROUP BY option_index ORDER BY option_index",
            (poll_id,),
        )
        return await cur.fetchall()

    async def clear_user_poll_votes(self, poll_id: int, user_id: int) -> int:
        """Clear all votes by a user for a poll (for single-choice polls)."""
        cur = await self.conn.execute(
            "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Raid Protection
    # ------------------------------------------------------------------

    async def get_raid_settings(self, guild_id: int):
        """Get raid protection settings for a guild."""
        cur = await self.conn.execute(
            "SELECT * FROM raid_settings WHERE guild_id = ?",
            (guild_id,),
        )
        return await cur.fetchone()

    async def update_raid_settings(
        self,
        guild_id: int,
        *,
        enabled: bool | None = None,
        join_threshold: int | None = None,
        join_window: int | None = None,
        account_age_min: int | None = None,
        lockdown_duration: int | None = None,
        alert_channel_id: int | None = None,
        auto_ban: bool | None = None,
    ) -> bool:
        """Update raid protection settings."""
        # Check if settings exist
        existing = await self.get_raid_settings(guild_id)
        
        if existing is None:
            # Create new settings
            await self.conn.execute(
                """INSERT INTO raid_settings 
                (guild_id, enabled, join_threshold, join_window, account_age_min, 
                 lockdown_duration, alert_channel_id, auto_ban) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    int(enabled if enabled is not None else False),
                    join_threshold if join_threshold is not None else 5,
                    join_window if join_window is not None else 60,
                    account_age_min if account_age_min is not None else 0,
                    lockdown_duration if lockdown_duration is not None else 300,
                    alert_channel_id,
                    int(auto_ban if auto_ban is not None else False),
                ),
            )
        else:
            # Update existing settings
            updates = []
            values = []
            
            if enabled is not None:
                updates.append("enabled = ?")
                values.append(int(enabled))
            if join_threshold is not None:
                updates.append("join_threshold = ?")
                values.append(join_threshold)
            if join_window is not None:
                updates.append("join_window = ?")
                values.append(join_window)
            if account_age_min is not None:
                updates.append("account_age_min = ?")
                values.append(account_age_min)
            if lockdown_duration is not None:
                updates.append("lockdown_duration = ?")
                values.append(lockdown_duration)
            if alert_channel_id is not None:
                updates.append("alert_channel_id = ?")
                values.append(alert_channel_id)
            if auto_ban is not None:
                updates.append("auto_ban = ?")
                values.append(int(auto_ban))
            
            updates.append("updated_at = datetime('now')")
            values.append(guild_id)
            
            await self.conn.execute(
                f"UPDATE raid_settings SET {', '.join(updates)} WHERE guild_id = ?",
                values,
            )
        
        await self.conn.commit()
        return True

    async def track_join(self, guild_id: int, user_id: int, account_created: str | None = None):
        """Track a user joining for raid detection."""
        await self.conn.execute(
            "INSERT OR REPLACE INTO join_tracking (guild_id, user_id, account_created) VALUES (?, ?, ?)",
            (guild_id, user_id, account_created),
        )
        await self.conn.commit()

    async def get_recent_joins(self, guild_id: int, seconds: int):
        """Get joins in the last N seconds for raid detection."""
        cur = await self.conn.execute(
            """SELECT user_id, joined_at, account_created 
               FROM join_tracking 
               WHERE guild_id = ? AND joined_at > datetime('now', '-{} seconds') 
               ORDER BY joined_at DESC""".format(seconds),
            (guild_id,),
        )
        return await cur.fetchall()

    async def cleanup_old_joins(self, guild_id: int, hours: int = 24):
        """Clean up old join tracking data."""
        cur = await self.conn.execute(
            "DELETE FROM join_tracking WHERE guild_id = ? AND joined_at < datetime('now', '-{} hours')".format(hours),
            (guild_id,),
        )
        await self.conn.commit()
        return cur.rowcount

    async def create_raid_event(
        self,
        guild_id: int,
        join_count: int,
        window_seconds: int,
        actions_taken: list[str],
    ) -> int:
        """Create a raid event log entry."""
        import json
        cur = await self.conn.execute(
            """INSERT INTO raid_events 
            (guild_id, join_count, window_seconds, actions_taken) 
            VALUES (?, ?, ?, ?)""",
            (guild_id, join_count, window_seconds, json.dumps(actions_taken)),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_raid_events(self, guild_id: int, limit: int = 10):
        """Get recent raid events for a guild."""
        cur = await self.conn.execute(
            "SELECT * FROM raid_events WHERE guild_id = ? ORDER BY triggered_at DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def resolve_raid_event(self, guild_id: int, event_id: int, resolved_by: int):
        """Mark a raid event as resolved."""
        await self.conn.execute(
            "UPDATE raid_events SET resolved_at = datetime('now'), resolved_by = ? WHERE guild_id = ? AND id = ?",
            (resolved_by, guild_id, event_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Invite Tracking
    # ------------------------------------------------------------------

    async def track_invite_use(
        self,
        guild_id: int,
        invite_code: str,
        user_id: int,
        inviter_id: int,
        account_created: str | None = None,
    ) -> bool:
        """Track when a user joins via an invite."""
        try:
            await self.conn.execute(
                "INSERT INTO invite_uses (guild_id, invite_code, user_id, inviter_id, account_created) VALUES (?, ?, ?, ?, ?)",
                (guild_id, invite_code, user_id, inviter_id, account_created),
            )
            
            # Update invite usage count
            await self.conn.execute(
                "UPDATE invite_codes SET uses = uses + 1 WHERE guild_id = ? AND invite_code = ?",
                (guild_id, invite_code),
            )
            
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            # User already tracked for this guild
            return False

    async def update_invite_codes(self, guild_id: int, invites: list[discord.Invite]) -> None:
        """Update the invite codes table with current server invites."""
        for invite in invites:
            if not invite.code or not invite.inviter:
                continue

            await self.conn.execute(
                """INSERT OR REPLACE INTO invite_codes 
                (guild_id, invite_code, inviter_id, channel_id, uses, max_uses, temporary, expires_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    invite.code,
                    invite.inviter.id,
                    invite.channel.id if invite.channel else None,
                    invite.uses or 0,
                    invite.max_uses,
                    int(invite.temporary or False),
                    invite.expires_at.isoformat() if invite.expires_at else None,
                ),
            )
        
        await self.conn.commit()

    async def get_invite_stats(self, guild_id: int, inviter_id: int | None = None):
        """Get invite statistics for a guild or specific inviter."""
        if inviter_id:
            cur = await self.conn.execute(
                """SELECT inviter_id, COUNT(*) as total_invites, 
                   COUNT(CASE WHEN left_at IS NULL THEN 1 END) as active_invites,
                   MIN(used_at) as first_invite, MAX(used_at) as last_invite
                   FROM invite_uses 
                   WHERE guild_id = ? AND inviter_id = ?""",
                (guild_id, inviter_id),
            )
        else:
            cur = await self.conn.execute(
                """SELECT inviter_id, COUNT(*) as total_invites, 
                   COUNT(CASE WHEN left_at IS NULL THEN 1 END) as active_invites,
                   MIN(used_at) as first_invite, MAX(used_at) as last_invite
                   FROM invite_uses 
                   WHERE guild_id = ? 
                   GROUP BY inviter_id 
                   ORDER BY total_invites DESC""",
                (guild_id,),
            )
        return await cur.fetchall()

    async def get_invite_leaderboard(self, guild_id: int, limit: int = 10):
        """Get top inviters for a guild."""
        cur = await self.conn.execute(
            """SELECT inviter_id, COUNT(*) as total_invites,
                   COUNT(CASE WHEN left_at IS NULL THEN 1 END) as active_invites
                   FROM invite_uses 
                   WHERE guild_id = ? 
                   GROUP BY inviter_id 
                   ORDER BY total_invites DESC 
                   LIMIT ?""",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def get_user_invite_info(self, guild_id: int, user_id: int):
        """Get information about how a user joined."""
        cur = await self.conn.execute(
            """SELECT invite_code, inviter_id, used_at, account_created 
               FROM invite_uses 
               WHERE guild_id = ? AND user_id = ?""",
            (guild_id, user_id),
        )
        return await cur.fetchone()

    async def mark_user_left(self, guild_id: int, user_id: int) -> bool:
        """Mark a user as having left the server."""
        cur = await self.conn.execute(
            "UPDATE invite_uses SET left_at = datetime('now') WHERE guild_id = ? AND user_id = ? AND left_at IS NULL",
            (guild_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_recent_invites(self, guild_id: int, days: int = 7):
        """Get recent invite activity for a guild."""
        cur = await self.conn.execute(
            """SELECT invite_code, inviter_id, user_id, used_at, left_at
               FROM invite_uses 
               WHERE guild_id = ? AND used_at > datetime('now', '-{} days')
               ORDER BY used_at DESC""".format(days),
            (guild_id,),
        )
        return await cur.fetchall()

    async def cleanup_old_invite_data(self, guild_id: int, days: int = 90):
        """Clean up old invite tracking data."""
        cur = await self.conn.execute(
            "DELETE FROM invite_uses WHERE guild_id = ? AND used_at < datetime('now', '-{} days')".format(days),
            (guild_id,),
        )
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Birthday System
    # ------------------------------------------------------------------

    async def set_birthday(self, guild_id: int, user_id: int, birthday: str, timezone: str = "UTC") -> bool:
        """Set a user's birthday."""
        try:
            await self.conn.execute(
                "INSERT OR REPLACE INTO birthdays (guild_id, user_id, birthday, timezone) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, birthday, timezone),
            )
            await self.conn.commit()
            return True
        except Exception:
            return False

    async def get_birthday(self, guild_id: int, user_id: int):
        """Get a user's birthday."""
        cur = await self.conn.execute(
            "SELECT * FROM birthdays WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return await cur.fetchone()

    async def get_birthdays_by_date(self, guild_id: int, date: str):
        """Get all users with birthdays on a specific date (MM-DD format)."""
        cur = await self.conn.execute(
            "SELECT * FROM birthdays WHERE guild_id = ? AND birthday = ?",
            (guild_id, date),
        )
        return await cur.fetchall()

    async def remove_birthday(self, guild_id: int, user_id: int) -> bool:
        """Remove a user's birthday."""
        cur = await self.conn.execute(
            "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def record_birthday_announcement(self, guild_id: int, user_id: int, date: str) -> bool:
        """Record that a birthday announcement was sent for a user on a specific date."""
        try:
            await self.conn.execute(
                "INSERT INTO birthday_announcements (guild_id, user_id, date_sent) VALUES (?, ?, ?)",
                (guild_id, user_id, date),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # Already recorded

    async def check_birthday_announced(self, guild_id: int, user_id: int, date: str) -> bool:
        """Check if a birthday announcement was already sent for a user on a specific date."""
        cur = await self.conn.execute(
            "SELECT 1 FROM birthday_announcements WHERE guild_id = ? AND user_id = ? AND date_sent = ?",
            (guild_id, user_id, date),
        )
        return await cur.fetchone() is not None

    async def cleanup_old_birthday_announcements(self, guild_id: int, days: int = 7):
        """Clean up old birthday announcement records."""
        cur = await self.conn.execute(
            "DELETE FROM birthday_announcements WHERE guild_id = ? AND date_sent < date('now', '-{} days')".format(days),
            (guild_id,),
        )
        await self.conn.commit()
        return cur.rowcount
