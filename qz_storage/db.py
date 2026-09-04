"""Phase 13 — SQLite persistent storage for sessions, tasks, events, checkpoints, and telemetry."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def get_default_db_path() -> Path:
    override = os.environ.get("QAZTERION_DB_PATH") or os.environ.get("QAZTERION_TASKS_DB")
    if override:
        return Path(override)
    data_dir = os.environ.get("QAZTERION_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "qazterion.db"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Qazterion" / "qazterion.db"
    return Path.home() / ".qazterion" / "qazterion.db"


class StorageManager:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else get_default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    summary TEXT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    workspace TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'standard',
                    status TEXT NOT NULL DEFAULT 'pending',
                    plan TEXT,
                    architecture TEXT,
                    tokens_used INTEGER DEFAULT 0,
                    latency_ms REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    git_hash TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    message TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    latency_ms REAL DEFAULT 0.0,
                    success INTEGER DEFAULT 1,
                    error TEXT,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id);
                """
            )

    def create_session(self, workspace: str) -> str:
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO sessions (id, workspace, started_at, last_active_at) VALUES (?, ?, ?, ?)",
                (session_id, str(workspace), now, now),
            )
        return session_id

    def touch_session(self, session_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("UPDATE sessions SET last_active_at = ? WHERE id = ?", (now, session_id))

    def create_task(self, prompt: str, workspace: str, session_id: Optional[str] = None, mode: str = "standard") -> str:
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            if session_id:
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, workspace, started_at, last_active_at) VALUES (?, ?, ?, ?)",
                    (session_id, str(workspace), now, now),
                )
            conn.execute(
                """
                INSERT INTO tasks (id, session_id, workspace, prompt, mode, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (task_id, session_id, str(workspace), prompt, mode, now),
            )
        if session_id:
            self.touch_session(session_id)
        return task_id

    def update_task_plan(self, task_id: str, plan: str, architecture: str = "") -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET plan = ?, architecture = ? WHERE id = ?",
                (plan, architecture, task_id),
            )

    def complete_task(self, task_id: str, status: str = "completed", tokens: int = 0, latency_ms: float = 0.0) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, tokens_used = ?, latency_ms = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, tokens, latency_ms, now, task_id),
            )

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_recent_tasks(self, workspace: Optional[str] = None, limit: int = 15) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if workspace:
            query += " WHERE workspace = ?"
            params.append(str(workspace))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def log_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload or {}, default=str)
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?, ?, ?, ?)",
                (task_id, event_type, encoded, now),
            )

    def get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY id ASC", (task_id,)
            ).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["payload"] = json.loads(item["payload"])
                except Exception:
                    pass
                results.append(item)
            return results

    def log_checkpoint(self, task_id: str, git_hash: str, step_name: str, message: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO checkpoints (task_id, git_hash, step_name, message, timestamp) VALUES (?, ?, ?, ?, ?)",
                (task_id, git_hash, step_name, message, now),
            )

    def list_checkpoints(self, task_id: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        query = "SELECT * FROM checkpoints"
        params: list[Any] = []
        if task_id:
            query += " WHERE task_id = ?"
            params.append(task_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def record_model_call(
        self,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
        error: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO model_calls (task_id, provider, model, input_tokens, output_tokens, latency_ms, success, error, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, provider, model, input_tokens, output_tokens, latency_ms, 1 if success else 0, error, now),
            )

    def get_metrics_summary(self) -> dict[str, Any]:
        with self._get_conn() as conn:
            total_calls = conn.execute("SELECT COUNT(*) as count FROM model_calls").fetchone()["count"]
            success_calls = conn.execute("SELECT COUNT(*) as count FROM model_calls WHERE success = 1").fetchone()["count"]
            token_stats = conn.execute("SELECT SUM(input_tokens + output_tokens) as total_tokens, AVG(latency_ms) as avg_latency FROM model_calls").fetchone()
            by_provider_rows = conn.execute(
                "SELECT provider, COUNT(*) as count, SUM(input_tokens + output_tokens) as tokens, AVG(latency_ms) as avg_latency FROM model_calls GROUP BY provider"
            ).fetchall()
            
            return {
                "total_calls": total_calls,
                "success_rate": (success_calls / total_calls * 100) if total_calls > 0 else 100.0,
                "total_tokens": token_stats["total_tokens"] or 0,
                "avg_latency_ms": round(token_stats["avg_latency"] or 0.0, 1),
                "by_provider": {r["provider"]: {"calls": r["count"], "tokens": r["tokens"] or 0, "avg_latency": round(r["avg_latency"] or 0.0, 1)} for r in by_provider_rows},
            }


_GLOBAL_STORAGE: Optional[StorageManager] = None


def get_storage() -> StorageManager:
    global _GLOBAL_STORAGE
    if _GLOBAL_STORAGE is None:
        _GLOBAL_STORAGE = StorageManager()
    return _GLOBAL_STORAGE
