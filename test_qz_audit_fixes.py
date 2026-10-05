"""Regression tests for fixes from the technical audits (non-provider parts)."""

import tempfile
import threading
import unittest
from pathlib import Path

from qz_providers.health import HealthTracker
from qz_repair import FailureCategory, RepairHistory
from qz_sandbox.backend import sanitize_subprocess_env
from qz_usage_tracker import UsageTracker


class SubprocessSecretIsolationTests(unittest.TestCase):
    def test_sanitize_subprocess_env_strips_api_keys_and_tokens(self):
        dirty_env = {
            "PATH": "C:\\Windows\\system32;C:\\Program Files\\Python313",
            "SYSTEMROOT": "C:\\Windows",
            "GROQ_KEY_1": "gsk_secret_12345",
            "GEMINI_API_KEY": "AIzaSySecret67890",
            "MISTRAL_KEY_1": "secret_mistral_token",
            "OPENAI_API_KEY": "sk-openai-key",
            "MY_APP_TOKEN": "token_abc",
            "USER_PASSWORD": "super_secret_password",
            "QAZTERION_KEYSTORE_PATH": "C:\\keystore.dat",
            "USERPROFILE": "C:\\Users\\test",
        }
        with tempfile.TemporaryDirectory() as ws:
            clean_env = sanitize_subprocess_env(ws, base_env=dirty_env)
            self.assertEqual(clean_env["PATH"], dirty_env["PATH"])
            self.assertEqual(clean_env["SYSTEMROOT"], dirty_env["SYSTEMROOT"])
            self.assertEqual(clean_env["USERPROFILE"], dirty_env["USERPROFILE"])
            self.assertTrue(clean_env["PYTHONPATH"].startswith(ws))
            for secret in ("GROQ_KEY_1", "GEMINI_API_KEY", "MISTRAL_KEY_1", "OPENAI_API_KEY",
                           "MY_APP_TOKEN", "USER_PASSWORD", "QAZTERION_KEYSTORE_PATH"):
                self.assertNotIn(secret, clean_env)


class RepairHistoryTests(unittest.TestCase):
    def test_repair_history_anti_loop_with_exact_diff(self):
        history = RepairHistory()
        sig = "unique_error_signature"
        diff = "@@ -1,3 +1,3 @@\n-old_code()\n+new_code()"
        history.record_attempt(1, FailureCategory.TEST_FAILURE, sig, diff_text=diff)
        self.assertEqual(history.get_anti_loop_feedback(sig, current_diff="@@ -1 +1 @@\n-old_code()\n+alternate_fix()"), "")
        self.assertIn("already attempted this exact patch", history.get_anti_loop_feedback(sig, current_diff=diff))


class UsageTrackerPersistentTotalsTests(unittest.TestCase):
    def test_task_totals_retained_beyond_deque_maxlen(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tracker = UsageTracker(log_path=Path(tmp_dir) / "usage.jsonl", max_events=10)
            for _ in range(25):
                tracker.record_request(model="groq/llama", task_id="task-1", duration=0.1, success=True,
                                       input_tokens=100, output_tokens=50)
            self.assertEqual(len(tracker.recent_events(limit=100)), 10)
            usage = tracker.get_task_usage("task-1")
            self.assertEqual(usage["request_count"], 25)
            self.assertEqual(usage["input_tokens"], 2500)
            self.assertEqual(usage["output_tokens"], 1250)
            self.assertEqual(usage["total_tokens"], 3750)
            self.assertEqual(usage["success_count"], 25)

    def test_second_tracker_sees_events_written_by_another_process(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log = Path(tmp_dir) / "usage.jsonl"
            reader = UsageTracker(log_path=log)
            writer = UsageTracker(log_path=log)
            writer.record_request(model="gemini/x", provider="gemini", task_id="t", duration=0.2, success=True,
                                  input_tokens=10, output_tokens=5)
            # The dashboard process picks up new lines without a restart.
            self.assertEqual(reader.summary()["request_count"], 1)
            self.assertEqual(reader.get_task_usage("t")["total_tokens"], 15)


class ConcurrencyStressTests(unittest.TestCase):
    def test_concurrent_usage_tracker_and_health_operations(self):
        tracker = UsageTracker()
        with tempfile.TemporaryDirectory() as tmp:
            health = HealthTracker(path=Path(tmp) / "health.json")
            errors = []

            def worker(w_id: int):
                try:
                    for i in range(40):
                        tracker.record_request(model="groq/x", task_id=f"task-{w_id}", duration=0.01,
                                               success=(i % 2 == 0), input_tokens=10, output_tokens=5)
                        tracker.summary()
                        tracker.get_task_usage(f"task-{w_id}")
                        health.record_failure(f"KEY_{w_id}", "groq/x", "server", "boom")
                        health.record_success(f"KEY_{w_id}", "groq/x", latency_ms=5.0)
                        health.key_available(f"KEY_{w_id}")
                except Exception as e:  # pragma: no cover - reported below
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(tracker.get_task_usage("task-0")["request_count"], 40)


if __name__ == "__main__":
    unittest.main()
