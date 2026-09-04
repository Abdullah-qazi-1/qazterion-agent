"""DeepSeek Provider Adapter."""

from __future__ import annotations

from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.models import ModelMetadata


class DeepSeekAdapter(OpenAICompatibleAdapter):
    """Adapter for DeepSeek API."""

    provider_id: str = "deepseek"
    display_name: str = "DeepSeek"
    base_url: str = "https://api.deepseek.com"
    default_concurrency: int = 2

    def get_static_models(self) -> list[ModelMetadata]:
        return [
            ModelMetadata(
                provider="deepseek",
                model_id="deepseek-chat",
                display_name="DeepSeek V3 (Chat)",
                capabilities=["chat", "streaming", "tool_calling", "structured_output", "fast", "large_context"],
                context_window=64000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="deepseek",
                model_id="deepseek-reasoner",
                display_name="DeepSeek R1 (Reasoner)",
                capabilities=["chat", "streaming", "reasoning", "large_context"],
                context_window=64000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
        ]
