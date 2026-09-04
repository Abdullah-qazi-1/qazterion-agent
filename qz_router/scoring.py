"""Scoring function for ranking candidate (alias, physical_key) routes."""

from __future__ import annotations

from typing import Any

from qz_pool.models import KeyHealth

# ============================================================
# Default Scoring Weights (Sum = 1.0)
# ============================================================
CAPABILITY_MATCH_WEIGHT = 0.40
HEALTH_WEIGHT = 0.35
LATENCY_WEIGHT = 0.15
CONCURRENCY_WEIGHT = 0.10

# Capability fit matrix mapping (capability, alias) to a suitability score [0.0, 1.0]
CAPABILITY_FIT_MAP: dict[str, dict[str, float]] = {
    "simple": {
        "groq-fast": 1.0,
        "coder-backup": 0.85,
        "coder-strong": 0.70,
        "reasoner": 0.50,
        "classify": 0.80,
    },
    "complex": {
        "coder-strong": 1.0,
        "reasoner": 0.90,
        "coder-backup": 0.60,
        "groq-fast": 0.40,
    },
    "reasoner": {
        "reasoner": 1.0,
        "coder-strong": 0.85,
        "coder-backup": 0.50,
        "groq-fast": 0.30,
    },
    "debugging": {
        "reasoner": 1.0,
        "coder-strong": 0.85,
        "coder-backup": 0.50,
        "groq-fast": 0.30,
    },
    "planner": {
        "planner": 1.0,
        "coder-strong": 0.85,
        "groq-fast": 0.60,
    },
    "classify": {
        "classify": 1.0,
        "groq-fast": 0.80,
        "coder-backup": 0.60,
    },
    "groq-fast": {
        "groq-fast": 1.0,
        "coder-backup": 0.85,
        "coder-strong": 0.70,
        "reasoner": 0.50,
    },
    "coder-strong": {
        "coder-strong": 1.0,
        "reasoner": 0.90,
        "coder-backup": 0.60,
        "groq-fast": 0.40,
    },
    "coder-backup": {
        "coder-backup": 1.0,
        "groq-fast": 0.85,
        "coder-strong": 0.70,
        "reasoner": 0.50,
    },
}


def get_capability_fit(required_capability: str, alias: str) -> float:
    """Return how well a candidate model alias satisfies the required capability."""
    norm_cap = required_capability.lower()
    norm_alias = alias.lower()
    fit_table = CAPABILITY_FIT_MAP.get(norm_cap)
    if fit_table and norm_alias in fit_table:
        return fit_table[norm_alias]
    if norm_cap == norm_alias:
        return 1.0
    return 0.5


def calculate_route_score(
    required_capability: str,
    alias: str,
    key_id: str,
    health: KeyHealth | None,
    current_load_fraction: float = 0.0,
    *,
    capability_match_weight: float = CAPABILITY_MATCH_WEIGHT,
    health_weight: float = HEALTH_WEIGHT,
    latency_weight: float = LATENCY_WEIGHT,
    concurrency_weight: float = CONCURRENCY_WEIGHT,
    lifecycle_state: str | None = None,
) -> float:
    """Calculate an overall fitness score for a candidate (alias, physical_key) route.

    Higher score means more preferred route.
    """
    # 1. Capability Fit [0.0, 1.0]
    cap_fit = get_capability_fit(required_capability, alias)

    # 2. Health Score [0.0, 1.0]
    if health is None:
        health_score = 1.0  # Fresh, untested key is assumed healthy
    elif health.is_in_cooldown():
        health_score = 0.0
    else:
        total_requests = health.success_count + health.error_count
        recent_error_rate = (health.error_count / total_requests) if total_requests > 0 else 0.0
        health_score = max(0.0, 1.0 - recent_error_rate)

    # 3. Normalized Latency [0.0, 1.0] (4000ms+ is considered high latency ceiling)
    if health is None or health.average_latency_ms <= 0:
        norm_latency = 0.0
    else:
        norm_latency = min(1.0, health.average_latency_ms / 4000.0)

    # 4. Concurrency Load Fraction [0.0, 1.0]
    load_fraction = max(0.0, min(1.0, current_load_fraction))

    # 5. Model Lifecycle State Adjustment
    lifecycle_penalty = 0.0
    if lifecycle_state:
        state_norm = str(lifecycle_state).lower()
        if state_norm == "degraded":
            lifecycle_penalty = 0.25
        elif state_norm in ("unavailable", "removed", "deprecated"):
            lifecycle_penalty = 1.0

    # Composite weighted score
    score = (
        capability_match_weight * cap_fit
        + health_weight * health_score
        - latency_weight * norm_latency
        - concurrency_weight * load_fraction
        - lifecycle_penalty
    )
    return round(score, 4)
