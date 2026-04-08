"""Enhanced LLM service with function calling, embeddings, image generation,
conversation compaction, and token tracking.
"""

from __future__ import annotations

import json
import logging
import math
import struct
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from bot.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _pack_embedding(vec: list[float]) -> bytes:
    """Serialise a float list to compact bytes."""
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(data: bytes) -> list[float]:
    """Deserialise bytes back to float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Built-in tool definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

BUILTIN_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current date and time in UTC or a specified timezone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone name, e.g. 'US/Eastern'. Defaults to UTC.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_embed",
            "description": "Create a rich embed to display structured information. Returns JSON that the bot will render.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Embed title"},
                    "description": {"type": "string", "description": "Embed body text (supports Markdown)"},
                    "color": {"type": "string", "description": "Hex color code like #FF5733"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                                "inline": {"type": "boolean"},
                            },
                            "required": ["name", "value"],
                        },
                        "description": "Optional list of fields",
                    },
                },
                "required": ["title", "description"],
            },
        },
    },
]


def _execute_builtin_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a built-in tool and return a string result."""
    if name == "get_time":
        from datetime import datetime, timezone as tz
        try:
            import zoneinfo
            zone = zoneinfo.ZoneInfo(arguments.get("timezone", "UTC"))
        except Exception:
            zone = tz.utc
        now = datetime.now(zone)
        return now.strftime("%A, %B %d, %Y at %I:%M %p %Z")

    if name == "create_embed":
        # Return JSON that the cog will parse and render as a Discord embed
        return json.dumps(arguments)

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class LLMService:
    """Async wrapper around any OpenAI-compatible chat/embedding/image endpoint.

    This service is **stateless** with respect to model, prompt, temperature,
    etc.  All behaviour settings are passed in per-call by the cog, which
    reads them from the guild DB (with env fallbacks via ``Config``).
    """

    def __init__(self, config: Config) -> None:
        self._client = AsyncOpenAI(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
        )

    # ------------------------------------------------------------------
    # Core chat completion (with function calling)
    # ------------------------------------------------------------------

    async def get_response(
        self,
        conversation_history: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
        max_tool_rounds: int = 5,
    ) -> dict[str, Any]:
        """Send a chat completion request and handle tool calls.

        All behaviour parameters are **required** — the caller (cog) resolves
        them from the guild DB with env-level fallbacks.

        Returns
        -------
        dict with keys:
            content  - The final assistant text reply.
            embeds   - List of embed dicts to render (from create_embed tool).
            usage    - {"prompt_tokens": int, "completion_tokens": int}
        """
        prompt = system_prompt
        mdl = model
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            *conversation_history,
        ]

        all_tools = list(BUILTIN_TOOLS)
        if tools:
            all_tools.extend(tools)

        total_prompt_tokens = 0
        total_completion_tokens = 0
        embeds: list[dict] = []

        for _ in range(max_tool_rounds):
            kwargs: dict[str, Any] = {
                "model": mdl,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if all_tools:
                kwargs["tools"] = all_tools
                kwargs["tool_choice"] = "auto"

            try:
                response = await self._client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
            except Exception:
                logger.exception("LLM request failed")
                return {
                    "content": "⚠️ Something went wrong while contacting the language model.",
                    "embeds": [],
                    "usage": {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens},
                }

            choice = response.choices[0]
            usage = response.usage
            if usage:
                total_prompt_tokens += usage.prompt_tokens
                total_completion_tokens += usage.completion_tokens

            # No tool calls — return final text
            if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                content = choice.message.content or ""
                return {
                    "content": content.strip(),
                    "embeds": embeds,
                    "usage": {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens},
                }

            # Process tool calls
            messages.append(choice.message.model_dump())  # type: ignore[arg-type]
            has_create_embed = False
            for tc in choice.message.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                result = _execute_builtin_tool(fn_name, fn_args)

                # Collect embeds from create_embed calls
                if fn_name == "create_embed":
                    has_create_embed = True
                    try:
                        embeds.append(json.loads(result))
                    except json.JSONDecodeError:
                        pass

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # create_embed is a terminal tool — return immediately with collected embeds
            # rather than looping back to the LLM (which causes duplicate embed spam)
            if has_create_embed:
                return {
                    "content": "",
                    "embeds": embeds,
                    "usage": {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens},
                }

        # Exhausted tool rounds
        return {
            "content": "I ran out of steps while processing tool calls.",
            "embeds": embeds,
            "usage": {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens},
        }

    # ------------------------------------------------------------------
    # Conversation compaction
    # ------------------------------------------------------------------

    async def compact_conversation(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        focus: str | None = None,
    ) -> str:
        """Summarise a conversation into a compact system-level summary."""
        mdl = model
        instruction = (
            "Summarize the following conversation concisely, preserving all key facts, "
            "decisions, code snippets, and context. Output ONLY the summary."
        )
        if focus:
            instruction += f" Focus especially on: {focus}"

        formatted = "\n".join(f"[{m['role']}]: {m['content']}" for m in messages)
        try:
            response = await self._client.chat.completions.create(
                model=mdl,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": formatted},
                ],
                temperature=0.3,
                max_tokens=1024,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception:
            logger.exception("Compaction failed")
            return ""

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    async def create_embedding(
        self, text: str, *, model: str = "text-embedding-3-small"
    ) -> tuple[list[float], bytes]:
        """Create an embedding vector for the given text.

        Returns (float_list, packed_bytes).
        """
        response = await self._client.embeddings.create(
            model=model,
            input=text,
        )
        vec = response.data[0].embedding
        return vec, _pack_embedding(vec)

    @staticmethod
    def unpack_embedding(data: bytes) -> list[float]:
        return _unpack_embedding(data)

    @staticmethod
    def similarity(a: list[float], b: list[float]) -> float:
        return cosine_similarity(a, b)

    # ------------------------------------------------------------------
    # Image generation (DALL-E)
    # ------------------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str = "dall-e-3",
        size: str = "1024x1024",
        quality: str = "standard",
        style: str = "vivid",
    ) -> str | None:
        """Generate an image and return its URL, or None on failure."""
        try:
            response = await self._client.images.generate(
                model=model,
                prompt=prompt,
                size=size,        # type: ignore[arg-type]
                quality=quality,  # type: ignore[arg-type]
                style=style,      # type: ignore[arg-type]
                n=1,
            )
            return response.data[0].url
        except Exception:
            logger.exception("Image generation failed")
            return None

    # ------------------------------------------------------------------
    # Channel / TLDR summarisation
    # ------------------------------------------------------------------

    async def summarise_messages(
        self,
        messages_text: str,
        *,
        model: str,
        question: str | None = None,
    ) -> str:
        """Summarise a block of channel messages."""
        mdl = model
        instruction = "Summarize the following Discord channel messages concisely."
        if question:
            instruction += f" The user specifically wants to know: {question}"

        try:
            response = await self._client.chat.completions.create(
                model=mdl,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": messages_text},
                ],
                temperature=0.3,
                max_tokens=1024,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception:
            logger.exception("Summarisation failed")
            return "⚠️ Failed to generate summary."
