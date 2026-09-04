import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qz_tasks.database import user_db_path
from qz_tasks.models import TaskStatus
from qz_tasks.task_manager import (
    TaskManager,
    announce_interrupted_tasks,
    format_interrupted_notice,
    get_manager,
    reset_manager,
)


class TaskManagerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "qazterion_tasks.db"
        self.manager = TaskManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_update_events_and_checkpoints_persist_and_read_back(self):
        task_id = self.manager.create_task("Add a login form", "/tmp/workspace")
        self.manager.update_status(
            task_id,
            TaskStatus.PLANNING,
            current_step="planning",
            complexity="medium",
            plan_text="1. Create the form",
        )
        self.manager.log_event(task_id, "PLAN_CREATED", {"mode": "complex"})
        self.manager.create_checkpoint(task_id, 1, git_commit_hash="abc1234", summary="Add form")

        reopened = TaskManager(self.db_path)
        task = reopened.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["user_request"], "Add a login form")
        self.assertEqual(task["workspace_path"], "/tmp/workspace")
        self.assertEqual(task["status"], TaskStatus.PLANNING)
        self.assertEqual(task["complexity"], "medium")
        self.assertEqual(task["current_step"], "planning")
        self.assertEqual(task["plan_text"], "1. Create the form")
        self.assertEqual(task["max_attempts"], 20)

        events = reopened.get_task_events(task_id)
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types[0], "TASK_STARTED")
        self.assertIn("PLAN_CREATED", event_types)
        self.assertIn("CHECKPOINT_CREATED", event_types)
        plan_event = next(event for event in events if event["event_type"] == "PLAN_CREATED")
        self.assertEqual(plan_event["payload"], {"mode": "complex"})

        checkpoint = reopened.get_latest_checkpoint(task_id)
        self.assertEqual(checkpoint["git_commit_hash"], "abc1234")
        self.assertEqual(checkpoint["summary"], "Add form")
        self.assertEqual(checkpoint["iteration_number"], 1)

    def test_find_interrupted_tasks_excludes_terminal_statuses(self):
        received = self.manager.create_task("open", "/ws")
        executing = self.manager.create_task("running", "/ws")
        completed = self.manager.create_task("done", "/ws")
        failed = self.manager.create_task("broke", "/ws")
        cancelled = self.manager.create_task("stopped", "/ws")
        blocked = self.manager.create_task("waiting", "/ws")

        self.manager.update_status(executing, TaskStatus.EXECUTING, current_step="iteration 2/20")
        self.manager.update_status(completed, TaskStatus.COMPLETED, current_step="completed")
        self.manager.update_status(failed, TaskStatus.FAILED, current_step="failed")
        self.manager.update_status(cancelled, TaskStatus.CANCELLED, current_step="cancelled")
        self.manager.update_status(blocked, TaskStatus.BLOCKED, current_step="blocked")

        interrupted_ids = {task["id"] for task in self.manager.find_interrupted_tasks()}
        self.assertIn(received, interrupted_ids)
        self.assertIn(executing, interrupted_ids)
        self.assertIn(blocked, interrupted_ids)
        self.assertNotIn(completed, interrupted_ids)
        self.assertNotIn(failed, interrupted_ids)
        self.assertNotIn(cancelled, interrupted_ids)

    def test_events_and_checkpoints_are_scoped_and_ordered(self):
        first = self.manager.create_task("first", "/ws")
        second = self.manager.create_task("second", "/ws")
        self.manager.log_event(first, "TOOL_STARTED", {"tool": "read_file"})
        self.manager.log_event(first, "TOOL_FINISHED", {"tool": "read_file"})
        self.manager.log_event(second, "MODEL_SELECTED", {"model": "coder-strong"})
        self.manager.create_checkpoint(first, 1, git_commit_hash="aaa")
        self.manager.create_checkpoint(first, 3, git_commit_hash="ccc")
        self.manager.create_checkpoint(first, 2, git_commit_hash="bbb")
        self.manager.create_checkpoint(second, 9, git_commit_hash="zzz")

        first_events = self.manager.get_task_events(first)
        self.assertTrue(all(event["task_id"] == first for event in first_events))
        self.assertEqual(
            [event["event_type"] for event in first_events],
            ["TASK_STARTED", "TOOL_STARTED", "TOOL_FINISHED", "CHECKPOINT_CREATED", "CHECKPOINT_CREATED", "CHECKPOINT_CREATED"],
        )
        event_ids = [event["id"] for event in first_events]
        self.assertEqual(event_ids, sorted(event_ids))

        second_events = self.manager.get_task_events(second)
        self.assertTrue(all(event["task_id"] == second for event in second_events))
        self.assertEqual(
            [event["event_type"] for event in second_events],
            ["TASK_STARTED", "MODEL_SELECTED", "CHECKPOINT_CREATED"],
        )

        latest = self.manager.get_latest_checkpoint(first)
        self.assertEqual(latest["iteration_number"], 3)
        self.assertEqual(latest["git_commit_hash"], "ccc")
        other = self.manager.get_latest_checkpoint(second)
        self.assertEqual(other["git_commit_hash"], "zzz")

    def test_crash_without_terminal_status_is_still_interrupted(self):
        task_id = self.manager.create_task("Implement search", "/ws")
        self.manager.update_status(task_id, TaskStatus.EXECUTING, current_step="iteration 4/20")
        self.manager.log_event(task_id, "TEST_FAILED", {"command": "pytest"})
        self.manager.create_checkpoint(task_id, 4, git_commit_hash="def5678", summary="partial search")

        # Simulate a crash: process exits without COMPLETED/FAILED/CANCELLED.
        after_crash = TaskManager(self.db_path)
        interrupted = after_crash.find_interrupted_tasks()
        self.assertEqual([task["id"] for task in interrupted], [task_id])
        self.assertEqual(interrupted[0]["status"], TaskStatus.EXECUTING)
        self.assertEqual(interrupted[0]["current_step"], "iteration 4/20")

        events = after_crash.get_task_events(task_id)
        self.assertEqual(events[-1]["event_type"], "CHECKPOINT_CREATED")
        checkpoint = after_crash.get_latest_checkpoint(task_id)
        self.assertEqual(checkpoint["git_commit_hash"], "def5678")

        notice = format_interrupted_notice(interrupted[0], manager=after_crash)
        self.assertIn(task_id, notice)
        self.assertIn("iteration 4/20", notice)
        self.assertIn("def5678", notice)

    def test_announce_interrupted_tasks_prints_notice_and_does_not_resume(self):
        task_id = self.manager.create_task("Unfinished", "/ws")
        self.manager.update_status(task_id, TaskStatus.VALIDATING, current_step="validating")
        self.manager.create_checkpoint(task_id, 2, git_commit_hash="cafe123")
        stream = io.StringIO()
        with patch("qz_tasks.task_manager.get_manager", return_value=self.manager):
            found = announce_interrupted_tasks(stream=stream, force=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], task_id)
        self.assertIn("was interrupted at step validating", stream.getvalue())
        self.assertIn("cafe123", stream.getvalue())
        self.assertEqual(self.manager.get_task(task_id)["status"], TaskStatus.VALIDATING)

    def test_process_wide_manager_does_not_use_the_user_facing_db_during_tests(self):
        reset_manager()
        manager = get_manager()
        self.assertNotEqual(manager.db_path.resolve(), user_db_path().resolve())
        self.assertTrue(str(manager.db_path.name).startswith("qazterion_tasks_test_"))
        task_id = manager.create_task("isolated suite row", "/ws")
        user_db = user_db_path()
        if user_db.exists():
            with sqlite3.connect(str(user_db)) as connection:
                try:
                    rows = connection.execute(
                        "SELECT id FROM tasks WHERE id = ?", (task_id,)
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
