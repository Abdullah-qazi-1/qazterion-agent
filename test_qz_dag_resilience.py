"""DAG node loop resilience: tool crashes, cancellation, escalation, safe repair rollback.
Also covers the desktop bridge protocol channel."""

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import qz_tools
from qz_core.dag_executor import HardDAGExecutor
from qz_tasks.models import SubtaskStatus


def reply(content=None, calls=()):
    tool_calls = [
        SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(calls)
    ]
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def passing_report():
    return SimpleNamespace(passed=True, summary="OK", failed_required_checks=[], skipped_required_checks=[],
                           to_dict=lambda: {"status": "PASS"})


class NodeResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.ws_patch = patch.object(qz_tools, "WORKSPACE", str(self.ws))
        self.ws_patch.start()
        self.executor = HardDAGExecutor(workspace=self.ws, max_node_retries=2, max_node_iterations=6)
        self.patches = [
            patch("qz_core.dag_executor.update_status"),
            patch("qz_core.dag_executor.update_subtask_status"),
            patch("qz_core.dag_executor.log_event"),
            patch("qz_core.dag_executor.create_checkpoint"),
            patch("qz_core.dag_executor.classify_task_complexity", return_value="simple"),
            patch("qz_core.dag_executor.generate_commit_message", return_value="msg"),
            patch("qz_core.dag_executor.commit_changes", return_value="Git commit skipped: test"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.ws_patch.stop()
        self.tmp.cleanup()

    def node(self):
        return {"id": "n1", "title": "Do it", "description": "Do it"}

    def test_a_crashing_tool_is_reported_to_the_model_not_raised(self):
        def explode(**_kw):
            raise RuntimeError("disk on fire")

        with patch("qz_core.dag_executor.get_task", return_value={"status": "EXECUTING"}), \
             patch("qz_core.dag_executor.request_completion", side_effect=[
                 (reply(calls=[("list_files", {})]), "coder"), (reply("done"), "coder")]) as req, \
             patch.dict(qz_tools.TOOL_FUNCTIONS, {"list_files": explode}), \
             patch.object(self.executor.val_pipeline, "run", return_value=passing_report()):
            result = self.executor.execute_node("t", "task", self.node(), [])
        self.assertEqual(result.status, SubtaskStatus.COMPLETED)
        history = req.call_args_list[1].kwargs["messages"]
        tool_messages = [m for m in history if m.get("role") == "tool"]
        self.assertIn("Tool list_files failed: RuntimeError: disk on fire", tool_messages[0]["content"])

    def test_cancellation_mid_node_stops_without_validating_or_committing(self):
        statuses = iter([{"status": "EXECUTING"}, {"status": "EXECUTING"}, {"status": "CANCELLED"}])
        with patch("qz_core.dag_executor.get_task", side_effect=lambda _t: next(statuses, {"status": "CANCELLED"})), \
             patch("qz_core.dag_executor.request_completion", return_value=(reply(calls=[("list_files", {})]), "coder")), \
             patch.object(self.executor.val_pipeline, "run") as validate, \
             patch("qz_core.dag_executor.commit_changes") as commit:
            result = self.executor.execute_node("t", "task", self.node(), [])
        self.assertEqual(result.status, SubtaskStatus.CANCELLED)
        validate.assert_not_called()
        commit.assert_not_called()

    def test_repeated_failures_escalate_to_the_reasoner_role(self):
        fail = (reply(calls=[("run_command", {"command": "python -m pytest"})]), "coder")
        roles = []

        def fake_request(**kwargs):
            roles.append(kwargs["model"])
            return fail if len(roles) <= 3 else (reply("done"), kwargs["model"])

        with patch("qz_core.dag_executor.get_task", return_value={"status": "EXECUTING"}), \
             patch("qz_core.dag_executor.request_completion", side_effect=fake_request), \
             patch.dict(qz_tools.TOOL_FUNCTIONS, {"run_command": lambda **_k: "exit_code=1\nSTDOUT:\nFAILED\n"}), \
             patch.object(self.executor.val_pipeline, "run", return_value=passing_report()):
            self.executor.execute_node("t", "task", self.node(), [])
        self.assertEqual(roles[:3], ["fast", "fast", "fast"])
        self.assertEqual(roles[3], "reasoner")

    def test_build_failure_repair_undoes_only_agent_edits(self):
        from qz_validation import CheckResult, CheckStatus, ValidationReport

        user_file = self.ws / "settings.py"
        user_file.write_text("DEBUG = True  # user's uncommitted tweak\n", encoding="utf-8")
        failing = ValidationReport(
            status="FAIL",
            checks={"tests": CheckResult(name="tests", status=CheckStatus.FAIL, summary="Build failed",
                                         output="SyntaxError: invalid syntax", is_required=True)},
            summary="SyntaxError", required_checks={"tests"},
        )
        responses = [
            (reply(calls=[("write_file", {"path": "broken.py", "content": "def (:\n"})]), "fast"),
            (reply("done"), "fast"),
            (reply("retry done"), "fast"),
        ]
        with patch("qz_core.dag_executor.get_task", return_value={"status": "EXECUTING"}), \
             patch("qz_core.dag_executor.request_completion", side_effect=responses), \
             patch("qz_repair.classify_failure") as classify, \
             patch.object(self.executor.val_pipeline, "run", side_effect=[failing, passing_report()]):
            classify.return_value = SimpleNamespace(
                category=SimpleNamespace(value="build_failure"), check_name="tests",
                diagnostics=SimpleNamespace(error_signature="SyntaxError"),
                format_for_repair_prompt=lambda *a, **k: "fix it",
            )
            result = self.executor.execute_node("t", "task", self.node(), [])
        self.assertEqual(result.status, SubtaskStatus.COMPLETED)
        self.assertFalse((self.ws / "broken.py").exists())
        self.assertIn("user's uncommitted tweak", user_file.read_text(encoding="utf-8"))


class BridgeProtocolTests(unittest.TestCase):
    def test_concurrent_rpc_replies_all_reach_the_protocol_stream(self):
        import qz_desktop_bridge as bridge

        channel = io.StringIO()
        backend = MagicMock()
        backend.status.return_value = {"proxy": {"healthy": True}, "aliases": [], "configured_providers": [], "routing": {}}
        with patch.object(bridge, "_PROTOCOL_OUT", channel):
            threads = [
                threading.Thread(target=bridge._handle_request,
                                 args=(backend, json.dumps({"id": i, "method": "status", "params": {}})))
                for i in range(20)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        replies = [json.loads(line) for line in channel.getvalue().splitlines()]
        self.assertEqual(sorted(r["id"] for r in replies), list(range(20)))
        self.assertTrue(all(r["ok"] for r in replies))

    def test_decrypted_keys_are_never_returned_to_the_ui(self):
        import qz_desktop_bridge as bridge

        channel = io.StringIO()
        backend = MagicMock()
        with patch.object(bridge, "_PROTOCOL_OUT", channel):
            bridge._handle_request(backend, json.dumps({"id": 1, "method": "get_enabled_env", "params": {}}))
        reply_obj = json.loads(channel.getvalue())
        self.assertFalse(reply_obj["ok"])
        backend.keystore.enabled_env.assert_not_called()

    def test_rpc_handlers_do_not_change_the_global_workspace(self):
        import qz_desktop_bridge as bridge

        before = qz_tools.WORKSPACE
        with tempfile.TemporaryDirectory() as ws, patch.object(bridge, "_PROTOCOL_OUT", io.StringIO()):
            bridge._handle_request(MagicMock(), json.dumps({"id": 1, "method": "get_diffs", "params": {"workspace": ws}}))
        self.assertEqual(qz_tools.WORKSPACE, before)


if __name__ == "__main__":
    unittest.main()
