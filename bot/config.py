import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(key: str, default: str) -> int:
    return int(os.getenv(key, default))


@dataclass(frozen=True)
class Config:
    """Application configuration loaded from environment variables.

    Connection credentials (Discord token, LLM endpoint) live here.
    LLM *behaviour* settings (model, prompt, temperature, …) are stored
    in the database per-guild and managed via ``/assistant`` commands.
    The ``default_*`` fields below are only used as initial fallbacks
    when a guild has no override in the DB yet.
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

    # ── LLM behaviour defaults (seed values for guild DB) ───────────
    default_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "gpt-3.5-turbo")
    )
    default_system_prompt: str = field(
        default_factory=lambda: os.getenv(
            "SYSTEM_PROMPT",
            "You are a helpful support assistant. Answer questions clearly and concisely. "
            "If you don't know the answer, say so honestly.",
        )
    )
    default_max_history_turns: int = field(
        default_factory=lambda: _int("MAX_HISTORY_TURNS", "20")
    )
    default_temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.7"))
    )
    default_max_tokens: int = field(
        default_factory=lambda: _int("LLM_MAX_TOKENS", "1024")
    )
    default_embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    )
    default_image_model: str = field(
        default_factory=lambda: os.getenv("IMAGE_MODEL", "dall-e-3")
    )

    # ── Moderation defaults ─────────────────────────────────────────
    default_mute_duration_minutes: int = field(
        default_factory=lambda: _int("DEFAULT_MUTE_DURATION_MINUTES", "10")
    )
    max_warnings_before_action: int = field(
        default_factory=lambda: _int("MAX_WARNINGS_BEFORE_ACTION", "3")
    )
    warning_action: str = field(
        default_factory=lambda: os.getenv("WARNING_ACTION", "mute")
    )

    # ── Auto-mod ────────────────────────────────────────────────────
    automod_spam_threshold: int = field(
        default_factory=lambda: _int("AUTOMOD_SPAM_THRESHOLD", "5")
    )
    automod_spam_interval: int = field(
        default_factory=lambda: _int("AUTOMOD_SPAM_INTERVAL", "5")
    )
