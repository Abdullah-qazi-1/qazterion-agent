"""SQLite connection helpers. Each write is committed immediately."""

from __future__ import annotations

import atexit
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

DEFAULT_DB_NAME = "qazterion_tasks.db"
_ephemeral_test_db: Path | None = None


def user_db_path() -> Path:
    """The on-disk database a real Qazterion process uses for interrupted-task notices."""
    # Installed applications execute modules from immutable resources (or a
    # PyInstaller temp extraction directory). Keep mutable state in a stable,
    # user-owned directory when the desktop bridge supplies one.
    runtime_dir = os.environ.get("QAZTERION_DATA_DIR")
    if runtime_dir:
        return Path(runtime_dir) / DEFAULT_DB_NAME
    return Path(__file__).resolve().parent.parent / DEFAULT_DB_NAME


def _module_looks_like_a_test(name: str, module: object) -> bool:
    if name.startswith("test_") or name.startswith("tests."):
        return True
    filename = getattr(module, "__file__", None)
    if not filename:
        return False
    base = Path(filename).name
    return base.startswith("test_") and base.endswith(".py")


def running_under_automated_tests() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    argv0 = Path(sys.argv[0]).name if sys.argv else ""
    if argv0 in {"unittest", "pytest", "py.test"} or argv0.startswith("test_"):
        return True
    return any(_module_looks_like_a_test(name, module) for name, module in list(sys.modules.items()))


def _cleanup_ephemeral_test_db() -> None:
    if _ephemeral_test_db is None:
        return
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{_ephemeral_test_db}{suffix}")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _ephemeral_test_db_path() -> Path:
    global _ephemeral_test_db
    if _ephemeral_test_db is None:
        handle, raw = tempfile.mkstemp(prefix="qazterion_tasks_test_", suffix=".db")
        os.close(handle)
        _ephemeral_test_db = Path(raw)
        atexit.register(_cleanup_ephemeral_test_db)
    return _ephemeral_test_db


def default_db_path() -> Path:
    override = os.environ.get("QAZTERION_TASKS_DB")
    if override:
        return Path(override)
    if running_under_automated_tests():
        return _ephemeral_test_db_path()
    return user_db_path()


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    if str(path) not in {":memory:"}:
        path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None => autocommit; a crash cannot leave an uncommitted txn.
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection
