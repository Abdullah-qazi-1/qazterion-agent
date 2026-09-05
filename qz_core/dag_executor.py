"""Hard DAG Execution Engine for Qazterion.

Enforces node-by-node DAG execution where Python state and validation are authoritative.
Each subtask has a strict execution boundary, scoped context, per-node validation,
bounded repair loop, dependency output propagation, and atomic state transitions.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

import qz_core.executor as executor_mod
from qz_core.client import get_client
from qz_core.common import _persist_task
from qz_core.git_ops import generate_commit_message, _commit_hash_from_result
from qz_core.executor import TestBaseline
from qz_indexer import format_index_summary, load_or_build_index
from qz_pool import get_pool
from qz_router import get_router
from qz_security.redaction import redact
from qz_tasks.models import SubtaskStatus, TaskStatus
from qz_tasks.task_manager import (
    create_checkpoint,
    ensure_task,
    get_completed_dependency_summaries,
    get_ready_subtasks,
    get_subtasks,
    get_task,
    log_event,
    mark_dependent_subtasks_blocked,
    update_status,
    update_subtask_status,
)
import qz_tools
from qz_tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, commit_changes
from qz_validation import CheckStatus, ValidationReport

# NOTE ON PATCHING: classify_task_complexity, select_route, request_completion
# and _task_requires_test_changes are wrapped below rather than imported by
# name. Some tests patch them at "qz_core.dag_executor.<name>" (this module's
# own attribute) while others patch "qz_core.executor.<name>" (the original
# home module). A plain `from qz_core.executor import request_completion`
# would bind a private reference here that neither style of patch could
# reach, silently falling back to real network calls. The wrapper functions
# below are themselves patchable module attributes on qz_core.dag_executor,
# and when *not* patched here they forward the call dynamically (looked up
# at call time, not import time) to qz_core.executor, so a patch applied
# there is honored too.


def classify_task_complexity(*args: Any, **kwargs: Any) -> Any:
    return executor_mod.classify_task_complexity(*args, **kwargs)


def select_route(*args: Any, **kwargs: Any) -> Any:
    return executor_mod.select_route(*args, **kwargs)


def request_completion(*args: Any, **kwargs: Any) -> Any:
    return executor_mod.request_completion(*args, **kwargs)


def _task_requires_test_changes(*args: Any, **kwargs: Any) -> Any:
    return executor_mod._task_requires_test_changes(*args, **kwargs)


@dataclass
class NodeResult:
    """Structured execution outcome for a single DAG node."""
    node_id: str
    status: str  # COMPLETED | FAILED | CANCELLED | BLOCKED
    summary: str = ""
    files_changed: list[dict[str, Any]] = field(default_factory=list)
    commands_executed: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    validation_result: dict[str, Any] | None = None
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)
    duration: float = 0.0
    model_used: str = ""
    provider_id: str = ""
    model_id: str = ""
    model_display_name: str = ""
    key_identity: str = ""
    attempts: int = 1
    routing_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "summary": self.summary,
            "files_changed": self.files_changed,
            "commands_executed": self.commands_executed,
            "tests_run": self.tests_run,
            "validation_result": self.validation_result,
            "error": self.error,
            "artifacts": self.artifacts,
            "duration": round(self.duration, 3),
            "model_used": self.model_used,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_display_name": self.model_display_name,
            "key_identity": self.key_identity,
            "attempts": self.attempts,
            "routing_details": self.routing_details,
        }


@dataclass
class DAGExecutionResult:
    """Overall outcome of executing a complete task DAG."""
    task_id: str
    status: str  # COMPLETED | FAILED | CANCELLED
    node_results: list[NodeResult] = field(default_factory=list)
    summary: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "node_results": [nr.to_dict() for nr in self.node_results],
            "summary": self.summary,
            "duration": round(self.duration, 3),
        }


def _format_dependency_context(dependencies: list[dict[str, Any]]) -> str:
    """Format outputs from completed prerequisite nodes for the active node prompt."""
    if not dependencies:
        return "No prior dependency outputs (this is an initial step)."

    lines = ["Outputs and artifacts from completed prerequisite subtasks:"]
    for dep in dependencies:
        title = dep.get("title", f"Subtask {dep.get('id')}")
        summary = dep.get("result_summary") or "Completed without extra summary."
        files = dep.get("files_touched") or []
        files_str = f" (Files touched: {', '.join(files)})" if files else ""
        lines.append(f"- [{dep.get('id')}] {title}{files_str}:\n  Summary: {summary}")
    return "\n".join(lines)


class HardDAGExecutor:
    """Authoritative Python DAG executor enforcing sequential node-by-node execution."""

    def __init__(
        self,
        workspace: str | Path | None = None,
        max_node_retries: int = 3,
        max_node_iterations: int = 15,
        client: OpenAI | None = None,
    ) -> None:
        # NOTE: qz_tools.WORKSPACE is read live via the module (qz_tools.WORKSPACE),
        # not via a `from qz_tools import WORKSPACE` name. A plain name-import
        # only copies the value once, at the moment this module is first
        # imported (e.g. during pytest collection) -- it never reflects later
        # changes, such as a test doing `patch.object(qz_tools, "WORKSPACE", ...)`.
        # That stale copy previously caused every HardDAGExecutor() built
        # without an explicit `workspace=` to silently run against whatever
        # directory was current process-wide at import time (e.g. the real
        # repo checkout) instead of the caller's intended workspace.
        self.workspace = Path(workspace or qz_tools.WORKSPACE).resolve()
        self.max_node_retries = max_node_retries
        self.max_node_iterations = max_node_iterations
        self.client = client
        self.val_pipeline = executor_mod.ValidationPipeline()

    def _is_task_cancelled(self, task_id: str) -> bool:
        task = get_task(task_id)
        return bool(task and str(task.get("status", "")).upper() == TaskStatus.CANCELLED)

    def execute_node(
        self,
        task_id: str,
        overall_task: str,
        node: dict[str, Any],
        dependency_outputs: list[dict[str, Any]],
        test_baseline: TestBaseline | None = None,
    ) -> NodeResult:
        """Execute a single active DAG node within strict execution boundaries and a bounded repair loop."""
        node_id = str(node["id"])
        node_title = str(node.get("title") or f"Subtask {node_id}")
        node_desc = str(node.get("description") or node_title)

        start_time = time.monotonic()
        complexity = classify_task_complexity(f"{overall_task}\n{node_title}\n{node_desc}")
        model_alias, chosen_key = select_route(complexity, task_id=task_id)

        model_meta = None
        try:
            from qz_providers.model_registry import get_model_registry
            model_meta = get_model_registry().get_model_by_alias(model_alias)
        except ImportError:
            pass

        if model_meta:
            provider_id = model_meta.provider
            model_id = model_meta.model_id
            model_display_name = model_meta.display_name
        else:
            provider_id = model_alias.split("/")[0] if "/" in model_alias else model_alias
            model_id = model_alias
            model_display_name = model_alias
        key_identity = chosen_key

        dep_context = _format_dependency_context(dependency_outputs)
        node_acceptance_criteria = (
            f"Acceptance Criteria for this node:\n"
            f"1. Implement: {node_title} - {node_desc}\n"
            f"2. Validate all changes using run_command tests where applicable.\n"
            f"3. Do NOT execute or complete future subtasks; focus strictly on this node."
        )

        # Phase 13: Project rules and persistent repository context
        try:
            from qz_security.rules_loader import load_project_rules
            project_rules = load_project_rules(self.workspace)
            rules_prompt = project_rules.format_for_prompt()
        except Exception:
            rules_prompt = ""

        # Phase 13: Hybrid context retrieval & budget management
        context_prompt = ""
        try:
            from qz_context import ContextBudgetManager
            ctx_mgr = ContextBudgetManager()
            ctx = ctx_mgr.select_context(
                f"{overall_task} {node_title} {node_desc}",
                workspace=self.workspace,
                model_alias=model_alias,
            )
            context_prompt = ctx.format_for_prompt()
            if ctx.files or ctx.snippets:
                log_event(task_id, "CONTEXT_SELECTED", {
                    "subtask_id": node_id,
                    "total_tokens": ctx.total_tokens,
                    "snippets_count": len(ctx.snippets),
                    "files_count": len(ctx.files),
                })
        except Exception:
            context_prompt = ""

        prompt_parts = [
            f"Overall Task Objective: {overall_task}\n",
            f"=== ACTIVE DAG NODE ===\nNode ID: {node_id}\nTitle: {node_title}\nDescription:\n{node_desc}\n",
            f"{node_acceptance_criteria}\n",
        ]
        if rules_prompt:
            prompt_parts.append(rules_prompt)
        if context_prompt:
            prompt_parts.append(context_prompt)
        prompt_parts.append(f"{dep_context}\n")
        prompt_parts.append(
            "Instruction: Implement and verify ONLY this active node step-by-step. "
            "When done and verified, provide a concise summary of your work."
        )

        user_prompt = "\n".join(prompt_parts)

        messages = [
            {"role": "system", "content": executor_mod.SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        attempt = 1
        consecutive_failures = 0
        escalated = False
        active_model = model_alias

        node_changes: list[dict[str, Any]] = []
        node_commands: list[str] = []
        node_tests: list[str] = []
        files_touched_set: set[str] = set()
        test_changes_required = _task_requires_test_changes(f"{overall_task} {node_title}")
        test_file_changed = False
        last_turn_response = ""

        try:
            from qz_repair import RepairHistory, classify_failure
            repair_history = RepairHistory()
        except Exception:
            repair_history = None

        while attempt <= self.max_node_retries:
            if self._is_task_cancelled(task_id):
                update_subtask_status(node_id, SubtaskStatus.CANCELLED, attempts=attempt)
                log_event(task_id, "NODE_CANCELLED", {"subtask_id": node_id, "title": node_title})
                log_event(task_id, "dag.node.cancelled", {"subtask_id": node_id, "title": node_title})
                return NodeResult(
                    node_id=node_id,
                    status=SubtaskStatus.CANCELLED,
                    summary="Node execution cancelled by user.",
                    duration=time.monotonic() - start_time,
                    attempts=attempt,
                )

            # Node turn loop
            for iteration in range(self.max_node_iterations):
                if self._is_task_cancelled(task_id):
                    break

                messages, _ = executor_mod.roll_conversation_summary(messages, client=self.client)
                _persist_task(task_id, lambda: update_status(
                    task_id,
                    TaskStatus.EXECUTING,
                    current_step=f"node {node_id} (attempt {attempt}, turn {iteration + 1})",
                ))

                try:
                    resp, used_model = request_completion(
                        model=active_model,
                        messages=messages,
                        tools=TOOL_SCHEMAS,
                        temperature=0.2,
                        fallbacks=executor_mod.EXECUTOR_FALLBACKS.get(active_model, (active_model,)),
                        client=self.client,
                        task_id=task_id,
                    )
                    if used_model != active_model:
                        active_model = used_model
                        log_event(task_id, "MODEL_FALLBACK", {"from": model_alias, "to": active_model})
                except Exception as e:
                    print(f"\033[91m[node {node_id}]\033[0m Model request failed: {e}")
                    log_event(task_id, "NODE_ERROR", {"subtask_id": node_id, "error": str(e)})
                    break

                msg = resp.choices[0].message
                msg_dict = msg.model_dump(exclude_none=True)
                clean_msg = {
                    k: v for k, v in msg_dict.items()
                    if k in ("role", "content", "tool_calls", "name", "tool_call_id", "function_call")
                }
                messages.append(clean_msg)

                tool_calls = msg.tool_calls or []
                if not tool_calls:
                    last_turn_response = msg.content or ""
                    break

                iteration_failed = None
                for tool_call in tool_calls:
                    fname = tool_call.function.name
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    tool_payload = {"tool": fname, "subtask_id": node_id}
                    if fname == "run_command" and "command" in args:
                        tool_payload["command"] = redact(str(args["command"]))
                    log_event(task_id, "TOOL_STARTED", tool_payload)

                    func = TOOL_FUNCTIONS.get(fname)
                    try:
                        result = func(**args) if func else f"Unknown tool: {fname}"
                    except (TypeError, ValueError, OSError) as e:
                        result = f"Tool {fname} failed: {e}"

                    log_event(task_id, "TOOL_FINISHED", {
                        "tool": fname,
                        "subtask_id": node_id,
                        "ok": not str(result).startswith("Tool "),
                    })

                    # Track file changes for this node
                    if fname == "apply_patch" and str(result).startswith("Patch applied:"):
                        changed_path = args.get("path", "unknown")
                        node_changes.append({"path": changed_path, "kind": "patch", "detail": args.get("diff", "")})
                        files_touched_set.add(changed_path)
                        test_file_changed = test_file_changed or executor_mod._is_test_file_path(changed_path)
                    elif fname == "write_file" and str(result).startswith("Written:"):
                        changed_path = args.get("path", "unknown")
                        node_changes.append({"path": changed_path, "kind": "new or rewritten file", "detail": args.get("content", "")})
                        files_touched_set.add(changed_path)
                        test_file_changed = test_file_changed or executor_mod._is_test_file_path(changed_path)

                    if fname == "run_command":
                        cmd_str = str(args.get("command", "")).strip()
                        node_commands.append(cmd_str)
                        node_tests.append(cmd_str)
                        iteration_failed = executor_mod._tool_result_failed(fname, str(result))
                        if (
                            iteration_failed
                            and test_baseline
                            and cmd_str == test_baseline.command.strip()
                            and executor_mod._test_failure_signature(str(result)) == test_baseline.signature
                        ):
                            iteration_failed = False

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(result),
                    })

                if iteration_failed is True:
                    consecutive_failures += 1
                elif iteration_failed is False:
                    consecutive_failures = 0

                if consecutive_failures >= 3 and not escalated:
                    prev_model = active_model
                    escalated_alias, _ = select_route("reasoner", task_id=task_id)
                    active_model = escalated_alias
                    escalated = True
                    consecutive_failures = 0
                    log_event(task_id, "MODEL_FALLBACK", {"from": prev_model, "to": active_model, "reason": "node_escalation"})

            # Validation Gate for this node
            _persist_task(task_id, lambda: update_status(task_id, TaskStatus.VALIDATING, current_step=f"validating node {node_id}"))
            val_report = self.val_pipeline.run(
                task_id=task_id,
                workspace=self.workspace,
                requirements_text=f"{overall_task}\n{node_title}\n{node_desc}",
                changes=node_changes,
                client=self.client,
            )

            if val_report.passed:
                # Validation passed -> Node is complete
                # Commit checkpoint strictly for files touched by this node
                touched_list = sorted(files_touched_set)
                commit_hash = None
                if touched_list:
                    commit_msg = generate_commit_message(f"[{node_id}] {node_title}", node_changes)
                    commit_res = commit_changes(touched_list, commit_msg)
                    if commit_res.startswith("Git commit created:"):
                        commit_hash = _commit_hash_from_result(commit_res)
                        create_checkpoint(task_id, attempt, git_commit_hash=commit_hash, summary=commit_msg)

                summary_text = last_turn_response.strip() or f"Node '{node_title}' completed successfully."
                update_subtask_status(
                    node_id,
                    SubtaskStatus.COMPLETED,
                    result_summary=summary_text,
                    files_touched=touched_list,
                    attempts=attempt,
                )

                log_event(task_id, "NODE_COMPLETED", {
                    "subtask_id": node_id,
                    "title": node_title,
                    "summary": summary_text,
                    "files_touched": touched_list,
                    "commit_hash": commit_hash,
                    "attempts": attempt,
                })
                log_event(task_id, "SUBTASK_COMPLETED", {
                    "subtask_id": node_id,
                    "title": node_title,
                    "summary": summary_text,
                    "files_touched": touched_list,
                    "attempts": attempt,
                })
                log_event(task_id, "dag.node.completed", {
                    "subtask_id": node_id,
                    "title": node_title,
                    "summary": summary_text,
                })

                return NodeResult(
                    node_id=node_id,
                    status=SubtaskStatus.COMPLETED,
                    summary=summary_text,
                    files_changed=node_changes,
                    commands_executed=node_commands,
                    tests_run=node_tests,
                    validation_result=val_report.to_dict(),
                    duration=time.monotonic() - start_time,
                    model_used=active_model,
                    provider_id=provider_id,
                    model_id=model_id,
                    model_display_name=model_display_name,
                    key_identity=key_identity,
                    attempts=attempt,
                )

            # Node validation failed -> repair loop if retries remain
            print(f"\033[93m[node {node_id}]\033[0m Validation failed on attempt {attempt}/{self.max_node_retries}:\n{val_report.summary}")
            if attempt < self.max_node_retries:
                attempt += 1
                update_subtask_status(node_id, SubtaskStatus.RETRYING, attempts=attempt)

                repair_prompt = f"Validation checks for this node failed:\n{val_report.summary}\n\nDiagnose the cause, apply fixes to the code/tests for this node, and rerun tests."
                try:
                    from qz_repair import classify_failure
                    classified = classify_failure(val_report, raw_command_output=node_commands[-1] if node_commands else None)
                    anti_loop = repair_history.get_anti_loop_feedback(classified.diagnostics.error_signature) if repair_history else ""
                    repair_prompt = classified.format_for_repair_prompt(attempt, self.max_node_retries, history_feedback=anti_loop)
                    if repair_history:
                        repair_history.record_attempt(attempt, classified.category, classified.diagnostics.error_signature)
                    log_event(task_id, "REPAIR_ATTEMPT", {
                        "subtask_id": node_id,
                        "attempt": attempt,
                        "category": classified.category.value,
                        "failing_check": classified.check_name,
                    })
                except Exception:
                    pass

                log_event(task_id, "NODE_RETRYING", {
                    "subtask_id": node_id,
                    "title": node_title,
                    "attempt": attempt,
                    "failing_checks": val_report.failed_required_checks,
                })
                log_event(task_id, "dag.node.retrying", {
                    "subtask_id": node_id,
                    "attempt": attempt,
                })
                messages.append({
                    "role": "user",
                    "content": repair_prompt,
                })
                continue
            else:
                # Retries exhausted -> Node permanently failed
                error_summary = f"Validation failed after {attempt} attempt(s): {', '.join(val_report.failed_required_checks)}"
                update_subtask_status(
                    node_id,
                    SubtaskStatus.FAILED,
                    result_summary=error_summary,
                    files_touched=sorted(files_touched_set),
                    attempts=attempt,
                )
                log_event(task_id, "NODE_FAILED", {
                    "subtask_id": node_id,
                    "title": node_title,
                    "error": error_summary,
                    "attempts": attempt,
                })
                log_event(task_id, "SUBTASK_FAILED", {
                    "subtask_id": node_id,
                    "title": node_title,
                    "error": error_summary,
                    "attempts": attempt,
                })
                log_event(task_id, "dag.node.failed", {
                    "subtask_id": node_id,
                    "title": node_title,
                    "error": error_summary,
                })

                return NodeResult(
                    node_id=node_id,
                    status=SubtaskStatus.FAILED,
                    summary=f"Failed: {error_summary}",
                    files_changed=node_changes,
                    commands_executed=node_commands,
                    tests_run=node_tests,
                    validation_result=val_report.to_dict(),
                    error=error_summary,
                    duration=time.monotonic() - start_time,
                    model_used=active_model,
                    provider_id=provider_id,
                    model_id=model_id,
                    model_display_name=model_display_name,
                    key_identity=key_identity,
                    attempts=attempt,
                )

        return NodeResult(
            node_id=node_id,
            status=SubtaskStatus.FAILED,
            error="Node retry loop completed without passing validation",
            duration=time.monotonic() - start_time,
            attempts=attempt,
        )

    def execute_dag(
        self,
        task_id: str,
        task_text: str,
        plan_text: str = "",
        architecture: str = "",
        test_baseline: TestBaseline | None = None,
        resume_from_subtask_id: str | None = None,
    ) -> DAGExecutionResult:
        """Execute the DAG sequentially node-by-node with authoritative state management."""
        dag_start_time = time.monotonic()

        # A task_id may be handed to us (e.g. from a caller that minted its own id) without a
        # corresponding `tasks` row yet persisted. Subtask rows have a FK dependency on
        # tasks.id, so guarantee the parent row exists before anything else touches the DB.
        ensure_task(task_id, task_text, str(self.workspace))

        all_subtasks = get_subtasks(task_id)

        if not all_subtasks:
            # Fallback: create single-node DAG if no subtasks were created during planning
            from qz_tasks.task_manager import create_subtasks
            single_node = [{
                "id": 0,
                "title": "Implement full plan",
                "description": f"{plan_text}\n\n{architecture}".strip() or task_text,
                "depends_on": [],
            }]
            all_subtasks = create_subtasks(task_id, single_node)

        log_event(task_id, "DAG_STARTED", {
            "task_id": task_id,
            "node_count": len(all_subtasks),
            "resumed": bool(resume_from_subtask_id),
        })
        log_event(task_id, "dag.started", {
            "task_id": task_id,
            "node_count": len(all_subtasks),
        })

        node_results: list[NodeResult] = []

        while True:
            if self._is_task_cancelled(task_id):
                update_status(task_id, TaskStatus.CANCELLED, current_step="cancelled")
                log_event(task_id, "TASK_CANCELLED", {"reason": "user_cancelled"})
                log_event(task_id, "dag.cancelled", {"task_id": task_id})
                return DAGExecutionResult(
                    task_id=task_id,
                    status=TaskStatus.CANCELLED,
                    node_results=node_results,
                    summary="DAG execution cancelled by user.",
                    duration=time.monotonic() - dag_start_time,
                )

            ready_nodes = get_ready_subtasks(task_id)
            current_all = get_subtasks(task_id)

            if not ready_nodes:
                completed_count = sum(1 for s in current_all if str(s.get("status", "")).upper() == SubtaskStatus.COMPLETED)
                failed_nodes = [s for s in current_all if str(s.get("status", "")).upper() == SubtaskStatus.FAILED]
                blocked_nodes = [s for s in current_all if str(s.get("status", "")).upper() == SubtaskStatus.BLOCKED]

                if completed_count == len(current_all):
                    # All nodes completed successfully!
                    update_status(task_id, TaskStatus.COMPLETED, current_step="completed")
                    log_event(task_id, "DAG_COMPLETED", {
                        "completed_nodes": completed_count,
                        "total_nodes": len(current_all),
                    })
                    log_event(task_id, "TASK_COMPLETED", {
                        "completed_nodes": completed_count,
                    })
                    log_event(task_id, "dag.completed", {
                        "task_id": task_id,
                        "node_count": completed_count,
                    })

                    consolidated_summary = "\n\n".join(
                        f"[{nr.node_id}] {nr.summary}" for nr in node_results if nr.summary
                    ) or "All DAG tasks completed successfully."

                    return DAGExecutionResult(
                        task_id=task_id,
                        status=TaskStatus.COMPLETED,
                        node_results=node_results,
                        summary=consolidated_summary,
                        duration=time.monotonic() - dag_start_time,
                    )

                if failed_nodes or blocked_nodes:
                    # Permanent failure in DAG
                    update_status(task_id, TaskStatus.FAILED, current_step="failed")
                    failed_titles = [f"[{s['id']}] {s.get('title')}" for s in failed_nodes]
                    log_event(task_id, "DAG_FAILED", {
                        "failed_nodes": failed_titles,
                        "blocked_nodes": len(blocked_nodes),
                    })
                    log_event(task_id, "TASK_FAILED", {
                        "reason": f"DAG nodes failed: {', '.join(failed_titles)}",
                    })
                    log_event(task_id, "dag.failed", {
                        "task_id": task_id,
                        "failed_nodes": failed_titles,
                    })

                    return DAGExecutionResult(
                        task_id=task_id,
                        status=TaskStatus.FAILED,
                        node_results=node_results,
                        summary=f"DAG execution failed on: {', '.join(failed_titles)}",
                        duration=time.monotonic() - dag_start_time,
                    )

                # Pending nodes exist with unsatisfied / circular dependencies
                unresolved = [s for s in current_all if str(s.get("status", "")).upper() not in (SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.BLOCKED)]
                for u in unresolved:
                    update_subtask_status(u["id"], SubtaskStatus.BLOCKED, result_summary="Unresolved dependencies")
                update_status(task_id, TaskStatus.FAILED, current_step="failed")
                log_event(task_id, "DAG_FAILED", {"reason": "unresolvable_dependencies"})
                return DAGExecutionResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    node_results=node_results,
                    summary="DAG stalled with unresolvable dependencies.",
                    duration=time.monotonic() - dag_start_time,
                )

            # Select active node (sequential execution)
            active_node = ready_nodes[0]
            node_id = str(active_node["id"])
            node_title = str(active_node.get("title") or f"Subtask {node_id}")

            # Notify node ready and transition to IN_PROGRESS
            log_event(task_id, "NODE_READY", {"subtask_id": node_id, "title": node_title})
            log_event(task_id, "dag.node.ready", {"subtask_id": node_id, "title": node_title})

            update_subtask_status(node_id, SubtaskStatus.IN_PROGRESS)
            log_event(task_id, "NODE_STARTED", {"subtask_id": node_id, "title": node_title})
            log_event(task_id, "SUBTASK_STARTED", {"subtask_id": node_id, "title": node_title})
            log_event(task_id, "SUBTASK_IN_PROGRESS", {"subtask_id": node_id, "title": node_title})
            log_event(task_id, "dag.node.started", {"subtask_id": node_id, "title": node_title})

            # Retrieve outputs from prerequisite nodes
            dep_summaries = get_completed_dependency_summaries(task_id, node_id)

            # Execute active node
            result = self.execute_node(
                task_id=task_id,
                overall_task=task_text,
                node=active_node,
                dependency_outputs=dep_summaries,
                test_baseline=test_baseline,
            )
            node_results.append(result)

            if result.status == SubtaskStatus.FAILED:
                # Cascade BLOCKED state to all downstream dependents
                mark_dependent_subtasks_blocked(task_id, node_id, reason=f"Prerequisite '{node_title}' failed")
                update_status(task_id, TaskStatus.FAILED, current_step="failed")
                log_event(task_id, "DAG_FAILED", {"failed_node": node_id})
                log_event(task_id, "TASK_FAILED", {"reason": f"Node {node_id} failed: {result.error}"})
                log_event(task_id, "dag.failed", {"task_id": task_id, "failed_node": node_id})
                return DAGExecutionResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    node_results=node_results,
                    summary=f"Task failed at node [{node_id}] {node_title}: {result.error}",
                    duration=time.monotonic() - dag_start_time,
                )

            if result.status == SubtaskStatus.CANCELLED:
                update_status(task_id, TaskStatus.CANCELLED, current_step="cancelled")
                log_event(task_id, "TASK_CANCELLED", {"reason": "node_cancelled"})
                log_event(task_id, "dag.cancelled", {"task_id": task_id})
                return DAGExecutionResult(
                    task_id=task_id,
                    status=TaskStatus.CANCELLED,
                    node_results=node_results,
                    summary="DAG execution cancelled.",
                    duration=time.monotonic() - dag_start_time,
                )