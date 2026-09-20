"""Unit tests for Phase 1 security remediation:
- KeyStore credentials not dumped into global os.environ
- Git operations pass sanitized environments
- Python venv and pip dependency installs pass sanitized environments
- Subprocess environment scrubbing verification
"""
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import qz_agent
import qz_environment
import qz_tools
from qz_core import git_ops
from qz_sandbox.backend import sanitize_subprocess_env


class Phase1SecurityTests(unittest.TestCase):
    def setUp(self):
        self.secret_keys = {
            "GROQ_KEY_1": "gsk_test123",
            "GEMINI_KEY_1": "AIzaSyTestKey",
            "MISTRAL_KEY_1": "mis_secret_token",
            "OPENROUTER_KEY_1": "sk-or-v1-secret",
            "LITELLM_MASTER_KEY": "sk-my-master-key",
            "DATABASE_PASSWORD": "secret_password",
        }
        for k, v in self.secret_keys.items():
            os.environ[k] = v

    def tearDown(self):
        for k in self.secret_keys:
            os.environ.pop(k, None)

    def test_sanitize_subprocess_env_scrubs_all_credentials(self):
        clean_env = sanitize_subprocess_env("D:/test_workspace")
        for secret_name in self.secret_keys:
            self.assertNotIn(
                secret_name,
                clean_env,
                f"Secret variable '{secret_name}' was not scrubbed from subprocess environment!",
            )
        self.assertIn("PYTHONPATH", clean_env)
        self.assertTrue(clean_env["PYTHONPATH"].startswith("D:/test_workspace"))

    def test_run_git_uses_sanitized_environment(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr="")
            qz_tools._run_git(["status"])
            mock_run.assert_called_once()
            called_env = mock_run.call_args.kwargs.get("env")
            self.assertIsNotNone(called_env, "Subprocess env must be explicitly provided for Git operations")
            for secret_name in self.secret_keys:
                self.assertNotIn(secret_name, called_env)

    def test_git_ops_subprocesses_use_sanitized_environment(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="test.py\n", stderr="")
            files = git_ops.get_uncommitted_files("D:/test_workspace")
            mock_run.assert_called_once()
            called_env = mock_run.call_args.kwargs.get("env")
            self.assertIsNotNone(called_env)
            for secret_name in self.secret_keys:
                self.assertNotIn(secret_name, called_env)

    def test_prepare_python_environment_uses_sanitized_environment(self):
        env = qz_environment.ProjectEnvironment(
            workspace=Path("D:/test_workspace"),
            project_type="python",
            manager="pip",
            python_interpreter=None,
            dependency_files=("requirements.txt",),
            protected_tools=(),
            test_command="pytest",
        )
        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.is_file", return_value=True):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            qz_environment.prepare_python_environment(env, approved=True)
            self.assertGreaterEqual(mock_run.call_count, 1)
            for call_item in mock_run.call_args_list:
                called_env = call_item.kwargs.get("env")
                self.assertIsNotNone(called_env)
                for secret_name in self.secret_keys:
                    self.assertNotIn(secret_name, called_env)


    def test_recovery_git_ops_use_sanitized_environment(self):
        from qz_recovery import resume_manager
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr="")
            resume_manager.get_uncommitted_files("D:/test_workspace")
            mock_run.assert_called_once()
            called_env = mock_run.call_args.kwargs.get("env")
            self.assertIsNotNone(called_env)
            for secret_name in self.secret_keys:
                self.assertNotIn(secret_name, called_env)

    def test_cli_handlers_git_ops_use_sanitized_environment(self):
        from qz_cli.commands import handlers
        from rich.console import Console
        console = Console(quiet=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=["git", "diff"], returncode=0, stdout="diff output", stderr="")
            handlers.handle_diff_command(console, Path("D:/test_workspace"))
            mock_run.assert_called_once()
            called_env = mock_run.call_args.kwargs.get("env")
            self.assertIsNotNone(called_env)
            for secret_name in self.secret_keys:
                self.assertNotIn(secret_name, called_env)


if __name__ == "__main__":
    unittest.main()
