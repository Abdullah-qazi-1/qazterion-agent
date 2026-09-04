import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qz_sandbox.backend import (
    DockerExecutionBackend,
    RestrictedHostBackend,
    build_docker_run_args,
)
from qz_sandbox.manager import NOT_TRANSLATABLE, SandboxManager, probe_docker


class SandboxManagerTests(unittest.TestCase):
    def test_falls_back_to_restricted_host_when_docker_info_fails(self):
        with patch("qz_sandbox.manager.subprocess.run") as run:
            run.side_effect = FileNotFoundError("docker")
            manager = SandboxManager()
        self.assertFalse(manager.docker_available)
        self.assertIsInstance(manager.backend, RestrictedHostBackend)
        self.assertEqual(manager.backend_name, "UNSANDBOXED")

        failed_info = SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect")
        with patch("qz_sandbox.manager.subprocess.run", return_value=failed_info):
            manager = SandboxManager()
        self.assertFalse(manager.docker_available)
        self.assertIsInstance(manager.backend, RestrictedHostBackend)

    def test_selects_docker_backend_when_docker_info_succeeds(self):
        ok = SimpleNamespace(returncode=0, stdout="Server Version: 29.0", stderr="")
        with patch("qz_sandbox.manager.subprocess.run", return_value=ok):
            manager = SandboxManager()
        self.assertTrue(manager.docker_available)
        self.assertIsInstance(manager.backend, DockerExecutionBackend)

    def test_translatable_command_runs_via_docker_sh_c(self):
        manager = SandboxManager(docker_available=True)
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        with tempfile.TemporaryDirectory() as workspace:
            with patch("qz_sandbox.backend.subprocess.run", return_value=completed) as run:
                result = manager.execute("python -m pytest", workspace, timeout=10)
        self.assertEqual(result.isolation, "docker")
        docker_run = next(call.args[0] for call in run.call_args_list if call.args[0][:2] == ["docker", "run"])
        self.assertEqual(docker_run[-3:], ["/bin/sh", "-c", "python -m pytest"])
        self.assertNotIn("powershell.exe", docker_run)

    def test_non_translatable_command_falls_back_with_auditable_reason(self):
        manager = SandboxManager(docker_available=True)
        host_result = SimpleNamespace(returncode=0, stdout="hello\n", stderr="")
        with tempfile.TemporaryDirectory() as workspace:
            with patch("qz_sandbox.backend.subprocess.run", return_value=host_result) as run:
                with self.assertLogs("qz_sandbox", level="WARNING") as logged:
                    result = manager.execute("Write-Output hello", workspace, timeout=10)
        self.assertEqual(result.isolation, "UNSANDBOXED")
        self.assertEqual(result.fallback_reason, NOT_TRANSLATABLE)
        self.assertTrue(any(NOT_TRANSLATABLE in message for message in logged.output))
        self.assertEqual(run.call_args.args[0][0], "powershell.exe" if os.name == "nt" else "/bin/sh")
        docker_invoked = any(
            call.args and call.args[0] and call.args[0][0] == "docker"
            for call in run.call_args_list
        )
        self.assertFalse(docker_invoked)


class LiveCommandTests(unittest.TestCase):
    def test_simple_command_returns_expected_stdout(self):
        manager = SandboxManager(docker_available=False)
        command = "Write-Output hello" if os.name == "nt" else "echo hello"
        with tempfile.TemporaryDirectory() as workspace:
            result = manager.execute(command, workspace, timeout=30)
        self.assertFalse(result.timed_out, result.error)
        self.assertEqual(result.isolation, "UNSANDBOXED")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)


    def test_git_and_npm_succeed_inside_the_container(self):
        if not probe_docker():
            self.skipTest("Docker daemon is not available")
        backend = DockerExecutionBackend()
        with tempfile.TemporaryDirectory() as workspace:
            git = backend.run("git --version", workspace, timeout=180)
            npm = backend.run("npm --version", workspace, timeout=180)
        self.assertEqual(git.exit_code, 0, git.error or git.stderr)
        self.assertIn("git version", (git.stdout + git.stderr).lower())
        self.assertEqual(npm.exit_code, 0, npm.error or npm.stderr)
        self.assertTrue((npm.stdout or npm.stderr).strip())


class DockerArgsTests(unittest.TestCase):
    def test_network_disabled_by_default(self):
        args = build_docker_run_args("echo hello", "/tmp/workspace")
        network_index = args.index("--network")
        self.assertEqual(args[network_index + 1], "none")

        bridged = build_docker_run_args("echo hello", "/tmp/workspace", allow_network=True)
        self.assertEqual(bridged[bridged.index("--network") + 1], "bridge")

    def test_mount_is_workspace_only_and_excludes_docker_socket(self):
        workspace = str(Path(tempfile.gettempdir()).resolve() / "qz-sandbox-ws")
        args = build_docker_run_args("pytest", workspace, container_name="qz-sandbox-test")
        self.assertEqual(args[0], "docker")
        volume_flags = [args[i + 1] for i, token in enumerate(args) if token == "-v"]
        self.assertEqual(len(volume_flags), 1)
        volume = volume_flags[0]
        suffix = ":/workspace:rw"
        self.assertTrue(volume.endswith(suffix), volume)
        host_path = volume[: -len(suffix)]
        self.assertEqual(Path(host_path), Path(workspace))
        joined = " ".join(args).lower()
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("docker_engine", joined)
        self.assertNotIn("/var/run/docker.sock", joined)
        self.assertIn("--cpus", args)
        self.assertIn("512m", args)
        self.assertEqual(args[args.index("--user") + 1], "1000:1000")
        self.assertEqual(args.count("-v"), 1)

    def test_docker_run_invokes_constructed_args_and_removes_container(self):
        backend = DockerExecutionBackend()
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        with tempfile.TemporaryDirectory() as workspace:
            with patch("qz_sandbox.backend.subprocess.run", return_value=completed) as run:
                result = backend.run("echo ok", workspace, timeout=10)
        self.assertEqual(result.isolation, "docker")
        self.assertEqual(result.stdout, "ok\n")
        docker_run = next(call.args[0] for call in run.call_args_list if call.args[0][:2] == ["docker", "run"])
        self.assertEqual(docker_run[0], "docker")
        self.assertEqual(docker_run[1], "run")
        self.assertIn("qazterion-sandbox:1", docker_run)
        self.assertEqual(docker_run[docker_run.index("--network") + 1], "none")
        self.assertTrue(any(call.args[0][:3] == ["docker", "rm", "-f"] for call in run.call_args_list))


class TimeoutTests(unittest.TestCase):
    def test_host_backend_times_out_a_long_sleep(self):
        backend = RestrictedHostBackend()
        command = "Start-Sleep -Seconds 20" if os.name == "nt" else "sleep 20"
        with tempfile.TemporaryDirectory() as workspace:
            result = backend.run(command, workspace, timeout=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.isolation, "UNSANDBOXED")

    def test_docker_backend_kills_container_on_timeout(self):
        backend = DockerExecutionBackend()

        def fake_run(args, **kwargs):
            if args[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(cmd=args, timeout=1, output=b"", stderr=b"")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as workspace:
            with patch("qz_sandbox.backend.subprocess.run", side_effect=fake_run) as run:
                result = backend.run("sleep 30", workspace, timeout=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.isolation, "docker")
        rm_calls = [call.args[0] for call in run.call_args_list if call.args[0][:3] == ["docker", "rm", "-f"]]
        self.assertTrue(rm_calls)


class ProbeTests(unittest.TestCase):
    def test_probe_docker_false_on_missing_binary(self):
        with patch("qz_sandbox.manager.subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(probe_docker())


if __name__ == "__main__":
    unittest.main()
