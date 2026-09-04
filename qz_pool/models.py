"""Data models for physical LLM key pool, providers, and key health tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Provider:
    """A configured LLM provider family (e.g. 'groq', 'mistral', 'gemini')."""
    name: str
    display_name: str | None = None
    default_concurrency: int = 2


@dataclass
class APIKey:
    """A physical API key metadata record referencing encrypted keystore storage.

    Never holds raw plaintext key secret material.
    """
    id: str                 # e.g. "GROQ_KEY_1"
    provider: str           # e.g. "groq"
    key_reference: str      # reference name in qz_keystore (e.g. "GROQ_KEY_1")
    label: str              # display label, e.g. "Groq Key #1"
    enabled: bool = True


@dataclass
class KeyHealth:
    """Health metrics, error counters, and cooldown state for one physical API key."""
    key_id: str
    success_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    cooldown_until: float | None = None
    last_error: str | None = None
    last_error_type: str | None = None  # "rate_limit", "auth_error", "timeout", "server_error", "other"
    last_used_at: float | None = None
    average_latency_ms: float = 0.0
    total_latency_ms: float = 0.0

    def is_in_cooldown(self, now: float | None = None) -> bool:
        if self.cooldown_until is None:
            return False
        current = now if now is not None else time.time()
        return current < self.cooldown_until
