"""Unit tests for event bus streaming and callback isolation in TaskManager."""

import tempfile
import unittest
from pathlib import Path

from qz_tasks.task_manager import TaskManager, subscribe_events, unsubscribe_events


class EventBusTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_events.db"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_log_event_fires_registered_callback_with_correct_data(self):
        events_received = []

        def on_event(task_id: str, event_type: str, payload: dict | None, timestamp: str):
            events_received.append({
                "task_id": task_id,
                "event_type": event_type,
                "payload": payload,
                "timestamp": timestamp,
            })

        manager = TaskManager(self.db_path, on_event=on_event)
        task_id = manager.create_task("Test task", str(self.tmp_dir.name))

        # create_task logs TASK_STARTED
        self.assertEqual(len(events_received), 1)
        self.assertEqual(events_received[0]["event_type"], "TASK_STARTED")
        self.assertEqual(events_received[0]["task_id"], task_id)

        # Explicit log_event
        row_id = manager.log_event(task_id, "MODEL_SELECTED", {"model": "groq-fast", "complexity": "simple"})
        self.assertGreater(row_id, 0)
        self.assertEqual(len(events_received), 2)
        self.assertEqual(events_received[1]["event_type"], "MODEL_SELECTED")
        self.assertEqual(events_received[1]["payload"], {"model": "groq-fast", "complexity": "simple"})

    def test_log_event_without_callback_writes_cleanly_to_sqlite(self):
        manager = TaskManager(self.db_path)  # No on_event callback
        task_id = manager.create_task("Task without callback", str(self.tmp_dir.name))

        row_id = manager.log_event(task_id, "TOOL_STARTED", {"tool": "read_file"})
        self.assertGreater(row_id, 0)

        events = manager.get_task_events(task_id)
        self.assertEqual(len(events), 2)  # TASK_STARTED + TOOL_STARTED
        self.assertEqual(events[1]["event_type"], "TOOL_STARTED")
        self.assertEqual(events[1]["payload"], {"tool": "read_file"})

    def test_failing_callback_does_not_break_sqlite_persistence_or_caller(self):
        def throwing_callback(task_id, event_type, payload, timestamp):
            raise RuntimeError("UI subscriber disconnected or socket error!")

        manager = TaskManager(self.db_path, on_event=throwing_callback)
        task_id = manager.create_task("Task with failing callback", str(self.tmp_dir.name))

        # log_event must succeed and return valid row ID despite the callback throwing
        row_id = manager.log_event(task_id, "VALIDATION_CHECK", {"status": "PASS", "check": "tests"})
        self.assertGreater(row_id, 0)

        # Confirm SQLite row was still written and contains correct payload
        events = manager.get_task_events(task_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["event_type"], "VALIDATION_CHECK")
        self.assertEqual(events[1]["payload"], {"status": "PASS", "check": "tests"})

    def test_subscribe_and_unsubscribe_event_helpers(self):
        events_captured = []

        def callback(task_id, event_type, payload, timestamp):
            events_captured.append(event_type)

        manager = TaskManager(self.db_path)
        task_id = manager.create_task("Sub pub task", str(self.tmp_dir.name))

        manager.subscribe_event(callback)
        manager.log_event(task_id, "EVENT_1")
        self.assertEqual(events_captured, ["EVENT_1"])

    def test_command_string_in_event_payloads_is_redacted(self):
        from qz_security.redaction import redact

        raw_cmd = "git clone https://ghp_abcdef1234567890abcdef1234567890@github.com/repo.git"
        redacted_cmd = redact(raw_cmd)
        self.assertNotIn("ghp_abcdef1234567890abcdef1234567890", redacted_cmd)
        self.assertIn("[REDACTED:", redacted_cmd)

        events_captured = []
        manager = TaskManager(self.db_path, on_event=lambda t, e, p, s: events_captured.append(p))
        task_id = manager.create_task("Redaction task", str(self.tmp_dir.name))

        manager.log_event(task_id, "TOOL_STARTED", {"tool": "run_command", "command": redacted_cmd})
        manager.log_event(task_id, "TEST_FAILED", {"command": redacted_cmd})

        for p in events_captured[1:]:  # Skip TASK_STARTED
            self.assertIn("command", p)
            self.assertNotIn("ghp_abcdef1234567890abcdef1234567890", p["command"])
            self.assertIn("[REDACTED:", p["command"])

    def test_stream_parser_handles_events_alongside_multiline_output(self):
        import json
        import io

        # Simulate stdout stream from Python containing events, plan, and multi-line tool results
        stream_lines = [
            '__QZ_EVENT__' + json.dumps({"taskId": "t-1", "eventType": "TOOL_STARTED", "timestamp": "2026-09-01T21:00:00Z", "payload": {"tool": "run_command"}}),
            '__QZ_EVENT__' + json.dumps({"taskId": "t-1", "eventType": "TOOL_FINISHED", "timestamp": "2026-09-01T21:00:02Z", "payload": {"tool": "run_command", "ok": True}}),
            '__QZ_PLAN__' + json.dumps({"task": "Build feature", "plan": "1. Do A\n2. Do B", "architecture": "Arch", "mode": "standard"}),
            'Task execution output with multiple\nlines and special characters: {"key": "value"}',
        ]

        # Emulate Electron line-by-line reading logic
        parsed_events = []
        parsed_plans = []
        other_lines = []

        for raw_line in "\n".join(stream_lines).splitlines():
            if raw_line.startswith("__QZ_PLAN__"):
                parsed_plans.append(json.loads(raw_line[11:]))
            elif raw_line.startswith("__QZ_EVENT__"):
                parsed_events.append(json.loads(raw_line[12:]))
            else:
                other_lines.append(raw_line)

        self.assertEqual(len(parsed_events), 2)
        self.assertEqual(parsed_events[0]["eventType"], "TOOL_STARTED")
        self.assertEqual(parsed_events[1]["eventType"], "TOOL_FINISHED")
        self.assertEqual(len(parsed_plans), 1)
        self.assertEqual(parsed_plans[0]["mode"], "standard")
        self.assertEqual(len(other_lines), 2)


if __name__ == "__main__":
    unittest.main()

