"""Task recovery and resume engine for interrupted Qazterion tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import subprocess

from qz_sandbox.backend import NO_WINDOW
from qz_sandbox.backend import sanitize_subprocess_env
from qz_tasks import SubtaskStatus, TaskStatus, get_manager
from qz_tools import current_workspace


def get_current_head(workspace: str | Path | None = None) -> str | None:
    """Return the current short or full git HEAD commit hash, or None if unavailable."""
    ws = Path(workspace or current_workspace()).resolve()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ws,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            env=sanitize_subprocess_env(str(ws)),
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_uncommitted_files(workspace: str | Path | None = None) -> list[str]:
    """Return tracked files with uncommitted (staged or unstaged) changes.

    Untracked files are ignored: a hard reset never touches them, and build
    artifacts such as __pycache__ must not block rollback or resume.
    """
    ws = Path(workspace or current_workspace()).resolve()
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ws,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            env=sanitize_subprocess_env(str(ws)),
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []



class IntegrityStatus(str, Enum):
    CLEAN = "CLEAN"
    DIRTY_UNCOMMITTED = "DIRTY_UNCOMMITTED"
    DIVERGED = "DIVERGED"


@dataclass(frozen=True)
class IntegrityResult:
    """Workspace git integrity state compared against a task's checkpoint commit."""
    status: IntegrityStatus
    message: str
    expected_commit: str | None = None
    current_head: str | None = None
    uncommitted_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "expected_commit": self.expected_commit,
            "current_head": self.current_head,
            "uncommitted_files": self.uncommitted_files,
        }


@dataclass(frozen=True)
class ResumePlan:
    """Structured plan describing completed subtasks and resume starting point."""
    task_id: str
    task_status: str
    total_subtasks: int
    completed_subtasks: list[dict[str, Any]]
    pending_subtasks: list[dict[str, Any]]
    next_ready_subtask: dict[str, Any] | None
    latest_checkpoint: dict[str, Any] | None
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_status": self.task_status,
            "total_subtasks": self.total_subtasks,
            "completed_subtasks": self.completed_subtasks,
            "pending_subtasks": self.pending_subtasks,
            "next_ready_subtask": self.next_ready_subtask,
            "latest_checkpoint": self.latest_checkpoint,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ResumeOutcome:
    """Outcome of attempting to resume, restart, rollback, or discard a task."""
    success: bool
    action: str
    message: str
    result: Any = None
    integrity: IntegrityResult | None = None
    plan: ResumePlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "message": self.message,
            "result": self.result,
            "integrity": self.integrity.to_dict() if self.integrity else None,
            "plan": self.plan.to_dict() if self.plan else None,
        }


def _check_workspace_bound(task_id: str, requested_workspace: Path | str) -> dict[str, Any] | None:
    manager = get_manager()
    task = manager.get_task(task_id)
    if task:
        task_ws = task.get("workspace_path")
        if task_ws:
            canon_task_ws = Path(task_ws).resolve()
            canon_req_ws = Path(requested_workspace).resolve()
            if canon_task_ws != canon_req_ws:
                raise ValueError(
                    f"Cross-workspace recovery refused: task '{task_id}' is bound to workspace '{canon_task_ws}', "
                    f"requested workspace is '{canon_req_ws}'."
                )
    return task


class ResumeManager:
    """Coordinates workspace integrity checks and task resumption from DAG checkpoints."""

    def __init__(self, workspace: str | Path | None = None) -> None:
        self.workspace = Path(workspace or current_workspace()).resolve()

    def get_resumable_state(self, task_id: str) -> ResumePlan:
        """Inspect task status, subtasks, and latest checkpoint to construct a ResumePlan."""
        task = _check_workspace_bound(task_id, self.workspace)
        if not task:
            raise ValueError(f"Task '{task_id}' not found.")
        manager = get_manager()

        subtasks = manager.get_subtasks(task_id)
        latest_checkpoint = manager.get_latest_checkpoint(task_id)

        completed = [
            s for s in subtasks
            if str(s.get("status", "")).upper() == SubtaskStatus.COMPLETED
        ]
        pending = [
            s for s in subtasks
            if str(s.get("status", "")).upper() != SubtaskStatus.COMPLETED
        ]

        ready_subtasks = manager.get_next_ready_subtasks(task_id)
        next_ready = ready_subtasks[0] if ready_subtasks else (pending[0] if pending else None)

        lines = [
            f"Task {task_id} ({task.get('status')}): {len(completed)}/{len(subtasks)} subtask(s) completed.",
        ]
        if next_ready:
            lines.append(f"Resume point: [{next_ready.get('id')}] {next_ready.get('title')}")
        else:
            lines.append("No pending subtasks remaining.")
        if latest_checkpoint:
            commit = latest_checkpoint.get("git_commit_hash")
            lines.append(f"Latest checkpoint commit: {commit[:7] if commit else 'none'}")

        return ResumePlan(
            task_id=task_id,
            task_status=str(task.get("status", "")),
            total_subtasks=len(subtasks),
            completed_subtasks=completed,
            pending_subtasks=pending,
            next_ready_subtask=next_ready,
            latest_checkpoint=latest_checkpoint,
            summary="\n".join(lines),
        )

    def verify_workspace_integrity(
        self,
        task_id: str,
        workspace: Path | str | None = None,
    ) -> IntegrityResult:
        """Check whether workspace git state matches the task's expected checkpoint commit."""
        ws = Path(workspace or self.workspace).resolve()
        _check_workspace_bound(task_id, ws)
        latest_checkpoint = get_manager().get_latest_checkpoint(task_id)
        expected_commit = latest_checkpoint.get("git_commit_hash") if latest_checkpoint else None

        # 1. Check for uncommitted working tree changes
        uncommitted = get_uncommitted_files(str(ws))
        if uncommitted:
            return IntegrityResult(
                status=IntegrityStatus.DIRTY_UNCOMMITTED,
                message=f"Workspace has {len(uncommitted)} uncommitted file(s) since last checkpoint.",
                expected_commit=expected_commit,
                current_head=get_current_head(str(ws)),
                uncommitted_files=uncommitted,
            )

        current_head = get_current_head(str(ws))

        # 2. No checkpoint commit recorded (e.g. task interrupted before first commit)
        if not expected_commit:
            return IntegrityResult(
                status=IntegrityStatus.CLEAN,
                message="Workspace is clean (no previous checkpoint commit required).",
                expected_commit=None,
                current_head=current_head,
                uncommitted_files=[],
            )

        # 3. Check if HEAD matches checkpoint commit hash (full or prefix)
        if current_head and (current_head.startswith(expected_commit) or expected_commit.startswith(current_head)):
            return IntegrityResult(
                status=IntegrityStatus.CLEAN,
                message=f"Workspace HEAD matches checkpoint commit ({expected_commit[:7]}).",
                expected_commit=expected_commit,
                current_head=current_head,
                uncommitted_files=[],
            )

        # 4. HEAD has diverged from checkpoint commit
        return IntegrityResult(
            status=IntegrityStatus.DIVERGED,
            message=(
                f"Workspace HEAD ({current_head[:7] if current_head else 'none'}) "
                f"does not match checkpoint commit ({expected_commit[:7]})."
            ),
            expected_commit=expected_commit,
            current_head=current_head,
            uncommitted_files=[],
        )

    def resume_task(
        self,
        task_id: str,
        workspace: Path | str | None = None,
        decision: str = "RESUME",
    ) -> ResumeOutcome:
        """Execute a resume decision (RESUME, RESTART_CURRENT_SUBTASK, ROLLBACK, DISCARD)."""
        manager = get_manager()
        ws = Path(workspace or self.workspace).resolve()
        try:
            task = _check_workspace_bound(task_id, ws)
        except ValueError as err:
            return ResumeOutcome(
                success=False,
                action="BLOCKED",
                message=str(err),
            )
        manager.log_event(task_id, "RESUME_ATTEMPTED", {"decision": decision, "workspace": str(ws)})

        decision_upper = decision.upper()

        if decision_upper == "RESUME":
            integrity = self.verify_workspace_integrity(task_id, ws)
            if integrity.status == IntegrityStatus.DIRTY_UNCOMMITTED:
                manager.log_event(task_id, "RESUME_BLOCKED_DIRTY_WORKSPACE", {"uncommitted": integrity.uncommitted_files})
                return ResumeOutcome(
                    success=False,
                    action="BLOCKED",
                    message=f"Resume blocked: {integrity.message}",
                    integrity=integrity,
                )
            if integrity.status == IntegrityStatus.DIVERGED:
                manager.log_event(task_id, "RESUME_BLOCKED_DIVERGED", {"expected": integrity.expected_commit, "current_head": integrity.current_head})
                return ResumeOutcome(
                    success=False,
                    action="BLOCKED",
                    message=f"Resume blocked: {integrity.message}",
                    integrity=integrity,
                )

            # Integrity is CLEAN -> run executor from next ready subtask
            plan = self.get_resumable_state(task_id)
            starting_subtask_id = plan.next_ready_subtask["id"] if plan.next_ready_subtask else None
            task = manager.get_task(task_id)
            if not task:
                return ResumeOutcome(
                    success=False,
                    action="FAILED",
                    message=f"Task '{task_id}' not found.",
                    integrity=integrity,
                    plan=plan,
                )

            from qz_core.executor import run_executor
            result = run_executor(
                task=task["user_request"],
                plan=task.get("plan_text") or "",
                architecture="",
                task_id=task_id,
                resume_from_subtask_id=starting_subtask_id,
            )
            return ResumeOutcome(
                success=True,
                action="RESUMED",
                message=f"Task resumed from subtask {starting_subtask_id or 'beginning'}.",
                result=result,
                integrity=integrity,
                plan=plan,
            )

        elif decision_upper == "RESTART_CURRENT_SUBTASK":
            plan = self.get_resumable_state(task_id)
            target = plan.next_ready_subtask or (plan.pending_subtasks[0] if plan.pending_subtasks else None)
            if target:
                manager.update_subtask_status(target["id"], SubtaskStatus.PENDING, attempts=0, result_summary=None)

            integrity = self.verify_workspace_integrity(task_id, ws)
            if integrity.status != IntegrityStatus.CLEAN:
                return ResumeOutcome(
                    success=False,
                    action="BLOCKED",
                    message=f"Restart subtask blocked: {integrity.message}",
                    integrity=integrity,
                    plan=plan,
                )

            task = manager.get_task(task_id)
            if not task:
                return ResumeOutcome(
                    success=False,
                    action="FAILED",
                    message=f"Task '{task_id}' not found.",
                    integrity=integrity,
                    plan=plan,
                )

            from qz_core.executor import run_executor
            result = run_executor(
                task=task["user_request"],
                plan=task.get("plan_text") or "",
                architecture="",
                task_id=task_id,
                resume_from_subtask_id=target["id"] if target else None,
            )
            return ResumeOutcome(
                success=True,
                action="RESTARTED_SUBTASK",
                message=f"Restarted subtask {target['id'] if target else 'none'}.",
                result=result,
                integrity=integrity,
                plan=plan,
            )

        elif decision_upper == "ROLLBACK":
            # Undo every Qazterion commit this task made (safety checks included).
            return self.safe_rollback_to_checkpoint(task_id, workspace=ws)

        elif decision_upper == "DISCARD":
            manager.update_status(task_id, TaskStatus.CANCELLED, current_step="discarded")
            manager.log_event(task_id, "TASK_DISCARDED", {})
            return ResumeOutcome(
                success=True,
                action="DISCARDED",
                message="Task cancelled and discarded.",
            )

        else:
            raise ValueError(
                f"Unknown resume decision: '{decision}'. Supported: RESUME, RESTART_CURRENT_SUBTASK, ROLLBACK, DISCARD"
            )

    def _git(self, ws: Path, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=ws,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            env=sanitize_subprocess_env(str(ws)),
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )

    def preview_rollback(
        self,
        task_id: str,
        checkpoint_id: int | None = None,
        workspace: Path | str | None = None,
    ) -> dict[str, Any]:
        """Describe what rolling back would do, without changing anything.

        Rolling back to a checkpoint restores the repository to the state *before*
        that checkpoint's commit (undoing that step and every later one). Without
        ``checkpoint_id`` the task's first checkpoint is used, undoing the whole
        task. Only commits created by Qazterion may be discarded, and only when
        the working tree has no uncommitted changes.
        """
        ws = Path(workspace or self.workspace).resolve()
        manager = get_manager()

        def blocked(reason: str, message: str, **extra: Any) -> dict[str, Any]:
            return {"can_rollback": False, "reason": reason, "message": message, "affected_files": [], **extra}

        integrity = self.verify_workspace_integrity(task_id, ws)
        if integrity.status == IntegrityStatus.DIRTY_UNCOMMITTED:
            return blocked(
                "dirty_workspace",
                f"Rollback blocked: {len(integrity.uncommitted_files)} uncommitted file(s) in workspace. Commit or stash them first.",
                uncommitted_files=integrity.uncommitted_files,
                target_commit=None,
            )

        if checkpoint_id is not None:
            checkpoint = manager.get_checkpoint(checkpoint_id)
            if checkpoint is None:
                return blocked("unknown_checkpoint", f"Rollback blocked: checkpoint {checkpoint_id} does not exist.")
            if str(checkpoint.get("task_id")) != str(task_id):
                return blocked("unowned_checkpoint", f"Rollback blocked: checkpoint {checkpoint_id} does not belong to task {task_id}.")
        else:
            checkpoints = manager.list_checkpoints(task_id=task_id, limit=1000)
            checkpoint = checkpoints[-1] if checkpoints else None
            if checkpoint is None:
                return blocked("no_checkpoint", "Rollback blocked: this task has no recorded checkpoint commits.")

        commit = str(checkpoint.get("git_commit_hash") or "")
        if not commit:
            return blocked("no_checkpoint", "Rollback blocked: the checkpoint has no commit recorded.")
        current_head = get_current_head(str(ws))
        if not current_head:
            return blocked("no_git_head", "Git HEAD commit could not be determined.")

        if self._git(ws, ["merge-base", "--is-ancestor", commit, current_head]).returncode != 0:
            return blocked(
                "not_ancestor",
                f"Rollback blocked: checkpoint commit '{commit[:7]}' is not part of the current branch history.",
                target_commit=commit,
                current_head=current_head,
            )
        target_ref = f"{commit}~1"
        if self._git(ws, ["rev-parse", "--verify", "--quiet", target_ref]).returncode != 0:
            return blocked("no_parent", "Rollback blocked: the checkpoint is the repository's first commit.")

        listing = self._git(ws, ["rev-list", f"{target_ref}..{current_head}"])
        discarded = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        from qz_tools import AGENT_COMMIT_TRAILER

        foreign = []
        for sha in discarded:
            body = self._git(ws, ["log", "-1", "--format=%an%n%B", sha]).stdout
            first = body.splitlines()[0].strip() if body else ""
            if AGENT_COMMIT_TRAILER not in body and first != "Qazterion Agent":
                foreign.append(sha[:7])
        if foreign:
            return blocked(
                "foreign_commits",
                f"Rollback blocked: it would discard {len(foreign)} commit(s) not made by Qazterion ({', '.join(foreign[:5])}).",
                target_commit=target_ref,
                current_head=current_head,
            )

        # A hard reset leaves untracked files alone, except where the target
        # commit has a file at the same path (one the agent deleted later).
        recreated = set(self._git(ws, ["diff", "--name-only", "--diff-filter=D", target_ref, current_head]).stdout.split())
        untracked = set(self._git(ws, ["ls-files", "--others", "--exclude-standard"]).stdout.split())
        clobbered = sorted(recreated & untracked)
        if clobbered:
            return blocked(
                "untracked_conflict",
                f"Rollback blocked: it would overwrite untracked file(s): {', '.join(clobbered[:5])}.",
                target_commit=target_ref,
                current_head=current_head,
            )

        res = self._git(ws, ["diff", "--numstat", target_ref, current_head])
        affected = []
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    affected.append({
                        "additions": int(parts[0]) if parts[0].isdigit() else 0,
                        "deletions": int(parts[1]) if parts[1].isdigit() else 0,
                        "path": parts[2],
                    })
        return {
            "can_rollback": True,
            "reason": "ready",
            "message": f"Safe rollback ready: undo {len(discarded)} Qazterion commit(s), restoring {len(affected)} file(s).",
            "target_commit": target_ref,
            "current_head": current_head,
            "discarded_commits": [sha[:7] for sha in discarded],
            "affected_files": affected,
        }

    def safe_rollback_to_checkpoint(
        self,
        task_id: str,
        checkpoint_id: int | None = None,
        workspace: Path | str | None = None,
        force: bool = False,
    ) -> ResumeOutcome:
        """Undo Qazterion commits back to before a checkpoint.

        ``force`` is accepted for API compatibility but never bypasses the
        safety checks: uncommitted work and foreign commits are always protected.
        """
        del force
        ws = Path(workspace or self.workspace).resolve()
        manager = get_manager()
        preview = self.preview_rollback(task_id, checkpoint_id=checkpoint_id, workspace=ws)
        if not preview["can_rollback"]:
            return ResumeOutcome(success=False, action="BLOCKED", message=preview["message"], result=preview)

        target_ref = preview["target_commit"]
        try:
            res = self._git(ws, ["reset", "--hard", target_ref])
        except Exception as e:
            return ResumeOutcome(success=False, action="FAILED", message=f"Safe rollback encountered error: {e}")
        if res.returncode != 0:
            return ResumeOutcome(success=False, action="FAILED", message=f"Git reset failed: {res.stderr or res.stdout}")

        manager.update_status(task_id, TaskStatus.CANCELLED, current_step="rolled_back")
        manager.log_event(task_id, "TASK_ROLLED_BACK", {"target_ref": target_ref, "preview": preview})
        return ResumeOutcome(
            success=True,
            action="ROLLED_BACK",
            message=f"Safe rollback complete: workspace restored to {target_ref}.",
            result=preview,
        )





def get_resume_manager(workspace: str | Path | None = None) -> ResumeManager:
    """Return a ResumeManager bound to ``workspace`` (cheap and stateless, so not shared)."""
    return ResumeManager(Path(workspace or current_workspace()).resolve())
