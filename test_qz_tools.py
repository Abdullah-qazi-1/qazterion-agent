import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import qz_tools
import qz_agent


class QzToolsTests(unittest.TestCase):
    def test_console_streams_are_utf8_safe(self):
        enc = (qz_agent.sys.stdout.encoding or "").lower().replace("-", "")
        self.assertTrue(enc in ("utf8", "ascii", "ansix3.41968", "usascii") or "utf" in enc)

    def test_safe_path_blocks_prefix_sibling(self):
        with tempfile.TemporaryDirectory() as parent:
            workspace = os.path.join(parent, "project")
            sibling = os.path.join(parent, "project-other", "secret.txt")
            os.makedirs(workspace)
            with patch.object(qz_tools, "WORKSPACE", workspace):
                with self.assertRaises(ValueError):
                    qz_tools._safe_path(sibling)

    @unittest.skipUnless(os.name == "nt", "PowerShell command shape only applies on Windows")
    def test_run_command_uses_powershell(self):
        from qz_sandbox.manager import SandboxManager

        with patch("qz_tools.get_manager", return_value=SandboxManager(docker_available=False)):
            with patch("qz_sandbox.backend.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "done"
                run.return_value.stderr = ""
                result = qz_tools.run_command("Write-Output done")

        self.assertIn("exit_code=0", result)
        self.assertEqual(run.call_args.args[0][:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_workspace_file_tools_and_patch_handle_success_and_stale_context(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(qz_tools, "WORKSPACE", workspace):
                self.assertIn("Written:", qz_tools.write_file("src/example.py", "first = 1\nsecond = 2\n"))
                self.assertIn("src", qz_tools.list_files())
                self.assertEqual(qz_tools.read_file("src/example.py"), "    1\tfirst = 1\n    2\tsecond = 2\n")

                result = qz_tools.apply_patch(
                    "src/example.py", "@@ -1,2 +1,2 @@\n-first = 1\n+first = 10\n second = 2\n"
                )
                self.assertIn("Patch applied:", result)
                self.assertEqual(qz_tools.read_file("src/example.py"), "    1\tfirst = 10\n    2\tsecond = 2\n")

                stale_result = qz_tools.apply_patch(
                    "src/example.py", "@@ -1,1 +1,1 @@\n-first = 1\n+first = 100\n"
                )
                self.assertIn("Context mismatch", stale_result)
                self.assertEqual(qz_tools.read_file("src/example.py"), "    1\tfirst = 10\n    2\tsecond = 2\n")

    def test_list_files_accepts_path_as_a_directory_alias(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(qz_tools, "WORKSPACE", workspace):
                qz_tools.write_file("src/example.py", "first = 1\n")
                # This is the exact call shape small/free models kept guessing —
                # {'path': ''} instead of {'directory': ''} — which previously
                # raised "unexpected keyword argument 'path'".
                self.assertIn("src", qz_tools.list_files(path=""))
                self.assertIn("src", qz_tools.list_files(path="."))
                # 'directory' still works and still takes priority if both given.
                self.assertIn("src", qz_tools.list_files(directory="."))

    def test_patch_can_add_content_to_an_empty_existing_file(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(qz_tools, "WORKSPACE", workspace):
                qz_tools.write_file("empty.py", "")
                result = qz_tools.apply_patch("empty.py", "@@ -0,0 +1 @@\n+value = 1\n")
                self.assertIn("Patch applied:", result)
                self.assertEqual(qz_tools.read_file("empty.py"), "    1\tvalue = 1\n")

    def test_run_command_reports_real_success_and_failure(self):
        if os.name == "nt":
            success = qz_tools.run_command("Write-Output qa-ok")
            failure = qz_tools.run_command("Write-Error qa-fail; exit 7")
        else:
            success = qz_tools.run_command("echo qa-ok")
            failure = qz_tools.run_command("echo qa-fail 1>&2; exit 7")
        self.assertIn("exit_code=0", success)
        self.assertIn("qa-ok", success)
        self.assertIn("exit_code=7", failure)
        self.assertIn("qa-fail", failure)

    def test_unittest_discovery_supports_a_non_package_tests_directory(self):
        with tempfile.TemporaryDirectory() as workspace:
            os.makedirs(os.path.join(workspace, "tests"))
            with open(os.path.join(workspace, "tests", "test_example.py"), "w", encoding="utf-8") as test_file:
                test_file.write(
                    "import unittest\n\n"
                    "class ExampleTests(unittest.TestCase):\n"
                    "    def test_value(self):\n"
                    "        self.assertEqual(2 + 2, 4)\n"
                )
            with patch.object(qz_tools, "WORKSPACE", workspace):
                result = qz_tools.run_command("python -m unittest discover -s tests")
        self.assertIn("exit_code=0", result)

    def test_git_commits_only_agent_paths_and_rolls_back_a_clean_latest_commit(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(qz_tools, "WORKSPACE", workspace):
                self.assertIn("initialized", qz_tools.ensure_git_repository())
                qz_tools.write_file("first.py", "value = 1\n")
                first_commit = qz_tools.commit_changes(["first.py"], "Add first file")
                self.assertIn("Git commit created:", first_commit)

                qz_tools.write_file("second.py", "value = 2\n")
                second_commit = qz_tools.commit_changes(["second.py"], "Add second file")
                self.assertIn("Git commit created:", second_commit)

                self.assertIn("Rollback complete:", qz_tools.rollback_last_change())
                self.assertTrue(os.path.exists(os.path.join(workspace, "first.py")))
                self.assertFalse(os.path.exists(os.path.join(workspace, "second.py")))

    def test_git_refuses_to_stage_sensitive_files_or_rollback_dirty_work(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(qz_tools, "WORKSPACE", workspace):
                qz_tools.ensure_git_repository()
                qz_tools.write_file(".env", "API_KEY=secret\n")
                result = qz_tools.commit_changes([".env"], "Never commit credentials")
                self.assertIn("Sensitive file", result)
                self.assertIn("Rollback refused", qz_tools.rollback_last_change())

    def test_complexity_heuristic_and_command_failure_detection(self):
        with patch.object(qz_agent.client.chat.completions, "create", side_effect=RuntimeError("proxy unavailable")):
            self.assertEqual(qz_agent.classify_task_complexity("Rename one variable"), "simple")
            self.assertEqual(qz_agent.classify_task_complexity("Refactor the multi-file architecture"), "complex")

        self.assertFalse(qz_agent._tool_result_failed("read_file", "exit_code=1"))
        self.assertFalse(qz_agent._tool_result_failed("run_command", "exit_code=0"))
        self.assertTrue(qz_agent._tool_result_failed("run_command", "exit_code=1"))

    def test_rolling_summary_condenses_old_history_and_keeps_latest_messages(self):
        messages = [{"role": "system", "content": "rules"}]
        messages.extend({"role": "user", "content": f"completed step {number}"} for number in range(10))
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Steps 0-6 completed."))])

        with patch.object(qz_agent.client.chat.completions, "create", return_value=response) as create:
            condensed, summarized = qz_agent.roll_conversation_summary(messages)

        self.assertTrue(summarized)
        self.assertEqual(len(condensed), 5)  # system, summary, and the latest three messages
        self.assertEqual(condensed[0], messages[0])
        self.assertEqual(condensed[1]["role"], "system")
        self.assertIn("Steps 0-6 completed.", condensed[1]["content"])
        self.assertEqual([item["content"] for item in condensed[2:]], ["completed step 7", "completed step 8", "completed step 9"])
        self.assertEqual(create.call_args.kwargs["model"], "groq-fast")

    def test_rolling_summary_keeps_a_complete_tool_exchange(self):
        messages = [{"role": "system", "content": "rules"}]
        messages.extend({"role": "user", "content": f"old {number}"} for number in range(7))
        tool_call = {"id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
        messages.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})
        messages.extend(
            {"role": "tool", "tool_call_id": "call-1", "content": f"tool result {number}"}
            for number in range(3)
        )
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Old work completed."))])

        with patch.object(qz_agent.client.chat.completions, "create", return_value=response):
            condensed, summarized = qz_agent.roll_conversation_summary(messages)

        self.assertTrue(summarized)
        self.assertEqual(condensed[2]["role"], "assistant")
        self.assertEqual(condensed[2]["tool_calls"], [tool_call])
        self.assertEqual([item["role"] for item in condensed[3:]], ["tool", "tool", "tool"])

    def test_rolling_summary_falls_back_to_full_history_if_model_fails(self):
        messages = [{"role": "system", "content": "rules"}]
        messages.extend({"role": "user", "content": f"step {number}"} for number in range(10))

        with patch.object(qz_agent.client.chat.completions, "create", side_effect=RuntimeError("proxy unavailable")):
            unchanged, summarized = qz_agent.roll_conversation_summary(messages)

        self.assertFalse(summarized)
        self.assertIs(unchanged, messages)

    def test_self_review_uses_only_changed_code_and_accepts_clean_result(self):
        changes = [
            {"path": "src/auth.py", "kind": "patch", "detail": "@@ -1 +1 @@\n-return False\n+return True\n"},
        ]
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="NO_ISSUES"))])

        with patch.object(qz_agent.client.chat.completions, "create", return_value=response) as create:
            result = qz_agent.self_review(changes)

        self.assertIsNone(result)
        self.assertEqual(create.call_args.kwargs["model"], "reasoner")
        review_input = create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("src/auth.py", review_input)
        self.assertIn("return True", review_input)
        self.assertNotIn("unrelated.py", review_input)

    def test_self_review_returns_findings_and_fails_open(self):
        changes = [{"path": "src/api.py", "kind": "new or rewritten file", "detail": "def endpoint(): pass"}]
        issue_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Validate empty input before processing."))])

        with patch.object(qz_agent.client.chat.completions, "create", return_value=issue_response):
            self.assertEqual(qz_agent.self_review(changes), "Validate empty input before processing.")
        with patch.object(qz_agent.client.chat.completions, "create", side_effect=RuntimeError("review proxy down")):
            self.assertIsNone(qz_agent.self_review(changes))


if __name__ == "__main__":
    unittest.main()