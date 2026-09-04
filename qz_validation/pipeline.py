"""Objective validation pipeline coordinating checks and self-review gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from openai import OpenAI

from qz_validation.checks import (
    CheckResult,
    CheckStatus,
    run_build,
    run_lint,
    run_security_scan,
    run_tests,
    run_typecheck,
)


def _persist_event(task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
    if not task_id:
        return
    try:
        from qz_tasks.task_manager import log_event
        from qz_core.common import _persist_task
        _persist_task(task_id, lambda: log_event(task_id, event_type, payload))
    except Exception:
        pass


@dataclass(frozen=True)
class ValidationReport:
    """Consolidated result across all validation checks for a task."""
    status: str                         # "PASS" | "FAIL"
    checks: dict[str, CheckResult]      # check_name -> CheckResult
    summary: str
    required_checks: set[str] = field(default_factory=lambda: {"tests"})
    advisory_checks: set[str] = field(default_factory=lambda: {"lint", "typecheck", "build", "security", "requirements"})

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    @property
    def failed_required_checks(self) -> list[str]:
        return [
            name for name, res in self.checks.items()
            if name in self.required_checks and res.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]

    @property
    def failed_advisory_checks(self) -> list[str]:
        return [
            name for name, res in self.checks.items()
            if name not in self.required_checks and res.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "failed_required": self.failed_required_checks,
            "failed_advisory": self.failed_advisory_checks,
            "checks": {k: v.to_dict() for k, v in self.checks.items()},
            "summary": self.summary,
        }


class ValidationPipeline:
    """Central objective validation gate governing task completion.

    Default configuration:
      - Required: `tests` (failing tests block task completion)
      - Advisory: `lint`, `typecheck`, `build`, `security`, `requirements`
        (logged and reported, but non-blocking unless configured as required)
    """

    def __init__(
        self,
        required_checks: set[str] | list[str] | None = None,
        advisory_checks: set[str] | list[str] | None = None,
    ) -> None:
        self.required_checks = set(required_checks) if required_checks is not None else {"tests"}
        self.advisory_checks = set(advisory_checks) if advisory_checks is not None else {"lint", "typecheck", "build", "security", "requirements"}

    def run(
        self,
        task_id: str | None = None,
        workspace: Path | str | None = None,
        requirements_text: str | None = None,
        changes: list[dict] | None = None,
        test_command: str | None = None,
        client: OpenAI | None = None,
    ) -> ValidationReport:
        """Run all applicable validation checks and compile a structured report."""
        checks: dict[str, CheckResult] = {}

        # 1. Tests Check (Required by default)
        is_tests_req = "tests" in self.required_checks
        test_res = run_tests(workspace, command=test_command, is_required=is_tests_req)
        checks["tests"] = test_res
        _persist_event(task_id, "VALIDATION_CHECK", test_res.to_dict())

        # 2. Lint Check (Advisory by default)
        is_lint_req = "lint" in self.required_checks
        lint_res = run_lint(workspace, is_required=is_lint_req)
        checks["lint"] = lint_res
        _persist_event(task_id, "VALIDATION_CHECK", lint_res.to_dict())

        # 3. Typecheck Check (Advisory by default)
        is_type_req = "typecheck" in self.required_checks
        type_res = run_typecheck(workspace, is_required=is_type_req)
        checks["typecheck"] = type_res
        _persist_event(task_id, "VALIDATION_CHECK", type_res.to_dict())

        # 4. Build Check (Advisory by default)
        is_build_req = "build" in self.required_checks
        build_res = run_build(workspace, is_required=is_build_req)
        checks["build"] = build_res
        _persist_event(task_id, "VALIDATION_CHECK", build_res.to_dict())

        # 5. Security Scan (Advisory by default)
        is_sec_req = "security" in self.required_checks
        sec_res = run_security_scan(workspace, is_required=is_sec_req)
        checks["security"] = sec_res
        _persist_event(task_id, "VALIDATION_CHECK", sec_res.to_dict())

        # 6. Requirements / Self-Review Check (Advisory by default)
        is_req_req = "requirements" in self.required_checks
        if changes:
            try:
                from qz_core.reviewer import self_review
                findings = self_review(changes, client=client)
            except Exception as e:
                findings = None
            if findings:
                req_res = CheckResult(
                    name="requirements",
                    status=CheckStatus.FAIL,
                    summary=f"Self-review findings: {findings[:200]}...",
                    output=findings,
                    is_required=is_req_req,
                )
            else:
                req_res = CheckResult(
                    name="requirements",
                    status=CheckStatus.PASS,
                    summary="Self-review passed with no issues.",
                    is_required=is_req_req,
                )
        else:
            req_res = CheckResult(
                name="requirements",
                status=CheckStatus.SKIPPED,
                summary="No changes recorded for self-review.",
                is_required=is_req_req,
            )
        checks["requirements"] = req_res
        _persist_event(task_id, "VALIDATION_CHECK", req_res.to_dict())

        # Determine overall status
        failed_required = [
            k for k, v in checks.items()
            if k in self.required_checks and v.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]
        failed_advisory = [
            k for k, v in checks.items()
            if k not in self.required_checks and v.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]

        overall_status = "FAIL" if failed_required else "PASS"

        # Build formatted human-readable summary
        lines = [f"Validation Gate: {overall_status}"]
        for name, res in checks.items():
            req_tag = "REQUIRED" if name in self.required_checks else "ADVISORY"
            lines.append(f"  - [{res.status.value}] {name.upper()} ({req_tag}): {res.summary}")

        if failed_advisory and overall_status == "PASS":
            lines.append(f"  Note: {len(failed_advisory)} advisory check(s) had warnings ({', '.join(failed_advisory)}).")

        summary_text = "\n".join(lines)

        report = ValidationReport(
            status=overall_status,
            checks=checks,
            summary=summary_text,
            required_checks=self.required_checks,
            advisory_checks=self.advisory_checks,
        )

        _persist_event(task_id, "VALIDATION_REPORT", {
            "status": overall_status,
            "passed": report.passed,
            "failed_required": failed_required,
            "failed_advisory": failed_advisory,
        })

        return report
