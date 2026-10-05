"""Token, usage and cost accounting plus per-task budgets.

Every provider call made by the gateway is appended to ``<data dir>/usage.jsonl``.
The desktop dashboard and the CLI read the same file; :meth:`UsageTracker.refresh`
picks up lines appended by other processes without re-reading the whole file.
Key health/cooldowns are *not* tracked here (see :mod:`qz_providers.health`).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qz_paths import data_dir

MAX_LOG_BYTES = 5 * 1024 * 1024

# Rough reference prices per 1M tokens (USD), by provider. Free tiers cost 0 in
# practice; these only give the dashboard a sense of scale.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "groq": (0.59, 0.79),
    "gemini": (0.075, 0.30),
    "deepseek": (0.14, 0.28),
    "mistral": (0.20, 0.60),
    "openrouter": (0.50, 1.50),
}
_FALLBACK_PRICE = (0.25, 0.75)


def calculate_estimated_cost(model: str, input_tokens: int, output_tokens: int, provider: str | None = None) -> float:
    provider_id = (provider or str(model).split("/", 1)[0]).lower()
    input_price, output_price = DEFAULT_PRICING.get(provider_id, _FALLBACK_PRICE)
    cost = (input_tokens / 1_000_000.0) * input_price + (output_tokens / 1_000_000.0) * output_price
    return round(cost, 6)


def default_usage_log_path() -> Path:
    override = os.environ.get("QAZTERION_USAGE_LOG_PATH")
    return Path(override) if override else data_dir() / "usage.jsonl"


def _display_key(value: str | None) -> str | None:
    """Key ids are env-style names (GEMINI_KEY_2) and safe to show; anything
    else (e.g. a raw secret passed by mistake) is masked."""
    if not value:
        return None
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        return value
    return "*" * 6 + value[-4:] if len(value) > 4 else "*" * len(value)


@dataclass
class TaskBudget:
    max_tokens: int | None = None
    max_cost: float | None = None
    warning_threshold: float = 0.80


class BudgetExceededError(RuntimeError):
    """Raised when a task exceeds its configured token or cost budget limit."""


def _new_total(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id, "request_count": 0, "input_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "estimated_cost": 0.0, "total_duration": 0.0, "success_count": 0,
        "error_count": 0, "fallback_count": 0, "models_used": set(), "providers_used": set(),
    }


class UsageTracker:
    def __init__(self, log_path: str | os.PathLike[str] | None = None, max_events: int = 1000) -> None:
        self._lock = threading.RLock()
        self.log_path = Path(log_path) if log_path else None
        self._events: deque[dict] = deque(maxlen=max_events)
        self._task_totals: dict[str, dict[str, Any]] = {}
        self._budgets: dict[str, TaskBudget] = {}
        self._offset = 0
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.log_path = None
        self.refresh()

    # ---- file sync ----------------------------------------------------------

    def refresh(self) -> None:
        """Ingest lines appended to the log (by any process) since the last read."""
        if not self.log_path:
            return
        with self._lock:
            try:
                size = self.log_path.stat().st_size
            except OSError:
                return
            if size < self._offset:  # rotated or truncated by another process
                self._offset = 0
                self._events.clear()
                self._task_totals.clear()
            if size == self._offset:
                return
            try:
                with self.log_path.open("rb") as handle:
                    handle.seek(self._offset)
                    chunk = handle.read()
            except OSError:
                return
            # Only consume complete lines; a partial trailing line is read next time.
            end = chunk.rfind(b"\n") + 1
            for line in chunk[:end].splitlines():
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    self._events.append(event)
                    self._accumulate(event)
            self._offset += end

    def _accumulate(self, event: dict[str, Any]) -> None:
        task_id = event.get("task_id")
        if not task_id:
            return
        total = self._task_totals.setdefault(task_id, _new_total(task_id))
        total["request_count"] += 1
        total["input_tokens"] += int(event.get("input_tokens") or 0)
        total["output_tokens"] += int(event.get("output_tokens") or 0)
        total["total_tokens"] += int(event.get("total_tokens") or 0)
        total["estimated_cost"] = round(total["estimated_cost"] + float(event.get("estimated_cost") or 0.0), 6)
        total["total_duration"] = round(total["total_duration"] + float(event.get("duration") or 0.0), 3)
        total["success_count" if event.get("success") else "error_count"] += 1
        if event.get("fallback_from"):
            total["fallback_count"] += 1
        if event.get("model"):
            total["models_used"].add(event["model"])
        if event.get("provider"):
            total["providers_used"].add(event["provider"])

    def _rotate_if_needed(self) -> None:
        if not self.log_path:
            return
        try:
            if self.log_path.stat().st_size > MAX_LOG_BYTES:
                backup = self.log_path.with_suffix(".jsonl.1")
                os.replace(self.log_path, backup)
                self._offset = 0
        except OSError:
            pass

    # ---- budgets --------------------------------------------------------------

    def set_task_budget(self, task_id: str, max_tokens: int | None = None, max_cost: float | None = None,
                        warning_threshold: float = 0.80) -> None:
        with self._lock:
            self._budgets[task_id] = TaskBudget(max_tokens, max_cost, warning_threshold)

    def check_task_budget(self, task_id: str | None) -> tuple[bool, str | None, bool]:
        """Return ``(is_ok, message, is_warning_only)``."""
        with self._lock:
            if not task_id or task_id not in self._budgets:
                return True, None, False
            budget = self._budgets[task_id]
            usage = self.get_task_usage(task_id)
            if budget.max_tokens:
                if usage["total_tokens"] >= budget.max_tokens:
                    return False, f"Task {task_id} exceeded token limit ({usage['total_tokens']:,}/{budget.max_tokens:,})", False
                if usage["total_tokens"] >= int(budget.max_tokens * budget.warning_threshold):
                    return True, f"Task {task_id} approaching token limit ({usage['total_tokens']:,}/{budget.max_tokens:,})", True
            if budget.max_cost:
                if usage["estimated_cost"] >= budget.max_cost:
                    return False, f"Task {task_id} exceeded cost limit (${usage['estimated_cost']:.4f}/${budget.max_cost:.4f})", False
                if usage["estimated_cost"] >= budget.max_cost * budget.warning_threshold:
                    return True, f"Task {task_id} approaching cost limit (${usage['estimated_cost']:.4f}/${budget.max_cost:.4f})", True
            return True, None, False

    def admit_request(self, task_id: str | None, estimated_in_tokens: int = 0) -> tuple[bool, str | None]:
        with self._lock:
            if not task_id or task_id not in self._budgets:
                return True, None
            budget = self._budgets[task_id]
            usage = self.get_task_usage(task_id)
            if budget.max_tokens and usage["total_tokens"] + estimated_in_tokens > budget.max_tokens:
                return False, (f"Task {task_id} exceeds hard token limit "
                               f"({usage['total_tokens'] + estimated_in_tokens:,}/{budget.max_tokens:,})")
            if budget.max_cost and usage["estimated_cost"] >= budget.max_cost:
                return False, f"Task {task_id} reached hard cost limit (${usage['estimated_cost']:.4f}/${budget.max_cost:.4f})"
            return True, None

    def get_task_usage(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            total = self._task_totals.get(task_id) or _new_total(task_id)
            result = dict(total)
            result["models_used"] = sorted(total["models_used"])
            result["providers_used"] = sorted(total["providers_used"])
            return result

    # ---- recording --------------------------------------------------------------

    def record_request(
        self,
        *,
        model: str,
        duration: float,
        success: bool,
        key_id: str | None = None,
        error: str | None = None,
        fallback_from: str | None = None,
        provider: str | None = None,
        task_id: str | None = None,
        subtask_id: str | None = None,
        attempt: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        is_estimated: bool = False,
        estimated_cost: float | None = None,
        request_id: str | None = None,
        task_type: str | None = None,
        http_status: int | None = None,
        retry_count: int = 0,
        rate_limit: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            total_tokens = total_tokens or (input_tokens + output_tokens)
            if estimated_cost is None:
                estimated_cost = calculate_estimated_cost(model, input_tokens, output_tokens, provider) if total_tokens else 0.0
            event = {
                "request_id": request_id or str(uuid.uuid4()),
                "ts": time.time(),
                "model": model,
                "provider": provider,
                "key_id": key_id,
                "task_id": task_id,
                "subtask_id": subtask_id,
                "attempt": attempt,
                "duration": duration,
                "success": bool(success),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "is_estimated": is_estimated,
                "estimated_cost": estimated_cost,
                "error": error,
                "fallback_from": fallback_from,
                "task_type": task_type,
                "http_status": http_status,
                "retry_count": retry_count,
                "rate_limit": rate_limit or {"status": "unknown"},
            }
            if not self.log_path:
                self._events.append(event)
                self._accumulate(event)
                return
            # Catch up with other writers first so our offset stays aligned.
            self.refresh()
            self._rotate_if_needed()
            line = (json.dumps(event) + "\n").encode("utf-8")
            try:
                with self.log_path.open("ab") as handle:
                    handle.write(line)
            except OSError:
                self._events.append(event)
                self._accumulate(event)
                return
            self.refresh()

    # ---- reporting ----------------------------------------------------------------

    def recent_events(self, limit: int = 50) -> list[dict]:
        self.refresh()
        with self._lock:
            return list(self._events)[-limit:]

    def summary(self) -> dict[str, Any]:
        self.refresh()
        with self._lock:
            events = list(self._events)
            successes = [e for e in events if e.get("success")]
            errors = [e for e in events if not e.get("success")]
            last = events[-1] if events else None

            def aggregate(items: list[dict]) -> dict[str, Any]:
                count = len(items)
                ok = sum(1 for item in items if item.get("success"))
                return {
                    "requests": count,
                    "tokens": sum(int(item.get("total_tokens") or 0) for item in items),
                    "cost": round(sum(float(item.get("estimated_cost") or 0) for item in items), 6),
                    "success_rate": round(ok / count * 100, 1) if count else 0.0,
                    "latency_ms": round(sum(float(item.get("duration") or 0) for item in items) / count * 1000, 1) if count else 0.0,
                    "evidence": "observed",
                }

            by_model: dict[str, list[dict]] = {}
            by_provider: dict[str, list[dict]] = {}
            by_key: dict[str, list[dict]] = {}
            for event in events:
                by_model.setdefault(str(event.get("model") or "unknown"), []).append(event)
                by_provider.setdefault(str(event.get("provider") or "unknown"), []).append(event)
                if event.get("key_id"):
                    by_key.setdefault(_display_key(str(event["key_id"])), []).append(event)

            return {
                "request_count": len(events),
                "error_count": len(errors),
                "fallback_count": sum(1 for e in events if e.get("fallback_from")),
                "total_input_tokens": sum(int(e.get("input_tokens") or 0) for e in events),
                "total_output_tokens": sum(int(e.get("output_tokens") or 0) for e in events),
                "total_tokens": sum(int(e.get("total_tokens") or 0) for e in events),
                "estimated_cost": round(sum(float(e.get("estimated_cost") or 0.0) for e in events), 6),
                "by_model": {name: aggregate(items) for name, items in by_model.items()},
                "by_provider": {name: aggregate(items) for name, items in by_provider.items()},
                "by_key": {name: aggregate(items) for name, items in by_key.items()},
                "global_pool": aggregate(events),
                "last_model": last.get("model") if last else None,
                "last_key_id": _display_key(last.get("key_id")) if last else None,
                "last_duration": last.get("duration") if last else None,
                "last_success_at": successes[-1].get("ts") if successes else None,
                "last_error": errors[-1].get("error") if errors else None,
            }


_tracker: UsageTracker | None = None
_tracker_lock = threading.Lock()


def get_usage_tracker() -> UsageTracker:
    global _tracker
    with _tracker_lock:
        if _tracker is None or (_tracker.log_path and _tracker.log_path != default_usage_log_path()):
            _tracker = UsageTracker(log_path=default_usage_log_path())
        return _tracker


def reset_usage_tracker() -> None:
    global _tracker
    with _tracker_lock:
        _tracker = None
