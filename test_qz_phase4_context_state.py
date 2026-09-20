"""Unit tests for Phase 4:
- Hard budget pre-admission enforcement
- Complete request context fitting and compaction
- Fallback project memory workspace hash partitioning
- Preferred model alias disk persistence
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
from qz_providers.model_registry import ModelRegistry
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

    def test_project_memory_fallback_partitions_workspaces(self):
        # Force fallback by patching Path.mkdir to simulate uncreatable project-local .qazterion dir
        with patch("pathlib.Path.mkdir", side_effect=OSError("Read only filesystem")):
            path_a = _memory_path("D:/project_alpha")
            path_b = _memory_path("D:/project_beta")
            # Both fallback paths should be distinct SHA-256 hashed filenames
            self.assertNotEqual(str(path_a), str(path_b))
            self.assertIn("project-memory-", str(path_a))
            self.assertIn("project-memory-", str(path_b))

    def test_preferred_model_alias_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"QAZTERION_DATA_DIR": tmpdir}):
                reg1 = ModelRegistry()
                reg1.register_alias("fast-coder", "groq", "custom-model-id-v1", persist=True)
                
                # Recreate registry (simulating process restart)
                reg2 = ModelRegistry()
                # Alias mapping should be preserved
                self.assertIn("fast-coder", reg2._aliases)
                self.assertEqual(reg2._aliases["fast-coder"], ("groq", "custom-model-id-v1"))


if __name__ == "__main__":
    unittest.main()
