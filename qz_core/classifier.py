from __future__ import annotations

import os
import sys
from openai import OpenAI
from qz_core.client import get_client

# ============================================================
# PHASE 2: Automatic model routing by complexity
# ============================================================

# Simple task -> inexpensive/fast model. Complex task -> more capable model.
# Escalates to "reasoner" after three consecutive verification failures.
COMPLEXITY_MODEL_MAP = {
    "simple": "groq-fast",
    "complex": "coder-strong",
}

_COMPLEX_KEYWORDS = [
    "refactor", "architecture", "redesign", "migrate", "migration",
    "optimize", "optimization", "security", "concurrency", "concurrent",
    "distributed", "scalability", "database schema", "multi-file",
    "rewrite", "performance", "race condition", "deadlock", "async",
]


def classify_task_complexity(task: str, client: OpenAI | None = None) -> str:
    """Classify a task as simple or complex.
    First attempt a short, inexpensive groq-fast LLM call (it is more accurate
    because it understands context). If the call fails (proxy down,
    model_not_found, and so on), fall back to a keyword/length heuristic
    so routing never crashes."""
    c = get_client(client)
    try:
        resp = c.chat.completions.create(
            model="classify",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a task-complexity classifier. Read the user's coding task "
                        "and answer with ONLY one word: 'simple' or 'complex'.\n"
                        "'simple' = small, single-file, straightforward work "
                        "(variable rename, small bug fix, adding one function, "
                        "writing a small script).\n"
                        "'complex' = multi-file changes, architecture/design decisions, "
                        "tricky business logic, refactoring, or anything that needs deep "
                        "reasoning.\n"
                        "Reply with only 'simple' or 'complex', nothing else."
                    ),
                },
                {"role": "user", "content": task},
            ],
            temperature=0,
            max_tokens=5,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        if "complex" in answer:
            return "complex"
        if "simple" in answer:
            return "simple"
        # Unexpected or empty response: fall back to the heuristic below.
    except Exception as e:
        print(f"\033[90m[classify] groq-fast classification failed ({e}); using the heuristic\033[0m")

    # --- Heuristic fallback ---
    task_lower = task.lower()
    if len(task) > 300 or any(kw in task_lower for kw in _COMPLEX_KEYWORDS):
        return "complex"
    return "simple"


# ============================================================
# PHASE 11: Task modes, plan approval, and clarification
# ============================================================
#
# Three task modes decide how much process a task gets before implementation:
#   - quick    : trivial, single-file work. No visible plan/architecture call
#                is made at all — straight to the executor.
#   - standard : a short internal plan/architecture is built and shown, but
#                execution proceeds without pausing unless the user's
#                approval setting is "always".
#   - complex  : plan/architecture are shown, ambiguity is checked first, and
#                (by default) the user must approve before implementation.
#
# QAZTERION_PLAN_APPROVAL controls when an approval pause happens:
#   "always"   -> ask before implementation for standard AND complex tasks.
#   "complex"  -> ask only for complex tasks (recommended default).
#   "auto"     -> the agent decides (currently: same as "complex").
#   "never"    -> never ask; implement immediately regardless of mode.
TASK_MODES = ("quick", "standard", "complex")
_VALID_APPROVAL_SETTINGS = {"always", "complex", "auto", "never"}

_raw_approval_setting = os.environ.get("QAZTERION_PLAN_APPROVAL", "complex").strip().lower()
PLAN_APPROVAL_SETTING = _raw_approval_setting if _raw_approval_setting in _VALID_APPROVAL_SETTINGS else "complex"

# Set by main() from --mode=quick|standard|complex to bypass classification
# entirely (useful for scripting/testing a specific mode deterministically).
FORCED_TASK_MODE: str | None = None

_QUICK_KEYWORDS = [
    "basic", "simple", "quick", "hello world", "one function", "single file",
    "small script", "tiny", "trivial", "rename", "typo",
]


def _heuristic_task_mode(task: str) -> str:
    """Deterministic fallback used when the 'classify' alias is unavailable."""
    task_lower = task.lower()
    if len(task) > 300 or any(kw in task_lower for kw in _COMPLEX_KEYWORDS):
        return "complex"
    if len(task) <= 80 and any(kw in task_lower for kw in _QUICK_KEYWORDS):
        return "quick"
    if len(task) <= 40:
        return "quick"
    return "standard"


def classify_task_mode(task: str, client: OpenAI | None = None) -> str:
    """Classify a task as quick / standard / complex for the approval workflow.

    This is a separate concern from classify_task_complexity (Phase 2), which
    only picks the executor model. A short, inexpensive 'classify' call is
    tried first; any failure falls back to the deterministic heuristic so
    mode selection never crashes or blocks a task.
    """
    agent_mod = sys.modules.get("qz_agent")
    forced = getattr(agent_mod, "FORCED_TASK_MODE", None) if agent_mod else None
    if forced is None:
        forced = FORCED_TASK_MODE
    if forced in TASK_MODES:
        return forced

    c = get_client(client)
    try:
        resp = c.chat.completions.create(
            model="classify",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify this coding task into exactly one mode: quick, standard, or complex.\n"
                        "quick = trivial, single-file, no design decisions "
                        "(e.g. 'create a basic calculator', 'fix a typo').\n"
                        "standard = a well-scoped feature or fix that benefits from a short plan but has "
                        "no major design decisions (e.g. 'add endpoint validation').\n"
                        "complex = touches multiple files/systems, changes architecture or data, or has "
                        "security/migration implications (e.g. 'refactor authentication and migrate schema').\n"
                        "Reply with only one word: quick, standard, or complex."
                    ),
                },
                {"role": "user", "content": task},
            ],
            temperature=0,
            max_tokens=5,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        for mode in TASK_MODES:
            if mode in answer:
                return mode
    except Exception as error:
        print(f"\033[90m[mode] classification failed ({error}); using the heuristic\033[0m")
    return _heuristic_task_mode(task)
