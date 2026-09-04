"""Health tracking, latency computation, cooldown backoff, and error tracking per physical key."""

from __future__ import annotations

import time
from typing import Any

from qz_pool.models import KeyHealth
from qz_pool.registry import KeyRegistry

# Default cooldown configuration
_BASE_COOLDOWN_SECONDS = 15.0       # 15s initial cooldown on rate limit
_MAX_COOLDOWN_SECONDS = 300.0       # 5 minutes max exponential backoff cap


class HealthManager:
    """Tracks latency, consecutive errors, backoff cooldowns, and availability for physical keys."""

    def __init__(
        self,
        registry: KeyRegistry | None = None,
        base_cooldown_seconds: float = _BASE_COOLDOWN_SECONDS,
        max_cooldown_seconds: float = _MAX_COOLDOWN_SECONDS,
    ) -> None:
        self.registry = registry
        self.base_cooldown_seconds = base_cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self._health: dict[str, KeyHealth] = {}

    def _get_or_create(self, key_id: str) -> KeyHealth:
        if key_id not in self._health:
            self._health[key_id] = KeyHealth(key_id=key_id)
        return self._health[key_id]

    @staticmethod
    def classify_error(error: Exception | str | int | None) -> str:
        """Classify errors into standard categories: rate_limit, auth_error, timeout, server_error, other."""
        if error is None:
            return "other"
        msg = str(error).lower()
        if any(marker in msg for marker in ("429", "rate limit", "ratelimit", "quota", "insufficient_quota", "too many requests")):
            return "rate_limit"
        if any(marker in msg for marker in ("401", "403", "unauthorized", "authentication", "invalid_api_key", "invalid api key", "forbidden", "permission denied")):
            return "auth_error"
        if any(marker in msg for marker in ("timeout", "timed out", "deadline", "timedout", "connection timeout")):
            return "timeout"
        if any(marker in msg for marker in ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable", "internal server error")):
            return "server_error"
        return "other"

    def record_success(self, key_id: str, latency_ms: float = 0.0) -> None:
        """Record a successful request, clear consecutive error count and cooldown."""
        health = self._get_or_create(key_id)
        health.success_count += 1
        health.consecutive_errors = 0
        health.cooldown_until = None
        health.last_used_at = time.time()
        health.total_latency_ms += max(0.0, latency_ms)
        health.average_latency_ms = health.total_latency_ms / health.success_count

    def record_failure(
        self,
        key_id: str,
        error_type: str,
        latency_ms: float = 0.0,
        error_message: str | None = None,
    ) -> None:
        """Record a failed request, compute cooldown backoff, or disable on auth failure."""
        health = self._get_or_create(key_id)
        health.error_count += 1
        health.consecutive_errors += 1
        health.last_error = error_message
        health.last_error_type = error_type
        health.last_used_at = time.time()
        if latency_ms > 0:
            health.total_latency_ms += latency_ms

        now = time.time()
        if error_type == "auth_error":
            # Permanently disable key in registry and set indefinite cooldown
            health.cooldown_until = float("inf")
            if self.registry:
                self.registry.disable_key(key_id)
            print(f"\033[91m[qz_pool] Key '{key_id}' disabled due to authentication error ({error_message})\033[0m")

        elif error_type == "rate_limit":
            # Exponential backoff: base * 2^(consecutive_errors - 1), capped at max_cooldown
            delay = min(
                self.max_cooldown_seconds,
                self.base_cooldown_seconds * (2 ** max(0, health.consecutive_errors - 1)),
            )
            health.cooldown_until = now + delay
            print(f"\033[93m[qz_pool] Key '{key_id}' in rate-limit cooldown for {delay:.1f}s (streak: {health.consecutive_errors})\033[0m")

        elif error_type == "server_error" and health.consecutive_errors >= 2:
            # Repeated server errors back off moderately
            delay = min(
                self.max_cooldown_seconds,
                self.base_cooldown_seconds * (2 ** max(0, health.consecutive_errors - 2)),
            )
            health.cooldown_until = now + delay
            print(f"\033[93m[qz_pool] Key '{key_id}' in server-error cooldown for {delay:.1f}s (streak: {health.consecutive_errors})\033[0m")

        elif error_type == "timeout" and health.consecutive_errors >= 3:
            # Consecutive timeouts back off briefly
            delay = min(self.max_cooldown_seconds, self.base_cooldown_seconds)
            health.cooldown_until = now + delay
            print(f"\033[93m[qz_pool] Key '{key_id}' in timeout cooldown for {delay:.1f}s\033[0m")

    def is_available(self, key_id: str, now: float | None = None) -> bool:
        """Return True if the key is enabled and not currently in cooldown."""
        if self.registry:
            key_record = self.registry.get_key(key_id)
            if key_record is not None and not key_record.enabled:
                return False
        health = self._health.get(key_id)
        if health is None:
            return True
        return not health.is_in_cooldown(now)

    def get_health(self, key_id: str) -> KeyHealth:
        return self._get_or_create(key_id)

    def get_provider_health_summary(self, provider: str) -> dict[str, Any]:
        """Aggregate health metrics across all physical keys for a provider."""
        keys = self.registry.get_keys_by_provider(provider) if self.registry else []
        total_keys = len(keys)
        available_count = 0
        in_cooldown_count = 0
        disabled_count = 0
        total_successes = 0
        total_errors = 0
        total_latencies = 0.0
        success_keys = 0

        for k in keys:
            if not k.enabled:
                disabled_count += 1
            elif not self.is_available(k.id):
                in_cooldown_count += 1
            else:
                available_count += 1

            h = self._health.get(k.id)
            if h:
                total_successes += h.success_count
                total_errors += h.error_count
                if h.average_latency_ms > 0:
                    total_latencies += h.average_latency_ms
                    success_keys += 1

        avg_latency = (total_latencies / success_keys) if success_keys > 0 else 0.0
        return {
            "provider": provider,
            "total_keys": total_keys,
            "available_keys": available_count,
            "in_cooldown_keys": in_cooldown_count,
            "disabled_keys": disabled_count,
            "total_successes": total_successes,
            "total_errors": total_errors,
            "average_latency_ms": round(avg_latency, 2),
        }
