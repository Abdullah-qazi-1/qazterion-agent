"""OpenRouter Provider Adapter."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.models import ModelMetadata


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """Adapter for OpenRouter API, supporting dynamic discovery with pricing and capabilities."""

    provider_id: str = "openrouter"
    display_name: str = "OpenRouter"
    base_url: str = "https://openrouter.ai/api/v1"
    default_concurrency: int = 3

    def get_static_models(self) -> list[ModelMetadata]:
        return [
            ModelMetadata(
                provider="openrouter",
                model_id="anthropic/claude-3.5-sonnet",
                display_name="Claude 3.5 Sonnet (OpenRouter)",
                capabilities=["chat", "streaming", "tool_calling", "vision", "reasoning", "large_context"],
                context_window=200000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="openrouter",
                model_id="meta-llama/llama-3.3-70b-instruct",
                display_name="Llama 3.3 70B Instruct (OpenRouter)",
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
                provider="openrouter",
                model_id="deepseek/deepseek-r1",
                display_name="DeepSeek R1 (OpenRouter)",
                capabilities=["chat", "streaming", "reasoning", "large_context"],
                context_window=64000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=True,
                supports_structured_output=True,
                source="static",
            ),
            ModelMetadata(
                provider="openrouter",
                model_id="google/gemini-2.0-flash-001",
                display_name="Gemini 2.0 Flash (OpenRouter)",
                capabilities=["chat", "streaming", "tool_calling", "vision", "fast", "large_context"],
                context_window=1000000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_reasoning=False,
                supports_structured_output=True,
                source="static",
            ),
        ]

    def discover_models(self, api_key: str | None = None) -> list[ModelMetadata]:
        """Fetch rich model metadata from OpenRouter public/authenticated models endpoint."""
        url = "https://openrouter.ai/api/v1/models"
        headers = {"User-Agent": "Qazterion-Agent/1.0", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", []) if isinstance(data, dict) else []
            models: list[ModelMetadata] = []
            for item in items:
                model_id = str(item.get("id") or "")
                if not model_id:
                    continue
                context_length = int(item.get("context_length") or 32768)
                pricing = item.get("pricing", {})
                prompt_price = float(pricing.get("prompt", 0)) * 1_000_000 if isinstance(pricing, dict) else None
                completion_price = float(pricing.get("completion", 0)) * 1_000_000 if isinstance(pricing, dict) else None
                arch = item.get("architecture", {})
                modality = str(arch.get("modality", "")).lower() if isinstance(arch, dict) else ""
                has_vision = "image" in modality or "multimodal" in modality
                description = str(item.get("description", "")).lower()
                is_reasoning = "reasoning" in description or "r1" in model_id.lower() or "qwq" in model_id.lower() or "o1" in model_id.lower() or "o3" in model_id.lower()

                caps = ["chat", "streaming", "tool_calling"]
                if is_reasoning:
                    caps.append("reasoning")
                if has_vision:
                    caps.append("vision")
                if context_length >= 65536:
                    caps.append("large_context")

                models.append(
                    ModelMetadata(
                        provider="openrouter",
                        model_id=model_id,
                        display_name=str(item.get("name") or model_id),
                        capabilities=caps,
                        context_window=context_length,
                        supports_tools=True,
                        supports_vision=has_vision,
                        supports_streaming=True,
                        supports_reasoning=is_reasoning,
                        supports_structured_output=True,
                        pricing_input_per_1m=prompt_price,
                        pricing_output_per_1m=completion_price,
                        source="discovery",
                    )
                )
            return models if models else self.get_static_models()
        except Exception:
            return self.get_static_models()
