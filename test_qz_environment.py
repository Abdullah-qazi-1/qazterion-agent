import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qz_environment
import qz_tools


class ProjectEnvironmentTests(unittest.TestCase):
    def test_python_project_requires_approval_before_creating_a_venv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            environment = qz_environment.detect_project_environment(root)
            self.assertEqual(environment.project_type, "python")
            self.assertTrue(environment.can_create_venv)
            self.assertIn("Approval required", qz_environment.preparation_plan(environment))
            with patch("qz_environment.subprocess.run") as run:
                self.assertIn("Approval required", qz_environment.prepare_python_environment(environment))
                run.assert_not_called()

    def test_existing_venv_is_reused_and_commands_use_its_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            interpreter = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")
            environment = qz_environment.detect_project_environment(root)
            self.assertEqual(environment.python_interpreter, interpreter.resolve())
            self.assertIn(str(interpreter.resolve()), environment.test_command)
            from qz_sandbox.manager import SandboxManager

            with patch("qz_tools.get_manager", return_value=SandboxManager(docker_available=False)):
                with patch("qz_sandbox.backend.subprocess.run") as run:
                    run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
                    old = qz_tools._PROJECT_PYTHON
                    try:
                        qz_tools.configure_project_environment(root)
                        qz_tools.run_command("python -m pytest")
                        self.assertIn(str(interpreter.resolve()), run.call_args.args[0][-1])
                    finally:
                        qz_tools._PROJECT_PYTHON = old

    def test_existing_conda_or_docker_project_is_not_prepared(self):
        for marker in ("environment.yml", "Dockerfile"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
                (root / marker).write_text("", encoding="utf-8")
                environment = qz_environment.detect_project_environment(root)
                self.assertFalse(environment.can_create_venv)
                self.assertIn("Preparation skipped", qz_environment.preparation_plan(environment))

    def test_native_project_detection_selects_native_test_commands(self):
        cases = (("package.json", "node", "npm test"), ("Cargo.toml", "rust", "cargo test"), ("go.mod", "go", "go test ./..."), ("pom.xml", "java", "mvn test"), ("project.csproj", "dotnet", "dotnet test"))
        for marker, kind, command in cases:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / marker).write_text("", encoding="utf-8")
                environment = qz_environment.detect_project_environment(root)
                self.assertEqual((environment.project_type, environment.test_command), (kind, command))

    def test_approved_python_setup_uses_only_the_new_project_venv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            environment = qz_environment.detect_project_environment(root)
            complete = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch("qz_environment.subprocess.run", return_value=complete) as run:
                result = qz_environment.prepare_python_environment(environment, approved=True)
            self.assertIn("dependencies installed", result)
            self.assertEqual(run.call_args_list[0].args[0][:3], [qz_environment.sys.executable, "-m", "venv"])
            self.assertIn(".venv", Path(run.call_args_list[1].args[0][0]).parts)


if __name__ == "__main__":
    unittest.main()
