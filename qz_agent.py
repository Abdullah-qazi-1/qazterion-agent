"""
Qazterion — multi-provider agentic coding assistant (compatibility facade).

The command-line entry point is ``qz_cli`` (``qazterion`` / ``qz``). This module
keeps the long-standing ``qz_agent`` namespace that the desktop bridge, the core
pipeline and the test-suite use to look up (and patch) pipeline steps.
"""

from __future__ import annotations

import os
import sys

from qz_paths import load_environment

load_environment()

from qz_tools import WORKSPACE, commit_changes, configure_project_environment, ensure_git_repository, run_command  # noqa: E402
from qz_indexer import format_index_summary, load_or_build_index  # noqa: E402
from qz_environment import detect_project_environment, prepare_python_environment, preparation_plan  # noqa: E402
from qz_memory import ProjectMemory  # noqa: E402
from qz_tasks.models import SubtaskStatus, TaskStatus  # noqa: E402
from qz_tasks.task_manager import (  # noqa: E402
    announce_interrupted_tasks,
    create_task,
    log_event,
    update_status,
)
from qz_core.client import client, get_client  # noqa: E402
from qz_core.common import _persist_task  # noqa: E402
from qz_core.classifier import (  # noqa: E402
    COMPLEXITY_MODEL_MAP,
    FORCED_TASK_MODE,
    PLAN_APPROVAL_SETTING,
    TASK_MODES,
    _VALID_APPROVAL_SETTINGS,
    classify_task_complexity,
    classify_task_mode,
)
from qz_core.planner import (  # noqa: E402
    ask_clarifying_questions,
    call_architect,
    call_planner,
    convert_plan_to_subtasks,
    finalize_plan,
    format_clarifications,
    generate_clarifying_questions,
    prompt_plan_approval,
    requires_plan_approval,
    resolve_plan,
)
from qz_core.reviewer import self_review  # noqa: E402
from qz_core.executor import (  # noqa: E402
    _tool_result_failed,
    capture_pre_existing_test_failures,
    request_completion,
    roll_conversation_summary,
    run_executor,
)
from qz_validation import ValidationPipeline  # noqa: E402

# Trusted projects can skip the interactive environment-setup prompt with
# QAZTERION_AUTO_SETUP=true in .env, or --auto-setup for a single run.
AUTO_SETUP_ENV = os.environ.get("QAZTERION_AUTO_SETUP", "false").strip().lower() in ("1", "true", "yes")

_PREPARED_WORKSPACES: set[str] = set()


def _ask_yes_no(prompt: str) -> bool:
    if not getattr(sys.stdin, "isatty", lambda: False)():
        return False
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return False
    return answer in ("y", "yes")


def prepare_environment(auto_setup: bool = False, workspace: str | None = None) -> None:
    """Detect the project's tooling once per workspace; for a Python project with no
    managed environment, show the install plan and act only on explicit approval
    (or when auto-setup is enabled). Other ecosystems are reported, never changed.
    """
    target = str(workspace or WORKSPACE)
    if target in _PREPARED_WORKSPACES:
        return
    _PREPARED_WORKSPACES.add(target)

    env = detect_project_environment(target)
    print(f"\033[90m[env] {env.summary()}\033[0m")

    if env.project_type == "python":
        if env.python_interpreter:
            print(f"\033[90m[env] Reusing existing project interpreter: {env.python_interpreter}\033[0m")
        elif env.protected_tools or not env.can_create_venv:
            print(f"\033[90m[env] {preparation_plan(env)}\033[0m")
        else:
            print(f"\033[93m[env] {preparation_plan(env)}\033[0m")
            approved = auto_setup or AUTO_SETUP_ENV or _ask_yes_no("[env] Create .venv and install dependencies now?")
            if approved:
                print(f"\033[90m[env] {prepare_python_environment(env, approved=True)}\033[0m")
            else:
                print("\033[90m[env] Skipped. Commands will use whatever interpreter is on PATH.\033[0m")
    elif env.project_type != "unknown":
        print(f"\033[90m[env] Detected {env.project_type} project (manager={env.manager}); "
              f"native commands will be used, e.g. `{env.test_command}`.\033[0m")

    configure_project_environment(target)


def run_task(task: str, auto_setup: bool = False):
    """Run one task in the current WORKSPACE with terminal prompts (legacy API).

    New front-ends should use :class:`qz_core.autonomous_loop.AutonomousRunner`.
    """
    task_id = None
    try:
        print(f"\033[90mWorking directory: {WORKSPACE}\033[0m\n")
        try:
            task_id = create_task(task, WORKSPACE)
        except Exception as error:
            print(f"\033[90m[tasks] persistence skipped ({error})\033[0m")
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.ANALYZING, current_step="analyzing"))
        _persist_task(task_id, lambda: log_event(task_id, "TASK_STARTED", {"workspace_path": WORKSPACE, "task": task}))
        prepare_environment(auto_setup=auto_setup)
        print(f"\033[90m[git]\033[0m {ensure_git_repository()}")
        baseline = capture_pre_existing_test_failures()
        if baseline:
            print("\033[93m[baseline]\033[0m Default tests already fail; later matching failures will be reported as pre-existing.")
        index, rebuilt = load_or_build_index(WORKSPACE)
        print(f"\033[90m[index] {'rebuilt' if rebuilt else 'loaded from cache'}: {len(index['files'])} source files\033[0m\n")

        mode = classify_task_mode(task)
        print(f"\033[95m[task mode]\033[0m classified as '{mode}' (approval setting: '{PLAN_APPROVAL_SETTING}')")
        _persist_task(task_id, lambda: log_event(task_id, "TASK_CLASSIFIED", {"mode": mode}))

        outcome = resolve_plan(task, format_index_summary(index), PLAN_APPROVAL_SETTING, mode, task_id=task_id)
        if outcome["status"] == "rejected":
            print("\n\033[91m[plan]\033[0m Rejected by user. No changes were made.")
            return "Task rejected by user before implementation."

        print("\n\033[93m=== IMPLEMENTING ===\033[0m")
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.EXECUTING, current_step="executing"))
        result = run_executor(outcome["task"], outcome["plan"], outcome["architecture"], test_baseline=baseline, task_id=task_id)
        if task_id:
            ProjectMemory(WORKSPACE).remember("completed_feature", task, tags=["task", "completed"], task_id=task_id)
        return result
    except KeyboardInterrupt:
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.CANCELLED, current_step="cancelled"))
        _persist_task(task_id, lambda: log_event(task_id, "TASK_CANCELLED", {"reason": "keyboard_interrupt"}))
        print("\n\033[93m[cancelled]\033[0m Task cancelled. Existing changes were left intact; no rollback was performed.")
        return "Task cancelled by user. Existing changes were left intact; no rollback was performed."
    except Exception as exc:
        err_msg = str(exc)
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.FAILED, current_step="failed"))
        _persist_task(task_id, lambda: log_event(task_id, "TASK_FAILED", {"reason": err_msg}))
        print(f"\n\033[91m[task error]\033[0m {err_msg}")
        raise


def main() -> int:
    """``python qz_agent.py ...`` runs the standard CLI."""
    from qz_cli.app import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
