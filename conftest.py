"""Repository-wide pytest configuration.

Several subsystems (LLM connection pool, smart router, provider registry,
model registry) are implemented as process-wide singletons so production
code can call `get_pool()` / `get_router()` / etc. without threading state
through every function call. That is convenient at runtime, but it means
state mutated by one test module (disabled keys, rate-limit cooldowns,
degraded models, disabled providers, ...) can silently leak into unrelated
tests that happen to run afterwards in the same pytest process.

This autouse fixture resets every known singleton before each test so the
full suite behaves the same regardless of test file/order, matching the
isolation that individual test files already got when they remembered to
call the relevant `reset_*()` helper in their own setUp.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_global_singletons():
    def _reset_all():
        try:
            from qz_pool.pool import reset_pool
            reset_pool()
        except Exception:
            pass
        try:
            from qz_router.router import reset_router
            reset_router()
        except Exception:
            pass
        try:
            from qz_providers.model_registry import reset_model_registry
            reset_model_registry()
        except Exception:
            pass
        try:
            from qz_providers.registry import reset_provider_registry
            reset_provider_registry()
        except Exception:
            pass
        try:
            from qz_sandbox.manager import reset_manager
            reset_manager()
        except Exception:
            pass

    _reset_all()
    yield
    _reset_all()