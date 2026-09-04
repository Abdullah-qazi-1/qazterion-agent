"""Provider connectivity and credential verification probe."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from qz_keystore import mask_key


@dataclass
class ConnectivityResult:
    provider: str
    connected: bool
    status: str  # "valid" | "invalid_key" | "rate_limited" | "network_error" | "unsupported"
    message: str
    models_found: int = 0
    latency_ms: float = 0.0
    masked_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_provider_connectivity(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: int = 8,
) -> ConnectivityResult:
    """Test API key validity directly against provider endpoint.

    Secrets are NEVER leaked in logs or error messages.
    """
    prov = provider.lower().strip()
    key = api_key.strip()
    masked = mask_key(key)

    if not key:
        return ConnectivityResult(
            provider=prov,
            connected=False,
            status="invalid_key",
            message="API key cannot be empty.",
            masked_key="",
        )

    # Determine endpoint & headers
    headers: dict[str, str] = {
        "User-Agent": "Qazterion-Desktop/1.0",
        "Accept": "application/json",
    }

    url = ""
    if prov == "groq":
        url = "https://api.groq.com/openai/v1/models"
        headers["Authorization"] = f"Bearer {key}"
    elif prov == "mistral":
        url = "https://api.mistral.ai/v1/models"
        headers["Authorization"] = f"Bearer {key}"
    elif prov == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    elif prov == "openrouter":
        url = "https://openrouter.ai/api/v1/auth/key"
        headers["Authorization"] = f"Bearer {key}"
    elif prov == "deepseek":
        url = "https://api.deepseek.com/models"
        headers["Authorization"] = f"Bearer {key}"
    else:
        # Fallback to base_url or OpenAI-compatible models endpoint
        if base_url:
            clean_base = base_url.rstrip("/")
            url = f"{clean_base}/models" if not clean_base.endswith("/models") else clean_base
            headers["Authorization"] = f"Bearer {key}"
        else:
            return ConnectivityResult(
                provider=prov,
                connected=True,
                status="valid",
                message=f"Key stored for provider '{prov}' (endpoint probe skipped).",
                masked_key=masked,
            )

    start_time = time.monotonic()
    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            elapsed = (time.monotonic() - start_time) * 1000.0
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

            # Count models if returned
            model_count = 0
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    model_count = len(data["data"])
                elif "models" in data and isinstance(data["models"], list):
                    model_count = len(data["models"])
                elif "key" in data or "data" in data:
                    model_count = 1  # OpenRouter auth response
            elif isinstance(data, list):
                model_count = len(data)

            msg = f"Connected successfully ({model_count} models discovered)" if model_count > 0 else "Authentication successful"
            return ConnectivityResult(
                provider=prov,
                connected=True,
                status="valid",
                message=msg,
                models_found=model_count,
                latency_ms=round(elapsed, 1),
                masked_key=masked,
            )

    except urllib.error.HTTPError as err:
        elapsed = (time.monotonic() - start_time) * 1000.0
        code = err.code
        if code in (401, 403):
            return ConnectivityResult(
                provider=prov,
                connected=False,
                status="invalid_key",
                message=f"Authentication failed: HTTP {code} Unauthorized. Please check your API key.",
                latency_ms=round(elapsed, 1),
                masked_key=masked,
            )
        elif code == 429:
            return ConnectivityResult(
                provider=prov,
                connected=False,
                status="rate_limited",
                message="Rate limit reached (HTTP 429). The key is valid but quota is exhausted.",
                latency_ms=round(elapsed, 1),
                masked_key=masked,
            )
        else:
            return ConnectivityResult(
                provider=prov,
                connected=False,
                status="network_error",
                message=f"Provider endpoint returned HTTP {code}",
                latency_ms=round(elapsed, 1),
                masked_key=masked,
            )

    except urllib.error.URLError as err:
        elapsed = (time.monotonic() - start_time) * 1000.0
        return ConnectivityResult(
            provider=prov,
            connected=False,
            status="network_error",
            message=f"Network connection failed: {err.reason}",
            latency_ms=round(elapsed, 1),
            masked_key=masked,
        )

    except Exception as exc:
        elapsed = (time.monotonic() - start_time) * 1000.0
        return ConnectivityResult(
            provider=prov,
            connected=False,
            status="network_error",
            message=f"Connection probe error: {str(exc)[:150]}",
            latency_ms=round(elapsed, 1),
            masked_key=masked,
        )


probe_provider_connectivity.__test__ = False  # type: ignore[attr-defined]

# Alias for compatibility
verify_provider_connectivity = probe_provider_connectivity
test_provider_connectivity = probe_provider_connectivity
test_provider_connectivity.__test__ = False  # type: ignore[attr-defined]
