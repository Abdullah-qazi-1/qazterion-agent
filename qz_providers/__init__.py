"""Multi-provider, multi-key model access for Qazterion.

* :mod:`qz_providers.catalog`  - providers, models and roles (YAML, user-overridable)
* :mod:`qz_providers.keys`     - API keys per provider (keystore + environment)
* :mod:`qz_providers.health`   - per-key / per-model cooldowns, persisted
* :mod:`qz_providers.adapters` - wire protocols (OpenAI-compatible, OpenRouter)
* :mod:`qz_providers.gateway`  - role -> provider/model/key selection with failover
"""

from __future__ import annotations

from qz_providers.catalog import CatalogError, ModelSpec, ProviderCatalog, ProviderSpec, parse_model_ref
from qz_providers.exceptions import (
    AuthenticationError,
    ConnectionError,
    ContextLengthExceededError,
    InvalidRequestError,
    ModelNotFoundError,
    ModelUnavailableError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
    ServerError,
    TimeoutError,
    normalize_error,
)
from qz_providers.gateway import (
    GatewayResponse,
    ModelGateway,
    NoRouteError,
    RouteInfo,
    get_gateway,
    reset_gateway,
    set_gateway,
)
from qz_providers.health import HealthTracker
from qz_providers.keys import ApiKey, KeySource

__all__ = [
    "ApiKey",
    "AuthenticationError",
    "CatalogError",
    "ConnectionError",
    "ContextLengthExceededError",
    "GatewayResponse",
    "HealthTracker",
    "InvalidRequestError",
    "KeySource",
    "ModelGateway",
    "ModelNotFoundError",
    "ModelSpec",
    "ModelUnavailableError",
    "NoRouteError",
    "ProviderCatalog",
    "ProviderError",
    "ProviderSpec",
    "QuotaExhaustedError",
    "RateLimitError",
    "RouteInfo",
    "ServerError",
    "TimeoutError",
    "get_gateway",
    "normalize_error",
    "parse_model_ref",
    "reset_gateway",
    "set_gateway",
]
