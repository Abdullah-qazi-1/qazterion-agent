"""Dynamic Provider Registry for Qazterion."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from qz_providers.adapters import BaseProviderAdapter, get_adapter_for_provider
from qz_providers.models import ProviderInfo


def _emit_event(task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
    try:
        from qz_tasks.task_manager import log_event
        from qz_core.common import _persist_task
        if task_id:
            _persist_task(task_id, lambda: log_event(task_id, event_type, payload))
    except Exception:
        pass


class ProviderRegistry:
    """Central registry managing LLM providers, enablement state, and adapter instances."""

    DEFAULT_PROVIDERS = ("groq", "mistral", "gemini", "openrouter", "deepseek")

    def __init__(self, config_path: Path | str | None = None) -> None:
        self.config_path = Path(config_path) if config_path else Path("config.yaml")
        self._providers: dict[str, ProviderInfo] = {}
        self._adapters: dict[str, BaseProviderAdapter] = {}
        self._listeners: list[Callable[[str, ProviderInfo], None]] = []
        self._init_defaults()

    def _init_defaults(self) -> None:
        for p_id in self.DEFAULT_PROVIDERS:
            adapter = get_adapter_for_provider(p_id)
            self._adapters[p_id] = adapter
            self._providers[p_id] = adapter.get_provider_info(enabled=True)

    def register_provider(
        self,
        provider_id: str,
        display_name: str | None = None,
        adapter: BaseProviderAdapter | None = None,
        base_url: str | None = None,
        default_concurrency: int = 2,
        enabled: bool = True,
        task_id: str | None = None,
    ) -> ProviderInfo:
        """Register or update a provider in the registry."""
        p_id = provider_id.lower().strip()
        if not p_id:
            raise ValueError("Provider ID cannot be empty.")

        if adapter is not None:
            self._adapters[p_id] = adapter
        elif p_id not in self._adapters:
            self._adapters[p_id] = get_adapter_for_provider(p_id, base_url=base_url)

        inst = self._adapters[p_id]
        d_name = display_name or inst.display_name or p_id.capitalize()
        b_url = base_url or inst.base_url
        models = [m.model_id for m in inst.get_static_models()]

        info = ProviderInfo(
            provider_id=p_id,
            display_name=d_name,
            enabled=enabled,
            adapter_type=inst.__class__.__name__,
            base_url=b_url,
            default_concurrency=default_concurrency,
            supported_capabilities=["chat", "streaming", "tool_calling"],
            models=models,
        )
        self._providers[p_id] = info
        _emit_event(task_id, "PROVIDER_REGISTERED", {"provider": p_id, "display_name": d_name})
        self._notify(p_id, info)
        return info

    def unregister_provider(self, provider_id: str) -> bool:
        p_id = provider_id.lower().strip()
        if p_id in self._providers:
            del self._providers[p_id]
            self._adapters.pop(p_id, None)
            return True
        return False

    def enable_provider(self, provider_id: str, task_id: str | None = None) -> bool:
        p_id = provider_id.lower().strip()
        if p_id in self._providers:
            self._providers[p_id].enabled = True
            _emit_event(task_id, "PROVIDER_ENABLED", {"provider": p_id})
            self._notify(p_id, self._providers[p_id])
            return True
        return False

    def disable_provider(self, provider_id: str, task_id: str | None = None) -> bool:
        p_id = provider_id.lower().strip()
        if p_id in self._providers:
            self._providers[p_id].enabled = False
            _emit_event(task_id, "PROVIDER_DISABLED", {"provider": p_id})
            self._notify(p_id, self._providers[p_id])
            return True
        return False

    def is_provider_enabled(self, provider_id: str) -> bool:
        p_id = provider_id.lower().strip()
        info = self._providers.get(p_id)
        return bool(info.enabled) if info else False

    def get_provider_info(self, provider_id: str) -> ProviderInfo | None:
        return self._providers.get(provider_id.lower().strip())

    def get_adapter(self, provider_id: str) -> BaseProviderAdapter:
        p_id = provider_id.lower().strip()
        if p_id not in self._adapters:
            self._adapters[p_id] = get_adapter_for_provider(p_id)
        return self._adapters[p_id]

    def list_providers(self, enabled_only: bool = False) -> list[ProviderInfo]:
        providers = list(self._providers.values())
        if enabled_only:
            return [p for p in providers if p.enabled]
        return providers

    def subscribe(self, callback: Callable[[str, ProviderInfo], None]) -> None:
        self._listeners.append(callback)

    def _notify(self, provider_id: str, info: ProviderInfo) -> None:
        for cb in self._listeners:
            try:
                cb(provider_id, info)
            except Exception:
                pass


_global_provider_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    global _global_provider_registry
    if _global_provider_registry is None:
        _global_provider_registry = ProviderRegistry()
    return _global_provider_registry


def reset_provider_registry() -> None:
    global _global_provider_registry
    _global_provider_registry = None
