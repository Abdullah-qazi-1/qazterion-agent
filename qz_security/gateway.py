"""Single entry point for agent tool calls: argument checks, path containment,
command risk policy, user approval, and secret redaction of results.

Approval contract: an ask handler is ``handler(tool_name, args, risk, reasons)``
and must return ``True`` (or one of the explicit allow words below) to approve.
Anything else - ``False``, ``"deny"``, ``None``, an exception - denies.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from qz_security.command_risk import RiskLevel, classify_command
from qz_security.policy import Decision, decide
from qz_security.redaction import redact
from qz_security.secret_scanner import scan
from qz_security.workspace_guard import WorkspacePathError, resolve_workspace_path

_MUTATING_TOOLS = {
    "write_file",
    "apply_patch",
    "make_directory",
    "run_command",
    "rollback_last_change",
}

Handler = Callable[..., str]
AskHandler = Callable[[str, dict, RiskLevel, list[str]], Any]

_ALLOW_WORDS = {"allow", "allow_once", "always", "always_allow", "approve", "approved", "yes", "y"}

_handlers: dict[str, Handler] = {}
_workspace_getter: Callable[[], str] = lambda: os.getcwd()
_ask_handler: AskHandler | None = None

# Every user-supplied path argument on TOOL_FUNCTIONS today.
_PATH_ARG_KEYS = ("path", "directory")


def configure(
    *,
    handlers: dict[str, Handler] | None = None,
    workspace_getter: Callable[[], str] | None = None,
    ask_handler: AskHandler | None = None,
) -> None:
    """Install tool handlers / workspace getter, and set (or clear) the ask handler."""
    global _handlers, _workspace_getter, _ask_handler
    if handlers is not None:
        _handlers = dict(handlers)
    if workspace_getter is not None:
        _workspace_getter = workspace_getter
    _ask_handler = ask_handler


def get_ask_handler() -> AskHandler | None:
    return _ask_handler


def is_approval(decision: Any) -> bool:
    """Interpret an ask-handler result strictly; only explicit approvals count."""
    if decision is True:
        return True
    if isinstance(decision, str):
        return decision.strip().lower() in _ALLOW_WORDS
    return False


def headless_medium_allowed() -> bool:
    """MEDIUM-risk commands with nobody to ask: denied unless explicitly allowed."""
    return os.environ.get("QAZTERION_HEADLESS_MEDIUM", "deny").strip().lower() in ("allow", "1", "true", "yes")


_REQUIRED_TOOL_ARGS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "apply_patch": ("path", "diff"),
    "make_directory": ("path",),
    "search_index": ("query",),
    "read_relevant_chunks": ("query",),
    "run_command": ("command",),
    "list_files": (),
    "rollback_last_change": (),
}


def _guard_paths(args: dict[str, Any], workspace: str) -> None:
    for key in _PATH_ARG_KEYS:
        if key in args and args[key] is not None:
            resolve_workspace_path(str(args[key]), workspace)


def _guard_command_paths(command: str, workspace: str) -> None:
    """Reject commands that reference paths outside the workspace."""
    cmd_str = command or ""
    if re.search(r"\b(?:cd|chdir|pushd|Set-Location|sl)\s+[\"']?\.\.", cmd_str, re.IGNORECASE):
        raise WorkspacePathError("Directory traversal escape detected in command")

    tokens = re.findall(
        r"(?:\"[A-Za-z]:[\\/][^\"]+\"|'([A-Za-z]:[\\/][^']+)'|[A-Za-z]:[\\/][^\s'\"]+|\\\\[^\s'\"]+|/(?:etc|usr|var|root|home|windows|system32|bin|sbin|dev|proc|sys|Users)[^\s'\"]*|[\"']?\.\.[\\/][^\s'\"]*|\b\.\.[\\/][^\s'\"]*)",
        cmd_str,
        re.IGNORECASE,
    )
    for token in tokens:
        clean_tok = token.strip("\"'")
        if clean_tok:
            try:
                resolve_workspace_path(clean_tok, workspace)
            except WorkspacePathError as err:
                raise WorkspacePathError(f"Path outside workspace in command: {clean_tok}") from err


def execute(tool_name: str, args: dict | None = None) -> str:
    if not isinstance(args, dict):
        return f"TOOL_ARGUMENT_ERROR: Expected arguments as dictionary for tool '{tool_name}', got {type(args).__name__}"

    handler = _handlers.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}"

    for req_field in _REQUIRED_TOOL_ARGS.get(tool_name, ()):
        if req_field not in args or args[req_field] is None:
            return f"TOOL_ARGUMENT_ERROR: Missing required argument '{req_field}' for tool '{tool_name}'"

    payload = dict(args)
    workspace = _workspace_getter()
    reasons: list[str] = []
    risk = RiskLevel.LOW
    path_ok = True

    try:
        from qz_core.common import get_task_context
        ctx = get_task_context()
    except Exception:
        ctx = None
    if ctx is not None and ctx.read_only and tool_name in _MUTATING_TOOLS:
        return "Denied by security policy (READ_ONLY): writes and commands are disabled for this task."

    if tool_name == "run_command":
        cmd_text = str(payload.get("command", ""))
        risk, reasons = classify_command(cmd_text)
        try:
            _guard_command_paths(cmd_text, workspace)
        except WorkspacePathError as e:
            path_ok = False
            reasons.append(str(e))
    try:
        _guard_paths(payload, workspace)
    except WorkspacePathError:
        path_ok = False
        reasons.append("path outside workspace")

    decision = decide(tool_name, payload, risk, path_ok=path_ok)
    if decision is Decision.ASK:
        if _ask_handler is not None:
            try:
                approved = is_approval(_ask_handler(tool_name, payload, risk, list(reasons)))
            except Exception as error:  # a broken prompt must never approve
                approved = False
                reasons.append(f"approval prompt failed: {error}")
            decision = Decision.ALLOW if approved else Decision.DENY
            if not approved:
                reasons.append("not approved by user")
        elif headless_medium_allowed():
            decision = Decision.ALLOW
        else:
            decision = Decision.DENY
            reasons.append(
                "approval required but no interactive approver is attached "
                "(run interactively, or set QAZTERION_HEADLESS_MEDIUM=allow for trusted automation)"
            )

    if decision is Decision.DENY:
        detail = "; ".join(reasons) or "blocked by policy"
        return redact(f"Denied by security policy ({risk.value}): {detail}")

    try:
        result = handler(**payload)
    except TypeError as error:
        return f"TOOL_ARGUMENT_ERROR: {tool_name}: {error}"
    text = result if isinstance(result, str) else str(result)
    return redact(text, scan(text))
