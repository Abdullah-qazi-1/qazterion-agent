"""Task pipeline shared by the CLI (and usable by other front-ends).

prepare environment -> git -> classify -> plan -> (approval) -> subtask DAG -> execute.
All progress is persisted as task events (qz_tasks), which front-ends subscribe to.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import qz_agent
import qz_tools
from qz_core.classifier import PLAN_APPROVAL_SETTING, _VALID_APPROVAL_SETTINGS, classify_task_mode
from qz_core.common import TaskContext, task_context_scope
from qz_core.planner import finalize_plan, requires_plan_approval, resolve_plan
from qz_security.gateway import configure as configure_security_gateway
from qz_security.gateway import get_ask_handler, is_approval
from qz_tasks.models import TaskStatus
from qz_tasks.task_manager import create_task, get_task, log_event, update_status

PlanApprovalHandler = Callable[[Dict[str, Any]], Any]
PermissionHandler = Callable[[Dict[str, Any]], Any]


def check_model_access() -> str | None:
    """Return a user-facing problem if no configured model can run the executor, else None.

    Fails a task in seconds instead of after every subtask retry when, for
    example, no API key is configured yet. Only applies to the real gateway.
    """
    from qz_core.client import accepts_task_id, get_client

    if not accepts_task_id(get_client()):
        return None
    from qz_core.executor import EXECUTOR_ROLE
    from qz_providers.gateway import get_gateway

    plan, skipped = get_gateway().plan(EXECUTOR_ROLE, needs_tools=True)
    if plan:
        return None
    return (
        f"No usable model for the '{EXECUTOR_ROLE}' role ({'; '.join(skipped[:3])}). "
        "Add a key with `qazterion /keys add <provider>` or set e.g. GEMINI_KEY_1 in .env."
    )


def _parse_plan_decision(decision: Any) -> tuple[str, str | None]:
    """Accept "approve"/"reject" strings, booleans, or {"decision": ..., "plan": ...}."""
    if isinstance(decision, dict):
        word = str(decision.get("decision", "reject")).strip().lower()
        plan = decision.get("plan") if isinstance(decision.get("plan"), str) else None
    elif decision is True:
        word, plan = "approve", None
    else:
        word, plan = str(decision or "reject").strip().lower(), None
    if word in ("approve", "approved", "yes", "y", "proceed", "allow"):
        return "approve", None
    if word == "edit" and plan and plan.strip():
        return "edit", plan
    return "reject", None


class AutonomousRunner:
    def __init__(
        self,
        workspace: str | Path | None = None,
        on_plan_approval: Optional[PlanApprovalHandler] = None,
        on_permission_request: Optional[PermissionHandler] = None,
        approval_policy: str | None = None,
        auto_setup: bool = False,
        on_task_created: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.workspace = Path(workspace or os.getcwd()).resolve()
        self.on_plan_approval = on_plan_approval
        self.on_permission_request = on_permission_request
        self.approval_policy = approval_policy
        self.auto_setup = auto_setup
        self.on_task_created = on_task_created

    def _policy(self, override: str | None) -> str:
        for candidate in (override, self.approval_policy, PLAN_APPROVAL_SETTING):
            if candidate and str(candidate).lower() in _VALID_APPROVAL_SETTINGS:
                return str(candidate).lower()
        return "complex"

    def _ask(self, tool_name: str, args: dict, risk: Any, reasons: list[str]) -> bool:
        if self.on_permission_request is None:
            return False
        details = {k: v for k, v in args.items() if k in ("command", "path", "directory")}
        return is_approval(self.on_permission_request({
            "action": tool_name,
            "risk": getattr(risk, "value", str(risk)),
            "reasons": reasons,
            "details": details,
        }))

    def run_task(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        mode_override: Optional[str] = None,
        approval_policy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run one task end-to-end. Never raises; failures are reported in the result."""
        start_time = time.monotonic()
        workspace = str(self.workspace)
        task_id = create_task(prompt, workspace)
        if self.on_task_created is not None:
            self.on_task_created(task_id)
        log_event(task_id, "TASK_STARTED", {"prompt": prompt, "workspace": workspace, "session_id": session_id})

        previous_handler = get_ask_handler()
        previous_workspace = (qz_tools.WORKSPACE, getattr(qz_agent, "WORKSPACE", None))
        plan_text = ""
        try:
            with task_context_scope(TaskContext(task_id=task_id, workspace=self.workspace,
                                                policy=mode_override or "standard",
                                                read_only=(mode_override == "read_only"))):
                qz_tools.WORKSPACE = workspace
                qz_agent.WORKSPACE = workspace
                if self.on_permission_request is not None:
                    configure_security_gateway(ask_handler=self._ask)

                problem = check_model_access()
                if problem:
                    raise RuntimeError(problem)
                update_status(task_id, TaskStatus.ANALYZING, current_step="analyzing")
                qz_agent.prepare_environment(auto_setup=self.auto_setup, workspace=workspace)
                git_status = qz_tools.ensure_git_repository()
                log_event(task_id, "GIT_STATUS", {"message": git_status})

                mode = mode_override if mode_override in ("quick", "standard", "complex") else classify_task_mode(prompt)
                log_event(task_id, "TASK_CLASSIFIED", {"mode": mode})

                index, _ = qz_agent.load_or_build_index(workspace)
                baseline = qz_agent.capture_pre_existing_test_failures(workspace)

                outcome = resolve_plan(
                    prompt,
                    qz_agent.format_index_summary(index),
                    "never",
                    mode,
                    interactive_clarifications=False,
                    task_id=task_id,
                    build_dag=False,
                )
                plan_text = outcome.get("plan", "")

                edited_plan = None
                if requires_plan_approval(mode, self._policy(approval_policy)):
                    update_status(task_id, TaskStatus.WAITING_APPROVAL, current_step="waiting_approval")
                    log_event(task_id, "WAITING_APPROVAL", {"mode": mode})
                    raw = self.on_plan_approval({
                        "task": prompt,
                        "plan": plan_text,
                        "architecture": outcome.get("architecture", ""),
                        "mode": mode,
                    }) if self.on_plan_approval else "reject"
                    decision, edited_plan = _parse_plan_decision(raw)
                    if decision == "reject":
                        update_status(task_id, TaskStatus.CANCELLED, current_step="cancelled")
                        log_event(task_id, "TASK_CANCELLED", {"reason": "plan_rejected"})
                        return {"task_id": task_id, "status": "cancelled", "message": "Task rejected by user.",
                                "plan": plan_text, "latency_ms": (time.monotonic() - start_time) * 1000.0}
                    log_event(task_id, "APPROVAL_RECEIVED", {"decision": decision})

                finalize_plan(outcome, task_id=task_id, plan=edited_plan)
                plan_text = outcome["plan"]
                update_status(task_id, TaskStatus.EXECUTING, current_step="executing")
                result_output = qz_agent.run_executor(
                    outcome["task"], outcome["plan"], outcome["architecture"],
                    test_baseline=baseline, task_id=task_id,
                )
        except KeyboardInterrupt:
            update_status(task_id, TaskStatus.CANCELLED, current_step="cancelled")
            log_event(task_id, "TASK_CANCELLED", {"reason": "keyboard_interrupt"})
            return {"task_id": task_id, "status": "cancelled", "message": "Task cancelled by user.",
                    "plan": plan_text, "latency_ms": (time.monotonic() - start_time) * 1000.0}
        except Exception as err:
            update_status(task_id, TaskStatus.FAILED, current_step="failed")
            log_event(task_id, "TASK_FAILED", {"reason": str(err)})
            return {"task_id": task_id, "status": "failed", "error": str(err), "plan": plan_text,
                    "latency_ms": (time.monotonic() - start_time) * 1000.0}
        finally:
            configure_security_gateway(ask_handler=previous_handler)
            qz_tools.WORKSPACE, agent_ws = previous_workspace
            if agent_ws is not None:
                qz_agent.WORKSPACE = agent_ws

        latency_ms = (time.monotonic() - start_time) * 1000.0
        info = get_task(task_id) or {}
        status = str(info.get("status") or "").upper()
        final = {"COMPLETED": "completed", "CANCELLED": "cancelled"}.get(status, "failed")
        payload = {"task_id": task_id, "status": final, "result": str(result_output), "plan": plan_text, "latency_ms": latency_ms}
        if final == "failed":
            payload["error"] = str(result_output)
        else:
            try:  # a concise project fact for later planning; never the transcript
                from qz_memory import ProjectMemory

                ProjectMemory(workspace).remember("completed_feature", prompt, tags=["task", "completed"], task_id=task_id)
            except Exception:
                pass
        return payload
