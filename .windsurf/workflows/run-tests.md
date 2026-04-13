---
description: Run the Playerbot-support test suite (all tests, or targeted by area)
---

# Run tests

## All tests

// turbo
```
pytest tests/ -v
```

## Targeted by area

| Area | Command |
|---|---|
| LLM / embeddings | `pytest tests/test_llm_service.py tests/test_message_learning.py -v` |
| Dashboard auth | `pytest tests/test_dashboard_auth.py tests/test_dashboard_github_state.py -v` |
| Dashboard knowledge | `pytest tests/test_dashboard_knowledge.py -v` |
| Trusted assistant | `pytest tests/test_dashboard_assistant_train_trusted.py -v` |

## Tips

- Add `-x` to stop on first failure.
- Add `-q` to suppress verbose output for a quick pass/fail summary.
- Tests run from repo root; `PYTHONPATH` is set by pytest discovery.
- If async tests hang, confirm `pytest-asyncio` is installed: `pip install pytest-asyncio`.
