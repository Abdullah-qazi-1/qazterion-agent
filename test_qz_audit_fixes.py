"""Comprehensive unit and integration tests verifying fixes from the deep technical audit."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openai import OpenAI
from qz_core.client import FallbackCompletions, RobustOpenAIClient
from qz_core.executor import request_completion
from qz_keystore import KeyEntry, KeyStore
from qz_pool.concurrency import ConcurrencyLimiter
from qz_pool.health_manager import HealthManager
from qz_pool.models import APIKey, KeyHealth
from qz_pool.pool import LLMPool, reset_pool
from qz_pool.registry import KeyRegistry
from qz_providers.model_registry import ModelRegistry
from qz_providers.models import ModelCapability, ModelLifecycleState, ModelMetadata
from qz_repair import FailureCategory, RepairHistory, classify_failure
from qz_router.router import SmartRouter, reset_router
from qz_sandbox.backend import RestrictedHostBackend, sanitize_subprocess_env
from qz_storage import StorageManager
from qz_usage_tracker import UsageTracker
from qz_validation.checks import CheckResult, CheckStatus
from qz_validation.pipeline import ValidationReport


class ClientFallbackAuditFixTests(unittest.TestCase):
    def test_client_direct_fallback_preserves_requested_model_and_does_not_mutate_environ(self):
        mock_raw = MagicMock(spec=OpenAI)
        mock_raw.chat.completions.create.side_effect = ConnectionError("Could not connect to proxy at localhost:4000")

        fb = FallbackCompletions(mock_raw)

        called_kwargs = {}

        def mock_litellm_completion(**kwargs):
            called_kwargs.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="Hello world",
                            tool_calls=None,
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

        mock_litellm = MagicMock()
        mock_litellm.completion = mock_litellm_completion

        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            # Test that requesting 'coder-strong' resolves to mistral model without hardcoded substitution
            res = fb.create(
                model="coder-strong",
                messages=[{"role": "user", "content": "test prompt"}],
            )
            self.assertEqual(res.choices[0].message.content, "Hello world")
            self.assertIn("model", called_kwargs)
            self.assertIn("mistral", called_kwargs["model"])
            # Ensure os.environ was not polluted with arbitrary plain keys
            self.assertNotIn("DUMMY_SECRET_KEY_EXPOSURE", os.environ)


class TelemetryAndStorageConsistencyTests(unittest.TestCase):
    def test_request_completion_records_to_both_storage_and_tracker(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            usage_log = Path(f.name)

        try:
            storage = StorageManager(db_path=db_path)
            tracker = UsageTracker(log_path=usage_log)

            mock_response = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="Completed task",
                            tool_calls=None,
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=25, completion_tokens=10, total_tokens=35),
            )
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_response

            with patch("qz_storage.get_storage", return_value=storage):
                resp, used_model = request_completion(
                    model="groq-fast",
                    messages=[{"role": "user", "content": "Write code"}],
                    usage_tracker=tracker,
                    client=mock_client,
                    task_id="task-storage-test",
                )

                self.assertEqual(used_model, "groq-fast")

                # Verify StorageManager record_model_call was executed
                metrics = storage.get_metrics_summary()
                self.assertEqual(metrics["total_calls"], 1)
                self.assertEqual(metrics["success_rate"], 100.0)
                self.assertEqual(metrics["total_tokens"], 35)

                # Verify UsageTracker recorded the exact same request
                task_usage = tracker.get_task_usage("task-storage-test")
                self.assertEqual(task_usage["request_count"], 1)
                self.assertEqual(task_usage["total_tokens"], 35)
                self.assertEqual(task_usage["success_count"], 1)
        finally:
            try:
                db_path.unlink(missing_ok=True)
                usage_log.unlink(missing_ok=True)
            except OSError:
                pass


class SubprocessSecretIsolationTests(unittest.TestCase):
    def test_sanitize_subprocess_env_strips_api_keys_and_tokens(self):
        dirty_env = {
            "PATH": "C:\\Windows\\system32;C:\\Program Files\\Python313",
            "SYSTEMROOT": "C:\\Windows",
            "GROQ_KEY_1": "gsk_secret_12345",
            "GEMINI_API_KEY": "AIzaSySecret67890",
            "MISTRAL_KEY_1": "secret_mistral_token",
            "LITELLM_MASTER_KEY": "sk-master-key-xyz",
            "OPENAI_API_KEY": "sk-openai-key",
            "MY_APP_TOKEN": "token_abc",
            "USER_PASSWORD": "super_secret_password",
            "USERPROFILE": "C:\\Users\\test",
        }

        with tempfile.TemporaryDirectory() as ws:
            clean_env = sanitize_subprocess_env(ws, base_env=dirty_env)

            # Essential variables preserved
            self.assertEqual(clean_env["PATH"], dirty_env["PATH"])
            self.assertEqual(clean_env["SYSTEMROOT"], dirty_env["SYSTEMROOT"])
            self.assertEqual(clean_env["USERPROFILE"], dirty_env["USERPROFILE"])
            self.assertIn("PYTHONPATH", clean_env)
            self.assertTrue(clean_env["PYTHONPATH"].startswith(ws))

            # Secret variables stripped
            self.assertNotIn("GROQ_KEY_1", clean_env)
            self.assertNotIn("GEMINI_API_KEY", clean_env)
            self.assertNotIn("MISTRAL_KEY_1", clean_env)
            self.assertNotIn("LITELLM_MASTER_KEY", clean_env)
            self.assertNotIn("OPENAI_API_KEY", clean_env)
            self.assertNotIn("MY_APP_TOKEN", clean_env)
            self.assertNotIn("USER_PASSWORD", clean_env)


class CapabilityAwareRoutingTests(unittest.TestCase):
    def setUp(self):
        reset_pool()
        reset_router()

    def tearDown(self):
        reset_pool()
        reset_router()

    def test_routing_filters_incompatible_models(self):
        mock_keystore = MagicMock(spec=KeyStore)
        mock_keystore.list_entries.return_value = [
            KeyEntry(provider="groq", family="GROQ_KEY", index=1, env_name="GROQ_KEY_1", masked_value="******1111", enabled=True),
            KeyEntry(provider="mistral", family="MISTRAL_KEY", index=1, env_name="MISTRAL_KEY_1", masked_value="******2222", enabled=True),
        ]
        registry = KeyRegistry(keystore=mock_keystore, config_path="nonexistent.yaml")
        registry._alias_to_keys["groq-fast"] = ["GROQ_KEY_1"]
        registry._alias_to_keys["reasoner"] = ["MISTRAL_KEY_1"]

        pool = LLMPool(registry=registry)
        router = SmartRouter(pool=pool)

        # Mock ModelRegistry with capability differences
        model_reg = ModelRegistry()
        model_no_tools = ModelMetadata(
            provider="groq",
            model_id="no-tools-model",
            display_name="No Tools Model",
            capabilities=[ModelCapability.CHAT.value],  # No TOOL_CALLING capability
            supports_tools=False,
            supports_reasoning=False,
        )
        model_reasoner = ModelMetadata(
            provider="mistral",
            model_id="reasoner-model",
            display_name="Reasoner Model",
            capabilities=[ModelCapability.CHAT.value, ModelCapability.TOOL_CALLING.value, ModelCapability.REASONING.value],
            supports_tools=True,
            supports_reasoning=True,
        )

        model_reg.register_model(model_no_tools)
        model_reg.register_model(model_reasoner)
        model_reg.register_alias("groq-fast", "groq", "no-tools-model")
        model_reg.register_alias("reasoner", "mistral", "reasoner-model")

        with patch("qz_providers.model_registry.get_model_registry", return_value=model_reg):
            # When requires_tools=True, groq-fast (supports_tools=False) must be excluded
            ranked_tools = router.rank_routes("simple", requires_tools=True)
            aliases_tools = [r[0] for r in ranked_tools]
            self.assertNotIn("groq-fast", aliases_tools)

            # When requires_reasoning=True, groq-fast must be excluded
            ranked_reasoning = router.rank_routes("reasoner", requires_reasoning=True)
            aliases_reasoning = [r[0] for r in ranked_reasoning]
            self.assertNotIn("groq-fast", aliases_reasoning)
            self.assertIn("reasoner", aliases_reasoning)


class CooldownAndRetryTests(unittest.TestCase):
    def setUp(self):
        reset_pool()
        reset_router()

    def tearDown(self):
        reset_pool()
        reset_router()

    def test_cooled_down_key_is_not_retried_immediately(self):
        pool = LLMPool()
        tracker = UsageTracker()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("429 Too Many Requests: Rate limit reached")

        pool.registry.register_key(APIKey(id="KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Key 1", enabled=True))
        pool.registry._alias_to_keys["groq-fast"] = ["KEY_1"]
        router = SmartRouter(pool=pool)

        with (
            patch("qz_core.executor.get_pool", return_value=pool),
            patch("qz_core.executor.get_router", return_value=router),
            patch("qz_core.executor.get_client", return_value=mock_client),
        ):
            with self.assertRaises(RuntimeError):
                request_completion(
                    model="groq-fast",
                    messages=[{"role": "user", "content": "test"}],
                    usage_tracker=tracker,
                    fallbacks=("groq-fast",),
                )

            # HealthManager should show key is in cooldown
            health = pool.health_manager.get_health("KEY_1")
            self.assertTrue(health.is_in_cooldown())


class RepairHistoryAndRollbackTests(unittest.TestCase):
    def test_repair_history_anti_loop_with_exact_diff(self):
        history = RepairHistory()
        sig = "unique_error_signature"
        diff = "@@ -1,3 +1,3 @@\n-old_code()\n+new_code()"

        # First attempt with this diff
        history.record_attempt(1, FailureCategory.TEST_FAILURE, sig, diff_text=diff)

        # Querying with different diff should return empty feedback
        fb1 = history.get_anti_loop_feedback(sig, current_diff="@@ -1,3 +1,3 @@\n-old_code()\n+alternate_fix()")
        self.assertEqual(fb1, "")

        # Querying with exact same diff should return warning
        fb2 = history.get_anti_loop_feedback(sig, current_diff=diff)
        self.assertIn("already attempted this exact patch", fb2)


class UsageTrackerPersistentTotalsTests(unittest.TestCase):
    def test_task_totals_retained_beyond_deque_maxlen(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "usage.jsonl"
            tracker = UsageTracker(log_path=log_file, max_events=10)

            # Record 25 requests for task-1
            for i in range(25):
                tracker.record_request(
                    model="groq-fast",
                    task_id="task-1",
                    duration=0.1,
                    success=True,
                    input_tokens=100,
                    output_tokens=50,
                )

            # Deque is capped at 10 events
            self.assertEqual(len(tracker._events), 10)

            # But task-1 totals must reflect all 25 requests
            usage = tracker.get_task_usage("task-1")
            self.assertEqual(usage["request_count"], 25)
            self.assertEqual(usage["input_tokens"], 2500)
            self.assertEqual(usage["output_tokens"], 1250)
            self.assertEqual(usage["total_tokens"], 3750)
            self.assertEqual(usage["success_count"], 25)


class TokenEstimationOnFailedRequestTests(unittest.TestCase):
    def test_failed_request_records_estimated_tokens_and_cost(self):
        tracker = UsageTracker()
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("500 Internal Server Error")

        with patch("qz_core.executor.get_client", return_value=mock_client):
            with self.assertRaises(RuntimeError):
                request_completion(
                    model="groq-fast",
                    messages=[{"role": "user", "content": "A" * 400}],
                    usage_tracker=tracker,
                    fallbacks=("groq-fast",),
                    task_id="failed-task",
                )

            usage = tracker.get_task_usage("failed-task")
            self.assertGreater(usage["input_tokens"], 0)
            self.assertGreater(usage["estimated_cost"], 0.0)
            self.assertGreater(usage["error_count"], 0)


class ConcurrencyLockingStressTests(unittest.TestCase):
    def test_concurrent_usage_tracker_and_health_manager_operations(self):
        tracker = UsageTracker()
        health_mgr = HealthManager()
        model_reg = ModelRegistry()

        errors = []

        def worker(w_id: int):
            try:
                for i in range(50):
                    tracker.record_request(
                        model="groq-fast",
                        task_id=f"task-{w_id}",
                        duration=0.01,
                        success=(i % 2 == 0),
                        input_tokens=10,
                        output_tokens=5,
                    )
                    _ = tracker.summary()
                    _ = tracker.get_task_usage(f"task-{w_id}")

                    health_mgr.record_failure(f"KEY_{w_id}", error_type="server_error", latency_ms=10.0)
                    health_mgr.record_success(f"KEY_{w_id}", latency_ms=5.0)
                    _ = health_mgr.is_available(f"KEY_{w_id}")

                    model_reg.record_model_failure("groq", "llama-3.3-70b-versatile", error="test err")
                    model_reg.record_model_success("groq", "llama-3.3-70b-versatile")
                    _ = model_reg.list_models()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])


class KeyAuthPersistenceTests(unittest.TestCase):
    def test_auth_error_disables_key_in_keystore(self):
        mock_keystore = MagicMock(spec=KeyStore)
        mock_keystore.list_entries.return_value = [
            KeyEntry(provider="groq", family="GROQ_KEY", index=1, env_name="GROQ_KEY_1", masked_value="******1111", enabled=True),
        ]

        registry = KeyRegistry(keystore=mock_keystore, config_path="nonexistent.yaml")
        health_mgr = HealthManager(registry=registry)

        health_mgr.record_failure("GROQ_KEY_1", error_type="auth_error", error_message="401 Unauthorized")

        # Verify key was disabled in registry
        key_record = registry.get_key("GROQ_KEY_1")
        self.assertFalse(key_record.enabled)

        # Verify set_enabled was called on keystore to persist the disable
        mock_keystore.set_enabled.assert_called_with("groq", 1, False)


class PoolExecuteAllCandidatesTests(unittest.TestCase):
    def test_execute_with_pool_attempts_all_candidates_by_default(self):
        mock_keystore = MagicMock(spec=KeyStore)
        mock_keystore.list_entries.return_value = [
            KeyEntry(provider="groq", family="GROQ_KEY", index=1, env_name="GROQ_KEY_1", masked_value="******1111", enabled=True),
            KeyEntry(provider="groq", family="GROQ_KEY", index=2, env_name="GROQ_KEY_2", masked_value="******2222", enabled=True),
            KeyEntry(provider="groq", family="GROQ_KEY", index=3, env_name="GROQ_KEY_3", masked_value="******3333", enabled=True),
        ]
        registry = KeyRegistry(keystore=mock_keystore, config_path="nonexistent.yaml")
        pool = LLMPool(registry=registry)

        attempted_keys = []

        def mock_request(key_id: str):
            attempted_keys.append(key_id)
            if key_id in ("GROQ_KEY_1", "GROQ_KEY_2"):
                raise RuntimeError("503 Unavailable")
            return "success_key_3"

        result, used_key = pool.execute_with_pool("groq", mock_request)
        self.assertEqual(result, "success_key_3")
        self.assertEqual(used_key, "GROQ_KEY_3")
        self.assertEqual(attempted_keys, ["GROQ_KEY_1", "GROQ_KEY_2", "GROQ_KEY_3"])


if __name__ == "__main__":
    unittest.main()
