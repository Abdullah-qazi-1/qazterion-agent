"""Unit and integration tests for qz_validation: checks, pipeline, and executor completion gate."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

from qz_validation.checks import (
    CheckResult,
    CheckStatus,
    run_build,
    run_lint,
    run_security_scan,
    run_tests,
    run_typecheck,
)
from qz_validation.pipeline import ValidationPipeline, ValidationReport
from qz_core.executor import run_executor


class ValidationCheckTests(unittest.TestCase):
    def test_run_tests_passes_and_fails_through_run_command_without_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

            # Mock run_command to return success exit code
            with (
                patch("qz_validation.checks.run_command", return_value="exit_code=0\nRan 5 tests in 0.1s\nOK\n") as mock_cmd,
                patch("subprocess.run") as mock_subproc,
                patch("subprocess.Popen") as mock_popen,
            ):
                result = run_tests(workspace=workspace)
                self.assertEqual(result.status, CheckStatus.PASS)
                self.assertIn("passed", result.summary.lower())
                self.assertTrue(mock_cmd.called)
                mock_subproc.assert_not_called()
                mock_popen.assert_not_called()

            # Mock run_command to return failing exit code
            with (
                patch("qz_validation.checks.run_command", return_value="exit_code=1\nFAILED test_math.py\n") as mock_cmd,
                patch("subprocess.run") as mock_subproc,
                patch("subprocess.Popen") as mock_popen,
            ):
                result = run_tests(workspace=workspace)
                self.assertEqual(result.status, CheckStatus.FAIL)
                self.assertIn("failed", result.summary.lower())
                self.assertTrue(mock_cmd.called)
                mock_subproc.assert_not_called()
                mock_popen.assert_not_called()

    def test_checks_return_skipped_when_no_tool_or_config_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)  # empty directory without linter/typechecker/build configs

            lint_res = run_lint(workspace=workspace)
            self.assertEqual(lint_res.status, CheckStatus.SKIPPED)

            type_res = run_typecheck(workspace=workspace)
            self.assertEqual(type_res.status, CheckStatus.SKIPPED)

            build_res = run_build(workspace=workspace)
            self.assertEqual(build_res.status, CheckStatus.SKIPPED)

    def test_security_scan_detects_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "service.py").write_text("API_KEY = 'sk-123456789012345678901234567890'\n", encoding="utf-8")
            (workspace / "safe.py").write_text("def hello(): return 'world'\n", encoding="utf-8")

            res = run_security_scan(workspace=workspace)
            self.assertEqual(res.status, CheckStatus.FAIL)
            self.assertIn("credentials", res.summary.lower())
            self.assertIn("service.py", res.output)

    def test_security_scan_passes_on_clean_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "safe.py").write_text("def hello(): return 'world'\n", encoding="utf-8")

            res = run_security_scan(workspace=workspace)
            self.assertEqual(res.status, CheckStatus.PASS)


class ValidationPipelineTests(unittest.TestCase):
    def test_pipeline_fails_when_required_tests_fail_even_if_advisory_pass(self):
        pipeline = ValidationPipeline(
            required_checks={"tests"},
            advisory_checks={"lint", "typecheck", "build", "security", "requirements"},
        )

        with (
            patch("qz_validation.pipeline.run_tests", return_value=CheckResult(name="tests", status=CheckStatus.FAIL, summary="Tests failed", is_required=True)),
            patch("qz_validation.pipeline.run_lint", return_value=CheckResult(name="lint", status=CheckStatus.PASS, summary="Lint clean", is_required=False)),
            patch("qz_validation.pipeline.run_typecheck", return_value=CheckResult(name="typecheck", status=CheckStatus.PASS, summary="Typecheck clean", is_required=False)),
            patch("qz_validation.pipeline.run_build", return_value=CheckResult(name="build", status=CheckStatus.PASS, summary="Build clean", is_required=False)),
            patch("qz_validation.pipeline.run_security_scan", return_value=CheckResult(name="security", status=CheckStatus.PASS, summary="Security clean", is_required=False)),
            patch("qz_core.reviewer.self_review", return_value=None),
        ):
            report = pipeline.run(task_id="task-test-1")
            self.assertFalse(report.passed)
            self.assertEqual(report.status, "FAIL")
            self.assertEqual(report.failed_required_checks, ["tests"])

    def test_pipeline_passes_when_tests_pass_and_advisory_checks_fail(self):
        pipeline = ValidationPipeline(
            required_checks={"tests"},
            advisory_checks={"lint", "typecheck", "build", "security", "requirements"},
        )

        with (
            patch("qz_validation.pipeline.run_tests", return_value=CheckResult(name="tests", status=CheckStatus.PASS, summary="Tests passed", is_required=True)),
            patch("qz_validation.pipeline.run_lint", return_value=CheckResult(name="lint", status=CheckStatus.FAIL, summary="Lint format issue", output="line too long", is_required=False)),
            patch("qz_validation.pipeline.run_typecheck", return_value=CheckResult(name="typecheck", status=CheckStatus.SKIPPED, summary="No mypy", is_required=False)),
            patch("qz_validation.pipeline.run_build", return_value=CheckResult(name="build", status=CheckStatus.SKIPPED, summary="No build", is_required=False)),
            patch("qz_validation.pipeline.run_security_scan", return_value=CheckResult(name="security", status=CheckStatus.PASS, summary="Clean", is_required=False)),
            patch("qz_core.reviewer.self_review", return_value="Minor style comment"),
        ):
            report = pipeline.run(task_id="task-test-2", changes=[{"path": "foo.py", "kind": "patch", "detail": "diff"}])
            self.assertTrue(report.passed)
            self.assertEqual(report.status, "PASS")
            # Confirm advisory failures are recorded in the report and not dropped
            self.assertIn("lint", report.failed_advisory_checks)
            self.assertIn("requirements", report.failed_advisory_checks)
            self.assertEqual(report.checks["lint"].status, CheckStatus.FAIL)
            self.assertEqual(report.checks["requirements"].status, CheckStatus.FAIL)
            self.assertIn("Note: 2 advisory check(s) had warnings", report.summary)

    def test_validation_events_persisted_to_task_manager(self):
        pipeline = ValidationPipeline()
        with (
            patch("qz_validation.pipeline.run_tests", return_value=CheckResult(name="tests", status=CheckStatus.PASS, summary="Tests passed", is_required=True)),
            patch("qz_validation.pipeline.run_lint", return_value=CheckResult(name="lint", status=CheckStatus.SKIPPED, summary="Skipped", is_required=False)),
            patch("qz_validation.pipeline.run_typecheck", return_value=CheckResult(name="typecheck", status=CheckStatus.SKIPPED, summary="Skipped", is_required=False)),
            patch("qz_validation.pipeline.run_build", return_value=CheckResult(name="build", status=CheckStatus.SKIPPED, summary="Skipped", is_required=False)),
            patch("qz_validation.pipeline.run_security_scan", return_value=CheckResult(name="security", status=CheckStatus.PASS, summary="Clean", is_required=False)),
            patch("qz_validation.pipeline._persist_event") as mock_persist,
        ):
            report = pipeline.run(task_id="task-persist-123")
            self.assertTrue(mock_persist.called)
            event_types = [call[0][1] for call in mock_persist.call_args_list]
            self.assertIn("VALIDATION_CHECK", event_types)
            self.assertIn("VALIDATION_REPORT", event_types)


class ExecutorValidationGateIntegrationTests(unittest.TestCase):
    def test_failed_validation_pipeline_re_enters_retry_loop(self):
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content="I am done with the task.",
                        tool_calls=[],
                        model_dump=lambda exclude_none=True: {"role": "assistant", "content": "I am done with the task."},
                    )
                )
            ]
        )

        mock_failing_report = ValidationReport(
            status="FAIL",
            checks={"tests": CheckResult(name="tests", status=CheckStatus.FAIL, summary="pytest failed", is_required=True)},
            summary="Validation Gate: FAIL\n  - [FAIL] TESTS (REQUIRED): pytest failed",
            required_checks={"tests"},
            advisory_checks=set(),
        )

        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.run.return_value = mock_failing_report

        persisted_statuses = []

        def mock_update_status(tid, status, **kwargs):
            persisted_statuses.append(status)

        with (
            patch("qz_agent.classify_task_complexity", return_value="simple", create=True),
            patch("qz_agent.select_route", return_value=("groq-fast", "GROQ_KEY_1"), create=True),
            patch("qz_agent.request_completion", return_value=(mock_response, "groq-fast"), create=True),
            patch("qz_agent.ValidationPipeline", return_value=mock_pipeline_instance, create=True),
            patch("qz_core.executor.classify_task_complexity", return_value="simple"),
            patch("qz_core.executor.select_route", return_value=("groq-fast", "GROQ_KEY_1")),
            patch("qz_core.executor.request_completion", return_value=(mock_response, "groq-fast")),
            patch("qz_core.executor.ValidationPipeline", return_value=mock_pipeline_instance),
            patch("qz_core.executor._task_requires_test_changes", return_value=False),
            patch("qz_tools.run_command", return_value="exit_code=1\nFAILED\n"),
            patch("qz_tasks.task_manager.update_status", side_effect=mock_update_status),
        ):
            # Run executor with max_iterations=2. Since validation fails on each attempt,
            # it should NOT mark COMPLETED and should reach max iterations.
            result = run_executor(
                task="Do some task",
                plan="Plan",
                architecture="arch",
                max_iterations=2,
                task_id="task-val-gate",
            )

            # Assert task was never marked COMPLETED
            from qz_tasks.models import TaskStatus
            self.assertNotIn(TaskStatus.COMPLETED, persisted_statuses)
            # Under the hard DAG executor, a node whose validation keeps failing is
            # retried up to its bounded retry limit and then reported as a failed
            # node/task (rather than the legacy "Maximum iterations" executor-loop
            # wording) -- assert on the current, equivalent failure semantics.
            self.assertIn("Validation failed", result)
            self.assertIn("failed", result.lower())


if __name__ == "__main__":
    unittest.main()
