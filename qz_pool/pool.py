"""LLMPool façade coordinating key registry, health tracking, and concurrency."""

from __future__ import annotations

import time
from typing import Any, Callable

from qz_pool.concurrency import ConcurrencyLimiter, ConcurrencyLimitExceeded
from qz_pool.health_manager import HealthManager
from qz_pool.models import APIKey, KeyHealth
from qz_pool.registry import KeyRegistry

_global_pool: LLMPool | None = None


class LLMPool:
    """Coordinates key discovery, health tracking, concurrency slots, and resilient pool execution."""

    def __init__(
        self,
        registry: KeyRegistry | None = None,
        health_manager: HealthManager | None = None,
        concurrency: ConcurrencyLimiter | None = None,
    ) -> None:
        self.registry = registry or KeyRegistry()
        self.health_manager = health_manager or HealthManager(registry=self.registry)
        self.concurrency = concurrency or ConcurrencyLimiter()

    def get_candidate_keys(self, provider: str) -> list[str]:
        """Return available keys for a provider ordered by health (lowest errors & latency first)."""
        keys = self.registry.get_keys_by_provider(provider)
        if not keys:
            return []

        # Filter to keys that are enabled, not in cooldown, and have open concurrency slots
        available_keys: list[APIKey] = []
        for k in keys:
            if k.enabled and self.health_manager.is_available(k.id) and self.concurrency.can_acquire(k.id):
                available_keys.append(k)

        if not available_keys:
            # Fallback: keys that are enabled and not disabled by auth, even if in soft cooldown or busy
            for k in keys:
                if k.enabled:
                    h = self.health_manager.get_health(k.id)
                    if h.last_error_type != "auth_error":
                        available_keys.append(k)

        # Health scoring function:
        # 1. Consecutive errors (ascending: 0 is best)
        # 2. Total error count (ascending)
        # 3. Average latency in ms (ascending)
        def health_score(k: APIKey) -> tuple[int, int, float]:
            h = self.health_manager.get_health(k.id)
            return (h.consecutive_errors, h.error_count, h.average_latency_ms)

        available_keys.sort(key=health_score)
        return [k.id for k in available_keys]

    def execute_with_pool(
        self,
        provider: str,
        request_fn: Callable[[str], Any],
        max_attempts: int = 2,
    ) -> tuple[Any, str]:
        """Execute a request function using the healthiest available key for a provider.

        Handles concurrency slots, records telemetry and health metrics, and
        fails over to next candidate key on retryable failure.
        """
        candidates = self.get_candidate_keys(provider)
        if not candidates:
            raise RuntimeError(f"No usable keys available for provider '{provider}'.")

        attempts = 0
        last_exception: Exception | None = None

        for key_id in candidates:
            if attempts >= max_attempts:
                break
            attempts += 1

            started = time.monotonic()
            try:
                with self.concurrency.slot(key_id):
                    result = request_fn(key_id)
                    latency_ms = (time.monotonic() - started) * 1000.0
                    self.health_manager.record_success(key_id, latency_ms)
                    return result, key_id

            except ConcurrencyLimitExceeded as e:
                last_exception = e
                continue

            except Exception as error:
                latency_ms = (time.monotonic() - started) * 1000.0
                err_type = self.health_manager.classify_error(error)
                self.health_manager.record_failure(
                    key_id,
                    error_type=err_type,
                    latency_ms=latency_ms,
                    error_message=str(error),
                )
                last_exception = error

        raise RuntimeError(
            f"All {attempts} pool execution attempt(s) for provider '{provider}' failed. Last error: {last_exception}"
        ) from last_exception


def get_pool() -> LLMPool:
    """Return or initialize the singleton process-wide LLMPool."""
    global _global_pool
    if _global_pool is None:
        _global_pool = LLMPool()
    return _global_pool


def reset_pool() -> None:
    """Reset the process-wide LLMPool singleton (primarily for testing)."""
    global _global_pool
    _global_pool = None
