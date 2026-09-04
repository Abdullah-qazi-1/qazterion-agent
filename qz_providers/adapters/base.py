"""Abstract Base Class for LLM Provider Adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator

from qz_providers.exceptions import ProviderError, normalize_error
from qz_providers.models import ModelMetadata, ProviderInfo


class BaseProviderAdapter(ABC):
    """Abstract interface that all provider adapters must implement."""

    provider_id: str
    display_name: str
    base_url: str | None = None
    default_concurrency: int = 2

    @abstractmethod
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        api_key: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute a non-streaming chat completion."""
        ...

    def stream_complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        api_key: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Execute a streaming chat completion. Default implementation raises or falls back."""
        raise NotImplementedError(f"Streaming is not implemented for provider '{self.provider_id}'")

    @abstractmethod
    def get_static_models(self) -> list[ModelMetadata]:
        """Return hardcoded/default models catalog with rich capability metadata."""
        ...

    def discover_models(self, api_key: str | None = None) -> list[ModelMetadata]:
        """Discover live models from provider endpoint. Falls back to static catalog if discovery fails."""
        return self.get_static_models()

    def check_health(self, api_key: str | None = None) -> dict[str, Any]:
        """Health/readiness check for provider. Returns status dict."""
        return {
            "provider": self.provider_id,
            "status": "configured" if api_key else "unconfigured",
            "base_url": self.base_url,
        }

    def normalize_error(self, error: Exception | str | None, status_code: int | None = None) -> ProviderError:
        """Normalize an adapter-specific error into standard ProviderError."""
        return normalize_error(error, status_code)

    def get_provider_info(self, enabled: bool = True) -> ProviderInfo:
        """Return descriptor of this provider and its capabilities."""
        static_models = [m.model_id for m in self.get_static_models()]
        return ProviderInfo(
            provider_id=self.provider_id,
            display_name=self.display_name,
            enabled=enabled,
            adapter_type=self.__class__.__name__,
            base_url=self.base_url,
            default_concurrency=self.default_concurrency,
            supported_capabilities=["chat", "streaming", "tool_calling"],
            models=static_models,
        )
