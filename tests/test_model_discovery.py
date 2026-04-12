from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.model_discovery import ModelDiscoveryService, ModelInfo


class ModelDiscoveryResolutionTests(unittest.IsolatedAsyncioTestCase):
    def _make_service(self) -> ModelDiscoveryService:
        return ModelDiscoveryService(
            SimpleNamespace(llm_base_url="https://example.com/v1", llm_api_key="test-key")
        )

    async def test_resolve_model_id_maps_human_friendly_qwen_name(self) -> None:
        service = self._make_service()
        service.get_available_models = AsyncMock(
            return_value=[
                ModelInfo("qwen3.5", "Qwen 3.5", "API", "chat"),
                ModelInfo("reasoning", "Reasoning", "API", "chat"),
            ]
        )

        resolved = await service.resolve_model_id("Qwen 3.5 397B", "chat")

        self.assertEqual(resolved, "qwen3.5")

    async def test_resolve_model_id_falls_back_to_preferred_available_model(self) -> None:
        service = self._make_service()
        service.get_available_models = AsyncMock(
            return_value=[
                ModelInfo("reasoning", "Reasoning", "API", "chat"),
                ModelInfo("qwen3.5", "Qwen 3.5", "API", "chat"),
            ]
        )

        resolved = await service.resolve_model_id("missing-model", "chat")

        self.assertEqual(resolved, "qwen3.5")

    async def test_resolve_model_id_qwen35_matches_hf_style_checkpoint(self) -> None:
        """Guilds often configure qwen3.5 while LM Studio / vLLM expose a long HF id."""
        hf_id = "Qwen/Qwen3-30B-A3B-Instruct-2507"
        service = self._make_service()
        service.get_available_models = AsyncMock(
            return_value=[
                ModelInfo(hf_id, "Qwen3 30B", "API", "chat"),
            ]
        )

        resolved = await service.resolve_model_id("qwen3.5", "chat")

        self.assertEqual(resolved, hf_id)

    def test_embeddinggemma_is_detected_as_embedding_model(self) -> None:
        service = self._make_service()

        self.assertTrue(service._is_embedding_model("embeddinggemma"))
        self.assertFalse(service._is_chat_model("embeddinggemma"))

    def test_ollama_http_root_strips_trailing_v1(self) -> None:
        from bot.model_discovery import _ollama_http_root

        self.assertEqual(_ollama_http_root("http://localhost:11434/v1"), "http://localhost:11434")
        self.assertEqual(_ollama_http_root("http://localhost:11434/v1/"), "http://localhost:11434")
        self.assertEqual(_ollama_http_root("http://host:11434"), "http://host:11434")

    def test_litellm_model_info_maps_proxied_ollama_gemma(self) -> None:
        service = self._make_service()
        item = {
            "model_name": "gemma4:31b-cloud",
            "litellm_params": {"model": "ollama/gemma4:31b-cloud"},
            "model_info": {"mode": "chat", "litellm_provider": "ollama"},
        }
        chat = service._model_info_from_litellm_entry(item, "chat")
        self.assertIsNotNone(chat)
        assert chat is not None
        self.assertEqual(chat.id, "gemma4:31b-cloud")
        self.assertEqual(chat.type, "chat")

        emb = service._model_info_from_litellm_entry(item, "embedding")
        self.assertIsNone(emb)