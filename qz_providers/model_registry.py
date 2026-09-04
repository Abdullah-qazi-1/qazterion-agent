"""Dynamic Model Registry, Discovery, and Lifecycle Manager for Qazterion."""

from __future__ import annotations

import time
from typing import Any, Callable

from qz_providers.models import ModelCapability, ModelLifecycleState, ModelMetadata
from qz_providers.registry import ProviderRegistry, get_provider_registry


def _emit_event(task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
    try:
        from qz_tasks.task_manager import log_event
        from qz_core.common import _persist_task
        if task_id:
            _persist_task(task_id, lambda: log_event(task_id, event_type, payload))
    except Exception:
        pass


DEFAULT_ALIAS_MAP: dict[str, tuple[str, str]] = {
    # alias -> (provider, model_id)
    "groq-fast": ("groq", "openai/gpt-oss-20b"),
    "coder-backup": ("groq", "compound-mini"),
    "coder-strong": ("mistral", "mistral-large-latest"),
    "planner": ("mistral", "codestral-latest"),
    "reasoner": ("mistral", "mistral-medium-3.5"),
    "classify": ("gemini", "gemini-3.1-flash-lite"),
}


class ModelRegistry:
    """Central registry maintaining live and historical models across all providers."""

    def __init__(
        self,
        provider_registry: ProviderRegistry | None = None,
        degradation_threshold: int = 3,
        unavailable_threshold: int = 5,
    ) -> None:
        self._provider_registry = provider_registry
        self.degradation_threshold = degradation_threshold
        self.unavailable_threshold = unavailable_threshold
        # Key: (provider, model_id) -> ModelMetadata
        self._models: dict[tuple[str, str], ModelMetadata] = {}
        # Historical models: (provider, model_id) -> ModelMetadata
        self._historical_models: dict[tuple[str, str], ModelMetadata] = {}
        # Alias -> (provider, model_id)
        self._aliases: dict[str, tuple[str, str]] = dict(DEFAULT_ALIAS_MAP)
        self._listeners: list[Callable[[str, ModelMetadata], None]] = []
        self._load_static_catalogs()

    @property
    def provider_registry(self) -> ProviderRegistry:
        return self._provider_registry or get_provider_registry()

    def _load_static_catalogs(self) -> None:
        """Seed registry with static models from all registered providers."""
        for prov_info in self.provider_registry.list_providers():
            adapter = self.provider_registry.get_adapter(prov_info.provider_id)
            for model in adapter.get_static_models():
                key = (model.provider.lower(), model.model_id)
                self._models[key] = model

    def register_model(self, model: ModelMetadata, task_id: str | None = None) -> None:
        """Register or update model metadata."""
        key = (model.provider.lower(), model.model_id)
        self._models[key] = model
        _emit_event(task_id, "MODEL_REGISTERED", {
            "provider": model.provider,
            "model_id": model.model_id,
            "display_name": model.display_name,
            "state": model.state.value,
        })
        self._notify(model)

    def register_alias(self, alias: str, provider: str, model_id: str) -> None:
        """Map a routing alias to a specific (provider, model_id)."""
        self._aliases[alias.lower()] = (provider.lower(), model_id)

    def get_model(self, provider: str, model_id: str) -> ModelMetadata | None:
        """Retrieve model metadata by provider and model_id, checking active and historical records."""
        key = (provider.lower(), model_id)
        if key in self._models:
            return self._models[key]
        return self._historical_models.get(key)

    def get_model_by_alias(self, alias: str) -> ModelMetadata | None:
        """Retrieve model metadata given a high-level alias (e.g. 'groq-fast')."""
        norm = alias.lower()
        if norm in self._aliases:
            provider, model_id = self._aliases[norm]
            return self.get_model(provider, model_id)
        # Try direct match where alias itself is a model_id across all models
        for m in self._models.values():
            if m.model_id.lower() == norm or f"{m.provider}/{m.model_id}".lower() == norm:
                return m
        return None

    def list_models(
        self,
        provider: str | None = None,
        state: ModelLifecycleState | None = None,
        capability: str | ModelCapability | None = None,
        include_historical: bool = False,
    ) -> list[ModelMetadata]:
        """List models matching criteria."""
        candidates = list(self._models.values())
        if include_historical:
            for hist in self._historical_models.values():
                if (hist.provider.lower(), hist.model_id) not in self._models:
                    candidates.append(hist)

        results: list[ModelMetadata] = []
        for m in candidates:
            if provider and m.provider.lower() != provider.lower():
                continue
            if state is not None and m.state != state:
                continue
            if capability is not None and not m.has_capability(capability):
                continue
            results.append(m)
        return results

    def discover_models_for_provider(
        self,
        provider_id: str,
        api_key: str | None = None,
        task_id: str | None = None,
    ) -> list[ModelMetadata]:
        """Discover live models for a provider. Gracefully handles errors and preserves history."""
        p_id = provider_id.lower()
        adapter = self.provider_registry.get_adapter(p_id)
        try:
            discovered = adapter.discover_models(api_key=api_key)
            now = time.time()
            discovered_keys: set[tuple[str, str]] = set()

            for model in discovered:
                model.last_discovery_time = now
                key = (p_id, model.model_id)
                discovered_keys.add(key)
                if key in self._models:
                    # Preserve existing lifecycle state and failure history
                    prev = self._models[key]
                    model.state = prev.state
                    model.consecutive_failures = prev.consecutive_failures
                self._models[key] = model

            _emit_event(task_id, "MODEL_DISCOVERED", {
                "provider": p_id,
                "count": len(discovered),
            })
            return discovered
        except Exception as error:
            # Discovery failure must NEVER break the application
            _emit_event(task_id, "MODEL_DISCOVERY_FAILED", {
                "provider": p_id,
                "error": str(error),
            })
            return self.list_models(provider=p_id)

    def record_model_success(self, provider: str, model_id: str, task_id: str | None = None) -> None:
        """Record model success, recovering degraded state if needed."""
        key = (provider.lower(), model_id)
        model = self.get_model(provider, model_id)
        if model:
            model.consecutive_failures = 0
            if model.state == ModelLifecycleState.DEGRADED:
                model.state = ModelLifecycleState.ACTIVE
                _emit_event(task_id, "MODEL_STATE_CHANGED", {
                    "provider": provider,
                    "model_id": model_id,
                    "previous_state": "degraded",
                    "new_state": "active",
                    "reason": "successful_execution",
                })
                self._notify(model)

    def record_model_failure(
        self,
        provider: str,
        model_id: str,
        error: Exception | str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Record a failure for a model, transitioning lifecycle state if thresholds exceeded."""
        model = self.get_model(provider, model_id)
        if not model:
            return

        model.consecutive_failures += 1
        msg = str(error) if error else ""
        msg_lower = msg.lower()

        prev_state = model.state
        if "not found" in msg_lower or "does not exist" in msg_lower or "unknown model" in msg_lower:
            # Explicitly not found on upstream provider -> mark UNAVAILABLE
            model.state = ModelLifecycleState.UNAVAILABLE
        elif model.consecutive_failures >= self.unavailable_threshold:
            model.state = ModelLifecycleState.UNAVAILABLE
        elif model.consecutive_failures >= self.degradation_threshold:
            model.state = ModelLifecycleState.DEGRADED

        if model.state != prev_state:
            _emit_event(task_id, "MODEL_STATE_CHANGED", {
                "provider": provider,
                "model_id": model_id,
                "previous_state": prev_state.value,
                "new_state": model.state.value,
                "consecutive_failures": model.consecutive_failures,
                "error": msg,
            })
            self._notify(model)

    def set_model_lifecycle(
        self,
        provider: str,
        model_id: str,
        state: ModelLifecycleState,
        task_id: str | None = None,
    ) -> bool:
        """Explicitly set lifecycle state of a model."""
        model = self.get_model(provider, model_id)
        if not model:
            return False
        prev = model.state
        model.state = state
        if prev != state:
            _emit_event(task_id, "MODEL_STATE_CHANGED", {
                "provider": provider,
                "model_id": model_id,
                "previous_state": prev.value,
                "new_state": state.value,
            })
            self._notify(model)
        return True

    def preserve_historical_model(
        self,
        provider: str,
        model_id: str,
        display_name: str | None = None,
    ) -> ModelMetadata:
        """Ensure historical task execution records can always resolve this model."""
        key = (provider.lower(), model_id)
        existing = self.get_model(provider, model_id)
        if existing:
            return existing

        hist = ModelMetadata(
            provider=provider.lower(),
            model_id=model_id,
            display_name=display_name or f"{provider}/{model_id}",
            state=ModelLifecycleState.REMOVED,
            source="historical",
            historical=True,
        )
        self._historical_models[key] = hist
        return hist

    def subscribe(self, callback: Callable[[ModelMetadata], None]) -> None:
        self._listeners.append(callback)

    def _notify(self, model: ModelMetadata) -> None:
        for cb in self._listeners:
            try:
                cb(model)
            except Exception:
                pass


_global_model_registry: ModelRegistry | None = None


def get_model_registry() -> ModelRegistry:
    global _global_model_registry
    if _global_model_registry is None:
        _global_model_registry = ModelRegistry()
    return _global_model_registry


def reset_model_registry() -> None:
    global _global_model_registry
    _global_model_registry = None
