import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qz_agent
from qz_environment import ProjectEnvironment
from qz_usage_tracker import UsageTracker


def completion(content="done"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))]
    )


class AgentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(log_path=None, cooldown_seconds=60)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_successful_request_is_recorded(self):
        with patch.object(qz_agent.client.chat.completions, "create", return_value=completion()):
            response, active = qz_agent.request_completion(
                model="groq-fast", messages=[], usage_tracker=self.tracker
            )
        self.assertEqual(active, "groq-fast")
        self.assertEqual(response.choices[0].message.content, "done")
        self.assertEqual(self.tracker.summary()["request_count"], 1)

    def test_rate_limit_marks_alias_and_falls_back(self):
        with patch.object(
            qz_agent.client.chat.completions,
            "create",
            side_effect=[RuntimeError("429 rate limit"), RuntimeError("429 rate limit"), completion()],
        ) as create:
            _, active = qz_agent.request_completion(
                model="groq-fast",
                messages=[],
                fallbacks=("groq-fast", "coder-backup"),
                usage_tracker=self.tracker,
            )
        self.assertEqual(active, "coder-backup")
        self.assertFalse(self.tracker.is_key_eligible("groq-fast"))
        self.assertEqual([call.kwargs["model"] for call in create.call_args_list],
                         ["groq-fast", "groq-fast", "coder-backup"])

    def test_quota_error_is_classified_separately(self):
        with patch.object(
            qz_agent.client.chat.completions,
            "create",
            side_effect=[RuntimeError("insufficient quota"), RuntimeError("insufficient quota")],
        ):
            with self.assertRaises(RuntimeError):
                qz_agent.request_completion(model="groq-fast", messages=[], usage_tracker=self.tracker)
        self.assertEqual(self.tracker.summary()["flagged_keys"]["groq-fast"]["reason"], "quota_exhausted")

    def test_flagged_alias_is_last_resort_and_success_clears_it(self):
        self.tracker.mark_rate_limited("groq-fast")
        with patch.object(qz_agent.client.chat.completions, "create", return_value=completion()) as create:
            _, active = qz_agent.request_completion(
                model="groq-fast",
                messages=[],
                fallbacks=("groq-fast", "coder-backup"),
                usage_tracker=self.tracker,
            )
        self.assertEqual(active, "coder-backup")
        self.assertEqual(create.call_args.kwargs["model"], "coder-backup")
        with patch.object(qz_agent.client.chat.completions, "create", return_value=completion()):
            qz_agent.request_completion(model="groq-fast", messages=[], usage_tracker=self.tracker)
        self.assertTrue(self.tracker.is_key_eligible("groq-fast"))

    def test_baseline_failure_is_captured_and_cancellation_is_clean(self):
        environment = ProjectEnvironment(
            workspace=Path("."), project_type="python", manager="pip", python_interpreter=None,
            dependency_files=("pyproject.toml",), protected_tools=(), test_command="python -m pytest",
        )
        with patch("qz_agent.detect_project_environment", return_value=environment), \
             patch("qz_agent.run_command", return_value="exit_code=1\nSTDOUT:\nFAILED test_old.py\n"):
            baseline = qz_agent.capture_pre_existing_test_failures()
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline.command, "python -m pytest")

        with patch("qz_agent.prepare_environment", side_effect=KeyboardInterrupt):
            result = qz_agent.run_task("Do something")
        self.assertIn("cancelled", result.lower())


if __name__ == "__main__":
    unittest.main()
