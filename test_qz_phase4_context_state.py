"""Unit tests for Phase 4:
- Hard budget pre-admission enforcement
- Complete request context fitting and compaction
- Project memory partitioned per workspace (outside the repo)
- Preferred model per role persisted in the user catalog
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qz_context import fit_request_context
from qz_core.executor import request_completion
from qz_memory import ProjectMemory, _memory_path
from qz_providers.catalog import ProviderCatalog
from qz_usage_tracker import UsageTracker


def make_completion(content="done"):
    return SimpleNamespace(
        model="groq/llama-3.3-70b-versatile",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )


class Phase4ContextAndStateTests(unittest.TestCase):
    def setUp(self):
        self.tracker = UsageTracker(log_path=None)

    def test_hard_budget_blocks_outbound_request(self):
        # Set max cost to $0.0001
        self.tracker.set_task_budget("task-budget-1", max_cost=0.0001)
        # Record usage exceeding limit
        self.tracker.record_request(
            model="groq-fast",
            duration=0.5,
            success=True,
            total_tokens=5000,
            estimated_cost=0.005,
            task_id="task-budget-1",
        )

        with self.assertRaises(RuntimeError) as ctx:
            request_completion(
                model="groq-fast",
                messages=[{"role": "user", "content": "hi"}],
                usage_tracker=self.tracker,
                task_id="task-budget-1",
            )
        self.assertIn("Request blocked by task budget", str(ctx.exception))

    def test_fit_request_context_compacts_oversized_tool_messages(self):
        huge_tool_content = "X" * 15000
        messages = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Fix the bug"},
            {"role": "tool", "tool_call_id": "call_1", "content": huge_tool_content},
            {"role": "user", "content": "Proceed with the fix"},
        ]
        # Window of 4000 tokens (approx 16k chars)
        fitted = fit_request_context(messages, model_context_window=4000, max_output_tokens=1000, reserve_tokens=500)
        self.assertEqual(len(fitted), 4)
        self.assertIn("[TRUNCATED DUE TO CONTEXT BUDGET]", fitted[2]["content"])
        self.assertEqual(fitted[0]["content"], "You are a coding agent.")
        self.assertEqual(fitted[-1]["content"], "Proceed with the fix")

    def test_project_memory_partitions_workspaces(self):
        path_a = _memory_path("D:/project_alpha")
        path_b = _memory_path("D:/project_beta")
        self.assertNotEqual(path_a, path_b)
        self.assertEqual(path_a.name, "project-memory.jsonl")
        self.assertNotIn("project_alpha", str(path_a))

    def test_preferred_model_persists_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_file = Path(tmpdir) / "providers.yaml"
            first = ProviderCatalog(user_path=user_file)
            first.set_role_preference("coder", "groq", "custom-model-id-v1")
            # A new catalog instance (process restart) sees the preference.
            second = ProviderCatalog(user_path=user_file)
            self.assertEqual(second.roles["coder"][0], "groq/custom-model-id-v1")
            self.assertIn("custom-model-id-v1", second.providers["groq"].models)
            # Legacy alias names resolve to roles.
            self.assertEqual(second.resolve_role("coder-strong"), "coder")


if __name__ == "__main__":
    unittest.main()
