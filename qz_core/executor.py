from __future__ import annotations

import json
import re
import sys
import os
from dataclasses import dataclass
from openai import OpenAI

from qz_core.client import accepts_task_id, get_client
# Re-exported for qz_core.dag_executor, which looks these up at call time.
from qz_core.classifier import classify_task_complexity, COMPLEXITY_MODEL_MAP  # noqa: F401
from qz_environment import detect_project_environment
import qz_tools
from qz_tools import run_command
from qz_usage_tracker import UsageTracker, get_usage_tracker
from qz_security.redaction import redact
from qz_validation import ValidationPipeline  # noqa: F401  (used via executor_mod)

# The executor asks for roles; the provider catalog decides which provider,
# model and key serve each role (see qz_providers/default_providers.yaml).
EXECUTOR_ROLE = "coder"
ESCALATION_ROLE = "reasoner"
SUMMARY_ROLE = "fast"


def request_completion(*, model: str, messages: list[dict], tools=None, temperature: float = 0.2,
                       max_tokens: int | None = None, fallbacks: tuple[str, ...] | None = None,
                       usage_tracker: UsageTracker | None = None, client: OpenAI | None = None,
                       task_id: str | None = None):
    """Return ``(response, role_used)`` for a chat request.

    ``model`` is a role (or ``provider/model``). Key rotation and provider/model
    failover inside a role are handled by the gateway; ``fallbacks`` lists extra
    roles to try, in order, only if every model of the requested role failed.
    """
    tracker = usage_tracker or get_usage_tracker()
    if tracker is not None and task_id:
        admitted, block_reason = tracker.admit_request(task_id)
        if not admitted:
            raise RuntimeError(f"Request blocked by task budget: {block_reason}")

    c = get_client(client)
    roles = list(dict.fromkeys([model, *(fallbacks or ())]))
    failures: list[str] = []
    for role in roles:
        kwargs = {"model": role, "messages": messages, "temperature": temperature}
        if tools is not None:
            kwargs["tools"] = tools
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if accepts_task_id(c):
            kwargs["task_id"] = task_id
        try:
            response = c.chat.completions.create(**kwargs)
            if not getattr(response, "choices", None):
                raise RuntimeError("provider returned no choices")
            message = response.choices[0].message
            if not (getattr(message, "content", None) or getattr(message, "tool_calls", None)):
                raise RuntimeError("provider returned an empty assistant message")
        except Exception as error:
            failures.append(f"{role}: {redact(str(error))}")
            continue

        if tracker is not None and task_id:
            _ok, budget_msg, is_warn = tracker.check_task_budget(task_id)
            if budget_msg:
                label = "budget warning" if is_warn else "budget exceeded"
                print(f"\033[93m[{label}]\033[0m {budget_msg}")
        if role != model:
            print(f"\033[90m[model routing] role fallback: {model} -> {role}\033[0m")
        return response, role
    raise RuntimeError("; ".join(failures) or f"No response for '{model}'")


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
    _ws = workspace or (getattr(agent_mod, "WORKSPACE", None) if agent_mod else None) or qz_tools.current_workspace()

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
            model=SUMMARY_ROLE,
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

    from qz_security.injection_guard import wrap_untrusted_content
    wrapped_summary = wrap_untrusted_content(summary, label="CONVERSATION_HISTORY_SUMMARY")
    condensed = [
        messages[0],
        {"role": "user", "content": f"[Earlier executor progress summary (untrusted state)]:\n{wrapped_summary}"},
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

{environment}"""

_WINDOWS_ENVIRONMENT = """Environment: run_command runs in Windows PowerShell, not bash.
- Heredoc syntax (`<< 'EOF'`, `<<'PY'` etc.) is INVALID in PowerShell — never use it.
- For a small inline Python test, use `python -c "..."`.
- For a multi-line test, first create a small temp `.py` file with write_file, then run it
  with `python temp_file.py`, then delete it once you're done.
- Chain commands with `;` or `&&`; avoid bash-only syntax such as `||` or backticks."""

_POSIX_ENVIRONMENT = """Environment: run_command runs in /bin/sh.
- For a small inline Python test, use `python -c "..."`; for longer checks write a temp file.
- Chain commands with `;` or `&&`."""

_TEST_GUIDANCE = """
- Always run tests with `python -m unittest discover -s tests` or `python -m pytest`,
  never run a test file directly with `python path/to/test_file.py` — that can cause
  package-relative imports (like `from src.utils import X`) to fail with
  `ModuleNotFoundError`, because direct script-run mode doesn't put the project root on
  sys.path correctly."""

SYSTEM_PROMPT = SYSTEM_PROMPT.format(
    environment=(_WINDOWS_ENVIRONMENT if os.name == "nt" else _POSIX_ENVIRONMENT) + _TEST_GUIDANCE
)


def run_executor(task: str, plan: str, architecture: str, max_iterations: int = 20,
                 test_baseline: TestBaseline | None = None, task_id: str | None = None,
                 resume_from_subtask_id: str | None = None):
    from qz_core.dag_executor import HardDAGExecutor

    if not task_id:
        try:
            from qz_tasks.task_manager import create_task
            task_id = create_task(task, qz_tools.current_workspace())
        except Exception:
            import uuid
            task_id = str(uuid.uuid4())

    executor = HardDAGExecutor(workspace=qz_tools.current_workspace(), max_node_iterations=max_iterations)
    result = executor.execute_dag(
        task_id=task_id,
        task_text=task,
        plan_text=plan,
        architecture=architecture,
        test_baseline=test_baseline,
        resume_from_subtask_id=resume_from_subtask_id,
    )
    return result.summary
