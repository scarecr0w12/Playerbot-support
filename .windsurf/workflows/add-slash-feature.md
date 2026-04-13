---
description: Add or change a Discord slash command, modal, button, or cog in Playerbot-support
---

# Add slash / cog feature

## 1. Explore

Use `code_search` to locate the relevant cog. If none exists, identify the best placement from the cog list in `main.py`.

// turbo
Read a reference cog with similar UX:
```
read_file bot/cogs/tickets.py   # for panel/view/button patterns
read_file bot/cogs/moderation.py  # for staff-action / mod-log patterns
```

## 2. Check guild config

Does the feature need stored guild settings?
- If yes: find or create a helper in `bot/db/` following existing domain modules.
- Keys must be added to `bot/database.py` schema and be backwards-compatible.

## 3. Implement

- Use `app_commands` on the cog `Group` or `Cog` as elsewhere.
- **Defer** if the handler does I/O beyond quick DB reads (`await interaction.response.defer()`).
- **Permissions:** Rely on Discord defaults + the global `interaction_check`; do not duplicate deny logic unless the cog already does for special cases.
- **Mod / audit:** For staff actions, log via `ModLogging` and persist a case where moderation-style tracking applies.
- **Persistent views:** For buttons/menus that must survive restarts, use `custom_id` and `bot.add_view` following sibling features.

## 4. Register

If a new cog file was created, add it to the load list in `main.py` after the two always-first cogs (`ModLoggingCog`, `PermissionsCog`).

## 5. Test & sync

// turbo
Run the test suite to confirm no regressions:
```
pytest tests/ -x -q
```

Remind the user: `python main.py` is required to sync the updated command tree with Discord (no hot-reload).

## Do not

- Change cog **load order** in `main.py` without explicit reason.
- Store secrets in code or `guild_config` values.
