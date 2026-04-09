"""Enhanced LLM service with function calling, embeddings, image generation,
conversation compaction, and token tracking.
"""

from __future__ import annotations

import json
import logging
import math
import re
import struct
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from bot.config import Config

logger = logging.getLogger(__name__)


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                if item.get("type") == "text" and isinstance(item.get("value"), str):
                    parts.append(item["value"])
        return "\n".join(part.strip() for part in parts if part and part.strip())
    return str(content)

_FACT_ALLOWED_CATEGORIES = {
    "user_preference",
    "user_identity",
    "community_info",
    "topic_fact",
    "policy",
}
_FACT_CATEGORY_ALIASES = {
    "preference": "user_preference",
    "identity": "user_identity",
    "server_info": "community_info",
    "server_information": "community_info",
    "community": "community_info",
    "objective_fact": "topic_fact",
    "fact": "topic_fact",
    "rule": "policy",
}
_FACT_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "been",
    "being",
    "between",
    "could",
    "does",
    "from",
    "have",
    "into",
    "just",
    "more",
    "over",
    "said",
    "says",
    "should",
    "some",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "very",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
    "your",
}
_FACT_META_SUBJECT_RE = re.compile(
    r"^(?:the\s+)?(?:assistant|bot|user|conversation|chat|message|reply|response|question|answer)\b",
    re.IGNORECASE,
)
_FACT_META_VERB_RE = re.compile(
    r"\b(?:asked|replied|responded|said|told|explained|mentioned|wrote|shared|formatted|summarized)\b",
    re.IGNORECASE,
)
_FACT_UNRESOLVED_PRONOUN_RE = re.compile(
    r"^(?:i|i'm|i am|i've|i'd|my|me|we|we're|we are|our|ours|you|your|yours)\b",
    re.IGNORECASE,
)
_FACT_HEDGING_RE = re.compile(
    r"\b(?:maybe|might|probably|possibly|perhaps|seems?|appears?|likely|i think|i believe|i guess)\b",
    re.IGNORECASE,
)
_FACT_PREFIX_RE = re.compile(r"^(?:[-*•]|\d+[.)]|fact:|answer:|remember:)\s*", re.IGNORECASE)
_FACT_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_fact_text(text: str) -> str:
    text = _FACT_PREFIX_RE.sub("", text.strip())
    text = _FACT_WHITESPACE_RE.sub(" ", text)
    return text.strip(" \t\n\r\"'")


def _significant_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]{4,}", text.lower())
        if token not in _FACT_STOPWORDS
    ]


def _has_grounding_overlap(fact: str, source_text: str) -> bool:
    fact_tokens = set(_significant_tokens(fact))
    if not fact_tokens:
        return False
    source_tokens = set(_significant_tokens(source_text))
    if not source_tokens:
        return False
    overlap = fact_tokens & source_tokens
    required = 1 if len(fact_tokens) == 1 else 2
    return len(overlap) >= min(required, len(fact_tokens))

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
        self._tool_support_by_model: dict[str, bool] = {}

    @staticmethod
    def _normalize_fact_category(category: Any) -> str:
        raw = str(category or "").strip().lower().replace("-", "_").replace(" ", "_")
        return _FACT_CATEGORY_ALIASES.get(raw, raw)

    @classmethod
    def fact_rejection_reason(
        cls,
        fact: str,
        *,
        source_text: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
        should_store: bool | None = None,
    ) -> str | None:
        normalized = _normalize_fact_text(fact)
        if not normalized:
            return "empty"
        if should_store is False:
            return "model_rejected"
        if len(normalized) < 12:
            return "too_short"
        if len(normalized) > 240:
            return "too_long"
        if normalized.endswith("?"):
            return "question"
        if _FACT_META_SUBJECT_RE.match(normalized) and _FACT_META_VERB_RE.search(normalized):
            return "conversation_meta"
        if _FACT_UNRESOLVED_PRONOUN_RE.match(normalized):
            return "unresolved_subject"
        if _FACT_HEDGING_RE.search(normalized):
            return "uncertain"
        if category:
            normalized_category = cls._normalize_fact_category(category)
            if normalized_category not in _FACT_ALLOWED_CATEGORIES:
                return "unsupported_category"
        if confidence is not None and confidence < 0.72:
            return "low_confidence"
        if source_text and not _has_grounding_overlap(normalized, source_text):
            return "not_grounded"
        return None

    @classmethod
    def is_storable_fact(cls, fact: str, *, source_text: str | None = None) -> bool:
        return cls.fact_rejection_reason(fact, source_text=source_text) is None

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
        allow_tools: bool = True,
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

        tool_support = getattr(self, "_tool_support_by_model", None)
        if tool_support is None:
            tool_support = {}
            self._tool_support_by_model = tool_support
        tools_allowed_for_model = allow_tools and tool_support.get(mdl, True)

        all_tools = list(BUILTIN_TOOLS) if tools_allowed_for_model else []
        if tools and tools_allowed_for_model:
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

            retried_without_tools = False
            try:
                response = await self._client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
            except Exception:
                if kwargs.get("tools"):
                    tool_support[mdl] = False
                    logger.warning("LLM request with tools failed for model %s; retrying without tools", mdl, exc_info=True)
                    try:
                        retry_kwargs = dict(kwargs)
                        retry_kwargs.pop("tools", None)
                        retry_kwargs.pop("tool_choice", None)
                        retried_without_tools = True
                        response = await self._client.chat.completions.create(**retry_kwargs)  # type: ignore[arg-type]
                    except Exception:
                        logger.exception("LLM request failed after retry without tools")
                        return {
                            "content": "⚠️ Something went wrong while contacting the language model.",
                            "embeds": [],
                            "usage": {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens},
                        }
                else:
                    logger.exception("LLM request failed")
                    return {
                        "content": "⚠️ Something went wrong while contacting the language model.",
                        "embeds": [],
                        "usage": {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens},
                    }

            if kwargs.get("tools") and not retried_without_tools:
                tool_support[mdl] = True

            choice = response.choices[0]
            usage = response.usage
            if usage:
                total_prompt_tokens += usage.prompt_tokens
                total_completion_tokens += usage.completion_tokens

            tool_calls = list(getattr(choice.message, "tool_calls", None) or [])

            # No tool calls — return final text
            if not tool_calls:
                content = _message_content_to_text(choice.message.content)
                return {
                    "content": content.strip(),
                    "embeds": embeds,
                    "usage": {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens},
                }

            # Process tool calls
            messages.append(choice.message.model_dump())  # type: ignore[arg-type]
            has_create_embed = False
            for tc in tool_calls:
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
    # Fact extraction (adaptive learning)
    # ------------------------------------------------------------------

    async def extract_facts(
        self,
        user_message: str,
        assistant_reply: str,
        *,
        model: str,
        max_facts: int = 5,
    ) -> list[str]:
        """Extract discrete, reusable facts from a Q&A exchange.

        Returns a list of short factual statements (empty list on failure
        or when there is nothing worth learning).
        """
        instruction = (
            "You are a knowledge curator deciding what deserves long-term memory. "
            "Given a user question and an assistant reply, extract up to {max} short, self-contained factual statements "
            "that are durable and reusable later. "
            "Only keep statements that are clearly supported by the text and belong to one of these categories: "
            "user_preference, user_identity, community_info, topic_fact, policy. "
            "Reject requests, one-off plans, speculation, uncertain claims, jokes, hypotheticals, conversational meta, "
            "and anything about tone/style/format. "
            "Rewrite first-person statements into explicit third-person facts when needed, for example 'I like dark mode' "
            "becomes 'The user prefers dark mode.' "
            "Output ONLY a JSON array. Each item must be an object with keys: fact, category, grounded_in, confidence, should_store, reason. "
            "grounded_in must be one of user_message, assistant_reply, or both. confidence must be a number from 0 to 1. "
            "If nothing should be remembered, output []."
        ).format(max=max_facts)

        exchange = f"User: {user_message}\nAssistant: {assistant_reply}"
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": exchange},
                ],
                temperature=0.2,
                max_tokens=512,
            )
            raw = (response.choices[0].message.content or "").strip()
            candidates = json.loads(raw)
            if isinstance(candidates, list):
                accepted: list[str] = []
                seen: set[str] = set()
                for candidate in candidates:
                    if isinstance(candidate, str):
                        fact = _normalize_fact_text(candidate)
                        reason = self.fact_rejection_reason(fact, source_text=exchange)
                    elif isinstance(candidate, dict):
                        fact = _normalize_fact_text(str(candidate.get("fact", "")))
                        reason = self.fact_rejection_reason(
                            fact,
                            source_text=exchange,
                            category=str(candidate.get("category", "")),
                            confidence=float(candidate.get("confidence", 0.0) or 0.0),
                            should_store=candidate.get("should_store"),
                        )
                    else:
                        continue

                    if reason:
                        logger.debug("Rejected learned fact candidate %r: %s", candidate, reason)
                        continue
                    if fact.lower() in seen:
                        continue
                    seen.add(fact.lower())
                    accepted.append(fact)
                    if len(accepted) >= max_facts:
                        break
                return accepted
        except Exception:
            logger.debug("Fact extraction failed (non-critical)", exc_info=True)
        return []

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
