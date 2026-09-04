"""Per-key concurrency limiting for physical LLM keys."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

DEFAULT_MAX_CONCURRENCY = 2


class ConcurrencyLimitExceeded(RuntimeError):
    """Raised when no concurrency slot is available for a key."""


class ConcurrencyLimiter:
    """Manages active in-flight request counts and concurrency slots per physical key."""

    def __init__(self, default_max_concurrency: int = DEFAULT_MAX_CONCURRENCY) -> None:
        self.default_max_concurrency = max(1, int(default_max_concurrency))
        self._key_limits: dict[str, int] = {}
        self._active_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def set_key_limit(self, key_id: str, limit: int) -> None:
        with self._lock:
            self._key_limits[key_id] = max(1, int(limit))

    def get_key_limit(self, key_id: str) -> int:
        with self._lock:
            return self._key_limits.get(key_id, self.default_max_concurrency)

    def get_active_count(self, key_id: str) -> int:
        with self._lock:
            return self._active_counts.get(key_id, 0)

    def can_acquire(self, key_id: str) -> bool:
        """Check whether a slot is currently open without acquiring it."""
        with self._lock:
            limit = self._key_limits.get(key_id, self.default_max_concurrency)
            active = self._active_counts.get(key_id, 0)
            return active < limit

    def acquire(self, key_id: str) -> bool:
        """Attempt to acquire an execution slot for the specified key immediately."""
        with self._lock:
            limit = self._key_limits.get(key_id, self.default_max_concurrency)
            active = self._active_counts.get(key_id, 0)
            if active < limit:
                self._active_counts[key_id] = active + 1
                return True
            return False

    def release(self, key_id: str) -> None:
        """Release an execution slot for the specified key."""
        with self._lock:
            active = self._active_counts.get(key_id, 0)
            if active > 0:
                self._active_counts[key_id] = active - 1
            else:
                self._active_counts[key_id] = 0

    @contextmanager
    def slot(self, key_id: str) -> Iterator[None]:
        """Context manager to acquire and automatically release a concurrency slot."""
        acquired = self.acquire(key_id)
        if not acquired:
            raise ConcurrencyLimitExceeded(
                f"Concurrency limit reached for key '{key_id}' "
                f"(active: {self.get_active_count(key_id)}/{self.get_key_limit(key_id)})"
            )
        try:
            yield
        finally:
            self.release(key_id)
