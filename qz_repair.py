"""Intelligent failure classification, diagnostic context retrieval, and repair history for Qazterion."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from qz_tools import WORKSPACE
from qz_validation import CheckResult, CheckStatus, ValidationReport


class FailureCategory(str, Enum):
    TEST_FAILURE = "TEST_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    TYPECHECK_FAILURE = "TYPECHECK_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    SECURITY_FAILURE = "SECURITY_FAILURE"
    COMMAND_FAILURE = "COMMAND_FAILURE"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


@dataclass
class DiagnosticContext:
    failing_files: list[str] = field(default_factory=list)
    line_numbers: list[int] = field(default_factory=list)
    error_summary: str = ""
    error_signature: str = ""
    snippets: list[str] = field(default_factory=list)


@dataclass
class ClassifiedFailure:
    category: FailureCategory
    check_name: str
    summary: str
    diagnostics: DiagnosticContext
    recommended_action: str

    def format_for_repair_prompt(self, attempt: int, max_attempts: int, history_feedback: str = "") -> str:
        lines = [
            f"=== DIAGNOSTIC REPAIR FEEDBACK (Attempt {attempt}/{max_attempts}) ===",
            f"Failure Category: {self.category.value}",
            f"Failing Check: {self.check_name}",
            f"Diagnosis: {self.summary}",
        ]
        if self.diagnostics.failing_files:
            lines.append(f"Failing Files: {', '.join(self.diagnostics.failing_files)}")
        if self.diagnostics.error_summary:
            lines.append(f"Error Details:\n{self.diagnostics.error_summary}")
        if self.diagnostics.snippets:
            lines.append("Relevant Code Context:")
            lines.extend(self.diagnostics.snippets)
        lines.append(f"Recommended Strategy: {self.recommended_action}")
        if history_feedback:
            lines.append(f"\n[ANTI-LOOP WARNING]:\n{history_feedback}")
        lines.append("==========================================================")
        return "\n".join(lines)


def _extract_error_signature(text: str) -> str:
    """Normalize error string to compute a stable signature for loop detection."""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("exit_code=") or "Ran " in s and " tests in " in s:
            continue
        # Strip timestamps, transient memory addresses (0x...)
        s = re.sub(r"0x[0-9a-fA-F]+", "0x...", s)
        s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "TIMESTAMP", s)
        lines.append(s)
    joined = "\n".join(lines[:30])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def classify_failure(report: ValidationReport, raw_command_output: str | None = None) -> ClassifiedFailure:
    """Analyze a failed ValidationReport and extract structured diagnostic context."""
    # Priority order of failure checks
    check_priority = ["security", "build", "tests", "typecheck", "lint", "requirements"]

    failing_check: CheckResult | None = None
    for name in check_priority:
        res = report.checks.get(name)
        if res and res.status in (CheckStatus.FAIL, CheckStatus.ERROR):
            failing_check = res
            break

    if not failing_check:
        for name, res in report.checks.items():
            if res.status in (CheckStatus.FAIL, CheckStatus.ERROR):
                failing_check = res
                break

    output = failing_check.output if failing_check and failing_check.output else (raw_command_output or "")
    chk_name = failing_check.name if failing_check else "command"

    failing_files: set[str] = set()
    line_numbers: list[int] = []

    # Detect file paths in output (e.g. `tests/test_auth.py:42:` or `File 'tests/test_user.py', line 42`)
    for m in re.finditer(r"(?:File\s+['\"]([^'\"]+)['\"],\s+line\s+(\d+)|([\w./\\-]+\.(?:py|ts|js|go|rs|cs|java)):(\d+))", output):
        fpath = m.group(1) or m.group(3)
        lineno = m.group(2) or m.group(4)
        if fpath:
            failing_files.add(fpath.replace("\\", "/"))
        if lineno:
            try:
                line_numbers.append(int(lineno))
            except ValueError:
                pass

    sig = _extract_error_signature(output)
    diag = DiagnosticContext(
        failing_files=sorted(failing_files),
        line_numbers=line_numbers,
        error_summary=output[:1500] if output else (failing_check.summary if failing_check else "Unknown failure"),
        error_signature=sig,
    )

    if chk_name == "security":
        return ClassifiedFailure(
            category=FailureCategory.SECURITY_FAILURE,
            check_name="security",
            summary=failing_check.summary if failing_check else "Security scan detected secrets or unsafe pattern",
            diagnostics=diag,
            recommended_action="Remove leaked credentials, use environment variables or keystore, and never hardcode secrets.",
        )
    if chk_name == "build":
        return ClassifiedFailure(
            category=FailureCategory.BUILD_FAILURE,
            check_name="build",
            summary=failing_check.summary if failing_check else "Build compilation failed",
            diagnostics=diag,
            recommended_action="Fix syntax or compilation errors, missing dependencies, or bad import paths.",
        )
    if chk_name == "tests":
        return ClassifiedFailure(
            category=FailureCategory.TEST_FAILURE,
            check_name="tests",
            summary=failing_check.summary if failing_check else "Unit or integration test failed",
            diagnostics=diag,
            recommended_action="Inspect the exact assertion failure and stack trace, fix logic in the source code or update tests to match expected behavior.",
        )
    if chk_name == "typecheck":
        return ClassifiedFailure(
            category=FailureCategory.TYPECHECK_FAILURE,
            check_name="typecheck",
            summary=failing_check.summary if failing_check else "Typechecker found type mismatches",
            diagnostics=diag,
            recommended_action="Correct type annotations, argument types, or return values.",
        )
    if chk_name == "lint":
        return ClassifiedFailure(
            category=FailureCategory.LINT_FAILURE,
            check_name="lint",
            summary=failing_check.summary if failing_check else "Linter reported style or lint violations",
            diagnostics=diag,
            recommended_action="Fix formatting, unused imports, or style errors reported by the linter.",
        )

    return ClassifiedFailure(
        category=FailureCategory.COMMAND_FAILURE,
        check_name=chk_name,
        summary=failing_check.summary if failing_check else "Command executed with non-zero exit code",
        diagnostics=diag,
        recommended_action="Diagnose runtime error from logs, verify inputs, and fix implementation.",
    )


@dataclass
class RepairAttemptRecord:
    attempt: int
    category: FailureCategory
    error_signature: str
    diff_hash: str
    summary: str


class RepairHistory:
    """Maintains repair history for a node/task to detect ineffective repeating loops."""

    def __init__(self) -> None:
        self.attempts: list[RepairAttemptRecord] = []

    def record_attempt(
        self,
        attempt: int,
        category: FailureCategory,
        error_signature: str,
        diff_text: str = "",
        summary: str = "",
    ) -> None:
        diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()[:16] if diff_text else ""
        self.attempts.append(
            RepairAttemptRecord(
                attempt=attempt,
                category=category,
                error_signature=error_signature,
                diff_hash=diff_hash,
                summary=summary,
            )
        )

    def get_anti_loop_feedback(self, current_sig: str, current_diff: str = "") -> str:
        """Return warning feedback if the agent is caught in a repetitive failure loop."""
        if not self.attempts:
            return ""

        same_errors = [a for a in self.attempts if a.error_signature == current_sig]
        if len(same_errors) >= 2:
            return (
                f"You have encountered the exact same failure {len(same_errors)} times consecutively. "
                "Do NOT repeat the same patch. Carefully re-read the entire source file with read_file, "
                "re-verify function signatures and imports, and consider rewriting the file with write_file."
            )

        if current_diff:
            diff_h = hashlib.sha256(current_diff.encode("utf-8")).hexdigest()[:16]
            same_diffs = [a for a in self.attempts if a.diff_hash == diff_h]
            if same_diffs:
                return "You already attempted this exact patch previously and it did not resolve the failure. Try an alternate approach."

        return ""
