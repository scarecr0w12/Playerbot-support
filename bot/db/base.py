"""Database connection, setup, and incremental migrations."""

from __future__ import annotations

import logging
import os

import aiosqlite

from .schema import SCHEMA

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "bot.db")


class BaseDatabase:
    """Manages the aiosqlite connection lifecycle and schema migrations."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def setup(self) -> None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._db = await aiosqlite.connect(DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()
        logger.info("Database ready at %s", DB_PATH)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not initialised – call setup() first"
        return self._db

    async def _migrate(self) -> None:
        """Apply incremental schema migrations for pre-existing databases."""
        cur = await self._db.execute("PRAGMA table_info(embeddings)")  # type: ignore[union-attr]
        cols = {row[1] for row in await cur.fetchall()}
        if "source_url" not in cols:
            await self._db.execute("ALTER TABLE embeddings ADD COLUMN source_url TEXT")  # type: ignore[union-attr]
            await self._db.commit()
            logger.info("Migration: added source_url to embeddings")

        await self._db.executescript(  # type: ignore[union-attr]
            """
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
            CREATE INDEX IF NOT EXISTS idx_learned_message_marks_guild
                ON learned_message_marks (guild_id, channel_id);
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
        )

        for col in ("embedding", "model", "qdrant_id"):
            try:
                await self._db.execute(  # type: ignore[union-attr]
                    f"ALTER TABLE learned_facts ADD COLUMN {col} {'BLOB' if col == 'embedding' else 'TEXT'}"
                )
            except Exception:
                pass

        try:
            await self._db.execute(  # type: ignore[union-attr]
                "ALTER TABLE embeddings ADD COLUMN qdrant_id TEXT"
            )
        except Exception:
            pass

        cur = await self._db.execute(  # type: ignore[union-attr]
            "SELECT name FROM sqlite_master WHERE type='table' AND name='assistant_triggers'"
        )
        if await cur.fetchone():
            cur = await self._db.execute("PRAGMA table_info(assistant_triggers)")  # type: ignore[union-attr]
            trig_cols = {row[1] for row in await cur.fetchall()}
            if trig_cols and "id" not in trig_cols:
                await self._db.executescript(  # type: ignore[union-attr]
                    """
                    CREATE TABLE assistant_triggers_new (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id    INTEGER NOT NULL,
                        pattern     TEXT    NOT NULL,
                        UNIQUE(guild_id, pattern)
                    );
                    INSERT INTO assistant_triggers_new (guild_id, pattern)
                        SELECT guild_id, pattern FROM assistant_triggers;
                    DROP TABLE assistant_triggers;
                    ALTER TABLE assistant_triggers_new RENAME TO assistant_triggers;
                    CREATE INDEX IF NOT EXISTS idx_triggers_guild ON assistant_triggers (guild_id);
                    """
                )
                logger.info("Migration: rebuilt assistant_triggers with id column")

        cur = await self._db.execute("PRAGMA table_info(giveaways)")  # type: ignore[union-attr]
        giveaway_cols = {row[1] for row in await cur.fetchall()}
        for col, defn in [
            ("message_id", "INTEGER"),
            ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
        ]:
            if col not in giveaway_cols:
                await self._db.execute(  # type: ignore[union-attr]
                    f"ALTER TABLE giveaways ADD COLUMN {col} {defn}"
                )
                logger.info("Migration: added %s to giveaways", col)

        await self._db.commit()  # type: ignore[union-attr]
