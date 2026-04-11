"""Centralised SQL schema for all tables.

Each domain section is labelled so it is easy to find and extend.
"""

SCHEMA = """
-- ── Guild config ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id    INTEGER NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT,
    PRIMARY KEY (guild_id, key)
);

-- ── Moderation ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mod_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    reason      TEXT,
    duration    INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cases_guild ON mod_cases (guild_id);
CREATE INDEX IF NOT EXISTS idx_cases_user  ON mod_cases (guild_id, user_id);

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

CREATE TABLE IF NOT EXISTS case_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    note        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON case_notes (guild_id, user_id);

-- ── Tickets ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER UNIQUE,
    user_id     INTEGER NOT NULL,
    subject     TEXT,
    status      TEXT    NOT NULL DEFAULT 'open',
    claimed_by  INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets (guild_id, status);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    user_id     INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Auto-mod ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS automod_filters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    filter_type TEXT    NOT NULL,
    pattern     TEXT    NOT NULL,
    UNIQUE(guild_id, filter_type, pattern)
);
CREATE INDEX IF NOT EXISTS idx_automod_guild ON automod_filters (guild_id, filter_type);

-- ── Support / AI ─────────────────────────────────────────────────────
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

CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   BLOB,
    model       TEXT,
    source_url  TEXT,
    qdrant_id   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_embed_guild  ON embeddings (guild_id);
CREATE INDEX IF NOT EXISTS idx_embed_source ON embeddings (guild_id, source_url);

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

CREATE TABLE IF NOT EXISTS custom_functions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL,
    parameters  TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_funcs_guild ON custom_functions (guild_id);

CREATE TABLE IF NOT EXISTS token_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_guild ON token_usage (guild_id);

CREATE TABLE IF NOT EXISTS assistant_triggers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    pattern     TEXT    NOT NULL,
    UNIQUE(guild_id, pattern)
);
CREATE INDEX IF NOT EXISTS idx_triggers_guild ON assistant_triggers (guild_id);

CREATE TABLE IF NOT EXISTS learned_facts (
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
);
CREATE INDEX IF NOT EXISTS idx_facts_guild ON learned_facts (guild_id, approved);

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

CREATE TABLE IF NOT EXISTS response_feedback (
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
);
CREATE INDEX IF NOT EXISTS idx_feedback_guild ON response_feedback (guild_id);

-- ── Economy ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS economy_accounts (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,
    last_payday TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- ── Custom commands ───────────────────────────────────────────────────
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

-- ── Reports ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    reporter_id     INTEGER NOT NULL,
    reported_user_id INTEGER NOT NULL,
    reason          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'open',
    resolved_by     INTEGER,
    resolution_note TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_guild ON reports (guild_id, status);

-- ── Community ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS selfroles (
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS levels (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    xp              INTEGER NOT NULL DEFAULT 0,
    level           INTEGER NOT NULL DEFAULT 0,
    last_xp_at      TEXT,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_levels_guild ON levels (guild_id, xp DESC);

CREATE TABLE IF NOT EXISTS giveaways (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    prize       TEXT    NOT NULL,
    end_time    TEXT    NOT NULL,
    winner_count INTEGER NOT NULL DEFAULT 1,
    host_id     INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_giveaways_guild ON giveaways (guild_id, status);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    entered_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (giveaway_id, user_id)
);

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

CREATE TABLE IF NOT EXISTS highlights (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    keyword     TEXT    NOT NULL,
    PRIMARY KEY (user_id, guild_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_highlights_guild ON highlights (guild_id);

-- ── Permissions ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS command_permissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    command     TEXT    NOT NULL,
    target_type TEXT    NOT NULL,
    target_id   INTEGER NOT NULL,
    allowed     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(guild_id, command, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_cmdperm_guild ON command_permissions (guild_id, command);

-- ── Integrations ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS github_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    repo            TEXT    NOT NULL,
    events          TEXT    NOT NULL DEFAULT 'push,pull_request,issues,release',
    added_by        INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, channel_id, repo)
);
CREATE INDEX IF NOT EXISTS idx_gh_subs_guild ON github_subscriptions (guild_id);

CREATE TABLE IF NOT EXISTS github_poll_state (
    repo        TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    last_id     TEXT,
    etag        TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (repo, event_type)
);

CREATE TABLE IF NOT EXISTS gitlab_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    project         TEXT    NOT NULL,
    events          TEXT    NOT NULL DEFAULT 'push,merge_request,issues,release',
    added_by        INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, channel_id, project)
);
CREATE INDEX IF NOT EXISTS idx_gl_subs_guild ON gitlab_subscriptions (guild_id);

CREATE TABLE IF NOT EXISTS gitlab_poll_state (
    project     TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    last_id     TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project, event_type)
);

-- ── MCP ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mcp_servers (
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
);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_guild ON mcp_servers (guild_id);
"""
