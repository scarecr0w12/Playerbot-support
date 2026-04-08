"""Dynamic configuration schema that integrates with model discovery.

This module provides a dynamic configuration schema that can fetch available models
from the configured LLM provider instead of using hardcoded lists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Any

if TYPE_CHECKING:
    from bot.model_discovery import ModelDiscoveryService, ModelInfo

logger = logging.getLogger(__name__)

# Static configuration schema (non-model fields)
STATIC_CONFIG_SCHEMA: Dict[str, Dict[str, Any]] = {
    # Assistant/AI Configuration (excluding model fields)
    "assistant_prompt": {
        "type": "textarea",
        "label": "System Prompt",
        "description": "The system prompt that defines the AI's personality and behavior",
        "placeholder": "You are a helpful assistant...",
        "default": "You are a helpful support assistant. Answer questions clearly and concisely. If you don't know the answer, say so honestly.",
    },
    "assistant_temperature": {
        "type": "select",
        "label": "Temperature",
        "description": "Controls randomness in responses (0.0 = deterministic, 1.0 = very creative)",
        "options": [
            {"value": "0.0", "label": "0.0 - Very Deterministic"},
            {"value": "0.1", "label": "0.1 - Mostly Deterministic"},
            {"value": "0.3", "label": "0.3 - Balanced"},
            {"value": "0.5", "label": "0.5 - Creative"},
            {"value": "0.7", "label": "0.7 - Very Creative"},
            {"value": "0.9", "label": "0.9 - Maximum Creativity"},
            {"value": "1.0", "label": "1.0 - Completely Random"},
        ],
        "default": "0.7",
    },
    "assistant_max_tokens": {
        "type": "select",
        "label": "Max Tokens",
        "description": "Maximum number of tokens in AI responses",
        "options": [
            {"value": "256", "label": "256 - Short"},
            {"value": "512", "label": "512 - Medium"},
            {"value": "1024", "label": "1024 - Long"},
            {"value": "2048", "label": "2048 - Very Long"},
            {"value": "4096", "label": "4096 - Extended"},
        ],
        "default": "1024",
    },
    "assistant_max_retention": {
        "type": "select",
        "label": "Conversation Retention",
        "description": "Number of conversation turns to keep in memory",
        "options": [
            {"value": "10", "label": "10 turns"},
            {"value": "20", "label": "20 turns"},
            {"value": "40", "label": "40 turns"},
            {"value": "60", "label": "60 turns"},
            {"value": "100", "label": "100 turns"},
        ],
        "default": "40",
    },

    # Moderation Configuration
    "mod_mute_duration_minutes": {
        "type": "select",
        "label": "Default Mute Duration",
        "description": "Default duration for mutes when not specified",
        "options": [
            {"value": "5", "label": "5 minutes"},
            {"value": "10", "label": "10 minutes"},
            {"value": "15", "label": "15 minutes"},
            {"value": "30", "label": "30 minutes"},
            {"value": "60", "label": "1 hour"},
            {"value": "1440", "label": "1 day"},
            {"value": "10080", "label": "1 week"},
        ],
        "default": "10",
    },
    "mod_max_warnings_before_action": {
        "type": "select",
        "label": "Warnings Before Action",
        "description": "Number of warnings before automatic action is taken",
        "options": [
            {"value": "1", "label": "1 warning"},
            {"value": "2", "label": "2 warnings"},
            {"value": "3", "label": "3 warnings"},
            {"value": "4", "label": "4 warnings"},
            {"value": "5", "label": "5 warnings"},
        ],
        "default": "3",
    },
    "mod_warning_action": {
        "type": "select",
        "label": "Warning Action",
        "description": "Action to take when warning threshold is exceeded",
        "options": [
            {"value": "mute", "label": "Mute"},
            {"value": "kick", "label": "Kick"},
            {"value": "ban", "label": "Ban"},
        ],
        "default": "mute",
    },

    # Auto-Moderation Configuration
    "automod_enabled": {
        "type": "select",
        "label": "Auto-Mod Status",
        "description": "Enable or disable auto-moderation",
        "options": [
            {"value": "1", "label": "Enabled"},
            {"value": "0", "label": "Disabled"},
        ],
        "default": "0",
    },
    "automod_spam_threshold": {
        "type": "select",
        "label": "Spam Threshold",
        "description": "Number of messages within interval that triggers spam detection",
        "options": [
            {"value": "3", "label": "3 messages"},
            {"value": "5", "label": "5 messages"},
            {"value": "7", "label": "7 messages"},
            {"value": "10", "label": "10 messages"},
            {"value": "15", "label": "15 messages"},
        ],
        "default": "5",
    },
    "automod_spam_interval": {
        "type": "select",
        "label": "Spam Interval",
        "description": "Time window (in seconds) for spam detection",
        "options": [
            {"value": "3", "label": "3 seconds"},
            {"value": "5", "label": "5 seconds"},
            {"value": "10", "label": "10 seconds"},
            {"value": "15", "label": "15 seconds"},
            {"value": "30", "label": "30 seconds"},
        ],
        "default": "5",
    },

    # Channel Configuration (text inputs for channel IDs)
    "mod_log_channel_id": {
        "type": "text",
        "label": "Mod Log Channel ID",
        "description": "Channel ID where moderation actions are logged",
        "placeholder": "123456789012345678",
    },
    "welcome_channel_id": {
        "type": "text",
        "label": "Welcome Channel ID",
        "description": "Channel ID where welcome messages are sent",
        "placeholder": "123456789012345678",
    },
    "ticket_category_id": {
        "type": "text",
        "label": "Ticket Category ID",
        "description": "Category ID where support tickets are created",
        "placeholder": "123456789012345678",
    },
    "starboard_channel_id": {
        "type": "text",
        "label": "Starboard Channel ID",
        "description": "Channel ID where starred messages are posted",
        "placeholder": "123456789012345678",
    },

    # Role Configuration (text inputs for role IDs)
    "mod_role_id": {
        "type": "text",
        "label": "Moderator Role ID",
        "description": "Role ID for moderators",
        "placeholder": "123456789012345678",
    },
    "admin_role_id": {
        "type": "text",
        "label": "Admin Role ID",
        "description": "Role ID for administrators",
        "placeholder": "123456789012345678",
    },
    "member_role_id": {
        "type": "text",
        "label": "Member Role ID",
        "description": "Role ID for regular members",
        "placeholder": "123456789012345678",
    },

    # Economy Configuration
    "economy_enabled": {
        "type": "select",
        "label": "Economy System",
        "description": "Enable or disable the economy system",
        "options": [
            {"value": "1", "label": "Enabled"},
            {"value": "0", "label": "Disabled"},
        ],
        "default": "0",
    },
    "default_payday_amount": {
        "type": "select",
        "label": "Daily Payday Amount",
        "description": "Credits awarded per daily payday",
        "options": [
            {"value": "50", "label": "50 credits"},
            {"value": "100", "label": "100 credits"},
            {"value": "200", "label": "200 credits"},
            {"value": "500", "label": "500 credits"},
            {"value": "1000", "label": "1000 credits"},
        ],
        "default": "100",
    },
    "default_payday_cooldown_hours": {
        "type": "select",
        "label": "Payday Cooldown",
        "description": "Hours between payday collections",
        "options": [
            {"value": "6", "label": "6 hours"},
            {"value": "12", "label": "12 hours"},
            {"value": "18", "label": "18 hours"},
            {"value": "24", "label": "24 hours"},
        ],
        "default": "12",
    },
    "default_currency_name": {
        "type": "text",
        "label": "Currency Name",
        "description": "Name of the server currency",
        "placeholder": "credits",
        "default": "credits",
    },

    # Level System Configuration
    "levels_enabled": {
        "type": "select",
        "label": "Level System",
        "description": "Enable or disable the leveling system",
        "options": [
            {"value": "1", "label": "Enabled"},
            {"value": "0", "label": "Disabled"},
        ],
        "default": "0",
    },
    "xp_per_message": {
        "type": "select",
        "label": "XP Per Message",
        "description": "XP awarded per message",
        "options": [
            {"value": "5", "label": "5 XP"},
            {"value": "10", "label": "10 XP"},
            {"value": "15", "label": "15 XP"},
            {"value": "20", "label": "20 XP"},
            {"value": "25", "label": "25 XP"},
        ],
        "default": "10",
    },
    "xp_cooldown_seconds": {
        "type": "select",
        "label": "XP Cooldown",
        "description": "Seconds between XP awards for the same user",
        "options": [
            {"value": "30", "label": "30 seconds"},
            {"value": "60", "label": "1 minute"},
            {"value": "120", "label": "2 minutes"},
            {"value": "300", "label": "5 minutes"},
        ],
        "default": "60",
    },
}


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
            chat_models = await self._get_model_options("chat")
            schema["assistant_model"] = {
                "type": "select",
                "label": "AI Model",
                "description": "The language model to use for responses",
                "options": chat_models,
                "default": "gpt-3.5-turbo" if self._model_exists(chat_models, "gpt-3.5-turbo") else (chat_models[0]["value"] if chat_models else "gpt-3.5-turbo"),
            }
            
            # Embedding models
            embedding_models = await self._get_model_options("embedding")
            schema["assistant_embedding_model"] = {
                "type": "select",
                "label": "Embedding Model",
                "description": "Model used for text embeddings (RAG/knowledge base)",
                "options": embedding_models,
                "default": "text-embedding-3-small" if self._model_exists(embedding_models, "text-embedding-3-small") else (embedding_models[0]["value"] if embedding_models else "text-embedding-3-small"),
            }
            
            # Image models
            image_models = await self._get_model_options("image")
            
            # If no native image models found, always provide fallback options
            if not image_models:
                image_models = self._get_fallback_model_options("image")
                logger.info(f"No native image models found, providing {len(image_models)} fallback options")
            
            schema["assistant_image_model"] = {
                "type": "select",
                "label": "Image Generation Model",
                "description": "Model used for generating images",
                "options": image_models,
                "default": "dall-e-3" if self._model_exists(image_models, "dall-e-3") else (image_models[0]["value"] if image_models else "dall-e-3"),
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
            options = []
            
            for model in models:
                option = {
                    "value": model.id,
                    "label": f"{model.name} ({model.provider})",
                }
                
                # Add additional info to label if available
                if model.context_length:
                    option["label"] += f" - {model.context_length:,} tokens"
                
                if model.capabilities:
                    caps = ", ".join(model.capabilities[:3])  # Show first 3 capabilities
                    if caps:
                        option["label"] += f" - {caps}"
                
                options.append(option)
            
            # Cache the results
            self._model_cache[cache_key] = options
            self._cache_timestamps[cache_key] = current_time
            
            return options
            
        except Exception as e:
            logger.error(f"Failed to get {model_type} models: {e}")
            return self._get_fallback_model_options(model_type)
    
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
    
    def get_config_categories(self) -> Dict[str, List[str]]:
        """Group configuration keys by category for better organization."""
        return {
            "AI Assistant": [
                "assistant_model",
                "assistant_prompt", 
                "assistant_temperature",
                "assistant_max_tokens",
                "assistant_max_retention",
                "assistant_embedding_model",
                "assistant_image_model",
            ],
            "Moderation": [
                "mod_mute_duration_minutes",
                "mod_max_warnings_before_action",
                "mod_warning_action",
                "mod_log_channel_id",
                "mod_role_id",
                "admin_role_id",
            ],
            "Auto-Moderation": [
                "automod_enabled",
                "automod_spam_threshold",
                "automod_spam_interval",
            ],
            "Channels": [
                "welcome_channel_id",
                "ticket_category_id",
                "starboard_channel_id",
            ],
            "Economy": [
                "economy_enabled",
                "default_payday_amount",
                "default_payday_cooldown_hours",
                "default_currency_name",
            ],
            "Leveling": [
                "levels_enabled",
                "xp_per_message",
                "xp_cooldown_seconds",
            ],
            "Roles": [
                "member_role_id",
            ],
        }
    
    def get_all_config_keys(self) -> List[str]:
        """Get a list of all valid configuration keys."""
        static_keys = list(STATIC_CONFIG_SCHEMA.keys())
        model_keys = ["assistant_model", "assistant_embedding_model", "assistant_image_model"]
        return static_keys + model_keys
