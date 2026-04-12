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
    # Model discovery: use LiteLLM proxy routes (e.g. /v1/model/info) when the hostname does not contain "litellm".
    llm_litellm_proxy: bool = field(
        default_factory=lambda: os.getenv("LLM_LITELLM_PROXY", "").strip().lower() in ("1", "true", "yes", "on")
    )
    # Verbose LLM tracing to stdout (set on the remote host / container). Never logs secrets.
    llm_debug: bool = field(
        default_factory=lambda: os.getenv("LLM_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    )
    # Optional: forwarded to OpenAI-compatible APIs as extra_body["reasoning_effort"]
    # (e.g. low / medium / high). Empty = omit.
    llm_reasoning_effort: str | None = field(
        default_factory=lambda: (v := os.getenv("LLM_REASONING_EFFORT", "").strip().lower())
        and v
        or None
    )
    # If set, do not send extra_body chat_template_kwargs for Qwen3 (some proxies reject it).
    llm_skip_qwen_chat_template_kwargs: bool = field(
        default_factory=lambda: os.getenv("LLM_SKIP_QWEN_CHAT_TEMPLATE_KWARGS", "").strip().lower()
        in ("1", "true", "yes", "on")
    )

    # ── Bot-level system prompt (prepended to every guild's prompt) ──
    # Set this in .env to enforce a foundation that cannot be overridden
    # by per-guild settings.  Leave blank to use only the guild prompt.
    system_prompt: str = field(
        default_factory=lambda: os.getenv("SYSTEM_PROMPT", "")
    )

    # ── GitHub integration ───────────────────────────────────────────
    github_token: str | None = field(
        default_factory=lambda: os.getenv("GITHUB_TOKEN")
    )

    # ── GitLab integration ───────────────────────────────────────────
    gitlab_token: str | None = field(
        default_factory=lambda: os.getenv("GITLAB_TOKEN")
    )
    gitlab_url: str = field(
        default_factory=lambda: os.getenv("GITLAB_URL", "https://gitlab.com")
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
