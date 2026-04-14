"""Expose the running Discord bot to the dashboard (separate asyncio loop / thread).

Set via ``set_discord_bot`` from ``main.py`` before the dashboard thread starts so
dashboard routes can schedule coroutines on the bot loop (e.g. registering poll views).
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

_bot: Any = None

T = TypeVar("T")


def set_discord_bot(bot: Any | None) -> None:
    global _bot
    _bot = bot


def get_discord_bot() -> Any | None:
    return _bot


def run_coroutine_on_bot_loop(coro: Coroutine[Any, Any, T], *, timeout: float = 25.0) -> T:
    """Run *coro* on the bot's loop and wait for the result (call from the dashboard thread)."""
    if _bot is None:
        raise RuntimeError("Discord bot is not registered with the dashboard bridge")
    loop = getattr(_bot, "loop", None)
    if loop is None or not loop.is_running():
        raise RuntimeError("Discord bot event loop is not available")
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)
