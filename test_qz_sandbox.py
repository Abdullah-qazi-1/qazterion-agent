"""Host command execution: no Docker required, secrets scrubbed, timeouts enforced."""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import qz_tools
from qz_sandbox.backend import HostBackend, _prepare_host_command
from qz_sandbox.manager import SandboxManager


class NoDockerTests(unittest.TestCase):
    def test_manager_never_invokes_docker(self):
        real_popen = subprocess.Popen
        launched = []

        def recording_popen(args, *a, **kw):
            launched.append(args)
            return real_popen(args, *a, **kw)

        command = "Write-Output hello" if os.name == "nt" else "echo hello"
        with tempfile.TemporaryDirectory() as workspace, \
             patch("qz_sandbox.backend.subprocess.Popen", side_effect=recording_popen), \
             patch("subprocess.run", side_effect=AssertionError("no probing subprocess expected")):
            result = SandboxManager().execute(command, workspace, timeout=30)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.isolation, "host")
        self.assertTrue(launched)
        self.assertFalse(any(str(args[0]).lower().startswith("docker") for args in launched))

    def test_run_command_works_with_docker_absent_from_path(self):
        command = "Write-Output qz-ok" if os.name == "nt" else "echo qz-ok"
        with tempfile.TemporaryDirectory() as workspace, patch.object(qz_tools, "WORKSPACE", workspace), \
             patch("shutil.which", side_effect=lambda name, *a, **k: None if "docker" in name else "x"):
            output = qz_tools.run_command(command)
        self.assertIn("exit_code=0", output)
        self.assertIn("qz-ok", output)


class LiveCommandTests(unittest.TestCase):
    def test_simple_command_returns_expected_stdout(self):
        command = "Write-Output hello" if os.name == "nt" else "echo hello"
        with tempfile.TemporaryDirectory() as workspace:
            result = SandboxManager().execute(command, workspace, timeout=30)
        self.assertFalse(result.timed_out, result.error)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)

    def test_child_process_does_not_inherit_api_keys(self):
        command = "Write-Output \"[$env:GROQ_KEY_1]\"" if os.name == "nt" else 'echo "[$GROQ_KEY_1]"'
        with tempfile.TemporaryDirectory() as workspace, patch.dict(os.environ, {"GROQ_KEY_1": "gsk_must_not_leak"}):
            result = HostBackend().run(command, workspace, timeout=30)
        self.assertNotIn("gsk_must_not_leak", result.stdout)
        self.assertIn("[]", result.stdout)

    def test_failed_statement_is_not_masked_by_later_success(self):
        command = "python -c \"import sys; sys.exit(3)\"; echo after"
        args = _prepare_host_command(command, is_windows=(os.name == "nt"))
        self.assertGreater(len(args), 1)
        with tempfile.TemporaryDirectory() as workspace:
            result = HostBackend().run(command, workspace, timeout=60)
        self.assertEqual(result.exit_code, 3, result.stderr)

    def test_missing_workspace_is_reported_not_raised(self):
        result = HostBackend().run("echo hi", os.path.join(tempfile.gettempdir(), "qz-does-not-exist-123"), timeout=5)
        self.assertEqual(result.exit_code, -1)
        self.assertIn("not a directory", result.error)


class TimeoutTests(unittest.TestCase):
    def test_host_backend_times_out_a_long_sleep(self):
        command = "Start-Sleep -Seconds 20" if os.name == "nt" else "sleep 20"
        with tempfile.TemporaryDirectory() as workspace:
            result = HostBackend().run(command, workspace, timeout=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, -1)


if __name__ == "__main__":
    unittest.main()
