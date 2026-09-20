"""Single entry point for agent tool calls."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from qz_security.command_risk import RiskLevel, classify_command, invokes_unrestricted_interpreter
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
AskHandler = Callable[[str, dict, RiskLevel, list[str]], bool]

_handlers: dict[str, Handler] = {}
_workspace_getter: Callable[[], str] = lambda: os.getcwd()
_ask_handler: AskHandler | None = None

# Matches every user-supplied path argument on TOOL_FUNCTIONS today:
# list_files(directory, path), read_file(path), write_file(path, ...),
# make_directory(path), apply_patch(path, ...). No tool uses file_path/target_path.
_PATH_ARG_KEYS = ("path", "directory")


def configure(
    *,
    handlers: dict[str, Handler] | None = None,
    workspace_getter: Callable[[], str] | None = None,
    ask_handler: AskHandler | None = None,
) -> None:
    global _handlers, _workspace_getter, _ask_handler
    if handlers is not None:
        _handlers = dict(handlers)
    if workspace_getter is not None:
        _workspace_getter = workspace_getter
    _ask_handler = ask_handler


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
    """Detect and block path traversal and out-of-bounds references in shell commands."""
    cmd_str = command or ""
    # Check directory traversal commands
    if re.search(r"\b(?:cd|chdir|pushd|Set-Location|sl)\s+[\"']?\.\.", cmd_str, re.IGNORECASE):
        raise WorkspacePathError("Directory traversal escape detected in command")

    tokens = re.findall(
        r"(?:\"[A-Za-z]:[\\/][^\"]+\"|'([A-Za-z]:[\\/][^']+)'|[A-Za-z]:[\\/][^\s'\"]+|/(?:etc|usr|var|root|home|windows|system32|bin|sbin|dev|proc|sys)[^\s'\"]*|[\"']?\.\.[\\/][^\s'\"]*|\b\.\.[\\/][^\s'\"]*)",
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

    required = _REQUIRED_TOOL_ARGS.get(tool_name, ())
    for req_field in required:
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
        return redact("Denied by security policy (READ_ONLY): writes and commands are disabled for this task.")

    if tool_name == "run_command":
        cmd_text = str(payload.get("command", ""))
        risk, reasons = classify_command(cmd_text)
        if invokes_unrestricted_interpreter(cmd_text) and risk is RiskLevel.LOW:
            risk = RiskLevel.HIGH
            reasons.append("unrestricted interpreter execution")
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
            if not _ask_handler(tool_name, payload, risk, reasons):
                decision = Decision.DENY
                reasons.append("approval required")
        else:
            # Headless: MEDIUM (ASK) may proceed only into the sandbox. Isolation
            # failure is denied at the sandbox boundary, never by silent host exec.
            decision = Decision.ALLOW

    if decision is Decision.DENY:
        detail = "; ".join(reasons) or "blocked by policy"
        return redact(f"Denied by security policy ({risk.value}): {detail}")

    result = handler(**payload)
    text = result if isinstance(result, str) else str(result)
    findings = scan(text)
    return redact(text, findings)
