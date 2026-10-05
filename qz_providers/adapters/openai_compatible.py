"""Adapter for any provider exposing the OpenAI chat-completions API.

Groq, Gemini (OpenAI compatibility endpoint), Mistral, DeepSeek, OpenRouter and
most new providers speak this protocol, so a provider is usually just a
``base_url`` + ``key_prefix`` entry in the catalog.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from qz_providers.adapters.base import ProviderAdapter, sanitize_messages
from qz_providers.exceptions import ProviderError, normalize_error
from qz_providers.keys import fingerprint

_MAX_CACHED_CLIENTS = 8


class OpenAICompatibleAdapter(ProviderAdapter):
    extra_headers: dict[str, str] = {}

    def __init__(self, spec) -> None:
        super().__init__(spec)
        self._clients: OrderedDict[tuple[str, float], Any] = OrderedDict()
        self._lock = threading.Lock()

    # One SDK client per key keeps HTTP connections alive between agent turns
    # without creating a new connection pool for every request.
    def _client(self, api_key: str, timeout: float) -> Any:
        from openai import OpenAI

        cache_key = (fingerprint(api_key), float(timeout))
        with self._lock:
            client = self._clients.get(cache_key)
            if client is not None:
                self._clients.move_to_end(cache_key)
                return client
            kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
            if self.spec.base_url:
                kwargs["base_url"] = self.spec.base_url
            if self.extra_headers:
                kwargs["default_headers"] = dict(self.extra_headers)
            client = OpenAI(**kwargs)
            self._clients[cache_key] = client
            while len(self._clients) > _MAX_CACHED_CLIENTS:
                _, old = self._clients.popitem(last=False)
                _close_quietly(old)
            return client

    def build_request(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"model": model, "messages": sanitize_messages(messages)}
        if temperature is not None:
            request["temperature"] = temperature
        if tools:
            request["tools"] = tools
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        return request

    def complete(
        self,
        *,
        api_key: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 60.0,
    ) -> Any:
        request = self.build_request(
            model=model, messages=messages, tools=tools, temperature=temperature, max_tokens=max_tokens
        )
        try:
            response = self._client(api_key, timeout).chat.completions.create(**request)
        except ProviderError:
            raise
        except Exception as error:
            raise normalize_error(error) from error
        if not getattr(response, "choices", None):
            raise normalize_error("Provider returned no choices (model unavailable)", status_code=503)
        return response

    def list_models(self, *, api_key: str, timeout: float = 10.0) -> list[dict[str, Any]]:
        try:
            page = self._client(api_key, timeout).models.list()
        except Exception as error:
            raise normalize_error(error) from error
        models = []
        for item in getattr(page, "data", None) or []:
            model_id = str(getattr(item, "id", "") or "")
            if not model_id:
                continue
            # Gemini prefixes ids with "models/"; strip so ids match chat usage.
            if model_id.startswith("models/"):
                model_id = model_id[len("models/"):]
            extra = getattr(item, "model_extra", None) or {}
            context = extra.get("context_window") or extra.get("context_length") or extra.get("max_model_len")
            models.append({"id": model_id, "context_window": int(context) if context else None, "tools": None})
        return models

    def close(self) -> None:
        with self._lock:
            while self._clients:
                _, client = self._clients.popitem()
                _close_quietly(client)


def _close_quietly(client: Any) -> None:
    try:
        client.close()
    except Exception:
        pass
