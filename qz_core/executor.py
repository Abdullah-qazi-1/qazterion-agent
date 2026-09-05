from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from openai import OpenAI

from qz_core.client import get_client
from qz_core.classifier import classify_task_complexity, COMPLEXITY_MODEL_MAP
from qz_core.common import _persist_task
from qz_core.git_ops import generate_commit_message, _commit_hash_from_result
from qz_core.reviewer import self_review
from qz_environment import detect_project_environment
import qz_tools
from qz_tools import TOOL_SCHEMAS, TOOL_FUNCTIONS, WORKSPACE, commit_changes, run_command
from qz_usage_tracker import UsageTracker, default_usage_log_path
from qz_tasks.models import TaskStatus
from qz_pool import get_pool
from qz_router import get_router, select_route
from qz_security.redaction import redact
from qz_validation import ValidationPipeline, ValidationReport, CheckStatus

# Retry a provider briefly, then let LiteLLM route the same request through an
# alternate alias.  The complete message history is preserved by the caller.
EXECUTOR_FALLBACKS = {
    "coder-strong": ("coder-strong", "groq-fast", "coder-backup", "reasoner"),
    "groq-fast": ("groq-fast", "coder-backup", "reasoner"),
    "reasoner": ("reasoner", "groq-fast", "coder-backup"),
}
MODEL_REQUEST_ATTEMPTS = 2
USAGE_TRACKER = UsageTracker(log_path=default_usage_log_path())


def _provider_failure_kind(error: Exception) -> str | None:
    """Classify provider errors conservatively for temporary fallback routing."""
    message = str(error).lower()
    if any(marker in message for marker in ("quota", "insufficient credits", "insufficient balance", "billing")):
        return "quota_exhausted"
    if "429" in message or "rate limit" in message or "ratelimit" in message:
        return "rate_limited"
    return None


def request_completion(*, model: str, messages: list[dict], tools=None, temperature: float = 0.2,
                       max_tokens: int | None = None, fallbacks: tuple[str, ...] | None = None,
                       usage_tracker: UsageTracker | None = None, client: OpenAI | None = None,
                       task_id: str | None = None):
    """Return a usable model response after bounded retry and smart route failover.

    An empty assistant message is treated as a failed response because it cannot
    advance a tool-driven task. No tool is executed until a response is returned.
    """
    c = get_client(client)
    tracker = usage_tracker or USAGE_TRACKER
    pool = get_pool()
    router = get_router()

    candidate_aliases = list(fallbacks) if fallbacks is not None else [model]

    # Filter out aliases/keys flagged in usage_tracker unless all are flagged
    tracker_excluded: set[str] = set()
    if tracker is not None:
        for alias in candidate_aliases:
            if not tracker.is_key_eligible(alias):
                tracker_excluded.add(alias)
            for k in pool.registry.get_keys_for_alias(alias):
                if not tracker.is_key_eligible(k.id):
                    tracker_excluded.add(k.id)

    # Exclude flagged only if there is at least one unflagged option remaining
    excluded = set(tracker_excluded) if len(tracker_excluded) < len(candidate_aliases) else set()

    # Rank candidate routes using SmartRouter live health & concurrency telemetry
    ranked_routes = router.rank_routes(
        model,
        exclude=excluded,
        fallbacks=tuple(candidate_aliases),
        task_id=task_id,
    )

    failures = []
    for candidate, candidate_key, _ in ranked_routes:
        for attempt in range(MODEL_REQUEST_ATTEMPTS):
            started = time.monotonic()
            try:
                kwargs = {"model": candidate, "messages": messages, "temperature": temperature}
                if tools is not None:
                    kwargs["tools"] = tools
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                response = c.chat.completions.create(**kwargs)
                if not response.choices:
                    raise RuntimeError("provider returned no choices")
                message = response.choices[0].message
                if not (getattr(message, "content", None) or getattr(message, "tool_calls", None)):
                    raise RuntimeError("provider returned an empty assistant message")
                duration = time.monotonic() - started
                usage_obj = getattr(response, "usage", None)
                in_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0) if usage_obj else 0
                out_tokens = int(getattr(usage_obj, "completion_tokens", 0) or 0) if usage_obj else 0
                tot_tokens = int(getattr(usage_obj, "total_tokens", 0) or 0) if usage_obj else 0
                is_estimated = False

                if tot_tokens == 0:
                    try:
                        from qz_context import estimate_tokens
                        in_tokens = estimate_tokens(json.dumps(messages, default=str))
                        out_text = str(getattr(message, "content", "") or "")
                        if getattr(message, "tool_calls", None):
                            out_text += str(message.tool_calls)
                        out_tokens = estimate_tokens(out_text)
                        tot_tokens = in_tokens + out_tokens
                        is_estimated = True
                    except Exception:
                        pass

                prov_id = None
                try:
                    from qz_providers.model_registry import get_model_registry
                    model_reg = get_model_registry()
                    meta = model_reg.get_model_by_alias(candidate)
                    if meta:
                        prov_id = meta.provider
                        model_reg.record_model_success(meta.provider, meta.model_id, task_id=task_id)
                except Exception:
                    pass

                tracker.record_request(
                    model=candidate,
                    provider=prov_id,
                    key_id=candidate_key,
                    task_id=task_id,
                    duration=duration,
                    success=True,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    total_tokens=tot_tokens,
                    is_estimated=is_estimated,
                    fallback_from=model if candidate != model else None,
                    task_type="coding",
                    retry_count=attempt,
                )
                tracker.clear_flag(candidate)
                if candidate_key:
                    tracker.clear_flag(candidate_key)
                try:
                    pool.health_manager.record_success(candidate_key, latency_ms=duration * 1000.0)
                except Exception:
                    pass

                # Check budget
                budget_ok, budget_msg, is_warn = tracker.check_task_budget(task_id)
                if budget_msg:
                    if is_warn:
                        print(f"\033[93m[budget warning]\033[0m {budget_msg}")
                    else:
                        print(f"\033[91m[budget exceeded]\033[0m {budget_msg}")

                if candidate != model:
                    print(f"\033[90m[model routing] failover: {model} -> {candidate} (key: {candidate_key})\033[0m")
                return response, candidate
            except Exception as error:
                duration = time.monotonic() - started
                tracker.record_request(
                    model=candidate,
                    key_id=candidate_key,
                    duration=duration,
                    success=False,
                    error=str(error),
                    fallback_from=model if candidate != model else None,
                    task_type="coding",
                    retry_count=attempt,
                )
                err_type = "other"
                try:
                    err_type = pool.health_manager.classify_error(error)
                    pool.health_manager.record_failure(
                        candidate_key,
                        error_type=err_type,
                        latency_ms=duration * 1000.0,
                        error_message=str(error),
                    )
                except Exception:
                    pass
                try:
                    from qz_providers.model_registry import get_model_registry
                    model_reg = get_model_registry()
                    meta = model_reg.get_model_by_alias(candidate)
                    if meta:
                        model_reg.record_model_failure(meta.provider, meta.model_id, error=error, task_id=task_id)
                except Exception:
                    pass
                failure_kind = _provider_failure_kind(error)
                if failure_kind == "quota_exhausted":
                    tracker.mark_quota_exhausted(candidate)
                    if candidate_key:
                        tracker.mark_quota_exhausted(candidate_key)
                elif failure_kind == "rate_limited":
                    tracker.mark_rate_limited(candidate)
                    if candidate_key:
                        tracker.mark_rate_limited(candidate_key)
                failures.append(f"{candidate} ({candidate_key}) attempt {attempt + 1}: {error}")
                if err_type == "auth_error":
                    # Permanent auth error - do not retry with the same key
                    break
                if attempt + 1 < MODEL_REQUEST_ATTEMPTS:
                    time.sleep(attempt + 1)
    raise RuntimeError("; ".join(failures))


@dataclass(frozen=True)
class TestBaseline:
    __test__ = False
    command: str
    result: str
    signature: str


def _test_failure_signature(result: str) -> str:
    """Normalize a test failure enough to recognize an unchanged rerun."""
    lines = []
    for line in str(result).splitlines():
        stripped = line.strip()
        if not stripped or stripped in {"STDOUT:", "STDERR:"} or stripped.startswith("exit_code="):
            continue
        stripped = re.sub(r"Ran (\d+) tests? in [\d.]+s", r"Ran \1 tests", stripped)
        lines.append(stripped)
    return "\n".join(lines)


def capture_pre_existing_test_failures(workspace: str | None = None) -> TestBaseline | None:
    """Record an existing project's failing default test command before edits."""
    agent_mod = sys.modules.get("qz_agent")
    _detect = getattr(agent_mod, "detect_project_environment", detect_project_environment) if agent_mod else detect_project_environment
    _run_cmd = getattr(agent_mod, "run_command", run_command) if agent_mod else run_command
    _ws = workspace or (getattr(agent_mod, "WORKSPACE", None) if agent_mod else None) or qz_tools.WORKSPACE

    environment = _detect(_ws)
    if not environment.test_command:
        return None
    result = _run_cmd(environment.test_command, timeout=180)
    if re.search(r"(?:^|\n)exit_code=0(?:\n|$)", result):
        return None
    return TestBaseline(environment.test_command, result, _test_failure_signature(result))


FAILURE_MARKERS = [
    "traceback", "exception", "error:", "could not", "not found",
    "unknown tool",
]

# Phase 5 keeps the prompt bounded during long tool-driven tasks. The system
# instructions and the newest complete exchange are always retained verbatim.
MAX_HISTORY_MESSAGES = 10
RECENT_HISTORY_MESSAGES = 3

_TEST_REQUEST_PATTERN = re.compile(r"\b(?:add|write|create|update|include)\s+(?:(?:focused|unit|new|relevant)\s+)?(?:test|tests|testing|pytest|unittest|coverage)\b", re.IGNORECASE)


def _task_requires_test_changes(task: str) -> bool:
    """Return whether the user explicitly requested tests as a deliverable."""
    return bool(_TEST_REQUEST_PATTERN.search(task))


def _is_test_file_path(path: object) -> bool:
    """Recognize conventional test-file paths without relying on the model's prose."""
    normalized = str(path).replace("\\", "/").lower()
    filename = normalized.rsplit("/", 1)[-1]
    return "/tests/" in f"/{normalized}" or filename.startswith("test_") or filename.endswith("_test.py")


def _tool_result_failed(fname: str, result: str) -> bool:
    """Detect failure only from the run_command verification exit code. Successful
    housekeeping tools such as read_file, apply_patch, and write_file must not
    incorrectly reset the escalation counter
    ."""
    if fname != "run_command":
        return False
    result_lower = str(result).lower()
    m = re.search(r"exit_code=(-?\d+)", result_lower)
    if m:
        return int(m.group(1)) != 0
    return False


# ============================================================
# PHASE 5: Rolling conversation summary
# ============================================================

def _recent_exchange_start(messages: list[dict]) -> int:
    """Choose a safe boundary for preserved recent history.

    A tool response is only valid after the assistant's matching tool-call
    message. If the last three messages start in the middle of a tool batch,
    preserve the preceding assistant message and every response in that batch.
    """
    start = max(1, len(messages) - RECENT_HISTORY_MESSAGES)
    while start > 1 and messages[start].get("role") == "tool":
        start -= 1
    return start


def _history_for_summary(messages: list[dict]) -> str:
    """Render old chat history into compact, provider-safe summary input."""
    rendered = []
    for message in messages:
        role = message.get("role", "unknown")
        content = str(message.get("content") or "")
        tool_calls = message.get("tool_calls")
        if tool_calls:
            content = f"{content}\nTool calls: {json.dumps(tool_calls, ensure_ascii=False, default=str)}"
        rendered.append(f"[{role}] {content[:6000]}")
    return "\n\n".join(rendered)


def roll_conversation_summary(messages: list[dict], client: OpenAI | None = None) -> tuple[list[dict], bool]:
    """Summarize old executor messages once the history grows beyond the limit.

    Returns the original history unchanged when it is short or the inexpensive
    summarizer is unavailable, so a transient model/proxy failure never stops
    code execution.
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages, False

    recent_start = _recent_exchange_start(messages)
    old_messages = messages[1:recent_start]  # Keep the original system prompt.
    if not old_messages:
        return messages, False

    c = get_client(client)
    try:
        response = c.chat.completions.create(
            model="groq-fast",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this coding-agent history for the next executor turn. "
                        "Keep completed work, changed files, decisions, test commands/results, "
                        "errors still unresolved, and the exact next action. Be concise and factual."
                    ),
                },
                {"role": "user", "content": _history_for_summary(old_messages)},
            ],
            temperature=0,
            max_tokens=700,
        )
        summary = (response.choices[0].message.content or "").strip()
    except Exception as error:
        print(f"\033[90m[summary] could not summarize history ({error}); retaining full history\033[0m")
        return messages, False

    if not summary:
        return messages, False

    condensed = [
        messages[0],
        {"role": "system", "content": f"Earlier executor progress summary:\n{summary}"},
        *messages[recent_start:],
    ]
    return condensed, True


SYSTEM_PROMPT = """You are a senior software engineer who writes clean, working, tested code.

Always respond in English only — never switch to Hindi, Roman Urdu, Devanagari script, or any
other language, regardless of what language the task is written in.

Rules:
- For an existing project, use search_index first to find relevant source files before reading code.
- Choose the amount of context deliberately: for a localized change in a large file, use read_relevant_chunks first. Read the whole relevant file with read_file when the task needs surrounding control flow, interfaces, configuration, cross-cutting behavior, or the focused chunks are not enough. Do not read unrelated files.
- Use write_file to create a NEW file. For a small/targeted change to an EXISTING file
  (like fixing a function, adding/removing a line), use apply_patch instead — don't rewrite
  the whole file, just send the diff. Only use write_file when the file is brand new or
  needs so much change that rewriting it entirely is simpler.
- Before using apply_patch, always read the current content with read_file first, so the
  diff's line numbers and context are correct. The diff MUST begin with a real hunk header
  containing line numbers, such as `@@ -1,3 +1,7 @@`, followed by context/`-`/`+` lines.
  NEVER use the `*** Begin Patch` / `*** Update File` / bare `@@` format — that is a
  different tool's convention and is always rejected here. A full-file replacement is not
  a valid apply_patch argument. If apply_patch reports no valid hunk or a context mismatch,
  read the file again and retry with a real numbered hunk header; if it fails twice in a
  row, stop retrying apply_patch and use write_file with the full corrected file content
  instead.
- After every code change, verify it yourself with run_command if possible.
- After a successful verification command, Qazterion automatically commits the files changed in that verified step. Do not use rollback_last_change unless the user explicitly asks to undo the latest agent commit.
- If run_command keeps giving the same error after apply_patch/write_file, don't immediately
  retry the exact same diff — read the whole file again with read_file, find the root cause
  (like a missing import, undefined name), and fix that specific problem instead of just
  repeating the previous change.
- Always verify with an assertion/expected-output check (e.g. `assert result == expected`) —
  don't treat "the script didn't crash" (exit_code=0) as "the test passed". If a strict/
  assertion-based test command is failing, don't abandon it for a weaker command (like just
  running the file with no assertion) just to get a green exit code — fix the actual problem
  and re-run the strict test.
- If an error occurs, read it and fix it yourself — don't ask the user back unless you're
  genuinely stuck.
- When the work is complete and tests pass, give a short summary of what you did.

Environment: run_command runs in Windows PowerShell, not bash.
- Heredoc syntax (`<< 'EOF'`, `<<'PY'` etc.) is INVALID in PowerShell — never use it.
- For a small inline Python test, use `python -c "..."`.
- For a multi-line test, first create a small temp `.py` file with write_file, then run it
  with `python temp_file.py`, then delete it once you're done.
- For command chaining, use PowerShell syntax (`;` or `&&`); avoid bash-specific syntax
  (`&&` also works, but avoid `||`, backticks, etc.).
- Always run tests with `python -m unittest discover -s tests` or `python -m pytest`,
  never run a test file directly with `python path/to/test_file.py` — that can cause
  package-relative imports (like `from src.utils import X`) to fail with
  `ModuleNotFoundError`, because direct script-run mode doesn't put the project root on
  sys.path correctly."""


def run_executor(task: str, plan: str, architecture: str, max_iterations: int = 20,
                 test_baseline: TestBaseline | None = None, task_id: str | None = None,
                 resume_from_subtask_id: str | None = None):
    from qz_core.dag_executor import HardDAGExecutor

    if not task_id:
        try:
            from qz_tasks.task_manager import create_task
            task_id = create_task(task, qz_tools.WORKSPACE)
        except Exception:
            import uuid
            task_id = str(uuid.uuid4())

    executor = HardDAGExecutor(workspace=qz_tools.WORKSPACE, max_node_iterations=max_iterations)
    result = executor.execute_dag(
        task_id=task_id,
        task_text=task,
        plan_text=plan,
        architecture=architecture,
        test_baseline=test_baseline,
        resume_from_subtask_id=resume_from_subtask_id,
    )
    return result.summary
