"""Dashboard web application for the Discord bot.

Serves a web UI backed by FastAPI + Jinja2 templates.
Authentication is handled with Discord OAuth and per-guild access control.

Route modules live in dashboard/routes/.
Shared helpers (auth, DB, guild access) live in dashboard/helpers.py.
"""

from __future__ import annotations

# Load .env before anything reads os.getenv
from dotenv import load_dotenv
load_dotenv()

import os
import secrets
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from dashboard.dynamic_config_schema import DynamicConfigSchema
from dashboard.routes.github_integrations import GitHubIntegrationsModule
from dashboard.routes.gitlab_integrations import GitLabIntegrationsModule
from dashboard.routes import auth, overview, assistant, community, moderation, economy, misc, knowledge, voice_music, welcome as welcome_routes, polls as polls_routes
from dashboard.routes import config as config_routes
from dashboard.helpers import (
    DB_PATH,
    ctx,
    auth_redirect,
    get_authorized_guilds,
    get_guild_config_map,
    require_guild_access,
    db_fetchall,
    db_fetchone,
    db_execute,
)
from bot.config import Config
from bot.model_discovery import ModelDiscoveryService

logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Bot Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# ---------------------------------------------------------------------------
# Shared services
# ---------------------------------------------------------------------------

config = Config()
model_discovery = ModelDiscoveryService(config)
dynamic_schema = DynamicConfigSchema(model_discovery)

# ---------------------------------------------------------------------------
# Register route modules
# ---------------------------------------------------------------------------

app.include_router(auth.init(templates))
app.include_router(overview.init(templates))
app.include_router(config_routes.init(templates, dynamic_schema))
app.include_router(assistant.init(templates, config))
app.include_router(community.init(templates))
app.include_router(moderation.init(templates))
app.include_router(economy.init(templates))
app.include_router(polls_routes.init(templates))
app.include_router(misc.init(templates))
app.include_router(voice_music.init(templates))
app.include_router(knowledge.init(templates))
app.include_router(welcome_routes.init(templates))

# ---------------------------------------------------------------------------
# Integrations (pre-existing APIRouter-based modules)
# ---------------------------------------------------------------------------

github_integrations = GitHubIntegrationsModule(
    templates=templates,
    ctx=ctx,
    auth_redirect=auth_redirect,
    get_authorized_guilds=get_authorized_guilds,
    get_guild_config_map=get_guild_config_map,
    require_guild_access=require_guild_access,
    db_fetchall=db_fetchall,
    db_fetchone=db_fetchone,
    db_execute=db_execute,
    github_token=config.github_token,
    github_token_configured=bool(config.github_token),
)
app.include_router(github_integrations.router)

gitlab_integrations = GitLabIntegrationsModule(
    templates=templates,
    ctx=ctx,
    auth_redirect=auth_redirect,
    get_authorized_guilds=get_authorized_guilds,
    get_guild_config_map=get_guild_config_map,
    require_guild_access=require_guild_access,
    db_fetchall=db_fetchall,
    db_fetchone=db_fetchone,
    db_execute=db_execute,
    gitlab_token=config.gitlab_token,
    gitlab_token_configured=bool(config.gitlab_token),
    gitlab_url=config.gitlab_url,
)
app.include_router(gitlab_integrations.router)

# Backward-compatible aliases for tests and existing imports.
integrations_page = github_integrations.integrations_page
integrations_github_save = github_integrations.integrations_github_save
integrations_github_delete = github_integrations.integrations_github_delete
integrations_github_reset_state = github_integrations.integrations_github_reset_state
integrations_github_workflow_save = github_integrations.integrations_github_workflow_save
integrations_github_user_link_save = github_integrations.integrations_github_user_link_save
integrations_github_user_link_delete = github_integrations.integrations_github_user_link_delete
