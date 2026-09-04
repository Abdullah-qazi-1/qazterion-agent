"""Qazterion Provider Abstraction, Dynamic Model Management, and Adapters."""

from __future__ import annotations

from qz_providers.models import (
    ModelCapability,
    ModelLifecycleState,
    ModelMetadata,
    ProviderInfo,
)
from qz_providers.exceptions import (
    ProviderError,
    AuthenticationError,
    RateLimitError,
    TimeoutError,
    ServerError,
    ContextLengthExceededError,
    ModelNotFoundError,
    ModelUnavailableError,
    InvalidRequestError,
    normalize_error,
)
from qz_providers.adapters import (
    BaseProviderAdapter,
    OpenAICompatibleAdapter,
    GroqAdapter,
    MistralAdapter,
    GeminiAdapter,
    OpenRouterAdapter,
    DeepSeekAdapter,
    get_adapter_for_provider,
    register_adapter,
)
from qz_providers.registry import (
    ProviderRegistry,
    get_provider_registry,
    reset_provider_registry,
)
from qz_providers.model_registry import (
    ModelRegistry,
    get_model_registry,
    reset_model_registry,
    DEFAULT_ALIAS_MAP,
)

__all__ = [
    "ModelCapability",
    "ModelLifecycleState",
    "ModelMetadata",
    "ProviderInfo",
    "ProviderError",
    "AuthenticationError",
    "RateLimitError",
    "TimeoutError",
    "ServerError",
    "ContextLengthExceededError",
    "ModelNotFoundError",
    "ModelUnavailableError",
    "InvalidRequestError",
    "normalize_error",
    "BaseProviderAdapter",
    "OpenAICompatibleAdapter",
    "GroqAdapter",
    "MistralAdapter",
    "GeminiAdapter",
    "OpenRouterAdapter",
    "DeepSeekAdapter",
    "get_adapter_for_provider",
    "register_adapter",
    "ProviderRegistry",
    "get_provider_registry",
    "reset_provider_registry",
    "ModelRegistry",
    "get_model_registry",
    "reset_model_registry",
    "DEFAULT_ALIAS_MAP",
]
