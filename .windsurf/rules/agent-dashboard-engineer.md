---
trigger: agent_requested
description: >-
  Implements or audits the FastAPI dashboard: OAuth/session flow, guild access
  control, route modules, Jinja templates, and config schema. Use when editing
  dashboard/ or Discord OAuth-related env vars.
---

# Dashboard engineer agent

## Security first

- **Guild access:** Every sensitive route must use patterns from `dashboard/helpers.py` (`require_guild_access`, authorized guild lists). Owner override via `BOT_OWNER_DISCORD_ID` must remain intentional, not broadened by accident.
- **Sessions:** `SessionMiddleware` in `app.py` — any change to cookies, secret, or max_age affects all users.
- **No secrets in HTML/JSON** — scrub templates and API responses before every commit.

## Workflow

1. `read_file dashboard/app.py` to check existing router mounts and middleware order.
2. Add a new `dashboard/routes/<area>.py` module with `init(templates, deps…)` returning an `APIRouter`, then wire in `app.py`.
3. Reuse `dashboard/helpers.py` for session-backed requests, guild authorization, and DB helpers.
4. Dynamic config: align with `DynamicConfigSchema` / `config_definitions.py` when exposing new guild settings.

## Deliverables

- Code changes described file-by-file.
- **Regression checklist:** login, guild switch, save settings, logout, unauthorized guild URL.
- Always remind that the dashboard **does not hot-reload** — full `python main.py` restart required.

## Tools to use

- `code_search` to find existing patterns before duplicating auth/helper logic.
- `read_file` on the closest existing route module as a template.
