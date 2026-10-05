"""Validate a provider API key without spending tokens (lists models / key info)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from qz_providers.adapters import create_adapter
from qz_providers.catalog import ProviderCatalog, ProviderSpec
from qz_providers.exceptions import ProviderError, normalize_error
from qz_providers.keys import mask

_STATUS_BY_KIND = {
    "auth": "invalid_key",
    "rate_limit": "rate_limited",
    "quota": "rate_limited",
}


@dataclass
class ConnectivityResult:
    provider: str
    connected: bool
    status: str  # "valid" | "invalid_key" | "rate_limited" | "network_error" | "unsupported"
    message: str
    models_found: int = 0
    latency_ms: float = 0.0
    masked_key: str = ""

    @property
    def error_message(self) -> str:
        return "" if self.connected else self.message

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_provider_connectivity(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 8,
    catalog: ProviderCatalog | None = None,
) -> ConnectivityResult:
    """Check ``api_key`` against ``provider``. Secrets never appear in results."""
    pid = provider.lower().strip()
    key = (api_key or "").strip()
    masked = mask(key) if key else ""
    if not key:
        return ConnectivityResult(pid, False, "invalid_key", "API key cannot be empty.")

    spec = (catalog or ProviderCatalog()).provider(pid)
    if spec is None:
        if not base_url:
            return ConnectivityResult(pid, False, "unsupported",
                                      f"Unknown provider '{pid}'. Register it with a base URL first.", masked_key=masked)
        spec = ProviderSpec(pid, pid.capitalize(), "openai_compatible", base_url, f"{pid.upper()}_KEY")
    elif base_url:
        spec = replace(spec, base_url=base_url)

    adapter = create_adapter(spec)
    started = time.monotonic()
    try:
        count = adapter.check_key(api_key=key, timeout=timeout_seconds)
    except Exception as raw:
        error = raw if isinstance(raw, ProviderError) else normalize_error(raw)
        status = _STATUS_BY_KIND.get(error.kind, "network_error")
        message = {
            "invalid_key": "Authentication failed. Please check the API key.",
            "rate_limited": "The key is valid but currently rate limited or out of quota.",
        }.get(status, f"Provider check failed ({error.kind}).")
        return ConnectivityResult(pid, False, status, message,
                                  latency_ms=round((time.monotonic() - started) * 1000.0, 1), masked_key=masked)
    finally:
        adapter.close()
    return ConnectivityResult(
        pid, True, "valid",
        f"Connected successfully ({count} models visible)" if count else "Authentication successful",
        models_found=count,
        latency_ms=round((time.monotonic() - started) * 1000.0, 1),
        masked_key=masked,
    )


test_provider_connectivity = probe_provider_connectivity
test_provider_connectivity.__test__ = False  # type: ignore[attr-defined]
