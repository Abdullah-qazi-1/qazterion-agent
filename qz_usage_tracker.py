"""Phase 13 — comprehensive token, usage, cost tracking and budget controls for Qazterion."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qz_keystore import mask_key

# Cooldown constants
_DEFAULT_COOLDOWN_SECONDS = 60.0
_DEFAULT_QUOTA_COOLDOWN_SECONDS = 6 * 60 * 60.0  # 6 hours

# Reference pricing per 1 million tokens ($) for fallback cost estimation
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # model / prefix -> (input_per_1m, output_per_1m)
    "groq": (0.59, 0.79),
    "gemini": (0.075, 0.30),
    "deepseek": (0.14, 0.28),
    "mistral": (0.20, 0.60),
    "openrouter": (0.50, 1.50),
    "openai": (0.50, 1.50),
    "coder-strong": (0.59, 0.79),
    "groq-fast": (0.59, 0.79),
    "coder-backup": (0.14, 0.28),
    "reasoner": (0.55, 2.19),
}


def calculate_estimated_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    provider: str | None = None,
) -> float:
    """Calculate estimated cost in USD for the given token usage."""
    input_price = None
    output_price = None

    try:
        from qz_providers.model_registry import get_model_registry
        reg = get_model_registry()
        meta = reg.get_model_by_alias(model)
        if meta:
            input_price = meta.pricing_input_per_1m
            output_price = meta.pricing_output_per_1m
    except Exception:
        pass

    if input_price is None or output_price is None:
        model_l = model.lower()
        matched = False
        for k, (inp, out) in DEFAULT_PRICING.items():
            if k in model_l or (provider and k in provider.lower()):
                input_price = inp
                output_price = out
                matched = True
                break
        if not matched:
            input_price, output_price = (0.25, 0.75)

    cost = (input_tokens / 1_000_000.0) * input_price + (output_tokens / 1_000_000.0) * output_price
    return round(cost, 6)


def default_usage_log_path() -> Path:
    """Return the per-user usage log shared by the CLI and desktop backend."""
    override = os.environ.get("QAZTERION_USAGE_LOG_PATH")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Qazterion" / "usage.jsonl"
    return Path.home() / ".qazterion" / "usage.jsonl"


@dataclass
class _Flag:
    reason: str  # "rate_limited" | "quota_exhausted"
    since: float


@dataclass
class TaskBudget:
    max_tokens: int | None = None
    max_cost: float | None = None
    warning_threshold: float = 0.80  # 80%


class BudgetExceededError(RuntimeError):
    """Raised when a task exceeds its configured token or cost budget limit."""


class UsageTracker:
    def __init__(
        self,
        log_path: str | os.PathLike[str] | None = None,
        max_events: int = 1000,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        quota_cooldown_seconds: float = _DEFAULT_QUOTA_COOLDOWN_SECONDS,
    ) -> None:
        self.log_path = Path(log_path) if log_path else None
        self._events: deque[dict] = deque(maxlen=max_events)
        self._flags: dict[str, _Flag] = {}
        self._budgets: dict[str, TaskBudget] = {}
        self._cooldown = cooldown_seconds
        self._quota_cooldown = quota_cooldown_seconds
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.log_path = None
            else:
                self._load_existing_events()

    def _load_existing_events(self) -> None:
        """Load prior valid events so a separately started UI can show them."""
        if not self.log_path or not self.log_path.is_file():
            return
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-self._events.maxlen:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self._events.append(event)

    # ---- budget management --------------------------------------------------

    def set_task_budget(
        self,
        task_id: str,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        warning_threshold: float = 0.80,
    ) -> None:
        self._budgets[task_id] = TaskBudget(
            max_tokens=max_tokens,
            max_cost=max_cost,
            warning_threshold=warning_threshold,
        )

    def check_task_budget(self, task_id: str | None) -> tuple[bool, str | None, bool]:
        """Check if task is within budget. Returns (is_ok, message, is_warning_only)."""
        if not task_id or task_id not in self._budgets:
            return True, None, False

        budget = self._budgets[task_id]
        usage = self.get_task_usage(task_id)

        # 1. Check token limits
        if budget.max_tokens is not None and budget.max_tokens > 0:
            if usage["total_tokens"] >= budget.max_tokens:
                msg = f"Task {task_id} exceeded token limit ({usage['total_tokens']:,}/{budget.max_tokens:,})"
                return False, msg, False
            if usage["total_tokens"] >= int(budget.max_tokens * budget.warning_threshold):
                msg = f"Task {task_id} approaching token limit ({usage['total_tokens']:,}/{budget.max_tokens:,})"
                return True, msg, True

        # 2. Check cost limits
        if budget.max_cost is not None and budget.max_cost > 0:
            if usage["estimated_cost"] >= budget.max_cost:
                msg = f"Task {task_id} exceeded cost limit (${usage['estimated_cost']:.4f}/${budget.max_cost:.4f})"
                return False, msg, False
            if usage["estimated_cost"] >= (budget.max_cost * budget.warning_threshold):
                msg = f"Task {task_id} approaching cost limit (${usage['estimated_cost']:.4f}/${budget.max_cost:.4f})"
                return True, msg, True

        return True, None, False

    def get_task_usage(self, task_id: str) -> dict[str, Any]:
        """Aggregate usage metrics for a specific task."""
        task_events = [e for e in self._events if e.get("task_id") == task_id]
        total_in = sum(int(e.get("input_tokens") or 0) for e in task_events)
        total_out = sum(int(e.get("output_tokens") or 0) for e in task_events)
        total_tokens = sum(int(e.get("total_tokens") or 0) for e in task_events)
        total_cost = sum(float(e.get("estimated_cost") or 0.0) for e in task_events)
        total_duration = sum(float(e.get("duration") or 0.0) for e in task_events)
        success_count = sum(1 for e in task_events if e.get("success"))
        error_count = sum(1 for e in task_events if not e.get("success"))
        fallback_count = sum(1 for e in task_events if e.get("fallback_from"))

        models_used = sorted(set(e.get("model") for e in task_events if e.get("model")))
        providers_used = sorted(set(e.get("provider") for e in task_events if e.get("provider")))

        return {
            "task_id": task_id,
            "request_count": len(task_events),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "total_tokens": total_tokens,
            "estimated_cost": round(total_cost, 6),
            "total_duration": round(total_duration, 3),
            "success_count": success_count,
            "error_count": error_count,
            "fallback_count": fallback_count,
            "models_used": models_used,
            "providers_used": providers_used,
        }

    # ---- recording ----------------------------------------------------------

    def record_request(
        self,
        *,
        model: str,
        key_id: str | None = None,
        duration: float,
        success: bool,
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
        if not total_tokens:
            total_tokens = input_tokens + output_tokens

        if estimated_cost is None and total_tokens > 0:
            estimated_cost = calculate_estimated_cost(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider=provider,
            )
        elif estimated_cost is None:
            estimated_cost = 0.0

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
            # Provider values are confirmed only when supplied by a response;
            # observed flags are intentionally labelled rather than guessed.
            "rate_limit": rate_limit or {"status": "unknown"},
        }
        self._events.append(event)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
        if success and key_id:
            self._flags.pop(key_id, None)

    def mark_rate_limited(self, key_id: str) -> None:
        self._flags[key_id] = _Flag(reason="rate_limited", since=time.time())

    def mark_quota_exhausted(self, key_id: str) -> None:
        self._flags[key_id] = _Flag(reason="quota_exhausted", since=time.time())

    def clear_flag(self, key_id: str) -> None:
        self._flags.pop(key_id, None)

    def is_key_eligible(self, key_id: str) -> bool:
        """False while a key is inside its post-failure cooldown window."""
        flag = self._flags.get(key_id)
        if flag is None:
            return True
        cooldown = self._quota_cooldown if flag.reason == "quota_exhausted" else self._cooldown
        return (time.time() - flag.since) >= cooldown

    # ---- reporting ------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        events = list(self._events)
        successes = [event for event in events if event["success"]]
        errors = [event for event in events if not event["success"]]
        fallbacks = [event for event in events if event.get("fallback_from")]
        last = events[-1] if events else None
        last_success = successes[-1] if successes else None

        total_input_tokens = sum(int(e.get("input_tokens") or 0) for e in events)
        total_output_tokens = sum(int(e.get("output_tokens") or 0) for e in events)
        total_tokens = sum(int(e.get("total_tokens") or 0) for e in events)
        total_cost = sum(float(e.get("estimated_cost") or 0.0) for e in events)

        def aggregate(items: list[dict]) -> dict[str, Any]:
            total = len(items)
            ok = sum(1 for item in items if item.get("success"))
            return {"requests": total, "tokens": sum(int(item.get("total_tokens") or 0) for item in items),
                    "cost": round(sum(float(item.get("estimated_cost") or 0) for item in items), 6),
                    "success_rate": round((ok / total) * 100, 1) if total else 0.0,
                    "latency_ms": round((sum(float(item.get("duration") or 0) for item in items) / total) * 1000, 1) if total else 0.0,
                    "evidence": "observed"}

        # Group by model/provider/API key. Key IDs are never returned here;
        # the UI receives their masked form only.
        by_model: dict[str, dict[str, Any]] = {}
        by_provider: dict[str, list[dict]] = {}
        by_key: dict[str, list[dict]] = {}
        for e in events:
            m = e.get("model", "unknown")
            by_model.setdefault(m, []).append(e)
            by_provider.setdefault(str(e.get("provider") or "unknown"), []).append(e)
            if e.get("key_id"):
                by_key.setdefault(mask_key(str(e["key_id"])), []).append(e)

        return {
            "request_count": len(events),
            "error_count": len(errors),
            "fallback_count": len(fallbacks),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost": round(total_cost, 6),
            "by_model": {name: aggregate(items) for name, items in by_model.items()},
            "by_provider": {name: aggregate(items) for name, items in by_provider.items()},
            "by_key": {name: aggregate(items) for name, items in by_key.items()},
            "global_pool": aggregate(events),
            "last_model": last["model"] if last else None,
            "last_key_id": mask_key(last["key_id"]) if last and last.get("key_id") else None,
            "last_duration": last["duration"] if last else None,
            "last_success_at": last_success["ts"] if last_success else None,
            "last_error": errors[-1]["error"] if errors else None,
            "flagged_keys": {
                key_id: {"reason": flag.reason, "since": flag.since, "eligible_again": self.is_key_eligible(key_id)}
                for key_id, flag in self._flags.items()
            },
        }
