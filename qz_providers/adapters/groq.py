"""Groq Provider Adapter."""

from __future__ import annotations

from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.models import ModelMetadata


class GroqAdapter(OpenAICompatibleAdapter):
    """Adapter for Groq Cloud API."""

    provider_id: str = "groq"
    display_name: str = "Groq"
    base_url: str = "https://api.groq.com/openai/v1"
    default_concurrency: int = 3

    def get_static_models(self) -> list[ModelMetadata]:
        return [
            ModelMetadata(
                provider="groq",
                model_id="llama-3.3-70b-versatile",
                display_name="Llama 3.3 70B Versatile",
                capabilities=["chat", "streaming", "tool_calling", "structured_output", "fast", "large_context"],
                context_window=128000,
                max_output_tokens=32768,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="groq",
                model_id="llama-3.1-8b-instant",
                display_name="Llama 3.1 8B Instant",
                capabilities=["chat", "streaming", "tool_calling", "fast", "large_context"],
                context_window=128000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="groq",
                model_id="openai/gpt-oss-20b",
                display_name="Groq GPT-OSS 20B (groq-fast)",
                capabilities=["chat", "streaming", "tool_calling", "fast"],
                context_window=32768,
                max_output_tokens=4096,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="groq",
                model_id="compound-mini",
                display_name="Groq Compound Mini (coder-backup)",
                capabilities=["chat", "streaming", "tool_calling", "fast"],
                context_window=32768,
                max_output_tokens=4096,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="groq",
                model_id="qwen-qwq-32b",
                display_name="Qwen QwQ 32B (Groq Reasoning)",
                capabilities=["chat", "streaming", "reasoning", "large_context"],
                context_window=128000,
                max_output_tokens=32768,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
        ]
