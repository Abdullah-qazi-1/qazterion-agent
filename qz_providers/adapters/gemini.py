"""Google Gemini Provider Adapter."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.models import ModelMetadata


class GeminiAdapter(OpenAICompatibleAdapter):
    """Adapter for Google Gemini via Generative Language OpenAI-compatible endpoint."""

    provider_id: str = "gemini"
    display_name: str = "Google Gemini"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    default_concurrency: int = 2

    def get_static_models(self) -> list[ModelMetadata]:
        return [
            ModelMetadata(
                provider="gemini",
                model_id="gemini-2.0-flash",
                display_name="Gemini 2.0 Flash",
                capabilities=["chat", "streaming", "tool_calling", "vision", "fast", "large_context", "structured_output"],
                context_window=1048576,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="gemini",
                model_id="gemini-1.5-pro",
                display_name="Gemini 1.5 Pro",
                capabilities=["chat", "streaming", "tool_calling", "vision", "reasoning", "large_context", "structured_output"],
                context_window=2097152,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="gemini",
                model_id="gemini-1.5-flash",
                display_name="Gemini 1.5 Flash",
                capabilities=["chat", "streaming", "tool_calling", "vision", "fast", "large_context", "structured_output"],
                context_window=1048576,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="gemini",
                model_id="gemini-3.1-flash-lite",
                display_name="Gemini 3.1 Flash Lite (classify)",
                capabilities=["chat", "streaming", "tool_calling", "fast", "large_context", "structured_output"],
                context_window=1048576,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
        ]
