import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()



@dataclass(frozen=True)
class Config:
    """Application configuration loaded from environment variables.

    **Only connection credentials live here** — everything that can be
    configured per-guild at runtime is stored in the database
    (``guild_config`` table) and managed via slash commands.
    See ``DEFAULTS`` below for the initial fallback values used when a
    guild has no DB override yet.
    """

    # ── Connection credentials (env-only, never in DB) ──────────────
    discord_token: str = field(
        default_factory=lambda: os.environ["DISCORD_BOT_TOKEN"]
    )
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    )
    llm_api_key: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", "no-key-needed")
    )

    # ── GitHub integration ───────────────────────────────────────────
    github_token: str | None = field(
        default_factory=lambda: os.getenv("GITHUB_TOKEN")
    )


# ── Hard-coded defaults for guild-level settings ────────────────────
# Used as fallback when a guild has no DB override.  Admins can change
# these at runtime via /assistant, /modset, /automodset, /econset, etc.
DEFAULTS: dict[str, str] = {
    # LLM behaviour
    "assistant_model":           "gpt-3.5-turbo",
    "assistant_prompt":          (
        "You are a helpful support assistant. Answer questions clearly and concisely. "
        "If you don't know the answer, say so honestly."
    ),
    "assistant_temperature":     "0.7",
    "assistant_max_tokens":      "1024",
    "assistant_max_retention":   "40",   # max_history_turns * 2
    "assistant_embedding_model": "text-embedding-3-small",
    "assistant_image_model":     "dall-e-3",
    # Moderation
    "mod_mute_duration_minutes":     "10",
    "mod_max_warnings_before_action": "3",
    "mod_warning_action":            "mute",
    # Auto-mod
    "automod_spam_threshold":   "5",
    "automod_spam_interval":    "5",
}
