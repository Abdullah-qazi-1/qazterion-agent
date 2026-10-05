import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qz_agent
from qz_environment import ProjectEnvironment
from qz_usage_tracker import UsageTracker


def completion(content="done"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))])


class AgentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(log_path=None)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_successful_request_returns_the_requested_role(self):
        with patch.object(qz_agent.client.chat.completions, "create", return_value=completion()) as create:
            response, active = qz_agent.request_completion(model="coder", messages=[], usage_tracker=self.tracker)
        self.assertEqual(active, "coder")
        self.assertEqual(response.choices[0].message.content, "done")
        self.assertEqual(create.call_args.kwargs["model"], "coder")

    def test_role_fallback_only_after_the_whole_role_failed(self):
        with patch.object(
            qz_agent.client.chat.completions, "create",
            side_effect=[RuntimeError("All models for 'coder' failed"), completion()],
        ) as create:
            _, active = qz_agent.request_completion(
                model="coder", messages=[], fallbacks=("reasoner",), usage_tracker=self.tracker,
            )
        self.assertEqual(active, "reasoner")
        self.assertEqual([c.kwargs["model"] for c in create.call_args_list], ["coder", "reasoner"])

    def test_empty_assistant_message_counts_as_failure(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[]))])
        with patch.object(qz_agent.client.chat.completions, "create", return_value=empty):
            with self.assertRaises(RuntimeError) as ctx:
                qz_agent.request_completion(model="coder", messages=[], usage_tracker=self.tracker)
        self.assertIn("empty assistant message", str(ctx.exception))

    def test_error_messages_are_redacted(self):
        with patch.object(qz_agent.client.chat.completions, "create",
                          side_effect=RuntimeError("bad key gsk_abcdefghijklmnopqrstuvwxyz123456")):
            with self.assertRaises(RuntimeError) as ctx:
                qz_agent.request_completion(model="coder", messages=[], usage_tracker=self.tracker)
        self.assertNotIn("gsk_abcdefghijklmnopqrstuvwxyz123456", str(ctx.exception))

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
