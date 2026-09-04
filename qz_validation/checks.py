"""Individual check functions for the Qazterion Objective Validation Gate.

All command executions strictly route through `qz_tools.run_command` so that
`qz_security` gateway policies and `qz_sandbox` isolation are preserved.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from qz_environment import detect_project_environment
from qz_security.secret_scanner import scan as scan_secrets
from qz_tools import WORKSPACE, run_command


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CheckResult:
    """Structured result of a single objective validation check."""
    name: str
    status: CheckStatus
    summary: str
    output: str | None = None
    command: str | None = None
    duration_seconds: float = 0.0
    is_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "output": self.output,
            "command": self.command,
            "duration_seconds": round(self.duration_seconds, 3),
            "is_required": self.is_required,
        }


def _truncate_output(text: str | None, max_length: int = 4000) -> str:
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"\n... [truncated, {len(text)} total characters]"


def _command_failed(result_str: str) -> bool:
    """Return True if command output indicates failure (non-zero exit code or error)."""
    text = str(result_str).strip()
    m = re.search(r"exit_code=(-?\d+)", text)
    if m:
        return int(m.group(1)) != 0
    if text.startswith("Command did not complete") or text.startswith("Could not start command:"):
        return True
    return False


def run_tests(
    workspace: Path | str | None = None,
    command: str | None = None,
    timeout: int = 180,
    is_required: bool = True,
) -> CheckResult:
    """Run project tests via the existing sandbox/security tool path."""
    ws = Path(workspace or WORKSPACE).resolve()
    start_time = time.monotonic()

    test_cmd = command
    if not test_cmd:
        env = detect_project_environment(ws)
        test_cmd = env.test_command

    if not test_cmd:
        return CheckResult(
            name="tests",
            status=CheckStatus.SKIPPED,
            summary="No test runner or test command detected for workspace.",
            is_required=is_required,
            duration_seconds=time.monotonic() - start_time,
        )

    try:
        raw_output = run_command(test_cmd, timeout=timeout)
        duration = time.monotonic() - start_time
        failed = _command_failed(raw_output)

        if failed:
            return CheckResult(
                name="tests",
                status=CheckStatus.FAIL,
                summary=f"Tests failed with command: '{test_cmd}'",
                output=_truncate_output(raw_output),
                command=test_cmd,
                duration_seconds=duration,
                is_required=is_required,
            )
        return CheckResult(
            name="tests",
            status=CheckStatus.PASS,
            summary=f"Tests passed successfully with command: '{test_cmd}'",
            output=_truncate_output(raw_output),
            command=test_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )
    except Exception as error:
        duration = time.monotonic() - start_time
        return CheckResult(
            name="tests",
            status=CheckStatus.ERROR,
            summary=f"Test execution failed to run: {error}",
            output=str(error),
            command=test_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )


def run_lint(
    workspace: Path | str | None = None,
    is_required: bool = False,
) -> CheckResult:
    """Detect and run configured linters (ruff/flake8 for Python, eslint for JS/TS)."""
    ws = Path(workspace or WORKSPACE).resolve()
    start_time = time.monotonic()

    lint_cmd: str | None = None

    # 1. Check Python linters
    if (ws / "ruff.toml").is_file() or (ws / ".ruff.toml").is_file():
        lint_cmd = "ruff check ."
    elif (ws / "pyproject.toml").is_file():
        try:
            content = (ws / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            if "[tool.ruff" in content:
                lint_cmd = "ruff check ."
            elif "[tool.flake8" in content:
                lint_cmd = "flake8"
        except Exception:
            pass
    elif (ws / ".flake8").is_file() or (ws / "setup.cfg").is_file():
        lint_cmd = "flake8"

    # 2. Check JS/TS linters
    if not lint_cmd and (ws / "package.json").is_file():
        try:
            pkg_data = json.loads((ws / "package.json").read_text(encoding="utf-8", errors="ignore"))
            scripts = pkg_data.get("scripts", {})
            if "lint" in scripts:
                lint_cmd = "npm run lint"
            elif any((ws / name).exists() for name in (".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", "eslint.config.js", "eslint.config.mjs")):
                lint_cmd = "npx eslint ."
        except Exception:
            pass

    if not lint_cmd:
        return CheckResult(
            name="lint",
            status=CheckStatus.SKIPPED,
            summary="No linter configuration detected for workspace.",
            is_required=is_required,
            duration_seconds=time.monotonic() - start_time,
        )

    try:
        raw_output = run_command(lint_cmd, timeout=60)
        duration = time.monotonic() - start_time
        failed = _command_failed(raw_output)

        if failed:
            return CheckResult(
                name="lint",
                status=CheckStatus.FAIL,
                summary=f"Linter detected issues with command: '{lint_cmd}'",
                output=_truncate_output(raw_output),
                command=lint_cmd,
                duration_seconds=duration,
                is_required=is_required,
            )
        return CheckResult(
            name="lint",
            status=CheckStatus.PASS,
            summary=f"Linter passed with command: '{lint_cmd}'",
            output=_truncate_output(raw_output),
            command=lint_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )
    except Exception as error:
        duration = time.monotonic() - start_time
        return CheckResult(
            name="lint",
            status=CheckStatus.ERROR,
            summary=f"Linter execution error: {error}",
            output=str(error),
            command=lint_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )


def run_typecheck(
    workspace: Path | str | None = None,
    is_required: bool = False,
) -> CheckResult:
    """Detect and run configured typecheckers (mypy/pyright for Python, tsc for TS)."""
    ws = Path(workspace or WORKSPACE).resolve()
    start_time = time.monotonic()

    typecheck_cmd: str | None = None

    # 1. Python typecheckers
    if (ws / "mypy.ini").is_file() or (ws / ".mypy.ini").is_file():
        typecheck_cmd = "mypy ."
    elif (ws / "pyrightconfig.json").is_file():
        typecheck_cmd = "pyright"
    elif (ws / "pyproject.toml").is_file():
        try:
            content = (ws / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            if "[tool.mypy" in content:
                typecheck_cmd = "mypy ."
            elif "[tool.pyright" in content:
                typecheck_cmd = "pyright"
        except Exception:
            pass

    # 2. TypeScript typecheckers
    if not typecheck_cmd and (ws / "tsconfig.json").is_file():
        if (ws / "package.json").is_file():
            try:
                pkg_data = json.loads((ws / "package.json").read_text(encoding="utf-8", errors="ignore"))
                if "typecheck" in pkg_data.get("scripts", {}):
                    typecheck_cmd = "npm run typecheck"
            except Exception:
                pass
        if not typecheck_cmd:
            typecheck_cmd = "npx tsc --noEmit"

    if not typecheck_cmd:
        return CheckResult(
            name="typecheck",
            status=CheckStatus.SKIPPED,
            summary="No typechecker configuration detected for workspace.",
            is_required=is_required,
            duration_seconds=time.monotonic() - start_time,
        )

    try:
        raw_output = run_command(typecheck_cmd, timeout=90)
        duration = time.monotonic() - start_time
        failed = _command_failed(raw_output)

        if failed:
            return CheckResult(
                name="typecheck",
                status=CheckStatus.FAIL,
                summary=f"Typecheck failed with command: '{typecheck_cmd}'",
                output=_truncate_output(raw_output),
                command=typecheck_cmd,
                duration_seconds=duration,
                is_required=is_required,
            )
        return CheckResult(
            name="typecheck",
            status=CheckStatus.PASS,
            summary=f"Typecheck passed with command: '{typecheck_cmd}'",
            output=_truncate_output(raw_output),
            command=typecheck_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )
    except Exception as error:
        duration = time.monotonic() - start_time
        return CheckResult(
            name="typecheck",
            status=CheckStatus.ERROR,
            summary=f"Typecheck execution error: {error}",
            output=str(error),
            command=typecheck_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )


def run_build(
    workspace: Path | str | None = None,
    is_required: bool = False,
) -> CheckResult:
    """Detect and execute a project build step if applicable."""
    ws = Path(workspace or WORKSPACE).resolve()
    start_time = time.monotonic()

    build_cmd: str | None = None

    if (ws / "package.json").is_file():
        try:
            pkg_data = json.loads((ws / "package.json").read_text(encoding="utf-8", errors="ignore"))
            if "build" in pkg_data.get("scripts", {}):
                build_cmd = "npm run build"
        except Exception:
            pass
    elif (ws / "Cargo.toml").is_file():
        build_cmd = "cargo check"
    elif (ws / "go.mod").is_file():
        build_cmd = "go build ./..."

    if not build_cmd:
        return CheckResult(
            name="build",
            status=CheckStatus.SKIPPED,
            summary="No build step applicable for workspace.",
            is_required=is_required,
            duration_seconds=time.monotonic() - start_time,
        )

    try:
        raw_output = run_command(build_cmd, timeout=120)
        duration = time.monotonic() - start_time
        failed = _command_failed(raw_output)

        if failed:
            return CheckResult(
                name="build",
                status=CheckStatus.FAIL,
                summary=f"Build failed with command: '{build_cmd}'",
                output=_truncate_output(raw_output),
                command=build_cmd,
                duration_seconds=duration,
                is_required=is_required,
            )
        return CheckResult(
            name="build",
            status=CheckStatus.PASS,
            summary=f"Build completed successfully with command: '{build_cmd}'",
            output=_truncate_output(raw_output),
            command=build_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )
    except Exception as error:
        duration = time.monotonic() - start_time
        return CheckResult(
            name="build",
            status=CheckStatus.ERROR,
            summary=f"Build execution error: {error}",
            output=str(error),
            command=build_cmd,
            duration_seconds=duration,
            is_required=is_required,
        )


_EXCLUDED_SCAN_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", ".pytest_cache",
    ".idea", ".vscode", ".qazterion", "dist", "build",
}

_EXCLUDED_SCAN_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".exe", ".dll", ".so", ".dylib", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".lock",
}


def run_security_scan(
    workspace: Path | str | None = None,
    is_required: bool = False,
) -> CheckResult:
    """Scan workspace file contents for committed secrets and credentials."""
    ws = Path(workspace or WORKSPACE).resolve()
    start_time = time.monotonic()

    if not ws.is_dir():
        return CheckResult(
            name="security",
            status=CheckStatus.SKIPPED,
            summary=f"Workspace directory not found: {ws}",
            is_required=is_required,
            duration_seconds=time.monotonic() - start_time,
        )

    findings_by_file: list[str] = []
    scanned_count = 0

    try:
        for root, dirs, files in os.walk(ws):
            dirs[:] = [d for d in dirs if d not in _EXCLUDED_SCAN_DIRS and not d.startswith(".qazterion_")]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in _EXCLUDED_SCAN_EXTENSIONS:
                    continue
                file_path = Path(root) / file
                # Ignore .env files as they are local config
                if file.startswith(".env"):
                    continue
                # Skip files larger than 1MB
                try:
                    if file_path.stat().st_size > 1_000_000:
                        continue
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    scanned_count += 1
                    file_findings = scan_secrets(content)
                    if file_findings:
                        rel_path = file_path.relative_to(ws)
                        kinds = ", ".join(sorted({f.kind for f in file_findings}))
                        findings_by_file.append(f"{rel_path}: {len(file_findings)} finding(s) ({kinds})")
                except (OSError, UnicodeDecodeError):
                    continue

        duration = time.monotonic() - start_time
        if findings_by_file:
            summary = f"Secret scan found credentials in {len(findings_by_file)} file(s)."
            output = "\n".join(findings_by_file)
            return CheckResult(
                name="security",
                status=CheckStatus.FAIL,
                summary=summary,
                output=output,
                duration_seconds=duration,
                is_required=is_required,
            )

        return CheckResult(
            name="security",
            status=CheckStatus.PASS,
            summary=f"Security secret scan clean ({scanned_count} files scanned).",
            duration_seconds=duration,
            is_required=is_required,
        )
    except Exception as error:
        duration = time.monotonic() - start_time
        return CheckResult(
            name="security",
            status=CheckStatus.ERROR,
            summary=f"Security scan error: {error}",
            output=str(error),
            duration_seconds=duration,
            is_required=is_required,
        )
