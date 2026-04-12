"""Dynamic model discovery service for LLM providers.

This module provides functionality to automatically discover available models
from different LLM providers like OpenAI, LiteLLM, OpenRouter, etc.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List

import aiohttp

if TYPE_CHECKING:
    from bot.config import Config

logger = logging.getLogger(__name__)

_MODEL_ALIAS_OVERRIDES: dict[str, str] = {
    "qwen35": "qwen3.5",
    "qwen35397b": "qwen3.5",
    "qwen35397binstruct": "qwen3.5",
    "qwen3397b": "qwen3.5",
    "qwen3.5397b": "qwen3.5",
    "qwen3.5397binstruct": "qwen3.5",
    "qwen3530b": "qwen3.5",
    "qwen3530ba3binstruct": "qwen3.5",
    "qwen3embedding8b": "qwen3-embedding-8b",
    "embeddinggemma": "embeddinggemma",
}


def _qwen3_chat_family_lookup_keys(model_id: str) -> set[str]:
    """Extra lookup keys so a guild can store qwen3.5 while the API lists HF-style ids."""
    lower = model_id.lower()
    keys: set[str] = set()
    if "embedding" in lower:
        return keys
    if "qwen2" in lower:
        return keys

    compact = re.sub(r"[^a-z0-9]+", "", lower)
    if "qwen3.5" in lower or compact == "qwen35" or compact.startswith("qwen35397b"):
        keys.add("qwen35")
        keys.add("qwen3530b")
        return keys

    is_qwen3 = (
        "qwen3-" in lower
        or "qwen3." in lower
        or "qwen3_" in lower
        or lower.startswith("qwen3")
        or "/qwen3" in lower
        or lower.startswith("qwen/qwen3")
    )
    if not is_qwen3:
        return keys

    size_or_role = (
        "instruct",
        "thinking",
        "chat",
        "next",
        "a3b",
        "2507",
        "2504",
        "30b",
        "32b",
        "235b",
        "48b",
        "14b",
        "8b",
        "4b",
        "0.6b",
        "1.7b",
    )
    if not any(tok in lower for tok in size_or_role):
        return keys

    keys.add("qwen35")
    if "30b" in lower or "32b" in lower:
        keys.add("qwen3530b")
    return keys


@dataclass
class ModelInfo:
    """Information about an available model."""
    id: str
    name: str
    provider: str
    type: str  # "chat", "embedding", "image"
    context_length: int | None = None
    pricing: Dict[str, float] | None = None
    capabilities: List[str] | None = None


class ModelDiscoveryService:
    """Service for discovering available models from different LLM providers."""
    
    def __init__(self, config: Config) -> None:
        self.config = config
        self.base_url = config.llm_base_url.rstrip('/')
        self.api_key = config.llm_api_key
        self._cache: Dict[str, tuple[List[ModelInfo], datetime]] = {}
        self._cache_ttl = timedelta(hours=1)  # Cache models for 1 hour
        
    async def get_available_models(self, model_type: str = "chat") -> List[ModelInfo]:
        """Get available models for the configured provider.
        
        Args:
            model_type: Type of models to fetch ("chat", "embedding", "image")
            
        Returns:
            List of available ModelInfo objects
        """
        cache_key = f"{self.base_url}:{model_type}"
        
        # Check cache first
        if cache_key in self._cache:
            models, cached_at = self._cache[cache_key]
            if datetime.now(timezone.utc) - cached_at < self._cache_ttl:
                logger.debug(f"Using cached models for {cache_key} ({len(models)} models)")
                return models
        
        logger.info(f"Fetching {model_type} models from provider at {self.base_url}")
        
        try:
            provider = self._detect_provider()
            logger.info(f"Detected provider: {provider}")
            
            models = await self._fetch_models(provider, model_type)
            logger.info(f"Fetched {len(models)} {model_type} models from {provider}")
            
            # Log each model for debugging
            for model in models[:5]:  # Log first 5 to avoid spam
                logger.debug(f"Found {model_type} model: {model.id} ({model.provider})")
            if len(models) > 5:
                logger.debug(f"... and {len(models) - 5} more {model_type} models")
            
            # Cache the results
            self._cache[cache_key] = (models, datetime.now(timezone.utc))
            return models
            
        except Exception as e:
            logger.error(f"Failed to fetch {model_type} models: {e}")
            fallback_models = self._get_fallback_models(model_type)
            logger.info(f"Using {len(fallback_models)} fallback {model_type} models")
            return fallback_models

    async def resolve_model_id(self, requested_model: str, model_type: str = "chat") -> str:
        """Resolve a configured model string to a currently available model ID."""
        models = await self.get_available_models(model_type)
        if not models:
            return requested_model

        default_model = self.select_default_model_id(models, model_type)
        requested = (requested_model or "").strip()
        if not requested:
            return default_model

        exact_matches = {model.id.lower(): model.id for model in models}
        if requested.lower() in exact_matches:
            return exact_matches[requested.lower()]

        model_lookup: dict[str, str] = {}
        for model in models:
            for candidate in self._model_lookup_keys(model.id):
                model_lookup.setdefault(candidate, model.id)
            for candidate in self._model_lookup_keys(model.name):
                model_lookup.setdefault(candidate, model.id)

        for candidate in self._model_lookup_keys(requested):
            if candidate in model_lookup:
                return model_lookup[candidate]

        requested_key = self._normalize_lookup_key(requested)
        for model in models:
            if requested_key and requested_key in self._normalize_lookup_key(model.id):
                return model.id
            if requested_key and requested_key in self._normalize_lookup_key(model.name):
                return model.id

        logger.warning(
            "Configured %s model %r is unavailable at %s; falling back to %s",
            model_type,
            requested_model,
            self.base_url,
            default_model,
        )
        return default_model

    def select_default_model_id(self, models: List[ModelInfo], model_type: str) -> str:
        """Choose a sensible default model from currently available models."""
        if not models:
            return ""

        preferred_ids = {
            "chat": [
                "qwen3.5",
                "qwen3-30b-instruct",
                "qwen",
                "gpt-4o",
                "gpt-4-turbo",
                "gpt-4",
                "gpt-3.5-turbo",
            ],
            "embedding": [
                "qwen3-embedding-8b",
                "embeddinggemma",
                "text-embedding-3-small",
                "text-embedding-3-large",
                "text-embedding-ada-002",
            ],
            "image": [
                "dall-e-3",
                "flux-1-dev",
                "stable-diffusion",
            ],
        }

        available = {model.id.lower(): model.id for model in models}
        for preferred in preferred_ids.get(model_type, []):
            if preferred.lower() in available:
                return available[preferred.lower()]

        if model_type == "chat":
            for model in models:
                model_id = model.id.lower()
                if "embedding" in model_id or "image" in model_id or "vision" in model_id:
                    continue
                if any(token in model_id for token in ("instruct", "chat", "assistant")):
                    return model.id
            for model in models:
                model_id = model.id.lower()
                if any(token in model_id for token in ("thinking", "reasoning", "coder")):
                    continue
                return model.id

        return models[0].id

    def _normalize_lookup_key(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def _model_lookup_keys(self, value: str) -> List[str]:
        normalized = self._normalize_lookup_key(value)
        if not normalized:
            return []

        keys = {normalized}
        if normalized in _MODEL_ALIAS_OVERRIDES:
            keys.add(self._normalize_lookup_key(_MODEL_ALIAS_OVERRIDES[normalized]))

        for alias, target in _MODEL_ALIAS_OVERRIDES.items():
            if target == value or self._normalize_lookup_key(target) == normalized:
                keys.add(alias)

        if normalized.endswith("instruct"):
            keys.add(normalized.removesuffix("instruct"))
        if normalized.endswith("chat"):
            keys.add(normalized.removesuffix("chat"))

        keys |= _qwen3_chat_family_lookup_keys(value)

        return [key for key in keys if key]
    
    def _detect_provider(self) -> str:
        """Detect the LLM provider based on base URL."""
        url_lower = self.base_url.lower()
        
        if "openai.com" in url_lower or "api.openai.com" in url_lower:
            return "openai"
        elif "openrouter.ai" in url_lower:
            return "openrouter"
        elif "litellm" in url_lower:
            return "litellm"
        elif "localhost" in url_lower or "127.0.0.1" in url_lower:
            # Could be Ollama, LM Studio, vLLM, etc.
            if ":11434" in url_lower:
                return "ollama"
            elif ":1234" in url_lower:
                return "lm_studio"
            elif ":8000" in url_lower:
                return "vllm"
            return "local"
        else:
            # Generic OpenAI-compatible endpoint
            return "openai_compatible"
    
    async def _fetch_models(self, provider: str, model_type: str) -> List[ModelInfo]:
        """Fetch models from the specific provider."""
        if provider == "openai":
            return await self._fetch_openai_models(model_type)
        elif provider == "openrouter":
            return await self._fetch_openrouter_models(model_type)
        elif provider == "litellm":
            return await self._fetch_litellm_models(model_type)
        elif provider in ["ollama", "lm_studio", "vllm", "local"]:
            return await self._fetch_local_models(provider, model_type)
        else:
            return await self._fetch_openai_compatible_models(model_type)
    
    async def _fetch_openai_models(self, model_type: str) -> List[ModelInfo]:
        """Fetch models from OpenAI API."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        logger.debug(f"Fetching OpenAI models from {self.base_url}/models")
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{self.base_url}/models") as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"OpenAI API error {response.status}: {error_text}")
                    raise Exception(f"OpenAI API error: {response.status}")
                
                data = await response.json()
                all_models = data.get("data", [])
                logger.debug(f"OpenAI returned {len(all_models)} total models")
                
                models = []
                chat_count = embed_count = image_count = 0
                
                for model in all_models:
                    model_id = model["id"]
                    
                    # Filter by model type
                    if model_type == "chat" and self._is_chat_model(model_id):
                        chat_count += 1
                        models.append(ModelInfo(
                            id=model_id,
                            name=self._format_model_name(model_id),
                            provider="OpenAI",
                            type="chat",
                            context_length=model.get("max_context_length"),
                        ))
                    elif model_type == "embedding" and self._is_embedding_model(model_id):
                        embed_count += 1
                        models.append(ModelInfo(
                            id=model_id,
                            name=self._format_model_name(model_id),
                            provider="OpenAI",
                            type="embedding",
                        ))
                    elif model_type == "image" and self._is_image_model(model_id):
                        image_count += 1
                        models.append(ModelInfo(
                            id=model_id,
                            name=self._format_model_name(model_id),
                            provider="OpenAI",
                            type="image",
                        ))
                
                logger.debug(f"OpenAI model filtering: {chat_count} chat, {embed_count} embedding, {image_count} image models for {model_type} request")
                
                # If no models found for the type, log some examples for debugging
                if not models:
                    sample_models = [m["id"] for m in all_models[:10]]
                    logger.warning(f"No {model_type} models found. Sample models: {sample_models}")
                
                return models
    
    async def _fetch_openrouter_models(self, model_type: str) -> List[ModelInfo]:
        """Fetch models from OpenRouter API."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/discord-bot",
            "X-Title": "Discord Bot",
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get("https://openrouter.ai/api/v1/models") as response:
                if response.status != 200:
                    raise Exception(f"OpenRouter API error: {response.status}")
                
                data = await response.json()
                models = []
                
                for model in data.get("data", []):
                    model_id = model["id"]
                    
                    # Filter by model type
                    if model_type == "chat":
                        models.append(ModelInfo(
                            id=model_id,
                            name=model.get("name", model_id),
                            provider="OpenRouter",
                            type="chat",
                            context_length=model.get("context_length"),
                            pricing=model.get("pricing"),
                            capabilities=model.get("capabilities", []),
                        ))
                
                return models
    
    async def _fetch_litellm_models(self, model_type: str) -> List[ModelInfo]:
        """Fetch models from LiteLLM proxy.
        
        Note: LiteLLM doesn't have a standard models endpoint, so we'll try
        to use the OpenAI-compatible endpoint or return common models.
        """
        try:
            # Try OpenAI-compatible endpoint first
            return await self._fetch_openai_compatible_models(model_type)
        except Exception:
            # Fallback to common LiteLLM models
            return self._get_litellm_fallback_models(model_type)
    
    async def _fetch_local_models(self, provider: str, model_type: str) -> List[ModelInfo]:
        """Fetch models from local providers (Ollama, LM Studio, vLLM)."""
        if provider == "ollama":
            return await self._fetch_ollama_models(model_type)
        elif provider == "lm_studio":
            return await self._fetch_lm_studio_models(model_type)
        elif provider == "vllm":
            return await self._fetch_vllm_models(model_type)
        else:
            return await self._fetch_openai_compatible_models(model_type)
    
    async def _fetch_ollama_models(self, model_type: str) -> List[ModelInfo]:
        """Fetch models from Ollama API."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/api/tags") as response:
                if response.status != 200:
                    raise Exception(f"Ollama API error: {response.status}")
                
                data = await response.json()
                models = []
                
                for model in data.get("models", []):
                    model_id = model["name"]
                    
                    if model_type == "chat":
                        models.append(ModelInfo(
                            id=model_id,
                            name=self._format_model_name(model_id),
                            provider="Ollama",
                            type="chat",
                            context_length=model.get("details", {}).get("context_length"),
                        ))
                
                return models
    
    async def _fetch_lm_studio_models(self, model_type: str) -> List[ModelInfo]:
        """Fetch models from LM Studio API."""
        try:
            return await self._fetch_openai_compatible_models(model_type)
        except Exception:
            # LM Studio might not expose models endpoint
            return self._get_lm_studio_fallback_models(model_type)
    
    async def _fetch_vllm_models(self, model_type: str) -> List[ModelInfo]:
        """Fetch models from vLLM API."""
        try:
            return await self._fetch_openai_compatible_models(model_type)
        except Exception:
            return self._get_vllm_fallback_models(model_type)
    
    async def _fetch_openai_compatible_models(self, model_type: str) -> List[ModelInfo]:
        """Fetch models from generic OpenAI-compatible endpoint."""
        if not self.api_key or self.api_key == "no-key-needed":
            # Try without auth key for local endpoints
            headers = {}
        else:
            headers = {"Authorization": f"Bearer {self.api_key}"}
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{self.base_url}/models") as response:
                if response.status != 200:
                    raise Exception(f"API error: {response.status}")
                
                data = await response.json()
                models = []
                
                for model in data.get("data", []):
                    model_id = model["id"]
                    
                    # Try to determine model type from ID
                    if model_type == "chat" and self._is_chat_model(model_id):
                        models.append(ModelInfo(
                            id=model_id,
                            name=self._format_model_name(model_id),
                            provider="API",
                            type="chat",
                            context_length=model.get("max_context_length"),
                        ))
                    elif model_type == "embedding" and self._is_embedding_model(model_id):
                        models.append(ModelInfo(
                            id=model_id,
                            name=self._format_model_name(model_id),
                            provider="API",
                            type="embedding",
                        ))
                    elif model_type == "image" and self._is_image_model(model_id):
                        models.append(ModelInfo(
                            id=model_id,
                            name=self._format_model_name(model_id),
                            provider="API",
                            type="image",
                        ))
                
                return models
    
    def _is_chat_model(self, model_id: str) -> bool:
        """Check if a model ID indicates a chat model."""
        model_id_lower = model_id.lower()
        
        # Explicit embedding patterns - these should NOT be chat models
        embedding_patterns = [
            r"text-embedding",
            r"embed-",
            r"sentence-",
            r"embedding-",
            r"embeddinggemma",
            r"e5-",
            r"bge-",
            r"all-MiniLM",
        ]
        
        # Explicit image patterns - these should NOT be chat models  
        image_patterns = [
            r"dall-e",
            r"stable-diffusion",
            r"midjourney",
            r"stability-",
            r"image-",
            r"img-",
            r"sd-",
        ]
        
        # Explicit chat patterns - these ARE chat models
        chat_patterns = [
            r"gpt-",
            r"gpt-oss",
            r"claude-",
            r"llama-",
            r"mistral-",
            r"mixtral-",
            r"gemini-",
            r"command-",
            r"qwen-",
            r"qwen3[\._\-/]",  # Qwen3 / Qwen3.5 ids without a digit immediately after ``qwen`` only
            r"qwen\d",
            r"\bqwen\b",
            r"yi-",
            r"deepseek-",
            r"\bgemma\d*\b",
            r"\bglm\b",
            r"\bkimi\b",
            r"\bminimax\b",
            r"\bsonar\b",
            r"\bcoder\b",
            r"\breasoning\b",
            r"chat-",
            r"instruct",
            r"turbo",
            r"sonnet",
            r"haiku",
            r"opus",
        ]
        
        # First check if it's explicitly an embedding or image model
        if any(re.search(pattern, model_id_lower) for pattern in embedding_patterns):
            logger.debug(f"Model {model_id} identified as embedding model")
            return False
            
        if any(re.search(pattern, model_id_lower) for pattern in image_patterns):
            logger.debug(f"Model {model_id} identified as image model")
            return False
        
        # Then check if it's explicitly a chat model
        if any(re.search(pattern, model_id_lower) for pattern in chat_patterns):
            logger.debug(f"Model {model_id} identified as chat model")
            return True
        
        # Default: assume it's a chat model unless proven otherwise
        logger.debug(f"Model {model_id} defaulting to chat model")
        return True
    
    def _is_embedding_model(self, model_id: str) -> bool:
        """Check if a model ID indicates an embedding model."""
        model_id_lower = model_id.lower()
        patterns = [
            r"text-embedding",
            r"embed-",
            r"sentence-",
            r"embedding-",
            r"embeddinggemma",
            r"e5-",
            r"bge-",
            r"all-MiniLM",
            r"multilingual-",
        ]
        
        is_embedding = any(re.search(pattern, model_id_lower) for pattern in patterns)
        if is_embedding:
            logger.debug(f"Model {model_id} identified as embedding model")
        return is_embedding
    
    def _is_image_model(self, model_id: str) -> bool:
        """Check if a model ID indicates an image generation model."""
        model_id_lower = model_id.lower()
        patterns = [
            r"dall-e",
            r"stable-diffusion",
            r"midjourney",
            r"stability-",
            r"image-",
            r"img-",
            r"sd-",
            r"diffusion",
            r"flux",
            r"sdxl",
            r"sdxl-turbo",
            r"kandinsky",
            r"playground",
            r"leonardo",
            r"art-",
            r"generate-",
            r"vision-",
            r"visual-",
            r"draw-",
            r"paint-",
        ]
        
        is_image = any(re.search(pattern, model_id_lower) for pattern in patterns)
        if is_image:
            logger.debug(f"Model {model_id} identified as image model")
        return is_image
    
    def _format_model_name(self, model_id: str) -> str:
        """Format model ID into a readable name."""
        # Replace common abbreviations and separators
        name = model_id.replace("-", " ").replace("_", " ")
        name = name.replace(".", ". ")
        name = re.sub(r"(\d+)", r" \1", name)  # Add space before numbers
        name = " ".join(word.capitalize() for word in name.split())
        
        # Handle special cases
        name = name.replace("Gpt", "GPT")
        name = name.replace("Oss", "OSS")
        name = name.replace("Claude", "Claude")
        name = name.replace("Llama", "Llama")
        name = name.replace("Mistral", "Mistral")
        name = name.replace("Mixtral", "Mixtral")
        name = name.replace("Qwen", "Qwen")
        name = name.replace("Glm", "GLM")
        
        return name
    
    def _get_fallback_models(self, model_type: str) -> List[ModelInfo]:
        """Get fallback models when API calls fail."""
        common_models = {
            "chat": [
                ModelInfo("qwen3.5", "Qwen 3.5", "Fallback", "chat"),
                ModelInfo("Qwen/Qwen3-30B-A3B-Instruct-2507", "Qwen3 30B A3B Instruct", "Fallback", "chat"),
                ModelInfo("gpt-3.5-turbo", "GPT 3.5 Turbo", "Fallback", "chat"),
                ModelInfo("gpt-4", "GPT 4", "Fallback", "chat"),
                ModelInfo("gpt-4-turbo", "GPT 4 Turbo", "Fallback", "chat"),
                ModelInfo("gpt-4o", "GPT 4o", "Fallback", "chat"),
                ModelInfo("claude-3-haiku", "Claude 3 Haiku", "Fallback", "chat"),
                ModelInfo("claude-3-sonnet", "Claude 3 Sonnet", "Fallback", "chat"),
                ModelInfo("llama-3-8b", "Llama 3 8B", "Fallback", "chat"),
                ModelInfo("mistral-7b", "Mistral 7B", "Fallback", "chat"),
            ],
            "embedding": [
                ModelInfo("text-embedding-3-small", "Text Embedding 3 Small", "Fallback", "embedding"),
                ModelInfo("text-embedding-3-large", "Text Embedding 3 Large", "Fallback", "embedding"),
                ModelInfo("text-embedding-ada-002", "Text Embedding Ada 002", "Fallback", "embedding"),
            ],
            "image": [
                ModelInfo("dall-e-3", "DALL-E 3", "Fallback", "image"),
                ModelInfo("dall-e-2", "DALL-E 2", "Fallback", "image"),
                ModelInfo("stable-diffusion", "Stable Diffusion", "Fallback", "image"),
                ModelInfo("dall-e-3-1080x1080", "DALL-E 3 1080x1080", "Fallback", "image"),
                ModelInfo("dall-e-3-1792x1024", "DALL-E 3 1792x1024", "Fallback", "image"),
                ModelInfo("dall-e-3-1024x1792", "DALL-E 3 1024x1792", "Fallback", "image"),
                ModelInfo("stable-diffusion-xl", "Stable Diffusion XL", "Fallback", "image"),
                ModelInfo("sdxl-turbo", "SDXL Turbo", "Fallback", "image"),
                ModelInfo("flux-1-dev", "FLUX 1 Dev", "Fallback", "image"),
                ModelInfo("flux-1-schnell", "FLUX 1 Schnell", "Fallback", "image"),
                ModelInfo("midjourney-v6", "Midjourney v6", "Fallback", "image"),
            ],
        }
        
        return common_models.get(model_type, [])
    
    def _get_litellm_fallback_models(self, model_type: str) -> List[ModelInfo]:
        """Get LiteLLM-specific fallback models."""
        if model_type == "chat":
            return [
                ModelInfo("gpt-3.5-turbo", "GPT 3.5 Turbo", "LiteLLM", "chat"),
                ModelInfo("gpt-4", "GPT 4", "LiteLLM", "chat"),
                ModelInfo("claude-3-haiku", "Claude 3 Haiku", "LiteLLM", "chat"),
                ModelInfo("claude-3-sonnet", "Claude 3 Sonnet", "LiteLLM", "chat"),
                ModelInfo("gemini-pro", "Gemini Pro", "LiteLLM", "chat"),
                ModelInfo("command-r-plus", "Command R+", "LiteLLM", "chat"),
                ModelInfo("llama-3-8b-instruct", "Llama 3 8B Instruct", "LiteLLM", "chat"),
                ModelInfo("mixtral-8x7b", "Mixtral 8x7B", "LiteLLM", "chat"),
            ]
        return self._get_fallback_models(model_type)
    
    def _get_lm_studio_fallback_models(self, model_type: str) -> List[ModelInfo]:
        """Get LM Studio fallback models."""
        if model_type == "chat":
            return [
                ModelInfo("local-model", "Local Model", "LM Studio", "chat"),
                ModelInfo("custom-model", "Custom Model", "LM Studio", "chat"),
            ]
        return self._get_fallback_models(model_type)
    
    def _get_vllm_fallback_models(self, model_type: str) -> List[ModelInfo]:
        """Get vLLM fallback models."""
        if model_type == "chat":
            return [
                ModelInfo("vllm-model", "vLLM Model", "vLLM", "chat"),
                ModelInfo("served-model", "Served Model", "vLLM", "chat"),
            ]
        return self._get_fallback_models(model_type)
    
    def clear_cache(self) -> None:
        """Clear the model cache."""
        self._cache.clear()
        logger.info("Model discovery cache cleared")
    
    async def refresh_models(self, model_type: str = "chat") -> List[ModelInfo]:
        """Force refresh the model list."""
        self.clear_cache()
        return await self.get_available_models(model_type)
