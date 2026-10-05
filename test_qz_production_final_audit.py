"""Comprehensive production-grade verification tests for Qazterion final audit.

Covers validation gates on testless vs. test-backed workspaces, pre-existing
failures (test baseline), and error normalization. Multi-provider routing is
covered by test_qz_provider_gateway.py.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qz_core.executor import TestBaseline, _test_failure_signature
from qz_providers.exceptions import AuthenticationError, RateLimitError, normalize_error
from qz_validation.checks import run_tests
from qz_validation.pipeline import ValidationPipeline


class ValidationAndEnvironmentAuditTests(unittest.TestCase):
    def test_testless_workspace_passes_validation_gate(self):
        """Workspaces without tests should not be permanently blocked."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir)
            (ws / "README.md").write_text("# Testless project\n", encoding="utf-8")

            pipe = ValidationPipeline()
            report = pipe.run(workspace=ws, requirements_text="Create README", changes=[{"path": "README.md"}])

            self.assertTrue(report.passed)
            self.assertEqual(report.status, "PASS")
            self.assertEqual(report.skipped_required_checks, [])

    def test_workspace_with_failing_tests_fails_validation_gate(self):
        """Workspaces with failing tests must be blocked by validation gate."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir)
            tests_dir = ws / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_sample.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )

            pipe = ValidationPipeline()
            report = pipe.run(workspace=ws, requirements_text="Fix test", changes=[{"path": "tests/test_sample.py"}])

            self.assertFalse(report.passed)
            self.assertEqual(report.status, "FAIL")
            self.assertIn("tests", report.failed_required_checks)


class PreExistingFailureBaselineTests(unittest.TestCase):
    def _failing_workspace(self, root: Path) -> None:
        tests_dir = root / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("", encoding="utf-8")
        (tests_dir / "test_old.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_old_bug(self):\n        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )

    def test_unchanged_pre_existing_failure_does_not_block_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir)
            self._failing_workspace(ws)
            first = run_tests(ws)
            self.assertEqual(first.status.value, "FAIL")
            from qz_validation.checks import _run_in
            raw = _run_in(ws, first.command, 120)
            baseline = TestBaseline(first.command, raw, _test_failure_signature(raw))
            report = ValidationPipeline().run(workspace=ws, requirements_text="unrelated change", test_baseline=baseline)
            self.assertTrue(report.passed, report.summary)
            self.assertIn("pre-existing", report.checks["tests"].summary)

    def test_new_failure_still_blocks_even_with_a_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws = Path(temp_dir)
            self._failing_workspace(ws)
            baseline = TestBaseline("python -m unittest discover -s tests", "exit_code=1\nother", "different signature")
            report = ValidationPipeline().run(workspace=ws, requirements_text="x", test_baseline=baseline)
            self.assertFalse(report.passed)


class ErrorNormalizationAuditTests(unittest.TestCase):
    def test_error_normalization_classes(self):
        """Verify errors are classified into retryable and non-retryable categories."""
        # 401 -> AuthenticationError (non-retryable)
        err_401 = normalize_error("Invalid API Key provided", status_code=401)
        self.assertIsInstance(err_401, AuthenticationError)
        self.assertFalse(err_401.retryable)

        # 429 -> RateLimitError (retryable)
        err_429 = normalize_error("Rate limit exceeded. Please wait.", status_code=429)
        self.assertIsInstance(err_429, RateLimitError)
        self.assertTrue(err_429.retryable)


if __name__ == "__main__":
    unittest.main()
