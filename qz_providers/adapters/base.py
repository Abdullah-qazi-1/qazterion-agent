"""Provider adapter interface.

An adapter knows how to talk to one provider *type* (wire protocol). It holds no
keys and no routing logic: the gateway passes the key for every call and
decides what to do with failures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from qz_providers.catalog import ProviderSpec


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Normalize a chat history so strict providers (e.g. Gemini) accept it.

    * a ``system`` message after the first turn becomes a user note;
    * ``content`` is always a string (``""`` when an assistant turn only has tool calls);
    * tool responses always carry a ``tool_call_id``.
    """
    clean: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content")
        if role == "system" and clean:
            role, content = "user", f"[System Note]: {content}"
        entry: dict[str, Any] = {"role": role}
        tool_calls = message.get("tool_calls")
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if content is not None:
            entry["content"] = content if isinstance(content, (str, list)) else str(content)
        else:
            entry["content"] = ""
        if role == "tool":
            entry["tool_call_id"] = str(message.get("tool_call_id") or "call_0")
        if message.get("name") and role != "tool":
            entry["name"] = message["name"]
        clean.append(entry)
    return clean


class ProviderAdapter(ABC):
    """Base class for provider wire-protocol adapters."""

    def __init__(self, spec: ProviderSpec) -> None:
        self.spec = spec

    @property
    def provider_id(self) -> str:
        return self.spec.provider_id

    @abstractmethod
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
        """Return an OpenAI-style chat completion or raise ``ProviderError``."""

    @abstractmethod
    def list_models(self, *, api_key: str, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Return ``[{"id": ..., "context_window": ..., "tools": ...}, ...]`` or raise ``ProviderError``."""

    def check_key(self, *, api_key: str, timeout: float = 10.0) -> int:
        """Validate a key cheaply. Returns the number of models visible; raises ``ProviderError``."""
        return len(self.list_models(api_key=api_key, timeout=timeout))

    def close(self) -> None:
        """Release network resources (connection pools)."""
