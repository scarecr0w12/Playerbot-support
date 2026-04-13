---
trigger: glob
glob: "bot/db/**/*.py,bot/database.py,bot/llm_service.py,bot/qdrant_service.py,bot/mcp_manager.py,bot/github_client.py,bot/model_discovery.py"
---

# DB and services

- **Schema:** `bot/database.py` owns table creation/migrations used at startup; keep new tables and columns backward-compatible when possible.
- **Access pattern:** Prefer small modules in `bot/db/` for domain queries; avoid raw SQL scattered in cogs when a `bot/db` helper already exists or should exist.
- **Transactions:** Use the shared async connection patterns already in `Database` / helpers; commit after batched writes.
- **LLM:** `LLMService` is OpenAI-compatible (`LLM_BASE_URL`, `LLM_API_KEY`, model names). Respect token limits, streaming vs non-streaming usage, and existing token-usage tracking.
- **Vectors:** `QdrantService` is per-guild collections; keep metadata in SQLite in sync with Qdrant point IDs (`qdrant_id`) as in existing embedding flows.
- **MCP:** `MCPManager` lifecycle is tied to `main.py` (`shutdown` in `finally`); guild connections are seeded in `on_ready` — do not leak connections on reconnect without reviewing that flow.

## Windsurf-specific

- When adding new DB columns, use `code_search` to find all `CREATE TABLE` and `ALTER TABLE` statements in `bot/database.py` before editing so column ordering and naming stays consistent.
- If a schema migration is needed, note a migration strategy explicitly — see `migrate_to_qdrant.py` as a pattern reference.
