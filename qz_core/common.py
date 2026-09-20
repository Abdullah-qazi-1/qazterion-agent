from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any, Iterator


@dataclass(frozen=True)
class TaskContext:
    """Immutable, explicitly-passed task context preventing global mutable state."""
    task_id: str
    workspace: Path
    policy: str = "standard"
    permissions: dict[str, Any] = field(default_factory=dict)
    checkpoint: str | None = None
    cancellation_token: Any = None
    read_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())


_tls = threading.local()
_workspace_lock = threading.RLock()


def set_task_context(ctx: TaskContext | None) -> None:
    """Bind an immutable TaskContext to the current thread only."""
    _tls.context = ctx


def get_task_context() -> TaskContext | None:
    return getattr(_tls, "context", None)


def get_active_workspace(fallback: str | Path | None = None) -> Path:
    ctx = get_task_context()
    if ctx is not None:
        return ctx.workspace
    if fallback is not None:
        return Path(fallback).resolve()
    raise RuntimeError("No TaskContext is bound and no workspace fallback was provided.")


@contextmanager
def task_context_scope(ctx: TaskContext) -> Iterator[TaskContext]:
    previous = get_task_context()
    set_task_context(ctx)
    try:
        yield ctx
    finally:
        set_task_context(previous)


def workspace_mutation_lock() -> threading.RLock:
    return _workspace_lock


def _persist_task(task_id: str | None, action: Callable[[], Any]) -> None:
    """Best-effort persistence; never changes agent control flow on DB errors."""
    if not task_id:
        return
    try:
        action()
    except Exception as error:
        print(f"\033[90m[tasks] persistence skipped ({error})\033[0m")
