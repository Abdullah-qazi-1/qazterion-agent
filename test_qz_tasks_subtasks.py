"""Tests for Phase 5: Structured task DAG, subtask persistence, cycle detection, and fallback."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from qz_tasks.models import SubtaskStatus, TaskStatus
from qz_tasks.task_manager import TaskManager
from qz_core.planner import (
    _parse_subtasks_json,
    convert_plan_to_subtasks,
    validate_subtask_dag,
)
import qz_agent


def _fake_completion(content: str):
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


class SubtaskPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_tasks.db"
        self.manager = TaskManager(self.db_path)
        self.task_id = self.manager.create_task("Build full authentication feature", "/workspace")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_subtasks_persists_and_resolves_index_dependencies(self):
        subtasks_input = [
            {"title": "Create User Model", "description": "Define DB schema", "depends_on": []},
            {"title": "Add Password Hashing", "description": "Implement argon2 hashing", "depends_on": [0]},
            {"title": "Implement Login Route", "description": "JWT endpoint", "depends_on": [0, 1]},
        ]
        created = self.manager.create_subtasks(self.task_id, subtasks_input)
        self.assertEqual(len(created), 3)

        subtask_0_id = created[0]["id"]
        subtask_1_id = created[1]["id"]
        subtask_2_id = created[2]["id"]

        self.assertEqual(created[0]["depends_on"], [])
        self.assertEqual(created[1]["depends_on"], [subtask_0_id])
        self.assertEqual(created[2]["depends_on"], [subtask_0_id, subtask_1_id])

        # Verify read-back from DB via get_subtasks
        persisted = self.manager.get_subtasks(self.task_id)
        self.assertEqual(len(persisted), 3)
        self.assertEqual(persisted[0]["title"], "Create User Model")
        self.assertEqual(persisted[0]["status"], SubtaskStatus.PENDING)
        self.assertEqual(persisted[1]["depends_on"], [subtask_0_id])
        self.assertEqual(persisted[2]["depends_on"], [subtask_0_id, subtask_1_id])

        # Verify SUBTASKS_CREATED event was logged
        events = self.manager.get_task_events(self.task_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("SUBTASKS_CREATED", event_types)

    def test_create_subtasks_resolves_string_temp_ids(self):
        subtasks_input = [
            {"id": "step_db", "title": "Database Schema", "description": "Tables", "depends_on": []},
            {"id": "step_api", "title": "API Routes", "description": "Endpoints", "depends_on": ["step_db"]},
        ]
        created = self.manager.create_subtasks(self.task_id, subtasks_input)
        self.assertEqual(len(created), 2)
        db_id = created[0]["id"]
        self.assertEqual(created[1]["depends_on"], [db_id])

    def test_get_next_ready_subtasks_advances_as_dependencies_complete(self):
        subtasks_input = [
            {"title": "Step 1", "description": "Independent step", "depends_on": []},
            {"title": "Step 2", "description": "Depends on 1", "depends_on": [0]},
            {"title": "Step 3", "description": "Also independent", "depends_on": []},
            {"title": "Step 4", "description": "Depends on 2 and 3", "depends_on": [1, 2]},
        ]
        created = self.manager.create_subtasks(self.task_id, subtasks_input)
        s1_id, s2_id, s3_id, s4_id = [s["id"] for s in created]

        # Initial state: only Step 1 and Step 3 are ready (no dependencies)
        ready = self.manager.get_next_ready_subtasks(self.task_id)
        ready_ids = [s["id"] for s in ready]
        self.assertEqual(set(ready_ids), {s1_id, s3_id})

        # Complete Step 1: Step 2 should now become ready
        self.manager.update_subtask_status(s1_id, SubtaskStatus.COMPLETED, result_summary="Step 1 done")
        ready = self.manager.get_next_ready_subtasks(self.task_id)
        ready_ids = [s["id"] for s in ready]
        self.assertEqual(set(ready_ids), {s2_id, s3_id})

        # Complete Step 2 (but not 3 yet): Step 4 should still NOT be ready
        self.manager.update_subtask_status(s2_id, SubtaskStatus.COMPLETED)
        ready = self.manager.get_next_ready_subtasks(self.task_id)
        ready_ids = [s["id"] for s in ready]
        self.assertEqual(set(ready_ids), {s3_id})

        # Complete Step 3: Step 4 is now ready
        self.manager.update_subtask_status(s3_id, SubtaskStatus.COMPLETED)
        ready = self.manager.get_next_ready_subtasks(self.task_id)
        ready_ids = [s["id"] for s in ready]
        self.assertEqual(ready_ids, [s4_id])

        # Complete Step 4: No more subtasks are ready
        self.manager.update_subtask_status(s4_id, SubtaskStatus.COMPLETED)
        ready = self.manager.get_next_ready_subtasks(self.task_id)
        self.assertEqual(ready, [])

    def test_update_subtask_status_records_files_and_attempts(self):
        created = self.manager.create_subtasks(self.task_id, [{"title": "Step", "description": "Desc", "depends_on": []}])
        subtask_id = created[0]["id"]
        self.manager.update_subtask_status(
            subtask_id,
            SubtaskStatus.IN_PROGRESS,
            result_summary="Testing in progress",
            files_touched=["src/auth.py", "tests/test_auth.py"],
            attempts=2,
        )
        updated = self.manager.get_subtasks(self.task_id)[0]
        self.assertEqual(updated["status"], SubtaskStatus.IN_PROGRESS)
        self.assertEqual(updated["result_summary"], "Testing in progress")
        self.assertEqual(updated["files_touched"], ["src/auth.py", "tests/test_auth.py"])
        self.assertEqual(updated["attempts"], 2)


class DAGCycleValidationTests(unittest.TestCase):
    def test_linear_dag_is_valid(self):
        subtasks = [
            {"id": 0, "title": "A", "depends_on": []},
            {"id": 1, "title": "B", "depends_on": [0]},
            {"id": 2, "title": "C", "depends_on": [1]},
        ]
        self.assertTrue(validate_subtask_dag(subtasks))

    def test_diamond_dag_is_valid(self):
        subtasks = [
            {"id": "root", "title": "Root", "depends_on": []},
            {"id": "left", "title": "Left", "depends_on": ["root"]},
            {"id": "right", "title": "Right", "depends_on": ["root"]},
            {"id": "join", "title": "Join", "depends_on": ["left", "right"]},
        ]
        self.assertTrue(validate_subtask_dag(subtasks))

    def test_direct_cycle_is_invalid(self):
        subtasks = [
            {"id": 0, "title": "A", "depends_on": [1]},
            {"id": 1, "title": "B", "depends_on": [0]},
        ]
        self.assertFalse(validate_subtask_dag(subtasks))

    def test_indirect_cycle_is_invalid(self):
        subtasks = [
            {"id": "A", "title": "A", "depends_on": ["C"]},
            {"id": "B", "title": "B", "depends_on": ["A"]},
            {"id": "C", "title": "C", "depends_on": ["B"]},
        ]
        self.assertFalse(validate_subtask_dag(subtasks))

    def test_self_dependency_is_invalid(self):
        subtasks = [
            {"id": 0, "title": "A", "depends_on": [0]},
        ]
        self.assertFalse(validate_subtask_dag(subtasks))


class DAGPlannerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_tasks.db"
        self.manager = TaskManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_subtasks_json_accepts_clean_and_fenced_json(self):
        raw_json = json.dumps([
            {"id": 0, "title": "Init DB", "description": "Create schema", "depends_on": []},
            {"id": 1, "title": "Add Routes", "description": "Create API", "depends_on": [0]},
        ])
        # Clean JSON
        parsed = _parse_subtasks_json(raw_json)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["title"], "Init DB")

        # Markdown fenced JSON
        fenced = f"```json\n{raw_json}\n```"
        parsed_fenced = _parse_subtasks_json(fenced)
        self.assertIsNotNone(parsed_fenced)
        self.assertEqual(len(parsed_fenced), 2)

    def test_convert_plan_to_subtasks_success(self):
        valid_response = json.dumps([
            {"id": 0, "title": "Setup repository", "description": "Init git", "depends_on": []},
            {"id": 1, "title": "Add tests", "description": "Write pytest tests", "depends_on": [0]},
        ])
        with patch.object(qz_agent.client.chat.completions, "create", return_value=_fake_completion(valid_response)):
            subtasks = convert_plan_to_subtasks("Setup project", "1. Setup\n2. Test", "src/\ntests/")
        self.assertEqual(len(subtasks), 2)
        self.assertEqual(subtasks[0]["title"], "Setup repository")
        self.assertEqual(subtasks[1]["depends_on"], [0])

    def test_convert_plan_to_subtasks_falls_back_on_malformed_json(self):
        # Model returns non-JSON text on both initial call and retry
        with patch.object(qz_agent.client.chat.completions, "create", return_value=_fake_completion("Sure! Here is the plan: 1. Do this.")):
            subtasks = convert_plan_to_subtasks("Setup project", "Plan text", "Architecture text")
        self.assertEqual(len(subtasks), 1)
        self.assertEqual(subtasks[0]["title"], "Implement full plan")
        self.assertIn("Plan text", subtasks[0]["description"])
        self.assertEqual(subtasks[0]["depends_on"], [])

    def test_convert_plan_to_subtasks_falls_back_on_cyclic_json(self):
        cyclic_response = json.dumps([
            {"id": 0, "title": "Step A", "description": "A", "depends_on": [1]},
            {"id": 1, "title": "Step B", "description": "B", "depends_on": [0]},
        ])
        with patch.object(qz_agent.client.chat.completions, "create", return_value=_fake_completion(cyclic_response)):
            subtasks = convert_plan_to_subtasks("Setup project", "Plan text", "Architecture text")
        # Should detect cycle and fall back to single subtask
        self.assertEqual(len(subtasks), 1)
        self.assertEqual(subtasks[0]["title"], "Implement full plan")


if __name__ == "__main__":
    unittest.main()
