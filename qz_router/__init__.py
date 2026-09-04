"""Qazterion Smart Router Package.

Provides intelligent model and physical key selection based on capability matching,
live health metrics, latency, and concurrency slot availability.
"""

from qz_router.scoring import (
    CAPABILITY_MATCH_WEIGHT,
    HEALTH_WEIGHT,
    LATENCY_WEIGHT,
    CONCURRENCY_WEIGHT,
    CAPABILITY_FIT_MAP,
    calculate_route_score,
    get_capability_fit,
)
from qz_router.router import (
    SmartRouter,
    get_router,
    reset_router,
    select_route,
    DEFAULT_CAPABILITY_FALLBACKS,
)

__all__ = [
    "CAPABILITY_MATCH_WEIGHT",
    "HEALTH_WEIGHT",
    "LATENCY_WEIGHT",
    "CONCURRENCY_WEIGHT",
    "CAPABILITY_FIT_MAP",
    "calculate_route_score",
    "get_capability_fit",
    "SmartRouter",
    "get_router",
    "reset_router",
    "select_route",
    "DEFAULT_CAPABILITY_FALLBACKS",
]
