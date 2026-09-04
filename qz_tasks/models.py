"""Task status names and SQLite schema for the persistent task engine."""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_request TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    status TEXT NOT NULL,
    complexity TEXT,
    current_step TEXT,
    plan_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    max_attempts INTEGER NOT NULL DEFAULT 20
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    iteration_number INTEGER NOT NULL,
    git_commit_hash TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task_id ON checkpoints(task_id);

CREATE TABLE IF NOT EXISTS subtasks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    files_touched TEXT,
    result_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_subtasks_task_id ON subtasks(task_id);
"""


class TaskStatus:
    RECEIVED = "RECEIVED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    RETRYING = "RETRYING"
    DEBUGGING = "DEBUGGING"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SubtaskStatus:
    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    RUNNING = "IN_PROGRESS"  # Backward-compatible alias
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    RETRYING = "RETRYING"


TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
})

TERMINAL_SUBTASK_STATUSES = frozenset({
    SubtaskStatus.COMPLETED,
    SubtaskStatus.FAILED,
    SubtaskStatus.BLOCKED,
    SubtaskStatus.CANCELLED,
})
