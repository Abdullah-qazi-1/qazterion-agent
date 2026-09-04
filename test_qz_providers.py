"""Tests for Phase 12 Provider Abstraction, Adapters, Error Normalization, and ProviderRegistry."""

import pytest
from unittest.mock import MagicMock, patch

from qz_providers.exceptions import (
    AuthenticationError,
    RateLimitError,
    ServerError,
    TimeoutError,
    ContextLengthExceededError,
    ModelNotFoundError,
    ModelUnavailableError,
    ProviderError,
    normalize_error,
)
from qz_providers.models import ModelCapability, ModelLifecycleState, ModelMetadata, ProviderInfo
from qz_providers.adapters.base import BaseProviderAdapter
from qz_providers.adapters.openai_compatible import OpenAICompatibleAdapter
from qz_providers.adapters.groq import GroqAdapter
from qz_providers.adapters.mistral import MistralAdapter
from qz_providers.adapters.gemini import GeminiAdapter
from qz_providers.adapters.openrouter import OpenRouterAdapter
from qz_providers.adapters.deepseek import DeepSeekAdapter
from qz_providers.adapters import get_adapter_for_provider, register_adapter
from qz_providers.registry import (
    ProviderRegistry,
    get_provider_registry,
    reset_provider_registry,
)


class TestErrorNormalization:
    def test_normalize_401_403_auth_error(self):
        err = normalize_error("Invalid API Key provided", status_code=401)
        assert isinstance(err, AuthenticationError)
        assert not err.retryable
        assert err.status_code == 401

        err_forbidden = normalize_error("403 Forbidden: Permission denied")
        assert isinstance(err_forbidden, AuthenticationError)
        assert not err_forbidden.retryable

    def test_normalize_429_rate_limit(self):
        err = normalize_error("Rate limit reached for requests per minute", status_code=429)
        assert isinstance(err, RateLimitError)
        assert err.retryable
        assert err.status_code == 429

        err_quota = normalize_error("Insufficient quota for current plan")
        assert isinstance(err_quota, RateLimitError)
        assert err_quota.retryable

    def test_normalize_5xx_server_error(self):
        err = normalize_error("Internal Server Error", status_code=500)
        assert isinstance(err, ServerError)
        assert err.retryable

        err_503 = normalize_error("503 Service Unavailable")
        assert isinstance(err_503, ServerError)
        assert err_503.retryable

    def test_normalize_timeout(self):
        err = normalize_error("Request timed out after 45.0 seconds")
        assert isinstance(err, TimeoutError)
        assert err.retryable

    def test_normalize_context_length(self):
        err = normalize_error("maximum context length exceeded: 40000 > 32768")
        assert isinstance(err, ContextLengthExceededError)
        assert not err.retryable

    def test_normalize_model_not_found(self):
        err = normalize_error("Model 'unknown-model-xyz' not found", status_code=404)
        assert isinstance(err, ModelNotFoundError)
        assert not err.retryable

    def test_normalize_model_unavailable(self):
        err = normalize_error("Model is temporarily unavailable due to high load")
        assert isinstance(err, ModelUnavailableError)
        assert err.retryable


class TestAdapters:
    def test_adapter_factory(self):
        groq = get_adapter_for_provider("groq")
        assert isinstance(groq, GroqAdapter)
        assert groq.provider_id == "groq"
        assert "groq.com" in (groq.base_url or "")

        mistral = get_adapter_for_provider("mistral")
        assert isinstance(mistral, MistralAdapter)
        assert mistral.provider_id == "mistral"

        gemini = get_adapter_for_provider("gemini")
        assert isinstance(gemini, GeminiAdapter)
        assert gemini.provider_id == "gemini"

        openrouter = get_adapter_for_provider("openrouter")
        assert isinstance(openrouter, OpenRouterAdapter)
        assert openrouter.provider_id == "openrouter"

        deepseek = get_adapter_for_provider("deepseek")
        assert isinstance(deepseek, DeepSeekAdapter)
        assert deepseek.provider_id == "deepseek"

        custom = get_adapter_for_provider("custom_ai", base_url="https://custom.ai/v1")
        assert isinstance(custom, OpenAICompatibleAdapter)
        assert custom.provider_id == "custom_ai"
        assert custom.base_url == "https://custom.ai/v1"

    def test_static_models_populated(self):
        for provider_id in ("groq", "mistral", "gemini", "openrouter", "deepseek"):
            adapter = get_adapter_for_provider(provider_id)
            models = adapter.get_static_models()
            assert len(models) > 0
            for m in models:
                assert m.provider == provider_id
                assert m.model_id
                assert m.display_name
                assert m.context_window > 0

    def test_openai_compatible_complete_mock(self):
        adapter = OpenAICompatibleAdapter(base_url="http://localhost:4000", provider_id="test_prov")
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Hello world"))]
        mock_client.chat.completions.create.return_value = mock_response

        with patch.object(adapter, "_create_client", return_value=mock_client):
            res = adapter.complete(
                messages=[{"role": "user", "content": "Hi"}],
                model="test-model",
                api_key="sk-test-key",
            )
            assert res == mock_response
            mock_client.chat.completions.create.assert_called_once()

    def test_health_check(self):
        adapter = GroqAdapter()
        unconfigured = adapter.check_health(api_key=None)
        assert unconfigured["status"] == "unconfigured"

        configured = adapter.check_health(api_key="gsk_dummy")
        assert "status" in configured


class TestProviderRegistry:
    def setup_method(self):
        reset_provider_registry()

    def teardown_method(self):
        reset_provider_registry()

    def test_default_providers_loaded(self):
        reg = get_provider_registry()
        providers = reg.list_providers()
        prov_ids = {p.provider_id for p in providers}
        assert {"groq", "mistral", "gemini", "openrouter", "deepseek"}.issubset(prov_ids)

    def test_enable_disable_provider(self):
        reg = get_provider_registry()
        assert reg.is_provider_enabled("groq")

        assert reg.disable_provider("groq")
        assert not reg.is_provider_enabled("groq")

        assert reg.enable_provider("groq")
        assert reg.is_provider_enabled("groq")

    def test_register_custom_provider(self):
        reg = get_provider_registry()
        info = reg.register_provider(
            provider_id="together_ai",
            display_name="Together AI",
            base_url="https://api.together.xyz/v1",
            default_concurrency=4,
        )
        assert info.provider_id == "together_ai"
        assert info.display_name == "Together AI"
        assert reg.is_provider_enabled("together_ai")

        adapter = reg.get_adapter("together_ai")
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert adapter.base_url == "https://api.together.xyz/v1"

    def test_unregister_provider(self):
        reg = get_provider_registry()
        reg.register_provider("temp_prov", display_name="Temp")
        assert reg.get_provider_info("temp_prov") is not None
        assert reg.unregister_provider("temp_prov")
        assert reg.get_provider_info("temp_prov") is None
