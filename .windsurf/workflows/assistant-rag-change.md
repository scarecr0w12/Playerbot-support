---
description: Modify AI assistant behaviour, embeddings, Qdrant usage, crawling, or LLMService in Playerbot-support
---

# Assistant & RAG change

## 1. Read first

// turbo
```
read_file bot/llm_service.py           # chat, embeddings, images, compaction
read_file bot/qdrant_service.py        # collections embeddings_{guild_id}, facts_{guild_id}
read_file bot/cogs/support.py          # triggers, memory, tool calls, feedback
```

Also scan the **AI section of `README.md`** to understand user-facing behaviour you must preserve.

## 2. Identify the data path

Trace: user message → history → optional RAG retrieve → LLM → tools → persist feedback/facts.

Note which stage your change affects and what its cost / latency implications are.

## 3. Rules of thumb

- **Metadata in SQLite, vectors in Qdrant** — rows that reference vectors must carry `qdrant_id`.
- Changing **embedding model** or dimensions may require re-embedding all existing points; call out breaking changes and provide a migration script reference (`migrate_to_qdrant.py` pattern).
- **Thresholds** (relatedness, learning cutoff) are behaviour-sensitive; prefer named constants or config keys co-located with existing defaults.
- Dashboard knowledge tools must stay consistent with chunk metadata the bot writes.

## 4. Implement

Make the change. Use `multi_edit` for coordinated edits within a single file.

## 5. Verify

// turbo
Run targeted tests:
```
pytest tests/test_llm_service.py tests/test_message_learning.py tests/test_dashboard_knowledge.py -v
```

Smoke-test in Discord: `/query` (embedding search) or the dashboard knowledge page if search or storage changed.

## 6. Output summary

Provide:
- Behaviour summary (before / after)
- Config keys or constants touched
- Explicit **rollback / migration notes** if schema or embedding shape changed
