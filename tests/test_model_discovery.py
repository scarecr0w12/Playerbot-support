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

    def test_embeddinggemma_is_detected_as_embedding_model(self) -> None:
        service = self._make_service()

        self.assertTrue(service._is_embedding_model("embeddinggemma"))
        self.assertFalse(service._is_chat_model("embeddinggemma"))