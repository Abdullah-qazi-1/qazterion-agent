"""
Qazterion — Free Agentic Coding Assistant
Plan -> Architect (folder/file structure) -> Execute (with tools) -> Test -> Self-fix loop

Before running:
1. Run `litellm --config config.yaml` in a separate terminal (starts the proxy)
2. Change to the project folder you want to work in, then run:
   `qazterion "your task"` (one-shot) or `qazterion` (interactive mode)
"""

from __future__ import annotations

import os
import sys
from dotenv import load_dotenv

from qz_tools import TOOL_SCHEMAS, TOOL_FUNCTIONS, WORKSPACE, commit_changes, ensure_git_repository
from qz_tools import configure_project_environment, run_command
from qz_indexer import format_index_summary, load_or_build_index
from qz_environment import detect_project_environment, prepare_python_environment, preparation_plan
from qz_usage_tracker import UsageTracker, default_usage_log_path
from qz_memory import ProjectMemory
from qz_tasks.models import SubtaskStatus, TaskStatus
from qz_tasks.task_manager import (
    announce_interrupted_tasks,
    create_checkpoint,
    create_subtasks,
    create_task,
    get_next_ready_subtasks,
    get_subtasks,
    log_event,
    update_status,
    update_subtask_status,
)

from qz_core.client import client, PROXY_URL, MASTER_KEY, get_client
from qz_core.common import _persist_task
from qz_core.classifier import (
    COMPLEXITY_MODEL_MAP,
    FORCED_TASK_MODE,
    PLAN_APPROVAL_SETTING,
    TASK_MODES,
    _COMPLEX_KEYWORDS,
    _QUICK_KEYWORDS,
    _VALID_APPROVAL_SETTINGS,
    _heuristic_task_mode,
    classify_task_complexity,
    classify_task_mode,
)
from qz_core.planner import (
    PLANNER_MODEL,
    SUBTASK_DAG_SYSTEM_PROMPT,
    ask_clarifying_questions,
    call_architect,
    call_planner,
    convert_plan_to_subtasks,
    format_clarifications,
    generate_clarifying_questions,
    prompt_plan_approval,
    requires_plan_approval,
    resolve_plan,
    validate_subtask_dag,
    _parse_subtasks_json,
)
from qz_core.reviewer import self_review
from qz_core.git_ops import generate_commit_message, _commit_hash_from_result
from qz_core.executor import (
    EXECUTOR_FALLBACKS,
    FAILURE_MARKERS,
    MAX_HISTORY_MESSAGES,
    MODEL_REQUEST_ATTEMPTS,
    RECENT_HISTORY_MESSAGES,
    SYSTEM_PROMPT,
    USAGE_TRACKER,
    TestBaseline,
    _history_for_summary,
    _is_test_file_path,
    _provider_failure_kind,
    _recent_exchange_start,
    _task_requires_test_changes,
    _test_failure_signature,
    _tool_result_failed,
    capture_pre_existing_test_failures,
    request_completion,
    roll_conversation_summary,
    run_executor,
)
from qz_core.dag_executor import (
    HardDAGExecutor,
    NodeResult,
    DAGExecutionResult,
)
from qz_router import select_route
from qz_validation import ValidationPipeline, ValidationReport
from qz_recovery import (
    ResumeManager,
    ResumePlan,
    IntegrityResult,
    IntegrityStatus,
    ResumeOutcome,
    get_resume_manager,
)

# Windows terminals may default to cp1252. Model responses routinely include
# characters such as non-breaking hyphens, which otherwise crash a successful
# agent run while it is only trying to display its output.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ============================================================
# PHASE 10: Global CLI and project environment management
# ============================================================
# Trusted projects can skip the interactive approval prompt by setting
# QAZTERION_AUTO_SETUP=true in .env, or by passing --auto-setup on the
# command line for a single run.
AUTO_SETUP_ENV = os.environ.get("QAZTERION_AUTO_SETUP", "false").strip().lower() in ("1", "true", "yes")

_ENVIRONMENT_PREPARED = False


def _ask_yes_no(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def prepare_environment(auto_setup: bool = False):
    """Detect the target project's type/tooling and, for Python projects with
    no managed environment, show the install plan and only act on explicit
    approval (or when auto_setup is enabled for trusted projects).

    Runs once per process. Never touches Node/Rust/Go/Java/.NET projects or
    projects using Poetry/Conda/Docker/uv/tox — those are reported only.
    """
    global _ENVIRONMENT_PREPARED
    if _ENVIRONMENT_PREPARED:
        return
    _ENVIRONMENT_PREPARED = True

    env = detect_project_environment(WORKSPACE)
    print(f"\033[90m[env] {env.summary()}\033[0m")

    if env.project_type == "python":
        if env.python_interpreter:
            print(f"\033[90m[env] Reusing existing project interpreter: {env.python_interpreter}\033[0m")
        elif env.protected_tools:
            print(f"\033[90m[env] {preparation_plan(env)}\033[0m")
        elif env.can_create_venv:
            plan = preparation_plan(env)
            print(f"\033[93m[env] {plan}\033[0m")
            approved = auto_setup or AUTO_SETUP_ENV
            if not approved:
                approved = _ask_yes_no("[env] Create .venv and install dependencies now?")
            if approved:
                result = prepare_python_environment(env, approved=True)
                print(f"\033[90m[env] {result}\033[0m")
            else:
                print("\033[90m[env] Skipped. Commands will use whatever interpreter is on PATH.\033[0m")
        else:
            print(f"\033[90m[env] {preparation_plan(env)}\033[0m")
    elif env.project_type != "unknown":
        print(f"\033[90m[env] Detected {env.project_type} project (manager={env.manager}); "
              f"native commands will be used, e.g. `{env.test_command}`.\033[0m")

    # Refresh qz_tools' cached interpreter selection now that setup (if any) is done.
    configure_project_environment(WORKSPACE)


try:
    from qz_keystore import KeyStore
    _ks = KeyStore()
    _ks_env = _ks.enabled_env()
    for _k, _v in _ks_env.items():
        if _k not in os.environ and _v:
            os.environ[_k] = _v
except Exception:
    pass


def run_task(task: str, auto_setup: bool = False):
    task_id = None
    try:
        print(f"\033[90mWorking directory: {WORKSPACE}\033[0m\n")
        try:
            task_id = create_task(task, WORKSPACE)
        except Exception as error:
            print(f"\033[90m[tasks] persistence skipped ({error})\033[0m")
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.ANALYZING, current_step="analyzing"))
        _persist_task(task_id, lambda: log_event(task_id, "TASK_STARTED", {"workspace_path": WORKSPACE, "task": task}))
        _persist_task(task_id, lambda: log_event(task_id, "AGENT_THINKING", {"phase": "planning", "detail": "Analyzing workspace and planning solution..."}))
        prepare_environment(auto_setup=auto_setup)
        git_status = ensure_git_repository()
        print(f"\033[90m[git]\033[0m {git_status}")
        baseline = capture_pre_existing_test_failures()
        if baseline:
            print("\033[93m[baseline]\033[0m Default tests already fail; later matching failures will be reported as pre-existing.")
        index, rebuilt = load_or_build_index(WORKSPACE)
        index_status = "rebuilt" if rebuilt else "loaded from cache"
        print(f"\033[90m[index] {index_status}: {len(index['files'])} source files\033[0m\n")
        index_summary = format_index_summary(index)

        mode = classify_task_mode(task)
        print(f"\033[95m[task mode]\033[0m classified as '{mode}' (approval setting: '{PLAN_APPROVAL_SETTING}')")
        _persist_task(task_id, lambda: log_event(task_id, "TASK_CLASSIFIED", {"mode": mode}))

        outcome = resolve_plan(task, index_summary, PLAN_APPROVAL_SETTING, mode, task_id=task_id)
        if outcome["status"] == "rejected":
            print("\n\033[91m[plan]\033[0m Rejected by user. No changes were made.")
            _persist_task(task_id, lambda: log_event(task_id, "TASK_CANCELLED", {"reason": "plan_rejected_by_user"}))
            return "Task rejected by user before implementation."

        print("\n\033[93m=== IMPLEMENTING ===\033[0m")
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.EXECUTING, current_step="executing"))
        _persist_task(task_id, lambda: log_event(task_id, "AGENT_THINKING", {"phase": "executing", "detail": "Executing subtasks and writing code..."}))
        result = run_executor(
            outcome["task"], outcome["plan"], outcome["architecture"],
            test_baseline=baseline, task_id=task_id,
        )
        # Store a concise project fact, never the conversation transcript.
        # This is deliberately best-effort so memory cannot affect execution.
        if task_id:
            ProjectMemory(WORKSPACE).remember(
                "completed_feature", task,
                tags=["task", "completed"], task_id=task_id,
            )
            _persist_task(task_id, lambda: log_event(task_id, "TASK_COMPLETED", {"summary": str(result)[:300]}))
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


def interactive_mode(auto_setup: bool = False):
    print("\033[96m╭──────────────────────────────╮")
    print("│   Qazterion — Coding Agent   │")
    print("╰──────────────────────────────╯\033[0m")
    print(f"Working directory: {WORKSPACE}")
    print("Enter a task and press Enter. Type 'exit' to quit.\n")
    announce_interrupted_tasks(stream=sys.stderr)

    prepare_environment(auto_setup=auto_setup)

    while True:
        try:
            task = input("\033[1mqazterion>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not task:
            continue
        if task.lower() in ("exit", "quit"):
            print("Bye!")
            break

        run_task(task, auto_setup=auto_setup)
        print()


def main():
    global PLAN_APPROVAL_SETTING, FORCED_TASK_MODE

    args = sys.argv[1:]
    auto_setup = "--auto-setup" in args
    args = [a for a in args if a != "--auto-setup"]

    remaining = []
    for arg in args:
        if arg.startswith("--approval="):
            value = arg.split("=", 1)[1].strip().lower()
            if value in _VALID_APPROVAL_SETTINGS:
                PLAN_APPROVAL_SETTING = value
            else:
                print(f"\033[91m[warning]\033[0m Unknown --approval value '{value}' "
                      f"(expected one of {sorted(_VALID_APPROVAL_SETTINGS)}); ignoring.")
        elif arg.startswith("--mode="):
            value = arg.split("=", 1)[1].strip().lower()
            if value in TASK_MODES:
                FORCED_TASK_MODE = value
            else:
                print(f"\033[91m[warning]\033[0m Unknown --mode value '{value}' "
                      f"(expected one of {TASK_MODES}); ignoring.")
        else:
            remaining.append(arg)
    args = remaining

    if args:
        task = " ".join(args)
        announce_interrupted_tasks(stream=sys.stderr)
        run_task(task, auto_setup=auto_setup)
    else:
        interactive_mode(auto_setup=auto_setup)


if __name__ == "__main__":
    main()
