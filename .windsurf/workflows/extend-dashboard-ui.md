---
description: Add or change a FastAPI dashboard route, Jinja template, or static asset in Playerbot-support
---

# Extend dashboard UI

## 1. Read app structure

// turbo
```
read_file dashboard/app.py            # router mounts, middleware, services passed in
read_file dashboard/helpers.py        # require_guild_access, db helpers, session ctx
```

## 2. Identify the right module

- Adding a **new page / area:** Create `dashboard/routes/<area>.py` with `init(templates, …)` → `APIRouter`.
- Extending an **existing page:** Identify the relevant `dashboard/routes/*.py` and extend it.

// turbo
Read the closest existing route module as a structural template:
```
read_file dashboard/routes/settings.py
```

## 3. Implement

### Route module

- All guild-scoped handlers must call `require_guild_access` (or equivalent) before any read/write.
- Never expose `SESSION_SECRET`, bot tokens, or API keys in template context or JSON.
- For new dynamic config fields: align with `DynamicConfigSchema` and `config_definitions.py`.

### Template

- Place under `dashboard/templates/`; extend `base.html` and follow existing block/include structure.
- Static JS/CSS under `dashboard/static/`; match existing naming conventions.

### Wire up

- Add `include_router(init(templates, …))` in `dashboard/app.py` after existing routers.

## 4. Test

// turbo
Run auth and knowledge tests (closest coverage to session + guild flow):
```
pytest tests/test_dashboard_auth.py tests/test_dashboard_knowledge.py -v
```

Regression checklist:
- [ ] Login and OAuth callback
- [ ] Guild switch
- [ ] Save settings
- [ ] Logout
- [ ] Unauthorized guild URL returns 403 / redirect

## 5. Restart

Remind the user: `python main.py` required — the dashboard thread does **not** hot-reload.
