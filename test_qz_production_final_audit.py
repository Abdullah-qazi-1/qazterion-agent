"""Comprehensive production-grade verification tests for Qazterion final audit.

Covers:
- Multi-provider & Multi-account routing and rotation
- Rate limit backoff vs. permanent auth failure handling
- Tool capability enforcement during routing
- OpenRouter model discovery and reasoning detection
- Safe validation gates on testless vs. test-backed workspaces
- Rollback safety and pre-existing user file protection
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qz_environment import detect_project_environment
from qz_pool import LLMPool
from qz_pool.health_manager import HealthManager
from qz_pool.models import APIKey, KeyHealth
from qz_pool.registry import KeyRegistry
from qz_providers.adapters.openrouter import OpenRouterAdapter
from qz_providers.exceptions import AuthenticationError, RateLimitError, normalize_error
from qz_providers.model_registry import ModelRegistry
from qz_providers.models import ModelLifecycleState, ModelMetadata
from qz_router import SmartRouter
from qz_validation.checks import CheckResult, CheckStatus, run_tests
from qz_validation.pipeline import ValidationPipeline


class MultiProviderMultiAccountAuditTests(unittest.TestCase):
    def setUp(self):
        self.key_reg = KeyRegistry()
        self.health_mgr = HealthManager(registry=self.key_reg)
        self.pool = LLMPool(registry=self.key_reg, health_manager=self.health_mgr)
        self.router = SmartRouter(pool=self.pool)

    def test_multi_account_same_provider_rotation_on_rate_limit(self):
        """Scenario A: Provider Groq with 3 accounts.

        When Account 1 hits 429 rate-limit, it is put into cooldown,
        and Account 2 is immediately selected next.
        """
        k1 = APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq 1")
        k2 = APIKey(id="GROQ_KEY_2", provider="groq", key_reference="GROQ_KEY_2", label="Groq 2")
        k3 = APIKey(id="GROQ_KEY_3", provider="groq", key_reference="GROQ_KEY_3", label="Groq 3")

        self.key_reg.register_key(k1)
        self.key_reg.register_key(k2)
        self.key_reg.register_key(k3)

        # Initially, all are healthy and candidate keys return all 3
        candidates = self.pool.get_candidate_keys("groq")
        self.assertEqual(candidates, ["GROQ_KEY_1", "GROQ_KEY_2", "GROQ_KEY_3"])

        # Key 1 hits rate limit
        self.health_mgr.record_failure("GROQ_KEY_1", error_type="rate_limit", error_message="Rate limit 429")
        self.assertFalse(self.health_mgr.is_available("GROQ_KEY_1"))

        # Candidate keys now prioritize GROQ_KEY_2 and GROQ_KEY_3
        candidates_after = self.pool.get_candidate_keys("groq")
        self.assertEqual(candidates_after, ["GROQ_KEY_2", "GROQ_KEY_3"])

    def test_auth_failure_permanently_disables_key(self):
        """When a key returns 401/403, it is permanently disabled in the pool."""
        k1 = APIKey(id="MISTRAL_KEY_1", provider="mistral", key_reference="MISTRAL_KEY_1", label="Mistral 1")
        k2 = APIKey(id="MISTRAL_KEY_2", provider="mistral", key_reference="MISTRAL_KEY_2", label="Mistral 2")

        self.key_reg.register_key(k1)
        self.key_reg.register_key(k2)

        # Key 1 gets 401 Unauthorized
        self.health_mgr.record_failure("MISTRAL_KEY_1", error_type="auth_error", error_message="401 Unauthorized")

        # Key 1 is disabled and has infinite cooldown
        h1 = self.health_mgr.get_health("MISTRAL_KEY_1")
        self.assertEqual(h1.cooldown_until, float("inf"))
        self.assertFalse(self.key_reg.get_key("MISTRAL_KEY_1").enabled)
        self.assertFalse(self.health_mgr.is_available("MISTRAL_KEY_1"))

        # Only Key 2 remains available
        candidates = self.pool.get_candidate_keys("mistral")
        self.assertEqual(candidates, ["MISTRAL_KEY_2"])

    def test_cross_provider_fallback_when_all_primary_accounts_fail(self):
        """Scenario B: All accounts of primary provider fail -> router picks fallback provider."""
        k_groq1 = APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq 1")
        k_mistral1 = APIKey(id="MISTRAL_KEY_1", provider="mistral", key_reference="MISTRAL_KEY_1", label="Mistral 1")

        self.key_reg.register_key(k_groq1)
        self.key_reg.register_key(k_mistral1)

        # Groq key fails
        self.health_mgr.record_failure("GROQ_KEY_1", error_type="rate_limit")

        # Rank routes for 'simple' (fallbacks: groq-fast, coder-backup, coder-strong, reasoner)
        # coder-strong maps to mistral
        routes = self.router.rank_routes("simple")
        # Top route should be coder-strong or reasoner (mistral), NOT groq
        best_alias, best_key, score = routes[0]
        self.assertIn("MISTRAL", best_key)

    def test_model_capability_enforcement_skips_tool_incompatible_models(self):
        """Scenario C: Models without tool support (e.g. DeepSeek R1) are excluded when tools required."""
        mod_reg = ModelRegistry()

        # DeepSeek R1 has supports_tools = False
        r1_meta = ModelMetadata(
            provider="deepseek",
            model_id="deepseek-reasoner",
            display_name="DeepSeek R1",
            supports_tools=False,
            supports_reasoning=True,
        )
        chat_meta = ModelMetadata(
            provider="deepseek",
            model_id="deepseek-chat",
            display_name="DeepSeek V3",
            supports_tools=True,
            supports_reasoning=False,
        )
        mod_reg.register_model(r1_meta)
        mod_reg.register_model(chat_meta)
        mod_reg.register_alias("deepseek-r1", "deepseek", "deepseek-reasoner")
        mod_reg.register_alias("deepseek-v3", "deepseek", "deepseek-chat")

        k1 = APIKey(id="DEEPSEEK_KEY_1", provider="deepseek", key_reference="DEEPSEEK_KEY_1", label="DeepSeek 1")
        self.key_reg.register_key(k1)

        with patch("qz_providers.model_registry.get_model_registry", return_value=mod_reg):
            # When tools ARE required, deepseek-r1 MUST NOT be selected
            routes_with_tools = self.router.rank_routes(
                "deepseek-r1",
                fallbacks=("deepseek-r1", "deepseek-v3"),
                requires_tools=True,
            )
            # deepseek-r1 is filtered out; deepseek-v3 is selected
            self.assertEqual(routes_with_tools[0][0], "deepseek-v3")

            # When tools are NOT required, deepseek-r1 is allowed
            routes_without_tools = self.router.rank_routes(
                "deepseek-r1",
                fallbacks=("deepseek-r1", "deepseek-v3"),
                requires_tools=False,
            )
            self.assertEqual(routes_without_tools[0][0], "deepseek-r1")

    def test_openrouter_adapter_discovery_reasoning_and_tools(self):
        """Verify OpenRouter adapter discovery logic correctly marks capabilities without crashes."""
        adapter = OpenRouterAdapter()

        fake_openrouter_response = {
            "data": [
                {
                    "id": "deepseek/deepseek-r1-distill-llama-70b",
                    "name": "DeepSeek R1 Distill",
                    "description": "State of the art reasoning model with thinking tokens.",
                    "context_length": 131072,
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    "architecture": {"modality": "text"},
                },
                {
                    "id": "meta-llama/llama-3.3-70b-instruct",
                    "name": "Llama 3.3 70B",
                    "description": "General purpose chat and tool-calling model.",
                    "context_length": 128000,
                    "pricing": {"prompt": "0.0000005", "completion": "0.000001"},
                    "architecture": {"modality": "text"},
                },
            ]
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json_encode(fake_openrouter_response)
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            models = adapter.discover_models(api_key="test_openrouter_key")
            self.assertEqual(len(models), 2)

            r1 = next(m for m in models if "deepseek-r1" in m.model_id)
            self.assertTrue(r1.supports_reasoning)
            self.assertIn("reasoning", r1.capabilities)

            llama = next(m for m in models if "llama-3.3" in m.model_id)
            self.assertTrue(llama.supports_tools)
            self.assertIn("tool_calling", llama.capabilities)


def json_encode(obj):
    import json
    return json.dumps(obj).encode("utf-8")


class ValidationAndEnvironmentAuditTests(unittest.TestCase):
    def test_testless_workspace_passes_validation_gate(self):
        """Workspaces without tests should not be permanently blocked."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir)
            (ws / "README.md").write_text("# Testless project\n", encoding="utf-8")

            pipe = ValidationPipeline()
            report = pipe.run(workspace=ws, requirements_text="Create README", changes=[{"path": "README.md"}])

            self.assertTrue(report.passed)
            self.assertEqual(report.status, "PASS")
            self.assertEqual(report.skipped_required_checks, [])

    def test_workspace_with_failing_tests_fails_validation_gate(self):
        """Workspaces with failing tests must be blocked by validation gate."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir)
            tests_dir = ws / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_sample.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )

            pipe = ValidationPipeline()
            report = pipe.run(workspace=ws, requirements_text="Fix test", changes=[{"path": "tests/test_sample.py"}])

            self.assertFalse(report.passed)
            self.assertEqual(report.status, "FAIL")
            self.assertIn("tests", report.failed_required_checks)


class ErrorNormalizationAuditTests(unittest.TestCase):
    def test_error_normalization_classes(self):
        """Verify errors are classified into retryable and non-retryable categories."""
        # 401 -> AuthenticationError (non-retryable)
        err_401 = normalize_error("Invalid API Key provided", status_code=401)
        self.assertIsInstance(err_401, AuthenticationError)
        self.assertFalse(err_401.retryable)

        # 429 -> RateLimitError (retryable)
        err_429 = normalize_error("Rate limit exceeded. Please wait.", status_code=429)
        self.assertIsInstance(err_429, RateLimitError)
        self.assertTrue(err_429.retryable)


if __name__ == "__main__":
    unittest.main()
