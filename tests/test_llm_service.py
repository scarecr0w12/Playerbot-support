from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.llm_service import (
    LLMService,
    _assistant_message_visible_text,
    _message_content_to_text,
    extended_reasoning_model,
)


class LLMServiceCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def _make_service(self, side_effect) -> tuple[LLMService, AsyncMock]:
        create = AsyncMock(side_effect=side_effect)
        llm = LLMService.__new__(LLMService)
        llm._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=create,
                )
            )
        )
        return llm, create

    async def test_get_response_retries_without_tools_when_connector_rejects_them(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="connector reply", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        )
        llm, create = self._make_service([Exception("tools unsupported"), response])

        result = await llm.get_response(
            [{"role": "user", "content": "hi"}],
            system_prompt="test",
            model="qwen3.5",
            tools=[{"type": "function", "function": {"name": "custom", "parameters": {}}}],
        )

        self.assertEqual(result["content"], "connector reply")
        self.assertEqual(create.await_count, 2)
        self.assertIn("tools", create.await_args_list[0].kwargs)
        self.assertNotIn("tools", create.await_args_list[1].kwargs)

    async def test_get_response_disables_tools_for_model_after_first_rejection(self) -> None:
        first_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="first reply", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )
        second_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="second reply", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
        )
        llm, create = self._make_service([Exception("tools unsupported"), first_response, second_response])

        first_result = await llm.get_response(
            [{"role": "user", "content": "hi"}],
            system_prompt="test",
            model="qwen3.5",
            tools=[{"type": "function", "function": {"name": "custom", "parameters": {}}}],
        )
        second_result = await llm.get_response(
            [{"role": "user", "content": "hi again"}],
            system_prompt="test",
            model="qwen3.5",
            tools=[{"type": "function", "function": {"name": "custom", "parameters": {}}}],
        )

        self.assertEqual(first_result["content"], "first reply")
        self.assertEqual(second_result["content"], "second reply")
        self.assertEqual(create.await_count, 3)
        self.assertIn("tools", create.await_args_list[0].kwargs)
        self.assertNotIn("tools", create.await_args_list[1].kwargs)
        self.assertNotIn("tools", create.await_args_list[2].kwargs)

    async def test_get_response_skips_tools_when_disabled(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="plain reply", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        )
        llm, create = self._make_service([response])

        result = await llm.get_response(
            [{"role": "user", "content": "hi"}],
            system_prompt="test",
            model="qwen3.5",
            allow_tools=False,
        )

        self.assertEqual(result["content"], "plain reply")
        self.assertNotIn("tools", create.await_args.kwargs)

    async def test_get_response_executes_tool_calls_even_without_tool_finish_reason(self) -> None:
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="create_embed",
                arguments='{"title":"Status","description":"Ready"}',
            ),
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[tool_call],
                        model_dump=lambda: {"role": "assistant", "content": None, "tool_calls": []},
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )
        llm, _ = self._make_service([response])

        result = await llm.get_response(
            [{"role": "user", "content": "show status"}],
            system_prompt="test",
            model="qwen3.5",
        )

        self.assertEqual(result["content"], "")
        self.assertEqual(result["embeds"], [{"title": "Status", "description": "Ready"}])

    def test_message_content_skips_thinking_blocks(self) -> None:
        content = [
            {"type": "thinking", "thinking": "secret scratchpad"},
            {"type": "text", "text": "Hello."},
        ]
        self.assertEqual(_message_content_to_text(content).strip(), "Hello.")

    def test_message_content_strips_redacted_thinking_xml_string(self) -> None:
        # Split literals so markup tools cannot shorten `` to ``.
        raw = "<" + "redacted_thinking>plan</" + "redacted_thinking>\nVisible answer."
        self.assertEqual(_message_content_to_text(raw).strip(), "Visible answer.")

    def test_message_content_does_not_pull_reasoning_into_visible_text(self) -> None:
        """Regression: reasoning-only dict parts must not become the user-visible reply."""
        content = [
            {"type": "reasoning", "reasoning": "step A then B"},
        ]
        self.assertEqual(_message_content_to_text(content).strip(), "")

    def test_assistant_message_visible_text_prefers_content(self) -> None:
        msg = SimpleNamespace(content=[{"type": "text", "text": "Done."}])
        self.assertEqual(_assistant_message_visible_text(msg), "Done.")

    def test_extended_reasoning_model_heuristic(self) -> None:
        self.assertTrue(extended_reasoning_model("o3-mini"))
        self.assertTrue(extended_reasoning_model("deepseek-ai/DeepSeek-R1"))
        self.assertTrue(extended_reasoning_model("qwen3-235b-a22b-thinking-2507"))
        self.assertFalse(extended_reasoning_model("gpt-4o-mini"))

    async def test_get_response_openai_reasoning_model_uses_completion_budget(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        llm, create = self._make_service([response])
        llm._llm_base_url = "https://api.openai.com/v1"

        await llm.get_response(
            [{"role": "user", "content": "x"}],
            system_prompt="s",
            model="o3-mini",
            temperature=0.2,
            max_tokens=4096,
        )
        kw = create.await_args.kwargs
        self.assertEqual(kw.get("max_completion_tokens"), 4096)
        self.assertNotIn("max_tokens", kw)
        self.assertNotIn("temperature", kw)

    async def test_get_response_non_reasoning_openai_still_uses_max_tokens(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        llm, create = self._make_service([response])
        llm._llm_base_url = "https://api.openai.com/v1"

        await llm.get_response(
            [{"role": "user", "content": "x"}],
            system_prompt="s",
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=512,
        )
        kw = create.await_args.kwargs
        self.assertEqual(kw.get("max_tokens"), 512)
        self.assertNotIn("max_completion_tokens", kw)
        self.assertEqual(kw.get("temperature"), 0.2)

    async def test_get_response_forwards_reasoning_effort_in_extra_body(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        llm, create = self._make_service([response])
        llm._llm_base_url = "http://localhost:11434/v1"

        await llm.get_response(
            [{"role": "user", "content": "x"}],
            system_prompt="s",
            model="gpt-4o-mini",
            reasoning_effort="high",
        )
        kw = create.await_args.kwargs
        self.assertEqual(kw.get("extra_body"), {"reasoning_effort": "high"})


if __name__ == "__main__":
    unittest.main()