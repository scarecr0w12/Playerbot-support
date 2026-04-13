---
trigger: glob
glob: "bot/cogs/**/*.py"
---

# Discord cogs

- Use **`discord.app_commands`** for slash commands; keep command names and descriptions consistent with existing cogs (see `moderation.py`, `tickets.py` for patterns).
- **Defer** interactions early when work may exceed ~2–3 seconds (DB, HTTP, LLM).
- Access shared state via cog `__init__` (e.g. `Database`, `Config`); **inject** services (`LLMService`, `QdrantService`, `MCPManager`) only where already established (e.g. `SupportCog`).
- **Mod cases / audit:** Moderation-style actions should record cases and notify **ModLogging** where the rest of the codebase does — follow existing helpers and cog references (`ModLoggingCog` type hints).
- **Persistent UI:** Ticket panels, rules, giveaways, etc. use views/buttons that must survive restarts — match existing registration patterns (`bot.add_view` / persistent IDs) when touching those flows.
- **Permissions:** Custom deny/allow is enforced globally; failing checks should stay **silent + ephemeral** only where the Permissions cog already does — do not bypass `interaction_check`.

## Windsurf-specific

- When generating or editing a cog, open a sibling cog (`moderation.py` or `tickets.py`) via `read_file` first to verify import style and `__init__` signature before writing.
- After adding new slash commands, remind the user that `python main.py` is required to sync the command tree with Discord.
