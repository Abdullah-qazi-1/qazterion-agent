"""Tests for Phase 12 Capability-Aware Routing, Provider/Model Fallbacks, and Multi-Tier Failover."""

import pytest
from unittest.mock import MagicMock

from qz_pool import LLMPool, reset_pool
from qz_pool.models import APIKey
from qz_providers.models import ModelLifecycleState
from qz_providers.model_registry import get_model_registry, reset_model_registry
from qz_providers.registry import get_provider_registry, reset_provider_registry
from qz_router.router import SmartRouter, get_router, reset_router
from qz_router.scoring import calculate_route_score


class TestCapabilityAwareRouting:
    def setup_method(self):
        reset_pool()
        reset_router()
        reset_provider_registry()
        reset_model_registry()

    def teardown_method(self):
        reset_pool()
        reset_router()
        reset_provider_registry()
        reset_model_registry()

    def test_disabled_provider_excluded_from_routes(self):
        pool = LLMPool()
        # Register keys for groq and mistral
        pool.registry.register_key(APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq Key 1"))
        pool.registry.register_key(APIKey(id="MISTRAL_KEY_1", provider="mistral", key_reference="MISTRAL_KEY_1", label="Mistral Key 1"))

        router = SmartRouter(pool=pool)
        prov_reg = get_provider_registry()

        # Disable groq
        prov_reg.disable_provider("groq")

        routes = router.rank_routes("simple")
        # Groq-fast should be excluded because groq provider is disabled
        aliases = [r[0] for r in routes]
        assert "groq-fast" not in aliases
        assert "coder-strong" in aliases or "reasoner" in aliases

    def test_unavailable_model_excluded_from_routes(self):
        pool = LLMPool()
        pool.registry.register_key(APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq Key 1"))
        pool.registry.register_key(APIKey(id="MISTRAL_KEY_1", provider="mistral", key_reference="MISTRAL_KEY_1", label="Mistral Key 1"))

        router = SmartRouter(pool=pool)
        model_reg = get_model_registry()

        # Mark groq-fast as UNAVAILABLE
        model_reg.set_model_lifecycle("groq", "openai/gpt-oss-20b", ModelLifecycleState.UNAVAILABLE)

        routes = router.rank_routes("simple")
        aliases = [r[0] for r in routes]
        assert "groq-fast" not in aliases

    def test_degraded_model_penalized_in_score(self):
        pool = LLMPool()
        pool.registry.register_key(APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq Key 1"))
        pool.registry.register_key(APIKey(id="MISTRAL_KEY_1", provider="mistral", key_reference="MISTRAL_KEY_1", label="Mistral Key 1"))

        router = SmartRouter(pool=pool)
        model_reg = get_model_registry()

        # Normal active routes
        routes_active = router.rank_routes("simple")
        score_active_groq = next(r[2] for r in routes_active if r[0] == "groq-fast")

        # Degrade groq-fast
        model_reg.set_model_lifecycle("groq", "openai/gpt-oss-20b", ModelLifecycleState.DEGRADED)

        routes_degraded = router.rank_routes("simple")
        score_degraded_groq = next(r[2] for r in routes_degraded if r[0] == "groq-fast")

        # Score must be lower due to degradation penalty
        assert score_degraded_groq < score_active_groq

    def test_context_window_filtering(self):
        pool = LLMPool()
        pool.registry.register_key(APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq Key 1"))
        pool.registry.register_key(APIKey(id="MISTRAL_KEY_1", provider="mistral", key_reference="MISTRAL_KEY_1", label="Mistral Key 1"))

        router = SmartRouter(pool=pool)
        # groq-fast has context 32768, mistral-large-latest (coder-strong) has 128000
        # If min_context is 60000, groq-fast must be filtered out
        routes = router.rank_routes("simple", min_context=60000)
        aliases = [r[0] for r in routes]
        assert "groq-fast" not in aliases
        assert "coder-strong" in aliases

    def test_multi_tier_fallback_key_to_model_to_provider(self):
        pool = LLMPool()
        # 2 Groq keys + 1 Mistral key
        pool.registry.register_key(APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq Key 1"))
        pool.registry.register_key(APIKey(id="GROQ_KEY_2", provider="groq", key_reference="GROQ_KEY_2", label="Groq Key 2"))
        pool.registry.register_key(APIKey(id="MISTRAL_KEY_1", provider="mistral", key_reference="MISTRAL_KEY_1", label="Mistral Key 1"))

        router = SmartRouter(pool=pool)

        # 1. First choice: best key for groq-fast (GROQ_KEY_1 or 2)
        route_alias, key_id = router.select_route("simple")
        assert route_alias == "groq-fast"
        assert key_id in ("GROQ_KEY_1", "GROQ_KEY_2")

        # 2. Key cooldown: GROQ_KEY_1 gets 429 rate limited
        pool.health_manager.record_failure(key_id, error_type="rate_limit")

        # Next route should use the second physical key of groq-fast
        route_alias_2, key_id_2 = router.select_route("simple")
        assert route_alias_2 == "groq-fast"
        assert key_id_2 != key_id

        # 3. Model failover: Both groq keys in cooldown
        pool.health_manager.record_failure(key_id_2, error_type="rate_limit")

        # Next route should failover to another provider/model (Mistral / coder-strong / coder-backup)
        route_alias_3, key_id_3 = router.select_route("simple")
        assert key_id_3 == "MISTRAL_KEY_1"
        assert route_alias_3 in ("coder-strong", "coder-backup", "reasoner")
