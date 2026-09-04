"""Tests for Phase 13: Diagnostic Repair Loop, Usage & Cost Tracking, Rollback, and Telemetry."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from qz_repair import (
    FailureCategory,
    RepairHistory,
    classify_failure,
)
from qz_usage_tracker import UsageTracker, calculate_estimated_cost
from qz_recovery.resume_manager import ResumeManager, IntegrityStatus
from qz_telemetry import TelemetryCollector
from qz_validation.checks import CheckResult, CheckStatus
from qz_validation.pipeline import ValidationReport
from qz_sandbox.manager import SandboxManager


class Phase13RepairUsageRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_failure_classification_categories(self):
        # 1. Test failure
        test_fail_report = ValidationReport(
            status="FAIL",
            checks={
                "tests": CheckResult(
                    name="tests",
                    status=CheckStatus.FAIL,
                    summary="Test failed: AssertionError in test_user.py:42",
                    output="AssertionError: expected 1 != 2\n  File 'tests/test_user.py', line 42",
                )
            },
            summary="Tests failed",
        )
        c1 = classify_failure(test_fail_report)
        self.assertEqual(c1.category, FailureCategory.TEST_FAILURE)
        self.assertIn("tests/test_user.py", c1.diagnostics.failing_files)

        # 2. Security scan failure
        sec_fail_report = ValidationReport(
            status="FAIL",
            checks={
                "security": CheckResult(
                    name="security",
                    status=CheckStatus.FAIL,
                    summary="Found secret token in config.py",
                    output="High risk: OpenAI API key detected",
                )
            },
            summary="Security scan failed",
        )
        c2 = classify_failure(sec_fail_report)
        self.assertEqual(c2.category, FailureCategory.SECURITY_FAILURE)

        # 3. Build compilation failure
        build_fail_report = ValidationReport(
            status="FAIL",
            checks={
                "build": CheckResult(
                    name="build",
                    status=CheckStatus.FAIL,
                    summary="tsc failed with error TS2304",
                    output="src/index.ts:10: error TS2304: Cannot find name 'foo'",
                )
            },
            summary="Build failed",
        )
        c3 = classify_failure(build_fail_report)
        self.assertEqual(c3.category, FailureCategory.BUILD_FAILURE)

    def test_repair_history_anti_looping(self):
        history = RepairHistory()
        sig = "abc123err"

        history.record_attempt(1, FailureCategory.TEST_FAILURE, sig, diff_text="+ foo = 1")
        self.assertEqual(history.get_anti_loop_feedback(sig, current_diff="+ bar = 2"), "")

        # Second same error triggers anti-loop warning
        history.record_attempt(2, FailureCategory.TEST_FAILURE, sig, diff_text="+ bar = 2")
        feedback = history.get_anti_loop_feedback(sig)
        self.assertIn("encountered the exact same failure 2 times", feedback)

    def test_usage_tracking_cost_and_budgets(self):
        tracker = UsageTracker()

        # Cost calculation helper
        cost = calculate_estimated_cost("groq", input_tokens=10_000, output_tokens=2_000)
        self.assertGreater(cost, 0.0)

        # Record requests
        tracker.set_task_budget("task-100", max_tokens=20_000, max_cost=1.0)
        tracker.record_request(
            model="groq-fast",
            task_id="task-100",
            duration=0.5,
            success=True,
            input_tokens=5_000,
            output_tokens=1_000,
        )

        usage = tracker.get_task_usage("task-100")
        self.assertEqual(usage["input_tokens"], 5_000)
        self.assertEqual(usage["output_tokens"], 1_000)
        self.assertEqual(usage["total_tokens"], 6_000)
        self.assertGreater(usage["estimated_cost"], 0.0)

        # Within budget check
        ok, msg, is_warn = tracker.check_task_budget("task-100")
        self.assertTrue(ok)
        self.assertIsNone(msg)

        # Record more to exceed warning threshold
        tracker.record_request(
            model="groq-fast",
            task_id="task-100",
            duration=0.5,
            success=True,
            input_tokens=12_000,
            output_tokens=1_000,
        )
        ok, msg, is_warn = tracker.check_task_budget("task-100")
        self.assertTrue(ok)
        self.assertTrue(is_warn)
        self.assertIn("approaching token limit", msg)

    def test_multi_command_aggregation(self):
        sandbox = SandboxManager(docker_available=False)

        # Command with failure in first command followed by second command
        cmd_failing_chain = 'python -c "import sys; sys.exit(42)"; echo finished'
        res = sandbox.execute(cmd_failing_chain, str(self.workspace), timeout=15)

        # Aggregated exit code must be non-zero (42) and not masked by echo finished
        self.assertEqual(res.exit_code, 42)

        # Successful chained commands
        cmd_ok_chain = 'python -c "print(\'step1\')"; python -c "print(\'step2\')"'
        res_ok = sandbox.execute(cmd_ok_chain, str(self.workspace), timeout=15)
        self.assertEqual(res_ok.exit_code, 0)
        self.assertIn("step1", res_ok.stdout)
        self.assertIn("step2", res_ok.stdout)

    def test_telemetry_and_benchmarks(self):
        collector = TelemetryCollector()
        collector.record_metric("task_duration", 12.5, task_id="t-1")
        report = collector.generate_report()
        self.assertIsInstance(report.to_dict(), dict)
        self.assertIn("task_success_rate", report.to_dict())


if __name__ == "__main__":
    unittest.main()
