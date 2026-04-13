---
trigger: agent_requested
description: >-
  Specialist for LLM calls, embeddings, Qdrant collections, adaptive learning,
  and Support-cog triggers in Playerbot-support. Use when changing RAG quality,
  costs, tool calling, or vector metadata sync.
---

# Assistant / RAG specialist agent

## Model

- OpenAI-compatible **`LLMService`** (`LLM_BASE_URL`, models, embeddings, images).
- **Qdrant** per guild: `embeddings_{guild_id}` (knowledge) and `facts_{guild_id}` (learned) — never orphan SQLite rows from Qdrant points.

## Read first

- `bot/llm_service.py` — chat, embeddings, images, compaction.
- `bot/qdrant_service.py` — collection naming, upsert, search.
- `bot/cogs/support.py` — triggers, memory, tool calls, feedback flow.
- `README.md` (AI section) — user-facing behaviour to keep aligned.

## When changing behaviour

1. Identify **data path:** user message → history → optional RAG retrieve → LLM → tools → persist feedback/facts.
2. Note **cost drivers:** context length, embedding calls, image generation.
3. Preserve **backwards compatibility** for stored rows; if not possible, describe a one-off migration (see `migrate_to_qdrant.py` pattern).

## Rules of thumb

- **Metadata in SQLite, vectors in Qdrant** — keep `qdrant_id` on rows that need vector updates or deletes.
- Changing **embedding model** or dimensions may require re-embed or migration; call out breaking changes explicitly.
- **Thresholds** (relatedness, learning) are behaviour-sensitive; prefer config keys / constants co-located with current defaults.
- Dashboard knowledge tools must stay consistent with chunk metadata the bot writes.

## Testing

- Run: `pytest tests/test_llm_service.py tests/test_message_learning.py tests/test_dashboard_knowledge.py`
- Smoke: `/query` or dashboard knowledge page if search or storage changed.

## Output

Behaviour summary (before/after), config keys touched, and explicit **rollback / migration notes** if schema or embedding shape changes.
