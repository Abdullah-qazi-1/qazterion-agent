"""OpenRouter: OpenAI-compatible, plus attribution headers and richer model metadata."""

from __future__ import annotations

from typing import Any

from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.exceptions import normalize_error


class OpenRouterAdapter(OpenAICompatibleAdapter):
    extra_headers = {"HTTP-Referer": "https://github.com/qazterion", "X-Title": "Qazterion"}

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
            extra = getattr(item, "model_extra", None) or {}
            params = extra.get("supported_parameters") or []
            context = extra.get("context_length")
            models.append({
                "id": model_id,
                "context_window": int(context) if context else None,
                "tools": ("tools" in params) if params else None,
            })
        return models

    def check_key(self, *, api_key: str, timeout: float = 10.0) -> int:
        # The model list is public on OpenRouter, so it cannot validate a key.
        # /key returns 401 for an invalid key and costs nothing.
        try:
            self._client(api_key, timeout).get("/key", cast_to=object)
        except Exception as error:
            raise normalize_error(error) from error
        return 1
