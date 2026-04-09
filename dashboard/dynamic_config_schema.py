"""Dynamic configuration schema that integrates with model discovery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from dashboard.config_definitions import BASE_CONFIG_SCHEMA, CONFIG_CATEGORIES

if TYPE_CHECKING:
    from bot.model_discovery import ModelDiscoveryService, ModelInfo

logger = logging.getLogger(__name__)

STATIC_CONFIG_SCHEMA = BASE_CONFIG_SCHEMA


class DynamicConfigSchema:
    """Dynamic configuration schema that integrates with model discovery."""
    
    def __init__(self, model_discovery: "ModelDiscoveryService") -> None:
        self.model_discovery = model_discovery
        self._model_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_ttl = 3600  # 1 hour cache
        
    async def get_config_schema(self) -> Dict[str, Dict[str, Any]]:
        """Get the complete configuration schema with dynamic model options."""
        schema = STATIC_CONFIG_SCHEMA.copy()
        
        # Add dynamic model fields
        try:
            # Chat models
            chat_model_infos = await self.model_discovery.get_available_models("chat")
            chat_models = self._build_model_options(chat_model_infos)
            schema["assistant_model"] = {
                "type": "select",
                "label": "AI Model",
                "description": "The language model to use for responses",
                "options": chat_models,
                "default": self.model_discovery.select_default_model_id(chat_model_infos, "chat") or "gpt-3.5-turbo",
            }
            
            # Embedding models
            embedding_model_infos = await self.model_discovery.get_available_models("embedding")
            embedding_models = self._build_model_options(embedding_model_infos)
            schema["assistant_embedding_model"] = {
                "type": "select",
                "label": "Embedding Model",
                "description": "Model used for text embeddings (RAG/knowledge base)",
                "options": embedding_models,
                "default": self.model_discovery.select_default_model_id(embedding_model_infos, "embedding") or "text-embedding-3-small",
            }
            
            # Image models
            image_model_infos = await self.model_discovery.get_available_models("image")
            image_models = self._build_model_options(image_model_infos)
            
            # If no native image models found, always provide fallback options
            if not image_models:
                image_models = self._get_fallback_model_options("image")
                logger.info(f"No native image models found, providing {len(image_models)} fallback options")
            
            schema["assistant_image_model"] = {
                "type": "select",
                "label": "Image Generation Model",
                "description": "Model used for generating images",
                "options": image_models,
                "default": self.model_discovery.select_default_model_id(image_model_infos, "image") or (image_models[0]["value"] if image_models else "dall-e-3"),
            }
            
        except Exception as e:
            logger.error(f"Failed to fetch dynamic models: {e}")
            # Fallback to hardcoded models
            schema.update(self._get_fallback_model_schema())
        
        return schema
    
    async def _get_model_options(self, model_type: str) -> List[Dict[str, Any]]:
        """Get model options for the given type."""
        import time
        
        cache_key = f"{model_type}_options"
        current_time = time.time()
        
        # Check cache
        if (cache_key in self._model_cache and 
            cache_key in self._cache_timestamps and
            current_time - self._cache_timestamps[cache_key] < self._cache_ttl):
            return self._model_cache[cache_key]
        
        try:
            models = await self.model_discovery.get_available_models(model_type)
            options = self._build_model_options(models)
            
            # Cache the results
            self._model_cache[cache_key] = options
            self._cache_timestamps[cache_key] = current_time
            
            return options
            
        except Exception as e:
            logger.error(f"Failed to get {model_type} models: {e}")
            return self._get_fallback_model_options(model_type)

    def _build_model_options(self, models: List["ModelInfo"]) -> List[Dict[str, Any]]:
        options = []

        for model in models:
            option = {
                "value": model.id,
                "label": f"{model.name} ({model.provider})",
            }

            if model.context_length:
                option["label"] += f" - {model.context_length:,} tokens"

            if model.capabilities:
                caps = ", ".join(model.capabilities[:3])
                if caps:
                    option["label"] += f" - {caps}"

            options.append(option)

        return options
    
    def _model_exists(self, options: List[Dict[str, Any]], model_id: str) -> bool:
        """Check if a model exists in the options list."""
        return any(option["value"] == model_id for option in options)
    
    def _get_fallback_model_options(self, model_type: str) -> List[Dict[str, Any]]:
        """Get fallback model options when dynamic fetching fails."""
        fallback_options = {
            "chat": [
                {"value": "gpt-3.5-turbo", "label": "GPT 3.5 Turbo (Fallback)"},
                {"value": "gpt-4", "label": "GPT 4 (Fallback)"},
                {"value": "gpt-4-turbo", "label": "GPT 4 Turbo (Fallback)"},
                {"value": "gpt-4o", "label": "GPT 4o (Fallback)"},
                {"value": "claude-3-haiku", "label": "Claude 3 Haiku (Fallback)"},
                {"value": "claude-3-sonnet", "label": "Claude 3 Sonnet (Fallback)"},
                {"value": "llama-3-8b", "label": "Llama 3 8B (Fallback)"},
                {"value": "mistral-7b", "label": "Mistral 7B (Fallback)"},
            ],
            "embedding": [
                {"value": "text-embedding-3-small", "label": "Text Embedding 3 Small (Fallback)"},
                {"value": "text-embedding-3-large", "label": "Text Embedding 3 Large (Fallback)"},
                {"value": "text-embedding-ada-002", "label": "Text Embedding Ada 002 (Fallback)"},
            ],
            "image": [
                {"value": "dall-e-3", "label": "DALL-E 3 (Not Available - Fallback)"},
                {"value": "dall-e-2", "label": "DALL-E 2 (Not Available - Fallback)"},
                {"value": "stable-diffusion", "label": "Stable Diffusion (Not Available - Fallback)"},
                {"value": "dall-e-3-1080x1080", "label": "DALL-E 3 1080x1080 (Not Available - Fallback)"},
                {"value": "flux-1-dev", "label": "FLUX 1 Dev (Not Available - Fallback)"},
                {"value": "flux-1-schnell", "label": "FLUX 1 Schnell (Not Available - Fallback)"},
                {"value": "stable-diffusion-xl", "label": "Stable Diffusion XL (Not Available - Fallback)"},
            ],
        }
        
        return fallback_options.get(model_type, [])
    
    def _get_fallback_model_schema(self) -> Dict[str, Dict[str, Any]]:
        """Get fallback model configuration schema."""
        return {
            "assistant_model": {
                "type": "select",
                "label": "AI Model",
                "description": "The language model to use for responses",
                "options": self._get_fallback_model_options("chat"),
                "default": "gpt-3.5-turbo",
            },
            "assistant_embedding_model": {
                "type": "select",
                "label": "Embedding Model",
                "description": "Model used for text embeddings (RAG/knowledge base)",
                "options": self._get_fallback_model_options("embedding"),
                "default": "text-embedding-3-small",
            },
            "assistant_image_model": {
                "type": "select",
                "label": "Image Generation Model",
                "description": "Model used for generating images",
                "options": self._get_fallback_model_options("image"),
                "default": "dall-e-3",
            },
        }
    
    async def refresh_models(self) -> None:
        """Force refresh the model cache."""
        # Clear model discovery cache
        self.model_discovery.clear_cache()
        
        # Clear our own cache
        self._model_cache.clear()
        self._cache_timestamps.clear()
        
        logger.info("Model configuration cache refreshed")
    
    def get_config_categories(self) -> dict[str, list[str]]:
        """Group configuration keys by category for better organization."""
        return CONFIG_CATEGORIES
    
    def get_all_config_keys(self) -> List[str]:
        """Get a list of all valid configuration keys."""
        static_keys = list(STATIC_CONFIG_SCHEMA.keys())
        model_keys = ["assistant_model", "assistant_embedding_model", "assistant_image_model"]
        return static_keys + model_keys
