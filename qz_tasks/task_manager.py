"""Create, update, and query persisted tasks, events, checkpoints, and subtasks."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path
from typing import Any, Iterator, TextIO

from qz_tasks.database import connect, default_db_path
from qz_tasks.models import SCHEMA_SQL, TERMINAL_STATUSES, SubtaskStatus, TaskStatus

_manager: TaskManager | None = None
_announced_interrupted = False
_global_event_subscribers: set[Any] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class TaskManager:
    def __init__(
        self,
        db_path: str | PathLike[str] | None = None,
        on_event: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._subscribers: set[Any] = set()
        if on_event is not None:
            self._subscribers.add(on_event)
        with self._session() as connection:
            connection.executescript(SCHEMA_SQL)

    def subscribe_event(self, callback: Any) -> None:
        self._subscribers.add(callback)

    def unsubscribe_event(self, callback: Any) -> None:
        self._subscribers.discard(callback)

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def create_task(self, user_request: str, workspace_path: str, *, max_attempts: int = 20) -> str:
        task_id = str(uuid.uuid4())
        stamp = _now()
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, user_request, workspace_path, status, complexity, current_step,
                    plan_text, created_at, updated_at, max_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    user_request,
                    workspace_path,
                    TaskStatus.RECEIVED,
                    None,
                    "received",
                    None,
                    stamp,
                    stamp,
                    int(max_attempts),
                ),
            )
            self.log_event(task_id, "TASK_STARTED", {"workspace_path": workspace_path})
        return task_id

    def ensure_task(self, task_id: str, user_request: str, workspace_path: str, *, max_attempts: int = 20) -> bool:
        """Insert a task row for an explicitly-supplied task_id if one doesn't already exist.

        Callers (e.g. the DAG executor) may be handed a task_id that was minted upstream
        without a corresponding `tasks` row yet being persisted. Subtask inserts have a
        foreign-key dependency on `tasks.id`, so this must run before any subtask is created
        for that task_id. Returns True if a new row was inserted, False if one already existed.
        """
        if self.get_task(task_id) is not None:
            return False
        stamp = _now()
        with self._session() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO tasks (
                    id, user_request, workspace_path, status, complexity, current_step,
                    plan_text, created_at, updated_at, max_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    user_request,
                    workspace_path,
                    TaskStatus.RECEIVED,
                    None,
                    "received",
                    None,
                    stamp,
                    stamp,
                    int(max_attempts),
                ),
            )
        self.log_event(task_id, "TASK_STARTED", {"workspace_path": workspace_path})
        return True

    def update_status(
        self,
        task_id: str,
        new_status: str,
        *,
        current_step: str | None = None,
        complexity: str | None = None,
        plan_text: str | None = None,
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [new_status, _now()]
        if current_step is not None:
            fields.append("current_step = ?")
            values.append(current_step)
        if complexity is not None:
            fields.append("complexity = ?")
            values.append(complexity)
        if plan_text is not None:
            fields.append("plan_text = ?")
            values.append(plan_text)
        values.append(task_id)
        with self._session() as connection:
            connection.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def create_subtasks(self, task_id: str, subtasks: list[dict]) -> list[dict[str, Any]]:
        """Bulk insert subtasks with temporary/index dependency resolution."""
        if not subtasks:
            return []

        real_ids = [str(uuid.uuid4()) for _ in subtasks]
        mapping: dict[Any, str] = {}
        for i, subtask in enumerate(subtasks):
            mapping[i] = real_ids[i]
            mapping[str(i)] = real_ids[i]
            if "id" in subtask and subtask["id"] is not None:
                mapping[subtask["id"]] = real_ids[i]
                mapping[str(subtask["id"])] = real_ids[i]
            if "temp_id" in subtask and subtask["temp_id"] is not None:
                mapping[subtask["temp_id"]] = real_ids[i]
                mapping[str(subtask["temp_id"])] = real_ids[i]

        stamp = _now()
        rows_to_insert = []
        for i, subtask in enumerate(subtasks):
            raw_deps = subtask.get("depends_on") or []
            if not isinstance(raw_deps, list):
                raw_deps = [raw_deps]
            resolved_deps: list[str] = []
            for dep in raw_deps:
                if dep in mapping:
                    target_id = mapping[dep]
                elif isinstance(dep, int) and 0 <= dep < len(subtasks):
                    target_id = real_ids[dep]
                elif isinstance(dep, str) and dep.isdigit() and 0 <= int(dep) < len(subtasks):
                    target_id = real_ids[int(dep)]
                elif isinstance(dep, str):
                    target_id = dep
                else:
                    continue
                if target_id != real_ids[i] and target_id not in resolved_deps:
                    resolved_deps.append(target_id)

            files_touched = subtask.get("files_touched")
            encoded_files = json.dumps(files_touched, default=str) if files_touched is not None else None

            rows_to_insert.append((
                real_ids[i],
                task_id,
                subtask.get("title", f"Subtask {i + 1}"),
                subtask.get("description", ""),
                json.dumps(resolved_deps),
                subtask.get("status", SubtaskStatus.PENDING),
                int(subtask.get("attempts", 0)),
                encoded_files,
                subtask.get("result_summary"),
                stamp,
                stamp,
            ))

        with self._session() as connection:
            connection.executemany(
                """
                INSERT INTO subtasks (
                    id, task_id, title, description, depends_on, status,
                    attempts, files_touched, result_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )

        self.log_event(task_id, "SUBTASKS_CREATED", {"count": len(subtasks)})
        return self.get_subtasks(task_id)

    def update_subtask_status(
        self,
        subtask_id: str,
        status: str,
        *,
        result_summary: str | None = None,
        files_touched: list[str] | None = None,
        attempts: int | None = None,
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, _now()]
        if result_summary is not None:
            fields.append("result_summary = ?")
            values.append(result_summary)
        if files_touched is not None:
            fields.append("files_touched = ?")
            values.append(json.dumps(files_touched, default=str))
        if attempts is not None:
            fields.append("attempts = ?")
            values.append(int(attempts))
        values.append(subtask_id)
        with self._session() as connection:
            connection.execute(
                f"UPDATE subtasks SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def get_subtasks(self, task_id: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subtasks
                WHERE task_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (task_id,),
            ).fetchall()
        items = []
        for row in rows:
            item = _row_to_dict(row)
            if item:
                try:
                    item["depends_on"] = json.loads(item["depends_on"]) if item.get("depends_on") else []
                except (json.JSONDecodeError, TypeError):
                    item["depends_on"] = []
                if item.get("files_touched"):
                    try:
                        item["files_touched"] = json.loads(item["files_touched"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                items.append(item)
        return items

    def get_ready_subtasks(self, task_id: str) -> list[dict[str, Any]]:
        all_subtasks = self.get_subtasks(task_id)
        completed_ids = {
            str(s["id"]) for s in all_subtasks
            if str(s.get("status", "")).upper() == SubtaskStatus.COMPLETED
        }
        ready = []
        for s in all_subtasks:
            status_upper = str(s.get("status", "")).upper()
            if status_upper in (SubtaskStatus.PENDING, SubtaskStatus.READY):
                deps = s.get("depends_on") or []
                if all(str(dep) in completed_ids for dep in deps):
                    ready.append(s)
        return ready

    def get_next_ready_subtasks(self, task_id: str) -> list[dict[str, Any]]:
        return self.get_ready_subtasks(task_id)

    def mark_dependent_subtasks_blocked(
        self,
        task_id: str,
        failed_subtask_id: str,
        reason: str | None = None,
    ) -> list[str]:
        """Find all transitive downstream dependent subtasks and mark them BLOCKED."""
        all_subtasks = self.get_subtasks(task_id)
        subtask_map = {str(s["id"]): s for s in all_subtasks}

        # Build child dependency map: parent_id -> list of child_ids
        children_map: dict[str, list[str]] = {}
        for s in all_subtasks:
            for dep in s.get("depends_on") or []:
                children_map.setdefault(str(dep), []).append(str(s["id"]))

        blocked_ids: list[str] = []
        queue = list(children_map.get(str(failed_subtask_id), []))
        visited = set(queue)

        while queue:
            curr_id = queue.pop(0)
            curr = subtask_map.get(curr_id)
            if curr:
                curr_status = str(curr.get("status", "")).upper()
                if curr_status not in (SubtaskStatus.COMPLETED, SubtaskStatus.BLOCKED, SubtaskStatus.FAILED, SubtaskStatus.CANCELLED):
                    blocked_ids.append(curr_id)
                    block_reason = reason or f"Prerequisite {failed_subtask_id} failed"
                    self.update_subtask_status(
                        curr_id,
                        SubtaskStatus.BLOCKED,
                        result_summary=f"Blocked: {block_reason}",
                    )
                    self.log_event(task_id, "NODE_BLOCKED", {
                        "subtask_id": curr_id,
                        "title": curr.get("title"),
                        "reason": block_reason,
                    })
                    self.log_event(task_id, "SUBTASK_BLOCKED", {
                        "subtask_id": curr_id,
                        "title": curr.get("title"),
                        "reason": block_reason,
                    })
            for next_child in children_map.get(curr_id, []):
                if next_child not in visited:
                    visited.add(next_child)
                    queue.append(next_child)

        return blocked_ids

    def get_completed_dependency_summaries(self, task_id: str, subtask_id: str) -> list[dict[str, Any]]:
        all_subtasks = self.get_subtasks(task_id)
        subtask_map = {str(s["id"]): s for s in all_subtasks}
        current = subtask_map.get(str(subtask_id))
        if not current:
            return []
        summaries = []
        for dep_id in current.get("depends_on") or []:
            dep = subtask_map.get(str(dep_id))
            if dep and str(dep.get("status", "")).upper() == SubtaskStatus.COMPLETED:
                summaries.append({
                    "id": dep.get("id"),
                    "title": dep.get("title"),
                    "status": dep.get("status"),
                    "result_summary": dep.get("result_summary") or "",
                    "files_touched": dep.get("files_touched") or [],
                })
        return summaries

    def log_event(self, task_id: str, event_type: str, payload: dict | None = None) -> int:
        stamp = _now()
        encoded = json.dumps(payload, default=str) if payload is not None else None
        with self._session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO task_events (task_id, timestamp, event_type, payload)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, stamp, event_type, encoded),
            )
            row_id = int(cursor.lastrowid)

        # Safely notify registered instance subscribers
        for callback in list(self._subscribers):
            try:
                callback(task_id, event_type, payload, stamp)
            except Exception:
                pass

        # Safely notify registered global subscribers
        for callback in list(_global_event_subscribers):
            try:
                callback(task_id, event_type, payload, stamp)
            except Exception:
                pass

        return row_id

    def create_checkpoint(
        self,
        task_id: str,
        iteration_number: int,
        git_commit_hash: str | None = None,
        summary: str | None = None,
    ) -> int:
        stamp = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO checkpoints (
                    task_id, iteration_number, git_commit_hash, summary, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, int(iteration_number), git_commit_hash, summary, stamp),
            )
            checkpoint_id = int(cursor.lastrowid)
        self.log_event(
            task_id,
            "CHECKPOINT_CREATED",
            {
                "iteration_number": iteration_number,
                "git_commit_hash": git_commit_hash,
                "summary": summary,
            },
        )
        return checkpoint_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row)

    def get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        events = []
        for row in rows:
            item = _row_to_dict(row)
            if item and item.get("payload"):
                try:
                    item["payload"] = json.loads(item["payload"])
                except json.JSONDecodeError:
                    pass
            events.append(item)
        return events

    def get_latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE task_id = ?
                ORDER BY iteration_number DESC, id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return _row_to_dict(row)

    def find_interrupted_tasks(self) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" * len(TERMINAL_STATUSES))
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM tasks
                WHERE status NOT IN ({placeholders})
                ORDER BY updated_at ASC
                """,
                tuple(TERMINAL_STATUSES),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]


def get_manager() -> TaskManager:
    global _manager
    path = default_db_path()
    if _manager is None or Path(_manager.db_path) != Path(path):
        _manager = TaskManager(path)
    return _manager


def reset_manager() -> None:
    """Drop the process-wide singleton so the next get_manager() reopens the current path."""
    global _manager
    _manager = None


def create_task(user_request: str, workspace_path: str, *, max_attempts: int = 20) -> str:
    return get_manager().create_task(user_request, workspace_path, max_attempts=max_attempts)


def ensure_task(task_id: str, user_request: str, workspace_path: str, *, max_attempts: int = 20) -> bool:
    return get_manager().ensure_task(task_id, user_request, workspace_path, max_attempts=max_attempts)


def update_status(task_id: str, new_status: str, **kwargs) -> None:
    get_manager().update_status(task_id, new_status, **kwargs)


def create_subtasks(task_id: str, subtasks: list[dict]) -> list[dict[str, Any]]:
    return get_manager().create_subtasks(task_id, subtasks)


def update_subtask_status(subtask_id: str, status: str, **kwargs) -> None:
    get_manager().update_subtask_status(subtask_id, status, **kwargs)


def get_subtasks(task_id: str) -> list[dict[str, Any]]:
    return get_manager().get_subtasks(task_id)


def get_ready_subtasks(task_id: str) -> list[dict[str, Any]]:
    return get_manager().get_ready_subtasks(task_id)


def get_next_ready_subtasks(task_id: str) -> list[dict[str, Any]]:
    return get_manager().get_next_ready_subtasks(task_id)


def mark_dependent_subtasks_blocked(task_id: str, failed_subtask_id: str, reason: str | None = None) -> list[str]:
    return get_manager().mark_dependent_subtasks_blocked(task_id, failed_subtask_id, reason=reason)


def get_completed_dependency_summaries(task_id: str, subtask_id: str) -> list[dict[str, Any]]:
    return get_manager().get_completed_dependency_summaries(task_id, subtask_id)


def log_event(task_id: str, event_type: str, payload: dict | None = None) -> int:
    return get_manager().log_event(task_id, event_type, payload)


def create_checkpoint(
    task_id: str,
    iteration_number: int,
    git_commit_hash: str | None = None,
    summary: str | None = None,
) -> int:
    return get_manager().create_checkpoint(task_id, iteration_number, git_commit_hash, summary)


def get_task(task_id: str) -> dict | None:
    return get_manager().get_task(task_id)


def get_task_events(task_id: str) -> list[dict]:
    return get_manager().get_task_events(task_id)


def get_latest_checkpoint(task_id: str) -> dict | None:
    return get_manager().get_latest_checkpoint(task_id)


def subscribe_events(callback: Any) -> None:
    _global_event_subscribers.add(callback)
    if _manager is not None:
        _manager.subscribe_event(callback)


def unsubscribe_events(callback: Any) -> None:
    _global_event_subscribers.discard(callback)
    if _manager is not None:
        _manager.unsubscribe_event(callback)


def find_interrupted_tasks() -> list[dict]:
    return get_manager().find_interrupted_tasks()


def format_interrupted_notice(task: dict, *, manager: TaskManager | None = None) -> str:
    mgr = manager or get_manager()
    checkpoint = mgr.get_latest_checkpoint(task["id"])
    commit = (checkpoint or {}).get("git_commit_hash") or "none"
    step = task.get("current_step") or task.get("status") or "unknown"
    return (
        f"Task {task['id']} was interrupted at step {step}, "
        f"last checkpoint at commit {commit}."
    )


def announce_interrupted_tasks(*, stream: TextIO | None = None, force: bool = False) -> list[dict]:
    """Print interrupted tasks so the operator can see them. Does not resume."""
    global _announced_interrupted
    stream = stream or sys.stderr
    if _announced_interrupted and not force:
        return []
    _announced_interrupted = True
    tasks = find_interrupted_tasks()
    for task in tasks:
        line = format_interrupted_notice(task)
        print(f"\033[93m[tasks]\033[0m {line}", file=stream, flush=True)
    return tasks
