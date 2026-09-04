"""Generic OpenAI-Compatible Provider Adapter."""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Iterator

from openai import OpenAI

from qz_providers.adapters.base import BaseProviderAdapter
from qz_providers.exceptions import ProviderError, normalize_error
from qz_providers.models import ModelLifecycleState, ModelMetadata


class OpenAICompatibleAdapter(BaseProviderAdapter):
    """Generic adapter for providers implementing the OpenAI REST API format."""

    provider_id: str = "openai_compatible"
    display_name: str = "OpenAI Compatible"
    base_url: str | None = None
    default_concurrency: int = 2

    def __init__(self, base_url: str | None = None, provider_id: str | None = None, display_name: str | None = None) -> None:
        if base_url:
            self.base_url = base_url
        if provider_id:
            self.provider_id = provider_id
        if display_name:
            self.display_name = display_name

    def _create_client(self, api_key: str, timeout: float = 45.0) -> OpenAI:
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        api_key: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float = 45.0,
        **kwargs: Any,
    ) -> Any:
        client = self._create_client(api_key, timeout=timeout)
        req: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            req["tools"] = tools
        if max_tokens is not None:
            req["max_tokens"] = max_tokens
        req.update(kwargs)

        try:
            return client.chat.completions.create(**req)
        except Exception as err:
            status_code = getattr(err, "status_code", None)
            raise self.normalize_error(err, status_code=status_code) from err

    def stream_complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        api_key: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float = 45.0,
        **kwargs: Any,
    ) -> Iterator[Any]:
        client = self._create_client(api_key, timeout=timeout)
        req: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools is not None:
            req["tools"] = tools
        if max_tokens is not None:
            req["max_tokens"] = max_tokens
        req.update(kwargs)

        try:
            response = client.chat.completions.create(**req)
            for chunk in response:
                yield chunk
        except Exception as err:
            status_code = getattr(err, "status_code", None)
            raise self.normalize_error(err, status_code=status_code) from err

    def get_static_models(self) -> list[ModelMetadata]:
        return []

    def discover_models(self, api_key: str | None = None) -> list[ModelMetadata]:
        """Query /models endpoint to discover live models."""
        if not self.base_url or not api_key:
            return self.get_static_models()

        url = self.base_url.rstrip("/") + "/models"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Qazterion-Agent/1.0",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", []) if isinstance(data, dict) else []
            models: list[ModelMetadata] = []
            for item in items:
                model_id = str(item.get("id") or "")
                if not model_id:
                    continue
                context_length = int(item.get("context_window") or item.get("max_model_len") or 32768)
                models.append(
                    ModelMetadata(
                        provider=self.provider_id,
                        model_id=model_id,
                        display_name=str(item.get("name") or model_id),
                        capabilities=["chat", "streaming", "tool_calling"],
                        context_window=context_length,
                        supports_tools=True,
                        supports_streaming=True,
                        source="discovery",
                    )
                )
            return models if models else self.get_static_models()
        except Exception:
            # Discovery failed: gracefully fallback to static catalog without crashing
            return self.get_static_models()

    def check_health(self, api_key: str | None = None) -> dict[str, Any]:
        base = super().check_health(api_key)
        if not api_key or not self.base_url:
            return base
        try:
            url = self.base_url.rstrip("/") + "/models"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                base["status"] = "healthy" if resp.status in (200, 204) else "degraded"
        except urllib.error.HTTPError as he:
            base["status"] = "auth_error" if he.code in (401, 403) else ("rate_limited" if he.code == 429 else "error")
            base["error_code"] = he.code
        except Exception as e:
            base["status"] = "unreachable"
            base["error"] = str(e)
        return base
