"""Comprehensive unit & integration tests for Qazterion CLI, autonomous execution loop, storage, and event bus."""

import os
import gc
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from rich.console import Console

from qz_storage.db import StorageManager
from qz_core.event_bus import EventBus, get_event_bus
from qz_core.autonomous_loop import AutonomousRunner
from qz_cli.banner import print_banner, get_git_branch
from qz_cli.formatters import render_diff, render_plan, render_status_table
from qz_cli.commands.handlers import (
    handle_status_command,
    handle_diff_command,
    handle_rules_command,
    handle_history_command,
)


def test_sqlite_storage_lifecycle():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "test_qazterion.db"
        storage = StorageManager(db_path=db_path)

        # 1. Session persistence
        sess_id = storage.create_session(workspace=tmpdir)
        assert sess_id is not None

        # 2. Task persistence
        task_id = storage.create_task(
            prompt="Refactor database layer",
            workspace=tmpdir,
            session_id=sess_id,
            mode="standard",
        )
        task = storage.get_task(task_id)
        assert task is not None
        assert task["prompt"] == "Refactor database layer"
        assert task["mode"] == "standard"

        # 3. Events persistence
        storage.log_event(
            task_id=task_id,
            event_type="STEP_COMPLETED",
            payload={"step": 1, "action": "inspect"},
        )
        events = storage.get_task_events(task_id)
        assert len(events) == 1
        assert events[0]["event_type"] == "STEP_COMPLETED"

        # 4. Checkpoints persistence
        storage.log_checkpoint(
            task_id=task_id,
            git_hash="abc1234",
            step_name="Initial migration checkpoint",
            message="checkpoint msg",
        )
        checkpoints = storage.list_checkpoints(task_id=task_id)
        assert len(checkpoints) == 1
        assert checkpoints[0]["git_hash"] == "abc1234"

        # 5. Model call metrics
        storage.record_model_call(
            provider="gemini",
            model="gemini-3.6-flash",
            input_tokens=150,
            output_tokens=40,
            latency_ms=320.0,
            success=True,
            task_id=task_id,
        )
        metrics = storage.get_metrics_summary()
        assert metrics["total_calls"] == 1
        assert metrics["success_rate"] == 100.0
        assert metrics["total_tokens"] == 190
        del storage
        gc.collect()


def test_event_bus_dispatch_and_subscribers():
    bus = EventBus()
    received_events = []

    def subscriber(event_type: str, task_id: str, payload: dict, timestamp: str):
        received_events.append((event_type, task_id, payload))

    bus.subscribe("PLAN_CREATED", subscriber)
    bus.subscribe("STEP_COMPLETED", subscriber)

    bus.emit("TASK_STARTED", "t-1", {"task": "test"})
    assert len(received_events) == 0  # Not subscribed to TASK_STARTED

    bus.emit("PLAN_CREATED", "t-1", {"plan_id": "p-1"})
    assert len(received_events) == 1
    assert received_events[0][0] == "PLAN_CREATED"

    bus.emit("STEP_COMPLETED", "t-1", {"step_id": 2})
    assert len(received_events) == 2


def test_cli_banner_rendering():
    console = Console(record=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        print_banner(console=console, workspace=Path(tmpdir))
        output = console.export_text()
        assert "AI SOFTWARE ENGINEERING AGENT" in output
        assert "Directory" in output


def test_cli_formatters():
    console = Console(record=True)
    diff_text = """--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
 def hello():
-    return 'old'
+    return 'new'
"""
    render_diff(console, "file.py", diff_text)
    output = console.export_text()
    assert "Diff: file.py" in output
    assert "+    return 'new'" in output

    # Test plan renderer
    render_plan(console, plan="1. Inspect files\n2. Edit code", architecture="Modular architecture")
    plan_out = console.export_text()
    assert "● Plan" in plan_out

    # Test status table
    providers = [{"provider_id": "gemini", "display_name": "Google Gemini", "models": ["gemini-3.6-flash"], "configured": True}]
    render_status_table(console, providers, "DPAPI")
    status_out = console.export_text()
    assert "Qazterion Provider & Routing Matrix" in status_out


def test_slash_handlers():
    console = Console(record=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        path = Path(tmpdir)
        handle_status_command(console, path)
        handle_diff_command(console, path)
        handle_rules_command(console, path)
        handle_history_command(console, path)


def test_autonomous_runner_init():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        runner = AutonomousRunner(workspace=tmpdir)
        assert runner.workspace == Path(tmpdir).resolve()
        assert runner.event_bus is not None
        assert runner.storage is not None
