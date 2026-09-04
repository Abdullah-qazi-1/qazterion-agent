"""Unit and integration tests for qz_recovery (ResumeManager, workspace integrity, and bridge RPC)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qz_recovery.resume_manager import (
    IntegrityResult,
    IntegrityStatus,
    ResumeManager,
    ResumeOutcome,
    ResumePlan,
    get_resume_manager,
)
from qz_tasks.models import SubtaskStatus, TaskStatus
from qz_tasks.task_manager import TaskManager


class ResumeManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_tasks.db"
        self.task_manager = TaskManager(self.db_path)
        self.patcher = patch("qz_recovery.resume_manager.get_manager", return_value=self.task_manager)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp_dir.cleanup()

    def test_get_resumable_state_identifies_completed_vs_pending_subtasks(self):
        task_id = self.task_manager.create_task("Implement feature", str(self.tmp_dir.name))
        self.task_manager.update_status(task_id, TaskStatus.EXECUTING, plan_text="1. Do A\n2. Do B\n3. Do C")

        subtasks = self.task_manager.create_subtasks(task_id, [
            {"id": "sub-1", "title": "Subtask 1", "description": "Do A", "depends_on": []},
            {"id": "sub-2", "title": "Subtask 2", "description": "Do B", "depends_on": [0]},
            {"id": "sub-3", "title": "Subtask 3", "description": "Do C", "depends_on": [1]},
        ])

        # Mark sub-1 as COMPLETED
        self.task_manager.update_subtask_status(subtasks[0]["id"], SubtaskStatus.COMPLETED)
        self.task_manager.create_checkpoint(task_id, iteration_number=1, git_commit_hash="abc1234", summary="Finished subtask 1")

        manager = ResumeManager(self.tmp_dir.name)
        plan = manager.get_resumable_state(task_id)

        self.assertEqual(plan.task_id, task_id)
        self.assertEqual(plan.total_subtasks, 3)
        self.assertEqual(len(plan.completed_subtasks), 1)
        self.assertEqual(len(plan.pending_subtasks), 2)
        self.assertIsNotNone(plan.next_ready_subtask)
        self.assertEqual(plan.next_ready_subtask["id"], subtasks[1]["id"])
        self.assertEqual(plan.latest_checkpoint["git_commit_hash"], "abc1234")

    def test_verify_workspace_integrity_returns_clean_dirty_and_diverged(self):
        task_id = self.task_manager.create_task("Test task", str(self.tmp_dir.name))
        self.task_manager.create_checkpoint(task_id, iteration_number=1, git_commit_hash="abc1234567890")

        manager = ResumeManager(self.tmp_dir.name)

        # 1. CLEAN: No uncommitted changes, HEAD matches checkpoint
        with (
            patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[]),
            patch("qz_recovery.resume_manager.get_current_head", return_value="abc1234567890"),
        ):
            result = manager.verify_workspace_integrity(task_id)
            self.assertEqual(result.status, IntegrityStatus.CLEAN)
            self.assertIn("matches checkpoint", result.message.lower())

        # 2. DIRTY_UNCOMMITTED: Uncommitted files present
        with (
            patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[" M foo.py", "?? bar.py"]),
            patch("qz_recovery.resume_manager.get_current_head", return_value="abc1234567890"),
        ):
            result = manager.verify_workspace_integrity(task_id)
            self.assertEqual(result.status, IntegrityStatus.DIRTY_UNCOMMITTED)
            self.assertEqual(len(result.uncommitted_files), 2)

        # 3. DIVERGED: No uncommitted changes, but HEAD differs from checkpoint
        with (
            patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[]),
            patch("qz_recovery.resume_manager.get_current_head", return_value="fed9876543210"),
        ):
            result = manager.verify_workspace_integrity(task_id)
            self.assertEqual(result.status, IntegrityStatus.DIVERGED)
            self.assertIn("does not match", result.message.lower())

    def test_resume_task_refuses_to_run_executor_when_integrity_not_clean(self):
        task_id = self.task_manager.create_task("Test task", str(self.tmp_dir.name))
        self.task_manager.create_checkpoint(task_id, iteration_number=1, git_commit_hash="abc1234567890")

        manager = ResumeManager(self.tmp_dir.name)

        # Mock DIRTY_UNCOMMITTED
        with (
            patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[" M modified.py"]),
            patch("qz_recovery.resume_manager.get_current_head", return_value="abc1234567890"),
            patch("qz_core.executor.run_executor") as mock_executor,
        ):
            outcome = manager.resume_task(task_id, decision="RESUME")
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.action, "BLOCKED")
            mock_executor.assert_not_called()

        # Mock DIVERGED
        with (
            patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[]),
            patch("qz_recovery.resume_manager.get_current_head", return_value="different_hash"),
            patch("qz_core.executor.run_executor") as mock_executor,
        ):
            outcome = manager.resume_task(task_id, decision="RESUME")
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.action, "BLOCKED")
            mock_executor.assert_not_called()

    def test_resume_task_invokes_executor_from_first_non_completed_subtask_when_clean(self):
        task_id = self.task_manager.create_task("Build feature", str(self.tmp_dir.name))
        subtasks = self.task_manager.create_subtasks(task_id, [
            {"id": "s1", "title": "Subtask 1", "description": "Part 1"},
            {"id": "s2", "title": "Subtask 2", "description": "Part 2"},
        ])
        self.task_manager.update_subtask_status(subtasks[0]["id"], SubtaskStatus.COMPLETED)
        self.task_manager.create_checkpoint(task_id, iteration_number=1, git_commit_hash="head123")

        manager = ResumeManager(self.tmp_dir.name)

        with (
            patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[]),
            patch("qz_recovery.resume_manager.get_current_head", return_value="head123"),
            patch("qz_core.executor.run_executor", return_value="Completed remaining subtasks") as mock_executor,
        ):
            outcome = manager.resume_task(task_id, decision="RESUME")
            self.assertTrue(outcome.success)
            self.assertEqual(outcome.action, "RESUMED")
            mock_executor.assert_called_once()
            call_kwargs = mock_executor.call_args.kwargs
            self.assertEqual(call_kwargs["resume_from_subtask_id"], subtasks[1]["id"])
            self.assertEqual(call_kwargs["task_id"], task_id)

    def test_resume_decisions_produce_correct_status_and_event_outcomes(self):
        manager = ResumeManager(self.tmp_dir.name)

        # 1. RESTART_CURRENT_SUBTASK
        task_id = self.task_manager.create_task("Restart subtask test", str(self.tmp_dir.name))
        subtasks = self.task_manager.create_subtasks(task_id, [{"id": "sub-fail", "title": "Failing subtask"}])
        self.task_manager.update_subtask_status(subtasks[0]["id"], SubtaskStatus.FAILED)

        with (
            patch("qz_recovery.resume_manager.get_uncommitted_files", return_value=[]),
            patch("qz_recovery.resume_manager.get_current_head", return_value="head123"),
            patch("qz_core.executor.run_executor", return_value="Restarted successfully") as mock_executor,
        ):
            outcome = manager.resume_task(task_id, decision="RESTART_CURRENT_SUBTASK")
            self.assertTrue(outcome.success)
            self.assertEqual(outcome.action, "RESTARTED_SUBTASK")
            # Subtask status was reset to PENDING
            updated_subtask = self.task_manager.get_subtasks(task_id)[0]
            self.assertEqual(updated_subtask["status"], SubtaskStatus.PENDING)
            mock_executor.assert_called_once()

        # 2. ROLLBACK
        task_id_rollback = self.task_manager.create_task("Rollback test", str(self.tmp_dir.name))
        self.task_manager.create_checkpoint(task_id_rollback, iteration_number=1, git_commit_hash="c1")
        with patch("qz_tools.rollback_last_change", return_value="Rollback complete: reset to c1."):
            outcome = manager.resume_task(task_id_rollback, decision="ROLLBACK")
            self.assertTrue(outcome.success)
            self.assertEqual(outcome.action, "ROLLED_BACK")
            task = self.task_manager.get_task(task_id_rollback)
            self.assertEqual(task["status"], TaskStatus.CANCELLED)

        # 3. DISCARD
        task_id_discard = self.task_manager.create_task("Discard test", str(self.tmp_dir.name))
        outcome = manager.resume_task(task_id_discard, decision="DISCARD")
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.action, "DISCARDED")
        task = self.task_manager.get_task(task_id_discard)
        self.assertEqual(task["status"], TaskStatus.CANCELLED)


class ExecutorResumeRegressionTests(unittest.TestCase):
    def test_run_executor_preserves_behavior_when_resume_from_subtask_id_omitted(self):
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content="Done with task without resume context.",
                        tool_calls=[],
                        model_dump=lambda exclude_none=True: {"role": "assistant", "content": "Done with task without resume context."},
                    )
                )
            ]
        )

        from qz_validation import ValidationReport

        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.run.return_value = ValidationReport(
            status="PASS",
            checks={},
            summary="All validation passed",
        )

        from qz_core.executor import run_executor

        with (
            patch("qz_agent.classify_task_complexity", return_value="simple", create=True),
            patch("qz_agent.select_route", return_value=("groq-fast", "GROQ_KEY_1"), create=True),
            patch("qz_agent.request_completion", return_value=(mock_response, "groq-fast"), create=True),
            patch("qz_agent.ValidationPipeline", return_value=mock_pipeline_instance, create=True),
            patch("qz_core.executor.classify_task_complexity", return_value="simple"),
            patch("qz_core.executor.select_route", return_value=("groq-fast", "GROQ_KEY_1")),
            patch("qz_core.executor.request_completion", return_value=(mock_response, "groq-fast")),
            patch("qz_core.executor.ValidationPipeline", return_value=mock_pipeline_instance),
            patch("qz_core.executor._task_requires_test_changes", return_value=False),
            patch("qz_validation.checks.run_command", return_value="exit_code=1\nSTDOUT:\nFAILED test_math.py\n"),
            patch("qz_tasks.task_manager.update_status"),
            patch("qz_tasks.task_manager.log_event"),
        ):
            # Calling run_executor without resume_from_subtask_id (default)
            result = run_executor(
                task="Normal execution task",
                plan="Step 1\nStep 2",
                architecture="Architecture",
                max_iterations=1,
            )
            # The hard DAG executor returns a consolidated summary prefixed with the
            # completed node's id (e.g. "[<node_id>] <summary>") rather than the bare
            # model response the legacy single-loop executor used to return directly.
            self.assertIn("Done with task without resume context.", result)


class BridgeRecoveryRpcTests(unittest.TestCase):
    def test_bridge_get_resume_options_and_resume_task(self):
        from qz_desktop_bridge import _dispatch

        mock_backend = MagicMock()
        mock_plan = ResumePlan(
            task_id="task-rpc-1",
            task_status="EXECUTING",
            total_subtasks=2,
            completed_subtasks=[{"id": "sub-1", "title": "Sub 1", "status": "COMPLETED"}],
            pending_subtasks=[{"id": "sub-2", "title": "Sub 2", "status": "PENDING"}],
            next_ready_subtask={"id": "sub-2", "title": "Sub 2", "status": "PENDING"},
            latest_checkpoint={"git_commit_hash": "abc"},
            summary="1/2 subtasks completed.",
        )
        mock_integrity = IntegrityResult(
            status=IntegrityStatus.CLEAN,
            message="Workspace clean",
            expected_commit="abc",
            current_head="abc",
            uncommitted_files=[],
        )
        mock_outcome = ResumeOutcome(
            success=True,
            action="RESUMED",
            message="Resumed",
        )

        mock_mgr = MagicMock()
        mock_mgr.get_resumable_state.return_value = mock_plan
        mock_mgr.verify_workspace_integrity.return_value = mock_integrity
        mock_mgr.resume_task.return_value = mock_outcome

        with patch("qz_desktop_bridge.get_resume_manager", return_value=mock_mgr):
            # Test getResumeOptions
            opts = _dispatch(mock_backend, "getResumeOptions", {"taskId": "task-rpc-1"})
            self.assertEqual(opts["plan"]["task_id"], "task-rpc-1")
            self.assertEqual(opts["integrity"]["status"], "CLEAN")
            self.assertIn("RESUME", opts["allowedDecisions"])

            # Test resumeTask
            res = _dispatch(mock_backend, "resumeTask", {"taskId": "task-rpc-1", "decision": "RESUME"})
            self.assertTrue(res["success"])
            self.assertEqual(res["action"], "RESUMED")


if __name__ == "__main__":
    unittest.main()

