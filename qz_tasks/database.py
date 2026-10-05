"""SQLite connection helpers for the task database. Each write is committed immediately.

The database lives in the per-user data directory (``qz_paths.data_dir()``),
never next to the source code. ``QAZTERION_TASKS_DB`` overrides the file path
(tests point it at a temporary file).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from qz_paths import data_dir

DEFAULT_DB_NAME = "qazterion_tasks.db"


def user_db_path() -> Path:
    """The on-disk database a real Qazterion process uses."""
    return data_dir() / DEFAULT_DB_NAME


def default_db_path() -> Path:
    override = os.environ.get("QAZTERION_TASKS_DB")
    return Path(override) if override else user_db_path()


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None => autocommit; a crash cannot leave an uncommitted txn.
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    # NORMAL is crash-safe in WAL mode and avoids an fsync on every event row.
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection
