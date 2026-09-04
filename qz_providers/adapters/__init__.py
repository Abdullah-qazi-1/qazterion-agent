"""Provider adapters registry and factory."""

from __future__ import annotations

from typing import Type

from qz_providers.adapters.base import BaseProviderAdapter
from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.adapters.groq import GroqAdapter
from qz_providers.adapters.mistral import MistralAdapter
from qz_providers.adapters.gemini import GeminiAdapter
from qz_providers.adapters.openrouter import OpenRouterAdapter
from qz_providers.adapters.deepseek import DeepSeekAdapter

_ADAPTER_REGISTRY: dict[str, Type[BaseProviderAdapter]] = {
    "groq": GroqAdapter,
    "mistral": MistralAdapter,
    "gemini": GeminiAdapter,
    "openrouter": OpenRouterAdapter,
    "deepseek": DeepSeekAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
}


def register_adapter(provider_id: str, adapter_cls: Type[BaseProviderAdapter]) -> None:
    """Register a custom provider adapter class."""
    _ADAPTER_REGISTRY[provider_id.lower()] = adapter_cls


def get_adapter_for_provider(provider_id: str, **kwargs) -> BaseProviderAdapter:
    """Get an instantiated adapter for a given provider name or return generic OpenAICompatibleAdapter."""
    p = provider_id.lower()
    adapter_cls = _ADAPTER_REGISTRY.get(p, OpenAICompatibleAdapter)
    if adapter_cls is OpenAICompatibleAdapter and p not in _ADAPTER_REGISTRY:
        return OpenAICompatibleAdapter(provider_id=p, display_name=p.capitalize(), **kwargs)
    return adapter_cls(**kwargs) if kwargs else adapter_cls()


__all__ = [
    "BaseProviderAdapter",
    "OpenAICompatibleAdapter",
    "GroqAdapter",
    "MistralAdapter",
    "GeminiAdapter",
    "OpenRouterAdapter",
    "DeepSeekAdapter",
    "register_adapter",
    "get_adapter_for_provider",
]
