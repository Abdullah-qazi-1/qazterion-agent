"""Mistral AI Provider Adapter."""

from __future__ import annotations

from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.models import ModelMetadata


class MistralAdapter(OpenAICompatibleAdapter):
    """Adapter for Mistral AI API."""

    provider_id: str = "mistral"
    display_name: str = "Mistral AI"
    base_url: str = "https://api.mistral.ai/v1"
    default_concurrency: int = 2

    def get_static_models(self) -> list[ModelMetadata]:
        return [
            ModelMetadata(
                provider="mistral",
                model_id="mistral-large-latest",
                display_name="Mistral Large (coder-strong)",
                capabilities=["chat", "streaming", "tool_calling", "reasoning", "structured_output", "large_context"],
                context_window=128000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="mistral",
                model_id="codestral-latest",
                display_name="Codestral (planner)",
                capabilities=["chat", "streaming", "tool_calling", "fast"],
                context_window=32768,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="mistral",
                model_id="mistral-medium-3.5",
                display_name="Mistral Medium 3.5 (reasoner)",
                capabilities=["chat", "streaming", "tool_calling", "reasoning"],
                context_window=32768,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="mistral",
                model_id="mistral-small-latest",
                display_name="Mistral Small",
                capabilities=["chat", "streaming", "tool_calling", "fast", "large_context"],
                context_window=128000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
        ]
