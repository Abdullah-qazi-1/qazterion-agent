"""CLI rendering, slash commands, and the AutonomousRunner task pipeline."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from qz_cli.app import QazterionCLI, _make_history
from qz_cli.banner import print_banner
from qz_cli.commands.handlers import (
    handle_diff_command,
    handle_history_command,
    handle_model_command,
    handle_rollback_command,
    handle_rules_command,
    handle_status_command,
)
from qz_cli.formatters import render_diff, render_plan, render_status_table
from qz_core.autonomous_loop import AutonomousRunner, _parse_plan_decision
from qz_security import gateway as security_gateway
from qz_security.command_risk import RiskLevel
from qz_tasks import task_manager


def test_task_manager_lists_tasks_and_checkpoints_per_workspace():
    task_id = task_manager.create_task("Refactor database layer", "/ws/a")
    task_manager.create_task("Other project", "/ws/b")
    task_manager.create_checkpoint(task_id, 1, git_commit_hash="abc1234", summary="step one")
    task_manager.create_checkpoint(task_id, 1, git_commit_hash="def5678", summary="step two")

    tasks = task_manager.list_recent_tasks(workspace="/ws/a")
    assert [t["id"] for t in tasks] == [task_id]
    checkpoints = task_manager.list_checkpoints(workspace="/ws/a")
    assert [c["git_commit_hash"] for c in checkpoints] == ["def5678", "abc1234"]
    # Latest is by insertion order, not by the per-subtask attempt counter.
    assert task_manager.get_latest_checkpoint(task_id)["git_commit_hash"] == "def5678"
    assert task_manager.get_checkpoint(checkpoints[1]["id"])["summary"] == "step one"


def test_task_events_reach_subscribers():
    received = []
    task_manager.subscribe_events(lambda tid, et, payload, ts: received.append((tid, et)))
    try:
        task_id = task_manager.create_task("x", "/ws")
        task_manager.log_event(task_id, "PLAN_CREATED", {"mode": "quick"})
    finally:
        task_manager._global_event_subscribers.clear()
    assert (task_id, "PLAN_CREATED") in received


def test_cli_banner_rendering_without_keys():
    console = Console(record=True, width=120)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        print_banner(console=console, workspace=Path(tmpdir))
    output = console.export_text()
    assert "Directory" in output
    assert "no usable model" in output


def test_cli_formatters():
    console = Console(record=True, width=120)
    render_diff(console, "file.py", "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-    return 'old'\n+    return 'new'\n")
    output = console.export_text()
    assert "Diff: file.py" in output and "+    return 'new'" in output

    render_plan(console, plan="1. Inspect [files]\n2. Edit code", architecture="src/\n  app.py")
    assert "● Plan" in console.export_text()

    providers = [{"provider_id": "gemini", "display_name": "Google Gemini", "enabled": True, "keys": [
        {"key_id": "GEMINI_KEY_1", "masked_value": "******abcd", "source": "keystore", "enabled": True,
         "available": False, "cooldown_remaining_s": 12.0},
    ]}]
    render_status_table(console, providers, "dpapi")
    status_out = console.export_text()
    assert "GEMINI_KEY_1" in status_out and "cooling down" in status_out


def test_slash_handlers_run_without_keys_or_git():
    console = Console(record=True, width=140)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        path = Path(tmpdir)
        handle_status_command(console, path)
        handle_diff_command(console, path)
        handle_rules_command(console, path)
        handle_history_command(console, path)
        handle_rollback_command(console, path, [])
        handle_model_command(console, [])
    output = console.export_text()
    assert "Roles" in output
    assert "No Qazterion checkpoints" in output


def test_history_never_records_key_commands():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        history_file = Path(tmpdir) / "history.txt"
        history = _make_history(history_file)
        history.store_string("/keys add groq 1")
        history.store_string("fix the login bug")
        content = history_file.read_text(encoding="utf-8")
    assert "fix the login bug" in content
    assert "/keys" not in content


def test_plan_decisions_are_parsed_strictly():
    assert _parse_plan_decision("approve") == ("approve", None)
    assert _parse_plan_decision(True) == ("approve", None)
    assert _parse_plan_decision({"decision": "edit", "plan": "1. new"}) == ("edit", "1. new")
    assert _parse_plan_decision({"decision": "edit", "plan": "  "})[0] == "reject"
    for value in ("reject", "n", None, False, "", "maybe"):
        assert _parse_plan_decision(value)[0] == "reject"


def test_permission_handler_must_explicitly_approve():
    cli = QazterionCLI(workspace=Path.cwd())
    with patch("qz_cli.formatters.render_permission_request", return_value="deny"):
        assert cli.permission_handler({"action": "run_command", "details": {"command": "pip install x"}}) is False
    with patch("qz_cli.formatters.render_permission_request", return_value="always"):
        assert cli.permission_handler({"action": "run_command", "details": {"command": "pip install x"}}) is True
    # "always" is remembered for the same command in this session.
    with patch("qz_cli.formatters.render_permission_request", side_effect=AssertionError("asked twice")):
        assert cli.permission_handler({"action": "run_command", "details": {"command": "pip install x"}}) is True


def test_gateway_treats_non_boolean_answers_as_denial():
    for answer in ("deny", "no", None, False, 0, "maybe"):
        assert security_gateway.is_approval(answer) is False
    for answer in (True, "allow", "always", "allow_once", "approve"):
        assert security_gateway.is_approval(answer) is True


def _fake_pipeline(runner_patches):
    """Patch the expensive pipeline steps used by AutonomousRunner."""
    return [
        patch("qz_core.autonomous_loop.check_model_access", return_value=None),
        patch("qz_agent.prepare_environment"),
        patch("qz_tools.ensure_git_repository", return_value="Git repository ready."),
        patch("qz_agent.load_or_build_index", return_value=({"files": []}, False)),
        patch("qz_agent.capture_pre_existing_test_failures", return_value=None),
        patch("qz_core.autonomous_loop.resolve_plan", return_value={
            "status": "ready", "task": "t", "plan": "1. original plan", "architecture": "a", "subtasks": None,
        }),
        *runner_patches,
    ]


def _run(runner, prompt, patches, **kwargs):
    from contextlib import ExitStack

    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        return runner.run_task(prompt, **kwargs), mocks


def test_runner_rejected_plan_cancels_without_executing():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        runner = AutonomousRunner(workspace=tmpdir, on_plan_approval=lambda data: "reject")
        executor_patch = patch("qz_agent.run_executor")
        finalize_patch = patch("qz_core.autonomous_loop.finalize_plan")
        result, mocks = _run(runner, "big refactor", _fake_pipeline([executor_patch, finalize_patch]),
                             mode_override="complex", approval_policy="complex")
    assert result["status"] == "cancelled"
    mocks[-2].assert_not_called()  # run_executor
    mocks[-1].assert_not_called()  # finalize_plan (no subtasks created)
    assert task_manager.get_task(result["task_id"])["status"] == "CANCELLED"


def test_runner_edited_plan_is_what_gets_executed():
    captured = {}

    def fake_finalize(outcome, task_id=None, plan=None):
        captured["plan"] = plan
        outcome["plan"] = plan or outcome["plan"]
        return outcome

    def fake_executor(task, plan, architecture, test_baseline=None, task_id=None):
        captured["executed_plan"] = plan
        task_manager.update_status(task_id, "COMPLETED", current_step="completed")
        return "done"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        runner = AutonomousRunner(workspace=tmpdir,
                                  on_plan_approval=lambda data: {"decision": "edit", "plan": "1. edited plan"})
        result, _ = _run(runner, "big refactor", _fake_pipeline([
            patch("qz_core.autonomous_loop.finalize_plan", side_effect=fake_finalize),
            patch("qz_agent.run_executor", side_effect=fake_executor),
        ]), mode_override="complex", approval_policy="always")
    assert result["status"] == "completed", result
    assert captured["plan"] == "1. edited plan"
    assert captured["executed_plan"] == "1. edited plan"


def test_runner_restores_previous_permission_handler():
    def previous(*_args):
        return False

    security_gateway.configure(ask_handler=previous)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        runner = AutonomousRunner(workspace=tmpdir, on_permission_request=lambda req: True)
        _run(runner, "x", _fake_pipeline([
            patch("qz_core.autonomous_loop.finalize_plan"),
            patch("qz_agent.run_executor", return_value="done"),
        ]), mode_override="quick")
    assert security_gateway.get_ask_handler() is previous
    # The runner's own handler converts the front-end answer to a strict bool.
    assert runner._ask("run_command", {"command": "pip install x"}, RiskLevel.MEDIUM, []) is True


def test_runner_fails_fast_when_no_model_is_usable():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        runner = AutonomousRunner(workspace=tmpdir)
        with patch("qz_core.autonomous_loop.resolve_plan") as plan, patch("qz_agent.prepare_environment") as prep:
            result = runner.run_task("anything")
    assert result["status"] == "failed"
    assert "No usable model" in result["error"]
    assert "/keys add" in result["error"]
    plan.assert_not_called()
    prep.assert_not_called()
