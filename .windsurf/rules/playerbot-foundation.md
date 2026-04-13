---
trigger: always_on
---

# Playerbot-support — foundation

- **Stack:** Python 3.10+, `discord.py` 2.x, `aiosqlite`, FastAPI + Jinja2 + uvicorn, OpenAI-compatible SDK, Qdrant, optional MCP (`mcp` package).
- **Entry:** `main.py` wires `Database` → `LLMService` / `QdrantService` / `MCPManager`, loads cogs, starts the dashboard in a **daemon thread** (no hot-reload). Restart after dashboard changes.
- **Cog order:** `ModLoggingCog` and `PermissionsCog` load **first**; a **global** `bot.tree.interaction_check` delegates to Permissions. Do not reorder without auditing permission and logging side effects.
- **Commands:** Slash-first; `!` is for **custom commands** (`CustomCommandsCog`) only unless explicitly extending prefix behavior.
- **Data:** Guild settings and metadata live in SQLite (`Database`, `bot/database.py` schema); vectors live in **Qdrant** per guild (`embeddings_*`, `facts_*`). Do not store embeddings only in SQLite for new features.
- **Secrets:** Never commit tokens; use `.env` / `Config` (`bot/config.py`). Dashboard uses `SessionMiddleware` and Discord OAuth — preserve session and redirect URL assumptions when changing auth.

## Windsurf-specific guidance

- Use `code_search` before editing to locate the relevant file rather than guessing paths.
- Prefer `multi_edit` for coordinated changes across multiple locations in a single file.
- After any change to `bot/` that requires a Discord command sync, note it explicitly — `run_command` with `python main.py` is required (not hot-reloadable).
- Dashboard restarts are always required; never assume changes take effect without a process restart.
