"""Unit tests for qz_pool: KeyRegistry, HealthManager, ConcurrencyLimiter, and LLMPool."""

import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from qz_keystore import KeyEntry, KeyStore
from qz_pool.concurrency import ConcurrencyLimiter, ConcurrencyLimitExceeded
from qz_pool.health_manager import HealthManager
from qz_pool.models import APIKey, KeyHealth, Provider
from qz_pool.pool import LLMPool
from qz_pool.registry import KeyRegistry


class KeyRegistryTests(unittest.TestCase):
    def test_discovers_multiple_keys_per_provider_from_mocked_keystore(self):
        mock_keystore = MagicMock(spec=KeyStore)
        mock_keystore.list_entries.return_value = [
            KeyEntry(provider="groq", family="GROQ_KEY", index=1, env_name="GROQ_KEY_1", masked_value="******1111", enabled=True),
            KeyEntry(provider="groq", family="GROQ_KEY", index=2, env_name="GROQ_KEY_2", masked_value="******2222", enabled=True),
            KeyEntry(provider="mistral", family="MISTRAL_KEY", index=1, env_name="MISTRAL_KEY_1", masked_value="******3333", enabled=False),
            KeyEntry(provider="gemini", family="GEMINI_KEY", index=1, env_name="GEMINI_KEY_1", masked_value="******4444", enabled=True),
        ]

        registry = KeyRegistry(keystore=mock_keystore, config_path="nonexistent_config.yaml")

        groq_keys = registry.get_keys_by_provider("groq")
        self.assertEqual(len(groq_keys), 2)
        self.assertEqual(groq_keys[0].id, "GROQ_KEY_1")
        self.assertEqual(groq_keys[0].provider, "groq")
        self.assertTrue(groq_keys[0].enabled)
        self.assertEqual(groq_keys[1].id, "GROQ_KEY_2")

        mistral_keys = registry.get_keys_by_provider("mistral")
        self.assertEqual(len(mistral_keys), 1)
        self.assertFalse(mistral_keys[0].enabled)

        gemini_keys = registry.get_keys_by_provider("gemini")
        self.assertEqual(len(gemini_keys), 1)
        self.assertEqual(gemini_keys[0].id, "GEMINI_KEY_1")

    def test_alias_mapping_from_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "config.yaml"
            cfg_path.write_text(
                """
model_list:
  - model_name: groq-fast
    litellm_params:
      model: groq/llama3
      api_key: os.environ/GROQ_KEY_1
  - model_name: groq-fast
    litellm_params:
      model: groq/llama3
      api_key: os.environ/GROQ_KEY_2
  - model_name: coder-strong
    litellm_params:
      model: mistral/codestral
      api_key: os.environ/MISTRAL_KEY_1
""",
                encoding="utf-8",
            )
            mock_keystore = MagicMock(spec=KeyStore)
            mock_keystore.list_entries.return_value = [
                KeyEntry(provider="groq", family="GROQ_KEY", index=1, env_name="GROQ_KEY_1", masked_value="******1111", enabled=True),
                KeyEntry(provider="groq", family="GROQ_KEY", index=2, env_name="GROQ_KEY_2", masked_value="******2222", enabled=True),
                KeyEntry(provider="mistral", family="MISTRAL_KEY", index=1, env_name="MISTRAL_KEY_1", masked_value="******3333", enabled=True),
            ]
            registry = KeyRegistry(keystore=mock_keystore, config_path=cfg_path)
            self.assertEqual(registry.get_provider_for_alias("groq-fast"), "groq")
            self.assertEqual(registry.get_provider_for_alias("coder-strong"), "mistral")

            mapped_groq_keys = registry.get_keys_for_alias("groq-fast")
            self.assertEqual([k.id for k in mapped_groq_keys], ["GROQ_KEY_1", "GROQ_KEY_2"])


class HealthManagerTests(unittest.TestCase):
    def setUp(self):
        self.registry = KeyRegistry(keystore=MagicMock(list_entries=lambda: []))
        self.key1 = APIKey(id="KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Groq Key #1", enabled=True)
        self.registry.register_key(self.key1)
        self.health_mgr = HealthManager(registry=self.registry, base_cooldown_seconds=10.0, max_cooldown_seconds=60.0)

    def test_record_success_updates_metrics_and_clears_cooldown(self):
        self.health_mgr.record_failure("KEY_1", error_type="rate_limit", error_message="429 Rate limited")
        self.assertFalse(self.health_mgr.is_available("KEY_1"))

        self.health_mgr.record_success("KEY_1", latency_ms=150.0)
        self.assertTrue(self.health_mgr.is_available("KEY_1"))
        health = self.health_mgr.get_health("KEY_1")
        self.assertEqual(health.success_count, 1)
        self.assertEqual(health.consecutive_errors, 0)
        self.assertAlmostEqual(health.average_latency_ms, 150.0)

    def test_rate_limit_exponential_backoff(self):
        now = time.time()
        with patch("time.time", return_value=now):
            # Attempt 1: 10s * 2^0 = 10s
            self.health_mgr.record_failure("KEY_1", error_type="rate_limit", error_message="429")
            h1 = self.health_mgr.get_health("KEY_1")
            self.assertAlmostEqual(h1.cooldown_until, now + 10.0)

            # Attempt 2: 10s * 2^1 = 20s
            self.health_mgr.record_failure("KEY_1", error_type="rate_limit", error_message="429")
            h2 = self.health_mgr.get_health("KEY_1")
            self.assertAlmostEqual(h2.cooldown_until, now + 20.0)

            # Attempt 3: 10s * 2^2 = 40s
            self.health_mgr.record_failure("KEY_1", error_type="rate_limit", error_message="429")
            h3 = self.health_mgr.get_health("KEY_1")
            self.assertAlmostEqual(h3.cooldown_until, now + 40.0)

            # Attempt 4: capped at max 60s
            self.health_mgr.record_failure("KEY_1", error_type="rate_limit", error_message="429")
            h4 = self.health_mgr.get_health("KEY_1")
            self.assertAlmostEqual(h4.cooldown_until, now + 60.0)

    def test_auth_error_permanently_disables_key(self):
        self.assertTrue(self.key1.enabled)
        self.health_mgr.record_failure("KEY_1", error_type="auth_error", error_message="401 Invalid API Key")
        self.assertFalse(self.key1.enabled)
        self.assertFalse(self.health_mgr.is_available("KEY_1"))
        health = self.health_mgr.get_health("KEY_1")
        self.assertEqual(health.cooldown_until, float("inf"))

    def test_provider_health_summary(self):
        self.health_mgr.record_success("KEY_1", latency_ms=100.0)
        summary = self.health_mgr.get_provider_health_summary("groq")
        self.assertEqual(summary["total_keys"], 1)
        self.assertEqual(summary["available_keys"], 1)
        self.assertEqual(summary["total_successes"], 1)
        self.assertEqual(summary["average_latency_ms"], 100.0)


class ConcurrencyLimiterTests(unittest.TestCase):
    def test_blocks_when_limit_reached_and_unblocks_on_release(self):
        limiter = ConcurrencyLimiter(default_max_concurrency=2)
        key_id = "GROQ_KEY_1"

        self.assertTrue(limiter.can_acquire(key_id))
        self.assertTrue(limiter.acquire(key_id))
        self.assertEqual(limiter.get_active_count(key_id), 1)

        self.assertTrue(limiter.can_acquire(key_id))
        self.assertTrue(limiter.acquire(key_id))
        self.assertEqual(limiter.get_active_count(key_id), 2)

        # 3rd acquire exceeds limit
        self.assertFalse(limiter.can_acquire(key_id))
        self.assertFalse(limiter.acquire(key_id))

        with self.assertRaises(ConcurrencyLimitExceeded):
            with limiter.slot(key_id):
                pass

        # Release one slot
        limiter.release(key_id)
        self.assertEqual(limiter.get_active_count(key_id), 1)
        self.assertTrue(limiter.can_acquire(key_id))

        # Context manager acquires and releases
        with limiter.slot(key_id):
            self.assertEqual(limiter.get_active_count(key_id), 2)
        self.assertEqual(limiter.get_active_count(key_id), 1)


class LLMPoolTests(unittest.TestCase):
    def setUp(self):
        self.mock_keystore = MagicMock(spec=KeyStore)
        self.mock_keystore.list_entries.return_value = [
            KeyEntry(provider="groq", family="GROQ_KEY", index=1, env_name="GROQ_KEY_1", masked_value="******1111", enabled=True),
            KeyEntry(provider="groq", family="GROQ_KEY", index=2, env_name="GROQ_KEY_2", masked_value="******2222", enabled=True),
        ]
        self.registry = KeyRegistry(keystore=self.mock_keystore, config_path="nonexistent.yaml")
        self.pool = LLMPool(registry=self.registry)

    def test_get_candidate_keys_orders_by_health(self):
        candidates = self.pool.get_candidate_keys("groq")
        self.assertEqual(candidates, ["GROQ_KEY_1", "GROQ_KEY_2"])

        # Give KEY_1 an error so KEY_2 becomes primary candidate
        self.pool.health_manager.record_failure("GROQ_KEY_1", error_type="server_error")
        # Give KEY_2 a success
        self.pool.health_manager.record_success("GROQ_KEY_2", latency_ms=50.0)

        new_candidates = self.pool.get_candidate_keys("groq")
        self.assertEqual(new_candidates[0], "GROQ_KEY_2")

    def test_execute_with_pool_success(self):
        called_with = []

        def mock_request(key_id: str):
            called_with.append(key_id)
            return "completion_result"

        result, used_key = self.pool.execute_with_pool("groq", mock_request)
        self.assertEqual(result, "completion_result")
        self.assertEqual(used_key, "GROQ_KEY_1")
        self.assertEqual(called_with, ["GROQ_KEY_1"])
        self.assertEqual(self.pool.health_manager.get_health("GROQ_KEY_1").success_count, 1)

    def test_execute_with_pool_fails_over_to_next_candidate_within_attempt_bound(self):
        call_history = []

        def failing_then_succeeding_request(key_id: str):
            call_history.append(key_id)
            if key_id == "GROQ_KEY_1":
                raise RuntimeError("503 Service Unavailable")
            return "success_from_backup"

        result, used_key = self.pool.execute_with_pool(
            "groq", failing_then_succeeding_request, max_attempts=2
        )
        self.assertEqual(result, "success_from_backup")
        self.assertEqual(used_key, "GROQ_KEY_2")
        self.assertEqual(call_history, ["GROQ_KEY_1", "GROQ_KEY_2"])
        self.assertEqual(self.pool.health_manager.get_health("GROQ_KEY_1").error_count, 1)
        self.assertEqual(self.pool.health_manager.get_health("GROQ_KEY_2").success_count, 1)

    def test_no_raw_secrets_in_logs_or_health_records(self):
        raw_secret_value = "gsk_live_secret_key_1234567890abcdef"
        mock_keystore = MagicMock(spec=KeyStore)
        mock_keystore.list_entries.return_value = [
            KeyEntry(provider="groq", family="GROQ_KEY", index=1, env_name="GROQ_KEY_1", masked_value="******cdef", enabled=True),
        ]
        registry = KeyRegistry(keystore=mock_keystore, config_path="nonexistent.yaml")
        pool = LLMPool(registry=registry)

        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            pool.health_manager.record_success("GROQ_KEY_1", latency_ms=100.0)
            pool.health_manager.record_failure("GROQ_KEY_1", error_type="rate_limit", error_message="429 Rate Limit")
            summary = pool.health_manager.get_provider_health_summary("groq")

        logged_text = stdout_capture.getvalue()
        summary_text = str(summary)
        health_text = str(pool.health_manager.get_health("GROQ_KEY_1"))
        registry_text = str([k for k in registry.get_all_keys()])

        for blob in (logged_text, summary_text, health_text, registry_text):
            self.assertNotIn(raw_secret_value, blob)
            self.assertNotIn("gsk_live", blob)


if __name__ == "__main__":
    unittest.main()
