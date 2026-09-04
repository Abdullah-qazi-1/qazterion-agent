"""Autonomous execution loop for Qazterion CLI and core engine."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import qz_agent
import qz_tools
from qz_core.event_bus import get_event_bus
from qz_core.planner import resolve_plan, requires_plan_approval
from qz_core.classifier import classify_task_mode
from qz_security.gateway import configure as configure_security_gateway
from qz_security.rules_loader import load_project_rules
from qz_storage import get_storage


class AutonomousRunner:
    def __init__(
        self,
        workspace: str | Path | None = None,
        on_plan_approval: Optional[Callable[[Dict[str, Any]], str]] = None,
        on_permission_request: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> None:
        self.workspace = Path(workspace or os.getcwd()).resolve()
        self.on_plan_approval = on_plan_approval
        self.on_permission_request = on_permission_request
        self.event_bus = get_event_bus()
        self.storage = get_storage()

    def run_task(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        mode_override: Optional[str] = None,
        approval_policy: str = "never",
    ) -> Dict[str, Any]:
        """Execute an autonomous task from prompt through planning and execution."""
        start_time = time.time()
        task_id = self.storage.create_task(
            prompt=prompt,
            workspace=str(self.workspace),
            session_id=session_id,
            mode=mode_override or "standard",
        )

        self.event_bus.emit("TASK_STARTED", task_id, {
            "prompt": prompt,
            "workspace": str(self.workspace),
            "session_id": session_id,
        })

        try:
            # 1. Environment & rules setup
            os.chdir(str(self.workspace))
            qz_agent.WORKSPACE = str(self.workspace)
            qz_tools.WORKSPACE = str(self.workspace)
            qz_agent.ensure_git_repository()
            rules = load_project_rules(self.workspace)

            # 2. Configure permission handler
            if self.on_permission_request:
                def ask_handler(action_type: str, details: dict) -> str:
                    self.event_bus.emit("PERMISSION_REQUESTED", task_id, {
                        "action": action_type,
                        "details": details,
                    })
                    decision = self.on_permission_request({"action": action_type, "details": details})
                    self.event_bus.emit("PERMISSION_RESOLVED", task_id, {
                        "action": action_type,
                        "decision": decision,
                    })
                    return decision
                configure_security_gateway(ask_handler=ask_handler)

            # 3. Classify task mode
            mode = mode_override or classify_task_mode(prompt)
            self.event_bus.emit("TASK_CLASSIFIED", task_id, {"mode": mode})

            # 4. Load baseline & context
            index, _ = qz_agent.load_or_build_index(str(self.workspace))
            baseline = qz_agent.capture_pre_existing_test_failures()

            # 5. Plan & Architecture generation
            outcome = resolve_plan(
                prompt,
                qz_agent.format_index_summary(index),
                "never",
                mode,
                interactive_clarifications=False,
                task_id=task_id,
            )

            plan_text = outcome.get("plan", "")
            arch_text = outcome.get("architecture", "")
            self.storage.update_task_plan(task_id, plan_text, arch_text)
            self.event_bus.emit("PLAN_CREATED", task_id, {
                "plan": plan_text,
                "architecture": arch_text,
                "mode": mode,
            })

            # 6. Plan Approval gate
            if requires_plan_approval(mode, approval_policy) and self.on_plan_approval:
                decision = self.on_plan_approval({
                    "task": prompt,
                    "plan": plan_text,
                    "architecture": arch_text,
                    "mode": mode,
                })
                if decision in ("reject", "cancel", "n"):
                    self.storage.complete_task(task_id, status="cancelled")
                    self.event_bus.emit("TASK_FAILED", task_id, {"reason": "Plan rejected by user"})
                    return {"task_id": task_id, "status": "cancelled", "message": "Task rejected by user."}

            # 7. Execute plan via DAG / core executor
            self.event_bus.emit("EXECUTION_STARTED", task_id, {"task": prompt})
            result_output = qz_agent.run_executor(
                outcome["task"],
                outcome["plan"],
                outcome["architecture"],
                test_baseline=baseline,
                task_id=task_id,
            )

            latency_ms = (time.time() - start_time) * 1000.0
            self.storage.complete_task(task_id, status="completed", latency_ms=latency_ms)
            self.event_bus.emit("TASK_COMPLETED", task_id, {
                "result": result_output,
                "latency_ms": latency_ms,
            })

            return {
                "task_id": task_id,
                "status": "completed",
                "result": result_output,
                "plan": plan_text,
                "latency_ms": latency_ms,
            }

        except Exception as err:
            latency_ms = (time.time() - start_time) * 1000.0
            self.storage.complete_task(task_id, status="failed", latency_ms=latency_ms)
            self.event_bus.emit("TASK_FAILED", task_id, {
                "error": str(err),
                "latency_ms": latency_ms,
            })
            return {"task_id": task_id, "status": "failed", "error": str(err)}
