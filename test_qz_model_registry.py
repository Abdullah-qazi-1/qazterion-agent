"""Tests for Phase 12 Dynamic Model Registry, Capabilities, Discovery, and Lifecycle States."""

import pytest
from unittest.mock import MagicMock, patch

from qz_providers.models import ModelCapability, ModelLifecycleState, ModelMetadata
from qz_providers.model_registry import (
    ModelRegistry,
    get_model_registry,
    reset_model_registry,
)
from qz_providers.registry import reset_provider_registry


class TestModelRegistry:
    def setup_method(self):
        reset_provider_registry()
        reset_model_registry()

    def teardown_method(self):
        reset_provider_registry()
        reset_model_registry()

    def test_static_models_seeded(self):
        reg = get_model_registry()
        models = reg.list_models()
        assert len(models) >= 10

        groq_fast = reg.get_model_by_alias("groq-fast")
        assert groq_fast is not None
        assert groq_fast.provider == "groq"
        assert groq_fast.supports_tools
        assert groq_fast.state == ModelLifecycleState.ACTIVE

        coder_strong = reg.get_model_by_alias("coder-strong")
        assert coder_strong is not None
        assert coder_strong.provider == "mistral"
        assert coder_strong.supports_reasoning

    def test_capabilities_querying(self):
        reg = get_model_registry()
        tool_models = reg.list_models(capability="tool_calling")
        assert len(tool_models) > 0
        for m in tool_models:
            assert m.supports_tools

        reasoning_models = reg.list_models(capability="reasoning")
        assert len(reasoning_models) > 0
        for m in reasoning_models:
            assert m.supports_reasoning

        vision_models = reg.list_models(capability="vision")
        assert len(vision_models) > 0
        for m in vision_models:
            assert m.supports_vision

    def test_lifecycle_degradation_and_recovery(self):
        reg = get_model_registry()
        model = reg.get_model("groq", "openai/gpt-oss-20b")
        assert model is not None
        assert model.state == ModelLifecycleState.ACTIVE

        # 1-2 failures: still ACTIVE
        reg.record_model_failure("groq", "openai/gpt-oss-20b", "500 Internal Error")
        assert model.state == ModelLifecycleState.ACTIVE
        assert model.consecutive_failures == 1

        reg.record_model_failure("groq", "openai/gpt-oss-20b", "500 Internal Error")
        assert model.state == ModelLifecycleState.ACTIVE
        assert model.consecutive_failures == 2

        # 3 failures (threshold): DEGRADED
        reg.record_model_failure("groq", "openai/gpt-oss-20b", "500 Internal Error")
        assert model.state == ModelLifecycleState.DEGRADED
        assert model.consecutive_failures == 3

        # Success recovers back to ACTIVE
        reg.record_model_success("groq", "openai/gpt-oss-20b")
        assert model.state == ModelLifecycleState.ACTIVE
        assert model.consecutive_failures == 0

    def test_lifecycle_unavailable_on_repeated_failures_or_not_found(self):
        reg = get_model_registry()
        model = reg.get_model("groq", "openai/gpt-oss-20b")
        assert model is not None

        # Model not found upstream -> immediately UNAVAILABLE
        reg.record_model_failure("groq", "openai/gpt-oss-20b", "Model not found upstream (404)")
        assert model.state == ModelLifecycleState.UNAVAILABLE
        assert not model.is_available()

    def test_explicit_lifecycle_transition(self):
        reg = get_model_registry()
        assert reg.set_model_lifecycle("groq", "llama-3.3-70b-versatile", ModelLifecycleState.DEPRECATED)
        m = reg.get_model("groq", "llama-3.3-70b-versatile")
        assert m.state == ModelLifecycleState.DEPRECATED
        assert not m.is_available()

    def test_historical_model_preservation(self):
        reg = get_model_registry()
        hist = reg.preserve_historical_model("legacy_provider", "gpt-3.5-turbo-old", "Legacy GPT-3.5")
        assert hist.provider == "legacy_provider"
        assert hist.model_id == "gpt-3.5-turbo-old"
        assert hist.historical

        # Can be fetched anytime
        fetched = reg.get_model("legacy_provider", "gpt-3.5-turbo-old")
        assert fetched is not None
        assert fetched.display_name == "Legacy GPT-3.5"

    def test_dynamic_discovery_success_and_fallback(self):
        reg = get_model_registry()
        mock_discovered = [
            ModelMetadata(
                provider="groq",
                model_id="llama-4-scifi-preview",
                display_name="Llama 4 SciFi Preview",
                capabilities=["chat", "streaming", "tool_calling"],
                context_window=256000,
                source="discovery",
            )
        ]

        with patch.object(reg.provider_registry.get_adapter("groq"), "discover_models", return_value=mock_discovered):
            discovered = reg.discover_models_for_provider("groq", api_key="gsk_mock")
            assert len(discovered) == 1
            assert discovered[0].model_id == "llama-4-scifi-preview"

            # Added to registry
            m = reg.get_model("groq", "llama-4-scifi-preview")
            assert m is not None
            assert m.context_window == 256000

    def test_dynamic_discovery_failure_does_not_crash(self):
        reg = get_model_registry()
        with patch.object(reg.provider_registry.get_adapter("groq"), "discover_models", side_effect=RuntimeError("Network down")):
            # Must return existing/static models and not raise
            res = reg.discover_models_for_provider("groq", api_key="gsk_mock")
            assert len(res) > 0
            # Static models intact
            assert reg.get_model("groq", "llama-3.3-70b-versatile") is not None
