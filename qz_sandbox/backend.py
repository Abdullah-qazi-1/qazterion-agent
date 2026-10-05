"""Host command execution for agent ``run_command`` calls.

Qazterion runs commands directly on the host (no Docker or VM is required or
used). Safety comes from the security gateway that runs *before* this module:
risk classification, workspace path checks and user approval for risky
commands. This module adds the runtime limits: the workspace as working
directory, an environment scrubbed of API keys and secrets, a timeout, and
termination of the whole process tree when the timeout expires.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("qz_sandbox")

ISOLATION_LABEL = "host"

# Helper processes (git, pip, taskkill) never get a console window on Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    error: str | None = None
    isolation: str = ISOLATION_LABEL


def _decode_captured(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _split_command_statements(command: str) -> list[tuple[str, str]]:
    """Split a compound command into top-level ``;``/``&&``/newline statements.

    Separators inside quotes, ``(...)``, ``{...}`` or ``[...]`` are left alone so
    PowerShell script blocks and loops stay intact.
    """
    statements: list[tuple[str, str]] = []
    current: list[str] = []
    in_single = in_double = False
    depth = 0
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "({[":
                depth += 1
            elif ch in ")}]" and depth > 0:
                depth -= 1
            elif depth == 0 and ch == "&" and i + 1 < n and command[i + 1] == "&":
                stmt = "".join(current).strip()
                if stmt:
                    statements.append((stmt, "and"))
                current = []
                i += 2
                continue
            elif depth == 0 and ch in (";", "\n"):
                stmt = "".join(current).strip()
                if stmt:
                    statements.append((stmt, "seq"))
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    remaining = "".join(current).strip()
    if remaining:
        statements.append((remaining, "last"))
    return statements


def _prepare_host_command(command: str, is_windows: bool) -> list[str]:
    """Build argv that runs ``command`` and reports the first failing exit code.

    A later successful statement in a ``;`` chain must not hide an earlier
    failure, while ``&&`` chains still stop at the first failure. On Windows
    this also makes ``&&`` work under Windows PowerShell 5.1.
    """
    statements = _split_command_statements(command)

    if not is_windows:
        if not statements or (len(statements) == 1 and statements[0][1] == "last"):
            return ["/bin/sh", "-c", command]
        parts = ["_qz_ec=0;"]
        for stmt, sep in statements:
            if sep == "and":
                parts.append(
                    f"{{ {stmt}; }}; _qz_last=$?; "
                    'if [ "$_qz_last" -ne 0 ]; then [ "$_qz_ec" -eq 0 ] && _qz_ec=$_qz_last; exit "$_qz_ec"; fi;'
                )
            else:
                parts.append(f'{{ {stmt}; }}; _qz_last=$?; [ "$_qz_last" -ne 0 ] && [ "$_qz_ec" -eq 0 ] && _qz_ec=$_qz_last;')
        parts.append('if [ "$_qz_ec" -ne 0 ]; then exit "$_qz_ec"; fi')
        return ["/bin/sh", "-c", " ".join(parts)]

    if not statements or (len(statements) == 1 and statements[0][1] == "last"):
        raw = statements[0][0] if statements else command
        script = (
            "$ErrorActionPreference = 'Continue'; "
            f"& {{ {raw} }}; "
            "if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
            "if (-not $?) { exit 1 }"
        )
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]

    parts = ["$ErrorActionPreference = 'Continue';", "$script:__qz_ec = 0;"]
    for stmt, sep in statements:
        if sep == "and":
            parts.append(
                f"$global:LASTEXITCODE = $null; & {{ {stmt} }}; "
                "if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { if ($script:__qz_ec -eq 0) { $script:__qz_ec = $LASTEXITCODE }; exit $script:__qz_ec }; "
                "if (-not $?) { if ($script:__qz_ec -eq 0) { $script:__qz_ec = 1 }; exit $script:__qz_ec };"
            )
        else:
            parts.append(
                f"$global:LASTEXITCODE = $null; & {{ {stmt} }}; "
                "if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0 -and $script:__qz_ec -eq 0) { $script:__qz_ec = $LASTEXITCODE }; "
                "if ((-not $?) -and $script:__qz_ec -eq 0) { $script:__qz_ec = 1 };"
            )
    parts.append("if ($script:__qz_ec -ne 0) { exit $script:__qz_ec }")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", " ".join(parts)]


_SECRET_MARKERS = (
    "_KEY", "_TOKEN", "_SECRET", "_AUTH", "_PASSWORD", "_PASS", "_CREDENTIAL",
    "API_KEY", "APIKEY", "ACCESS_KEY", "PRIVATE_KEY",
)
_SECRET_EXACT = {"QAZTERION_KEYSTORE_PATH"}


def sanitize_subprocess_env(workspace: str, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Copy the environment without API keys/secrets and with the workspace on PYTHONPATH."""
    source = dict(base_env if base_env is not None else os.environ)
    safe = {
        name: value
        for name, value in source.items()
        if name.upper() not in _SECRET_EXACT and not any(marker in name.upper() for marker in _SECRET_MARKERS)
    }
    current = safe.get("PYTHONPATH", "")
    safe["PYTHONPATH"] = f"{workspace}{os.pathsep}{current}" if current else str(workspace)
    safe.setdefault("PYTHONIOENCODING", "utf-8")
    return safe


def _kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                           creationflags=NO_WINDOW, timeout=30)
        except Exception:
            pass
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


class HostBackend:
    """Run commands as a child process of Qazterion with a scrubbed environment."""

    def run(self, command: str, workspace: str, timeout: int) -> ExecutionResult:
        is_windows = os.name == "nt"
        workspace_path = Path(workspace).resolve()
        if not workspace_path.is_dir():
            return ExecutionResult("", "", -1, False, error=f"Workspace is not a directory: {workspace_path}")
        args = _prepare_host_command(command, is_windows=is_windows)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(workspace_path),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": sanitize_subprocess_env(str(workspace_path)),
        }
        if is_windows:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["start_new_session"] = True
        logger.info("run_command host workspace=%s timeout=%s", workspace_path, timeout)
        try:
            proc = subprocess.Popen(args, **popen_kwargs)
        except OSError as exc:
            return ExecutionResult("", "", -1, False, error=str(exc))
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            stdout, stderr = proc.communicate()
            return ExecutionResult(_decode_captured(stdout), _decode_captured(stderr), -1, True, error="timeout")
        return ExecutionResult(stdout or "", stderr or "", proc.returncode, False)


# Backwards-compatible names.
UnsandboxedHostBackend = HostBackend
RestrictedHostBackend = HostBackend
