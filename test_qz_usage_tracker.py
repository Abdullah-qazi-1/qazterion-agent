import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from qz_usage_tracker import UsageTracker, default_usage_log_path


class UsageTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "usage.jsonl"
        self.tracker = UsageTracker(log_path=self.log_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_records_request_count_and_last_model(self):
        self.tracker.record_request(model="groq-fast", key_id="GROQ_KEY_1", duration=0.4, success=True)
        self.tracker.record_request(model="coder-strong", key_id="MISTRAL_KEY_1", duration=1.2, success=True)
        summary = self.tracker.summary()
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["last_model"], "coder-strong")
        self.assertEqual(summary["last_duration"], 1.2)
        self.assertIsNotNone(summary["last_success_at"])

    def test_last_key_id_is_masked_never_raw(self):
        self.tracker.record_request(model="groq-fast", key_id="sk-abcdefghijklmno", duration=0.1, success=True)
        summary = self.tracker.summary()
        self.assertNotEqual(summary["last_key_id"], "sk-abcdefghijklmno")
        self.assertNotIn("abcdefgh", summary["last_key_id"])

    def test_error_and_fallback_counts(self):
        self.tracker.record_request(model="coder-strong", duration=0.5, success=False, error="RateLimitError")
        self.tracker.record_request(
            model="groq-fast", duration=0.3, success=True, fallback_from="coder-strong"
        )
        summary = self.tracker.summary()
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["fallback_count"], 1)
        self.assertEqual(summary["last_error"], "RateLimitError")

    def test_key_names_are_shown_but_secret_looking_ids_are_masked(self):
        self.tracker.record_request(model="groq/x", key_id="GROQ_KEY_2", duration=0.1, success=True)
        self.assertEqual(self.tracker.summary()["last_key_id"], "GROQ_KEY_2")
        self.assertIn("GROQ_KEY_2", self.tracker.summary()["by_key"])

    def test_events_are_persisted_to_the_log_file(self):
        self.tracker.record_request(model="groq-fast", duration=0.1, success=True)
        self.assertTrue(self.log_path.is_file())
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_reopens_an_existing_usage_log_and_honors_the_path_override(self):
        self.tracker.record_request(model="groq-fast", duration=0.1, success=True)
        reopened = UsageTracker(log_path=self.log_path)
        self.assertEqual(reopened.summary()["request_count"], 1)
        with patch.dict("os.environ", {"QAZTERION_USAGE_LOG_PATH": str(self.log_path)}):
            self.assertEqual(default_usage_log_path(), self.log_path)

    def test_bounded_memory_keeps_only_the_most_recent_events(self):
        bounded = UsageTracker(log_path=None, max_events=3)
        for i in range(5):
            bounded.record_request(model=f"m{i}", duration=0.1, success=True)
        self.assertEqual(bounded.summary()["request_count"], 3)
        self.assertEqual(bounded.summary()["last_model"], "m4")

    def test_no_events_yields_empty_but_valid_summary(self):
        empty_tracker = UsageTracker(log_path=None)
        summary = empty_tracker.summary()
        self.assertEqual(summary["request_count"], 0)
        self.assertIsNone(summary["last_model"])
        self.assertIsNone(summary["last_error"])


if __name__ == "__main__":
    unittest.main()