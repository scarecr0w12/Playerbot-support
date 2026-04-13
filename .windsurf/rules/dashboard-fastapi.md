---
trigger: glob
glob: "dashboard/**/*.py,dashboard/**/*.html,dashboard/static/**"
---

# Dashboard (FastAPI)

- **Composition:** `dashboard/app.py` mounts static files, templates, and **`dashboard/routes/*.py`** routers. New pages: add a route module (or extend one), expose `init(templates, …)` returning an `APIRouter`, then `include_router` in `app.py`.
- **Context helpers:** Use `dashboard/helpers.py` (`require_guild_access`, `db_fetchone`, `ctx`, etc.) instead of duplicating DB or session logic.
- **Auth:** Discord OAuth and guild access rules are security-sensitive; changing `auth.py` or session settings requires checking callback URLs, `BOT_OWNER_DISCORD_ID`, and `Manage Server` assumptions documented in `README.md`.
- **Config:** Dynamic guild config may go through `DynamicConfigSchema` / `config_definitions.py` — align new fields with both dashboard forms and bot-side readers.
- **Templates & static:** Live under `dashboard/templates/` and `dashboard/static/`; keep JS/CSS consistent with existing dashboard patterns.

## Security

- Every guild-scoped handler must enforce **`require_guild_access`** (or equivalent) before reads/writes.
- Never expose raw `SESSION_SECRET`, bot tokens, or API keys to templates or JSON responses.
- Scrub all HTML `<script>` contexts for accidental secret interpolation.

## Windsurf-specific

- After any dashboard change, remind the user to restart `python main.py` — the dashboard thread does **not** hot-reload.
- Use `read_file` on `dashboard/app.py` first to check existing router mounts before adding a new route module.
- Regression checklist to include in responses: login, guild switch, save settings, logout, unauthorized guild URL.
