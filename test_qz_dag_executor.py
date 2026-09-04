"""Comprehensive tests for Phase 11: Hard DAG Execution Engine."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from qz_core.dag_executor import DAGExecutionResult, HardDAGExecutor, NodeResult
from qz_recovery import IntegrityStatus, ResumeManager
from qz_tasks.models import SubtaskStatus, TaskStatus
from qz_tasks.task_manager import (
    TaskManager,
    create_subtasks,
    create_task,
    get_manager,
    get_ready_subtasks,
    get_subtasks,
    get_task,
    get_task_events,
    mark_dependent_subtasks_blocked,
    reset_manager,
    update_status,
    update_subtask_status,
)
from qz_validation import CheckResult, CheckStatus, ValidationReport


def _fake_tool_completion(tool_name: str, args: dict, content: str = ""):
    tool_call = MagicMock()
    tool_call.id = "call_123"
    tool_call.function.name = tool_name
    tool_call.function.arguments = json.dumps(args)

    message = MagicMock()
    message.content = content
    message.tool_calls = [tool_call]
    message.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        "tool_calls": [{"id": "call_123", "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}],
    }

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    return response, "coder-strong"


def _fake_text_completion(content: str):
    message = MagicMock()
    message.content = content
    message.tool_calls = []
    message.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        "tool_calls": None,
    }

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    return response, "coder-strong"


class HardDAGExecutorUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_tasks.db"
        self.manager = TaskManager(self.db_path)
        # Patch default manager to point to this temp DB
        self._mgr_patch = patch("qz_tasks.task_manager.default_db_path", return_value=self.db_path)
        self._mgr_patch.start()
        self._class_patch = patch("qz_core.dag_executor.classify_task_complexity", return_value="standard")
        self._class_patch.start()
        reset_manager()

        self.workspace_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.workspace_dir.name)

        self.executor = HardDAGExecutor(
            workspace=self.workspace,
            max_node_retries=3,
            max_node_iterations=5,
        )

    def tearDown(self):
        self._class_patch.stop()
        self._mgr_patch.stop()
        reset_manager()
        self.temp_dir.cleanup()
        self.workspace_dir.cleanup()

    def test_node_result_and_dag_result_to_dict(self):
        nr = NodeResult(
            node_id="sub_1",
            status=SubtaskStatus.COMPLETED,
            summary="Created auth service",
            files_changed=[{"path": "auth.py", "kind": "new or rewritten file"}],
            commands_executed=["pytest tests/test_auth.py"],
            tests_run=["pytest tests/test_auth.py"],
            provider_id="groq",
            model_id="groq/llama-3.3-70b-versatile",
            attempts=1,
            duration=1.234,
        )
        d = nr.to_dict()
        self.assertEqual(d["node_id"], "sub_1")
        self.assertEqual(d["status"], "COMPLETED")
        self.assertEqual(d["provider_id"], "groq")
        self.assertEqual(d["model_id"], "groq/llama-3.3-70b-versatile")
        self.assertEqual(d["duration"], 1.234)

        dag_res = DAGExecutionResult(
            task_id="task_1",
            status=TaskStatus.COMPLETED,
            node_results=[nr],
            summary="All done",
            duration=2.5,
        )
        dag_dict = dag_res.to_dict()
        self.assertEqual(dag_dict["task_id"], "task_1")
        self.assertEqual(dag_dict["status"], "COMPLETED")
        self.assertEqual(len(dag_dict["node_results"]), 1)

    def test_linear_dag_sequential_execution(self):
        task_id = create_task("Build calculator with ops and tests", str(self.workspace))
        subtasks_spec = [
            {"id": 0, "title": "Create Add Op", "description": "Implement add in ops.py", "depends_on": []},
            {"id": 1, "title": "Create Sub Op", "description": "Implement sub in ops.py", "depends_on": [0]},
            {"id": 2, "title": "Add Unit Tests", "description": "Write tests in test_ops.py", "depends_on": [1]},
        ]
        created = create_subtasks(task_id, subtasks_spec)
        self.assertEqual(len(created), 3)

        # Mock request_completion to return text response for each node turn
        with patch("qz_core.dag_executor.request_completion", side_effect=[
            _fake_text_completion("Implemented add operation."),
            _fake_text_completion("Implemented sub operation."),
            _fake_text_completion("Implemented unit tests."),
        ]), patch.object(self.executor.val_pipeline, "run", return_value=ValidationReport(
            status="PASS",
            checks={"tests": CheckResult(name="tests", status=CheckStatus.PASS, summary="All tests passed", is_required=True)},
            summary="Validation Gate: PASS",
        )):
            res = self.executor.execute_dag(task_id, "Build calculator with ops and tests")

        self.assertEqual(res.status, TaskStatus.COMPLETED)
        self.assertEqual(len(res.node_results), 3)

        # Verify all subtasks in DB are COMPLETED
        persisted = get_subtasks(task_id)
        for s in persisted:
            self.assertEqual(s["status"], SubtaskStatus.COMPLETED)

        # Verify DAG activity events
        events = get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("DAG_STARTED", event_types)
        self.assertIn("NODE_STARTED", event_types)
        self.assertIn("NODE_COMPLETED", event_types)
        self.assertIn("DAG_COMPLETED", event_types)

    def test_diamond_dag_dependency_ordering(self):
        task_id = create_task("Diamond DAG Task", str(self.workspace))
        subtasks_spec = [
            {"id": "root", "title": "Root Task", "description": "Base models", "depends_on": []},
            {"id": "branch_a", "title": "Branch A", "description": "Service A", "depends_on": ["root"]},
            {"id": "branch_b", "title": "Branch B", "description": "Service B", "depends_on": ["root"]},
            {"id": "join", "title": "Join Task", "description": "Integration", "depends_on": ["branch_a", "branch_b"]},
        ]
        create_subtasks(task_id, subtasks_spec)

        execution_order = []

        def mock_execute_node(task_id, overall_task, node, dependency_outputs, test_baseline=None):
            execution_order.append(node["title"])
            update_subtask_status(node["id"], SubtaskStatus.COMPLETED, result_summary=f"{node['title']} done")
            return NodeResult(
                node_id=str(node["id"]),
                status=SubtaskStatus.COMPLETED,
                summary=f"{node['title']} done",
                duration=0.1,
            )

        with patch.object(self.executor, "execute_node", side_effect=mock_execute_node):
            res = self.executor.execute_dag(task_id, "Diamond DAG Task")

        self.assertEqual(res.status, TaskStatus.COMPLETED)
        self.assertEqual(len(execution_order), 4)
        # Root must execute first
        self.assertEqual(execution_order[0], "Root Task")
        # Join must execute last
        self.assertEqual(execution_order[3], "Join Task")
        # Branches A and B must execute in between
        self.assertEqual(set(execution_order[1:3]), {"Branch A", "Branch B"})

    def test_dependency_output_propagation_between_nodes(self):
        task_id = create_task("Dependency context test", str(self.workspace))
        subtasks_spec = [
            {"id": "node_1", "title": "Design Database", "description": "Define User table", "depends_on": []},
            {"id": "node_2", "title": "Build API", "description": "Use User table", "depends_on": ["node_1"]},
        ]
        create_subtasks(task_id, subtasks_spec)

        passed_dependency_contexts = []

        def mock_execute_node(task_id, overall_task, node, dependency_outputs, test_baseline=None):
            passed_dependency_contexts.append(dependency_outputs)
            update_subtask_status(
                node["id"],
                SubtaskStatus.COMPLETED,
                result_summary=f"Finished {node['title']} with custom tables",
                files_touched=["models.py"],
            )
            return NodeResult(
                node_id=str(node["id"]),
                status=SubtaskStatus.COMPLETED,
                summary=f"Finished {node['title']}",
                duration=0.1,
            )

        with patch.object(self.executor, "execute_node", side_effect=mock_execute_node):
            self.executor.execute_dag(task_id, "Dependency context test")

        self.assertEqual(len(passed_dependency_contexts), 2)
        # Node 1 has no prerequisites
        self.assertEqual(passed_dependency_contexts[0], [])
        # Node 2 receives summary & files touched from Node 1
        self.assertEqual(len(passed_dependency_contexts[1]), 1)
        dep1 = passed_dependency_contexts[1][0]
        self.assertEqual(dep1["title"], "Design Database")
        self.assertIn("custom tables", dep1["result_summary"])
        self.assertEqual(dep1["files_touched"], ["models.py"])

    def test_node_validation_failure_triggers_repair_loop_and_succeeds(self):
        task_id = create_task("Repair loop test", str(self.workspace))
        create_subtasks(task_id, [
            {"id": 0, "title": "Fix Math Op", "description": "Fix division by zero", "depends_on": []},
        ])

        node = get_subtasks(task_id)[0]

        fail_report = ValidationReport(
            status="FAIL",
            checks={"tests": CheckResult(name="tests", status=CheckStatus.FAIL, summary="ZeroDivisionError in test_div", is_required=True)},
            summary="Validation Gate: FAIL (tests failed)",
        )
        pass_report = ValidationReport(
            status="PASS",
            checks={"tests": CheckResult(name="tests", status=CheckStatus.PASS, summary="All tests passed", is_required=True)},
            summary="Validation Gate: PASS",
        )

        with patch("qz_core.dag_executor.request_completion", side_effect=[
            _fake_text_completion("First attempt: wrote buggy code."),
            _fake_text_completion("Second attempt: fixed ZeroDivisionError check."),
        ]), patch.object(self.executor.val_pipeline, "run", side_effect=[fail_report, pass_report]):
            result = self.executor.execute_node(task_id, "Fix Math Op", node, [])

        self.assertEqual(result.status, SubtaskStatus.COMPLETED)
        self.assertEqual(result.attempts, 2)

        # Check DB status is COMPLETED
        subtask_db = get_subtasks(task_id)[0]
        self.assertEqual(subtask_db["status"], SubtaskStatus.COMPLETED)
        self.assertEqual(subtask_db["attempts"], 2)

        # Events verify RETRYING was logged
        events = get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("NODE_RETRYING", event_types)
        self.assertIn("NODE_COMPLETED", event_types)

    def test_retry_exhaustion_marks_node_failed_and_blocks_dependents(self):
        task_id = create_task("Failure cascade test", str(self.workspace))
        created = create_subtasks(task_id, [
            {"id": "step_a", "title": "Step A", "description": "Fails permanently", "depends_on": []},
            {"id": "step_b", "title": "Step B", "description": "Depends on A", "depends_on": ["step_a"]},
            {"id": "step_c", "title": "Step C", "description": "Depends on B", "depends_on": ["step_b"]},
        ])
        sa_id, sb_id, sc_id = created[0]["id"], created[1]["id"], created[2]["id"]

        fail_report = ValidationReport(
            status="FAIL",
            checks={"tests": CheckResult(name="tests", status=CheckStatus.FAIL, summary="SyntaxError", is_required=True)},
            summary="Validation Gate: FAIL",
        )

        with patch("qz_core.dag_executor.request_completion", return_value=_fake_text_completion("Code change")), \
             patch.object(self.executor.val_pipeline, "run", return_value=fail_report):
            dag_result = self.executor.execute_dag(task_id, "Failure cascade test")

        self.assertEqual(dag_result.status, TaskStatus.FAILED)

        # Verify subtask states in DB
        subtasks = {s["id"]: s for s in get_subtasks(task_id)}
        self.assertEqual(subtasks[sa_id]["status"], SubtaskStatus.FAILED)
        self.assertEqual(subtasks[sb_id]["status"], SubtaskStatus.BLOCKED)
        self.assertEqual(subtasks[sc_id]["status"], SubtaskStatus.BLOCKED)

        # Verify overall task is FAILED
        task = get_task(task_id)
        self.assertEqual(task["status"], TaskStatus.FAILED)

        # Verify NODE_BLOCKED event was logged
        events = get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("NODE_FAILED", event_types)
        self.assertIn("NODE_BLOCKED", event_types)
        self.assertIn("DAG_FAILED", event_types)

    def test_cancellation_stops_execution_and_persists_safely(self):
        task_id = create_task("Cancel test", str(self.workspace))
        create_subtasks(task_id, [
            {"id": 0, "title": "Node 0", "description": "First", "depends_on": []},
            {"id": 1, "title": "Node 1", "description": "Second", "depends_on": [0]},
        ])

        # Cancel the task in DB before execution loop begins
        update_status(task_id, TaskStatus.CANCELLED)

        dag_result = self.executor.execute_dag(task_id, "Cancel test")
        self.assertEqual(dag_result.status, TaskStatus.CANCELLED)

        events = get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("TASK_CANCELLED", event_types)

    def test_resume_idempotency_does_not_rerun_completed_nodes(self):
        task_id = create_task("Resume test", str(self.workspace))
        created = create_subtasks(task_id, [
            {"id": "node_1", "title": "Node 1", "description": "Done earlier", "depends_on": []},
            {"id": "node_2", "title": "Node 2", "description": "Interrupted", "depends_on": ["node_1"]},
            {"id": "node_3", "title": "Node 3", "description": "Pending", "depends_on": ["node_2"]},
        ])
        n1_id, n2_id, n3_id = created[0]["id"], created[1]["id"], created[2]["id"]

        # Simulate Node 1 completed in previous session
        update_subtask_status(n1_id, SubtaskStatus.COMPLETED, result_summary="Already done")

        executed_node_ids = []

        def mock_execute_node(task_id, overall_task, node, dependency_outputs, test_baseline=None):
            executed_node_ids.append(node["id"])
            update_subtask_status(node["id"], SubtaskStatus.COMPLETED, result_summary=f"Done {node['id']}")
            return NodeResult(node_id=str(node["id"]), status=SubtaskStatus.COMPLETED, duration=0.1)

        with patch.object(self.executor, "execute_node", side_effect=mock_execute_node):
            dag_result = self.executor.execute_dag(task_id, "Resume test", resume_from_subtask_id=n2_id)

        self.assertEqual(dag_result.status, TaskStatus.COMPLETED)
        # Node 1 must NOT have been executed again!
        self.assertNotIn(n1_id, executed_node_ids)
        # Node 2 and Node 3 must be executed
        self.assertEqual(executed_node_ids, [n2_id, n3_id])

    def test_resume_manager_integration(self):
        task_id = create_task("Recovery integration test", str(self.workspace))
        created = create_subtasks(task_id, [
            {"id": "sub_1", "title": "Subtask 1", "description": "Done", "depends_on": []},
            {"id": "sub_2", "title": "Subtask 2", "description": "Pending", "depends_on": ["sub_1"]},
        ])
        s1_id, s2_id = created[0]["id"], created[1]["id"]
        update_subtask_status(s1_id, SubtaskStatus.COMPLETED, result_summary="Sub 1 complete")

        rm = ResumeManager(self.workspace)
        state = rm.get_resumable_state(task_id)
        self.assertEqual(len(state.completed_subtasks), 1)
        self.assertEqual(len(state.pending_subtasks), 1)
        self.assertEqual(state.next_ready_subtask["id"], s2_id)

        # Verify resume_task calls HardDAGExecutor
        with patch.object(HardDAGExecutor, "execute_node", return_value=NodeResult(
            node_id=s2_id, status=SubtaskStatus.COMPLETED, duration=0.1
        )):
            with patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[]), \
                 patch("qz_recovery.resume_manager.get_current_head", return_value=None):
                outcome = rm.resume_task(task_id, workspace=self.workspace, decision="RESUME")

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.action, "RESUMED")

    def test_node_execution_boundary_ignores_claims_about_future_nodes(self):
        task_id = create_task("Node boundary test", str(self.workspace))
        created = create_subtasks(task_id, [
            {"id": "node_a", "title": "Node A", "description": "Step A", "depends_on": []},
            {"id": "node_b", "title": "Node B", "description": "Step B", "depends_on": ["node_a"]},
        ])
        na_id, nb_id = created[0]["id"], created[1]["id"]

        # When Node A is executed, model claims it did both Node A and Node B
        with patch("qz_core.dag_executor.request_completion", return_value=_fake_text_completion(
            "I have finished Node A and also fully completed Node B! Everything is done."
        )), patch.object(self.executor.val_pipeline, "run", return_value=ValidationReport(
            status="PASS", checks={}, summary="PASS"
        )):
            node_result = self.executor.execute_node(task_id, "Goal", created[0], [])

        self.assertEqual(node_result.status, SubtaskStatus.COMPLETED)
        self.assertEqual(node_result.node_id, na_id)

        # Python authority check: Node A is COMPLETED, Node B MUST STILL BE PENDING!
        subtasks = {s["id"]: s for s in get_subtasks(task_id)}
        self.assertEqual(subtasks[na_id]["status"], SubtaskStatus.COMPLETED)
        self.assertEqual(subtasks[nb_id]["status"], SubtaskStatus.PENDING)

    def test_dag_execution_fallback_for_empty_subtasks(self):
        task_id = create_task("Fallback single node test", str(self.workspace))
        # No subtasks created in DB beforehand

        with patch("qz_core.dag_executor.request_completion", return_value=_fake_text_completion("Single node completed.")), \
             patch.object(self.executor.val_pipeline, "run", return_value=ValidationReport(
                 status="PASS", checks={}, summary="PASS"
             )):
            dag_result = self.executor.execute_dag(task_id, "Fallback single node test", plan_text="Simple plan")

        self.assertEqual(dag_result.status, TaskStatus.COMPLETED)
        persisted = get_subtasks(task_id)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["status"], SubtaskStatus.COMPLETED)

    def test_malformed_tool_json_handled_safely(self):
        task_id = create_task("Malformed tool test", str(self.workspace))
        created = create_subtasks(task_id, [
            {"id": 0, "title": "Run tool", "description": "Desc", "depends_on": []},
        ])

        # Malformed tool arguments
        tool_call = MagicMock()
        tool_call.id = "bad_call"
        tool_call.function.name = "read_file"
        tool_call.function.arguments = "INVALID_JSON_NOT_AN_OBJECT"

        msg1 = MagicMock()
        msg1.content = None
        msg1.tool_calls = [tool_call]
        msg1.model_dump.return_value = {"role": "assistant", "content": None, "tool_calls": [{"id": "bad_call"}]}
        resp1 = MagicMock(choices=[MagicMock(message=msg1)])

        msg2 = MagicMock(content="Finished gracefully", tool_calls=[])
        msg2.model_dump.return_value = {"role": "assistant", "content": "Finished gracefully"}
        resp2 = MagicMock(choices=[MagicMock(message=msg2)])

        with patch("qz_core.dag_executor.request_completion", side_effect=[(resp1, "coder-strong"), (resp2, "coder-strong")]), \
             patch.object(self.executor.val_pipeline, "run", return_value=ValidationReport(
                 status="PASS", checks={}, summary="PASS"
             )):
            res = self.executor.execute_node(task_id, "Goal", created[0], [])

        self.assertEqual(res.status, SubtaskStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
