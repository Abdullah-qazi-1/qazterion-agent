from __future__ import annotations
from typing import Callable, Any


def _persist_task(task_id: str | None, action: Callable[[], Any]) -> None:
    """Best-effort persistence; never changes agent control flow on DB errors."""
    if not task_id:
        return
    try:
        action()
    except Exception as error:
        print(f"\033[90m[tasks] persistence skipped ({error})\033[0m")
