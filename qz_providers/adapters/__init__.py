"""Adapter types, selected per provider by the catalog's ``adapter`` field."""

from __future__ import annotations

from qz_providers.adapters.base import ProviderAdapter, sanitize_messages
from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.adapters.openrouter import OpenRouterAdapter
from qz_providers.catalog import ProviderSpec

_ADAPTER_TYPES: dict[str, type[ProviderAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "openrouter": OpenRouterAdapter,
}


def register_adapter_type(name: str, adapter_cls: type[ProviderAdapter]) -> None:
    """Make a new wire protocol available to the catalog's ``adapter:`` field."""
    _ADAPTER_TYPES[name.strip().lower()] = adapter_cls


def create_adapter(spec: ProviderSpec) -> ProviderAdapter:
    adapter_cls = _ADAPTER_TYPES.get(spec.adapter.strip().lower())
    if adapter_cls is None:
        raise ValueError(
            f"Provider '{spec.provider_id}' uses unknown adapter '{spec.adapter}'. "
            f"Known adapters: {', '.join(sorted(_ADAPTER_TYPES))}."
        )
    return adapter_cls(spec)


__all__ = [
    "ProviderAdapter",
    "OpenAICompatibleAdapter",
    "OpenRouterAdapter",
    "create_adapter",
    "register_adapter_type",
    "sanitize_messages",
]
