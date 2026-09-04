"""SmartRouter for selecting optimal (alias, physical_key) routes based on capabilities, health & concurrency."""

from __future__ import annotations

import sys
from typing import Any

from qz_pool import LLMPool, get_pool
from qz_pool.models import APIKey
from qz_router.scoring import calculate_route_score

_global_router: SmartRouter | None = None

# Sentinel score used to mark routes returned by the "all candidates unavailable"
# fallback path (see rank_routes). select_route uses this to avoid emitting a
# redundant ROUTER_DECISION event on top of the ROUTER_FALLBACK event already
# persisted by rank_routes.
_FALLBACK_SCORE_SENTINEL: float = -1.0

DEFAULT_CAPABILITY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "simple": ("groq-fast", "coder-backup", "coder-strong", "reasoner"),
    "complex": ("coder-strong", "groq-fast", "coder-backup", "reasoner"),
    "reasoner": ("reasoner", "coder-strong", "groq-fast", "coder-backup"),
    "debugging": ("reasoner", "coder-strong", "groq-fast", "coder-backup"),
    "groq-fast": ("groq-fast", "coder-backup", "reasoner"),
    "coder-strong": ("coder-strong", "groq-fast", "coder-backup", "reasoner"),
    "coder-backup": ("coder-backup", "groq-fast", "reasoner"),
    "planner": ("planner", "coder-strong", "groq-fast"),
    "classify": ("classify", "groq-fast"),
}


def _persist_event(task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
    if not task_id:
        return
    try:
        from qz_tasks.task_manager import log_event
        from qz_core.common import _persist_task
        _persist_task(task_id, lambda: log_event(task_id, event_type, payload))
    except Exception:
        pass


class SmartRouter:
    """Selects and ranks candidate (alias, physical_key) routes using live telemetry and model metadata."""

    def __init__(
        self,
        pool: LLMPool | None = None,
        capability_fallbacks: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._pool = pool
        self.capability_fallbacks = capability_fallbacks or DEFAULT_CAPABILITY_FALLBACKS

    @property
    def pool(self) -> LLMPool:
        return self._pool or get_pool()

    def rank_routes(
        self,
        required_capability: str,
        *,
        exclude: set[str] | list[str] | None = None,
        fallbacks: tuple[str, ...] | None = None,
        task_id: str | None = None,
        min_context: int = 0,
        requires_tools: bool = False,
        requires_reasoning: bool = False,
    ) -> list[tuple[str, str, float]]:
        """Return all available (alias, key_id, score) routes sorted by score descending."""
        norm_cap = required_capability.lower()
        if fallbacks is not None:
            candidate_aliases = list(fallbacks)
        else:
            candidate_aliases = list(self.capability_fallbacks.get(norm_cap, (required_capability,)))

        excluded = set(exclude or ())
        scored_candidates: list[tuple[str, str, float]] = []

        # Optional ModelRegistry integration
        model_registry = None
        provider_registry = None
        try:
            from qz_providers.model_registry import get_model_registry
            from qz_providers.registry import get_provider_registry
            model_registry = get_model_registry()
            provider_registry = get_provider_registry()
        except ImportError:
            pass

        for alias in candidate_aliases:
            if alias in excluded:
                continue

            model_meta = model_registry.get_model_by_alias(alias) if model_registry else None
            lifecycle_state = model_meta.state.value if model_meta else None

            # 1. Provider enablement check
            if model_meta and provider_registry:
                if not provider_registry.is_provider_enabled(model_meta.provider):
                    continue

            # 2. Lifecycle availability check (exclude UNAVAILABLE/REMOVED unless forced)
            if model_meta and not model_meta.is_available():
                continue

            # 3. Context window constraint check
            if min_context > 0 and model_meta and model_meta.context_window < min_context:
                continue

            # 4. Tool calling support check
            if requires_tools and model_meta and not model_meta.supports_tools:
                continue

            # 5. Reasoning support check
            if requires_reasoning and model_meta and not model_meta.supports_reasoning:
                # If specifically requested reasoning, deprioritize or exclude non-reasoning
                pass

            # Resolve physical keys for this alias from KeyRegistry
            physical_keys = self.pool.registry.get_keys_for_alias(alias)
            if not physical_keys:
                key_id = alias
                if key_id not in excluded and self.pool.health_manager.is_available(key_id):
                    health = self.pool.health_manager.get_health(key_id)
                    active = self.pool.concurrency.get_active_count(key_id)
                    limit = self.pool.concurrency.get_key_limit(key_id)
                    load = (active / limit) if limit > 0 else 0.0
                    score = calculate_route_score(
                        norm_cap, alias, key_id, health, load, lifecycle_state=lifecycle_state
                    )
                    scored_candidates.append((alias, key_id, score))
                continue

            for k in physical_keys:
                key_id = k.id
                if key_id in excluded:
                    continue
                if not k.enabled:
                    continue
                if not self.pool.health_manager.is_available(key_id):
                    continue
                if not self.pool.concurrency.can_acquire(key_id):
                    continue

                health = self.pool.health_manager.get_health(key_id)
                active = self.pool.concurrency.get_active_count(key_id)
                limit = self.pool.concurrency.get_key_limit(key_id)
                load = (active / limit) if limit > 0 else 0.0

                score = calculate_route_score(
                    norm_cap, alias, key_id, health, load, lifecycle_state=lifecycle_state
                )
                scored_candidates.append((alias, key_id, score))

        # Sort candidate routes by score descending
        scored_candidates.sort(key=lambda item: item[2], reverse=True)

        if not scored_candidates:
            # Full fallback path: all candidate keys in cooldown, disabled, or saturated
            default_aliases = [a for a in candidate_aliases if a not in excluded] or candidate_aliases or [("groq-fast" if norm_cap == "simple" else "coder-strong")]
            fallback_routes = []
            for d_alias in default_aliases:
                keys = self.pool.registry.get_keys_for_alias(d_alias)
                fb_key_id = keys[0].id if keys else d_alias
                fallback_routes.append((d_alias, fb_key_id, _FALLBACK_SCORE_SENTINEL))

            print(f"\033[93m[smart router] All candidate routes unavailable for '{required_capability}'; falling back to static alias order\033[0m")
            _persist_event(task_id, "ROUTER_FALLBACK", {
                "reason": "all_candidates_unavailable",
                "required_capability": required_capability,
                "fallback_alias": default_aliases[0],
                "fallback_key_id": fallback_routes[0][1],
            })
            return fallback_routes

        return scored_candidates

    def select_route(
        self,
        required_capability: str,
        *,
        exclude: set[str] | list[str] | None = None,
        fallbacks: tuple[str, ...] | None = None,
        task_id: str | None = None,
        min_context: int = 0,
        requires_tools: bool = False,
        requires_reasoning: bool = False,
    ) -> tuple[str, str]:
        """Select the single best available (alias, key_id) route."""
        ranked = self.rank_routes(
            required_capability,
            exclude=exclude,
            fallbacks=fallbacks,
            task_id=task_id,
            min_context=min_context,
            requires_tools=requires_tools,
            requires_reasoning=requires_reasoning,
        )
        best_alias, best_key_id, best_score = ranked[0]
        if best_score != _FALLBACK_SCORE_SENTINEL:
            _persist_event(task_id, "ROUTER_DECISION", {
                "required_capability": required_capability,
                "chosen_alias": best_alias,
                "chosen_key_id": best_key_id,
                "score": best_score,
                "candidates": [
                    {"alias": alias, "key_id": key_id, "score": score}
                    for alias, key_id, score in ranked[:5]
                ],
                "major_factors": ["capability", "key health", "concurrency", "model lifecycle", "observed latency"],
                "reason": "highest deterministic route score among eligible candidates",
            })
        return best_alias, best_key_id


def get_router() -> SmartRouter:
    """Return or initialize the singleton process-wide SmartRouter."""
    global _global_router
    if _global_router is None:
        _global_router = SmartRouter()
    return _global_router


def reset_router() -> None:
    """Reset the process-wide SmartRouter singleton."""
    global _global_router
    _global_router = None


def select_route(
    required_capability: str,
    *,
    exclude: set[str] | list[str] | None = None,
    fallbacks: tuple[str, ...] | None = None,
    task_id: str | None = None,
    min_context: int = 0,
    requires_tools: bool = False,
    requires_reasoning: bool = False,
) -> tuple[str, str]:
    """Convenience functional route selector using the default SmartRouter."""
    return get_router().select_route(
        required_capability,
        exclude=exclude,
        fallbacks=fallbacks,
        task_id=task_id,
        min_context=min_context,
        requires_tools=requires_tools,
        requires_reasoning=requires_reasoning,
    )
