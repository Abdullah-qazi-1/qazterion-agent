"""Single entry point for agent tool calls."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from qz_security.command_risk import RiskLevel, classify_command
from qz_security.policy import Decision, decide
from qz_security.redaction import redact
from qz_security.secret_scanner import scan
from qz_security.workspace_guard import WorkspacePathError, resolve_workspace_path

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


def _guard_paths(args: dict[str, Any], workspace: str) -> None:
    for key in _PATH_ARG_KEYS:
        if key in args and args[key] is not None:
            resolve_workspace_path(str(args[key]), workspace)


def execute(tool_name: str, args: dict | None = None) -> str:
    payload = dict(args or {})
    handler = _handlers.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}"

    workspace = _workspace_getter()
    reasons: list[str] = []
    risk = RiskLevel.LOW
    path_ok = True

    if tool_name == "run_command":
        risk, reasons = classify_command(str(payload.get("command", "")))
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
            # Headless Phase 1: MEDIUM (ASK) is allowed so pip/npm/git clone keep working.
            # HIGH and FORBIDDEN are DENY from policy and never reach this branch.
            decision = Decision.ALLOW

    if decision is Decision.DENY:
        detail = "; ".join(reasons) or "blocked by policy"
        return redact(f"Denied by security policy ({risk.value}): {detail}")

    result = handler(**payload)
    text = result if isinstance(result, str) else str(result)
    findings = scan(text)
    return redact(text, findings)
