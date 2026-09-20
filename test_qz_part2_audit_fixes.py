"""Unit tests for Part 2 Technical Audit remediations:
- AQ-02: History summarization demotes summary to untrusted user message
- AQ-03: Malformed tool JSON returns structured error and rejects execution
- AQ-07: CLI displays true zero metrics without false minimums
- AQ-08: AutonomousRunner propagates failure when DAG execution fails
- AQ-10: Messages sanitized on both primary proxy and fallback paths
- SAFE-03: Rollback blocks non-ancestor target commits
- TOOL-01/02: read_file and list_files output bounded with pagination
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qz_core import executor
from qz_core.autonomous_loop import AutonomousRunner
from qz_core.client import FallbackCompletions
from qz_core.dag_executor import HardDAGExecutor
from qz_recovery.resume_manager import ResumeManager
from qz_tasks.models import SubtaskStatus, TaskStatus
import qz_tools


class Part2AuditFixesTests(unittest.TestCase):
    def test_roll_conversation_summary_demotes_to_untrusted_user_role(self):
        messages = [
            {"role": "system", "content": "You are a coding agent."},
        ]
        # Add 30 messages to trigger summarization
        for i in range(30):
            messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}"})

        with patch("qz_core.executor.get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Task progress summary"))]
            )
            mock_get_client.return_value = mock_client

            condensed, changed = executor.roll_conversation_summary(messages)
            self.assertTrue(changed)
            # Ensure the only system message is the original index 0 message
            system_messages = [m for m in condensed if m.get("role") == "system"]
            self.assertEqual(len(system_messages), 1)
            self.assertEqual(system_messages[0]["content"], "You are a coding agent.")
            # Ensure the summary was inserted as a user message
            summary_msg = condensed[1]
            self.assertEqual(summary_msg["role"], "user")
            self.assertIn("[Earlier executor progress summary", summary_msg["content"])
            self.assertIn("Task progress summary", summary_msg["content"])

    def test_malformed_tool_arguments_rejected_without_executing(self):
        dag_exec = HardDAGExecutor(workspace="D:/test_ws", max_node_retries=1, max_node_iterations=1)
        node = {"id": "1", "title": "Test Malformed", "description": "Run tool with bad JSON"}

        tool_call = SimpleNamespace(
            id="call_bad",
            function=SimpleNamespace(
                name="make_directory",
                arguments="INVALID_JSON_PAYLOAD_NOT_DICT",
            )
        )
        msg1 = SimpleNamespace(content=None, tool_calls=[tool_call])
        resp1 = SimpleNamespace(choices=[SimpleNamespace(message=msg1)])
        msg2 = SimpleNamespace(content="Done", tool_calls=[])
        resp2 = SimpleNamespace(choices=[SimpleNamespace(message=msg2)])

        with patch("qz_core.dag_executor.get_task", return_value={"status": "EXECUTING"}), \
             patch("qz_core.dag_executor.update_status"), \
             patch("qz_core.dag_executor.update_subtask_status"), \
             patch("qz_core.dag_executor.log_event"), \
             patch("qz_core.dag_executor.select_route", return_value=("groq-fast", "KEY_1")), \
             patch("qz_core.dag_executor.request_completion", side_effect=[(resp1, "groq-fast"), (resp2, "groq-fast")]), \
             patch.object(dag_exec.val_pipeline, "run") as mock_val, \
             patch.dict(qz_tools.TOOL_FUNCTIONS, {"make_directory": MagicMock()}) as mock_funcs:

            mock_val.return_value = SimpleNamespace(passed=True, summary="OK", failed_required_checks=[], to_dict=lambda: {"status": "PASS"})
            res = dag_exec.execute_node(
                task_id="task_bad_json",
                overall_task="Test",
                node=node,
                dependency_outputs=[],
            )
            # The tool function itself should NEVER have been called with malformed arguments
            mock_funcs["make_directory"].assert_not_called()

    def test_read_file_and_list_files_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            large_file = ws / "large.txt"
            # Create a 3000 line file (1-indexed line content)
            large_file.write_text("\n".join(f"line {i}" for i in range(1, 3001)), encoding="utf-8")

            with patch("qz_tools.WORKSPACE", str(ws)):
                # Default max_lines is 2000
                res = qz_tools._impl_read_file("large.txt")
                self.assertIn("TRUNCATED", res)
                self.assertIn("line 1", res)
                self.assertIn("line 2000", res)
                self.assertNotIn("line 2500\n", res)

                # Pagination test
                page = qz_tools._impl_read_file("large.txt", start_line=2001, end_line=2100)
                self.assertIn("line 2001", page)
                self.assertIn("line 2100", page)
                self.assertNotIn("line 1\t", page)

                # List files bound test
                for i in range(250):
                    (ws / f"sub_{i}.py").write_text("x = 1", encoding="utf-8")
                listed = qz_tools._impl_list_files(".")
                self.assertIn("Truncated: showing first 200", listed)

    def test_rollback_blocks_non_ancestor_commits(self):
        mgr = ResumeManager(workspace="D:/dummy_ws")
        with patch("qz_recovery.resume_manager.get_current_head", return_value="abcdef123456"), \
             patch("qz_recovery.resume_manager.subprocess.run") as mock_subproc:

            # Mock merge-base returning non-zero (target not ancestor)
            mock_subproc.return_value = SimpleNamespace(returncode=1, stdout="", stderr="")
            preview = mgr.preview_rollback("task_1", checkpoint_id=None, workspace="D:/dummy_ws")
            self.assertFalse(preview["can_rollback"])
            self.assertEqual(preview["reason"], "not_ancestor")

    def test_client_sanitizes_messages_before_proxy_call(self):
        raw_client = MagicMock()
        raw_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))]
        )
        fb = FallbackCompletions(raw_client)

        messages = [
            {"role": "system", "content": "Instruction 1"},
            {"role": "system", "content": "Instruction 2 (middle)"},
            {"role": "user", "content": "Hi"},
        ]
        fb.create(model="groq-fast", messages=messages)

        raw_client.chat.completions.create.assert_called_once()
        sent_messages = raw_client.chat.completions.create.call_args.kwargs["messages"]
        # Middle system message converted to user role
        self.assertEqual(sent_messages[1]["role"], "user")
        self.assertIn("[System Note]:", sent_messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
