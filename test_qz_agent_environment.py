"""Tests for the phase 10 wiring: qz_agent.prepare_environment().

These cover the part of phase 10 that was previously missing — showing the
plan and only creating/installing a Python environment on explicit approval
or opt-in auto-setup, and never touching non-Python / tooled projects.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import qz_agent


def _fake_env(**overrides):
    base = dict(
        workspace="/fake/workspace",
        project_type="python",
        manager=None,
        python_interpreter=None,
        dependency_files=("requirements.txt",),
        protected_tools=(),
        test_command="python -m pytest",
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    ns.can_create_venv = ns.project_type == "python" and not ns.python_interpreter and not ns.protected_tools
    ns.summary = lambda: f"type={ns.project_type}"
    return ns


class PrepareEnvironmentTests(unittest.TestCase):
    def setUp(self):
        qz_agent._ENVIRONMENT_PREPARED = False

    def test_declines_without_approval_and_never_installs(self):
        env = _fake_env()
        with patch("qz_agent.detect_project_environment", return_value=env), \
             patch("qz_agent.preparation_plan", return_value="Approval required: create .venv"), \
             patch("qz_agent._ask_yes_no", return_value=False), \
             patch("qz_agent.prepare_python_environment") as prepare, \
             patch("qz_agent.configure_project_environment") as configure:
            qz_agent.prepare_environment(auto_setup=False)
            prepare.assert_not_called()
            configure.assert_called_once()

    def test_manual_approval_creates_environment(self):
        env = _fake_env()
        with patch("qz_agent.detect_project_environment", return_value=env), \
             patch("qz_agent.preparation_plan", return_value="Approval required: create .venv"), \
             patch("qz_agent._ask_yes_no", return_value=True), \
             patch("qz_agent.prepare_python_environment", return_value="Environment created and dependencies installed") as prepare, \
             patch("qz_agent.configure_project_environment") as configure:
            qz_agent.prepare_environment(auto_setup=False)
            prepare.assert_called_once_with(env, approved=True)
            configure.assert_called_once()

    def test_auto_setup_skips_the_prompt(self):
        env = _fake_env()
        with patch("qz_agent.detect_project_environment", return_value=env), \
             patch("qz_agent.preparation_plan", return_value="Approval required: create .venv"), \
             patch("qz_agent._ask_yes_no") as ask, \
             patch("qz_agent.prepare_python_environment", return_value="Environment created") as prepare, \
             patch("qz_agent.configure_project_environment"):
            qz_agent.prepare_environment(auto_setup=True)
            ask.assert_not_called()
            prepare.assert_called_once_with(env, approved=True)

    def test_existing_venv_is_reused_without_prompting(self):
        env = _fake_env(python_interpreter="/fake/workspace/.venv/bin/python")
        with patch("qz_agent.detect_project_environment", return_value=env), \
             patch("qz_agent._ask_yes_no") as ask, \
             patch("qz_agent.prepare_python_environment") as prepare, \
             patch("qz_agent.configure_project_environment") as configure:
            qz_agent.prepare_environment(auto_setup=False)
            ask.assert_not_called()
            prepare.assert_not_called()
            configure.assert_called_once()

    def test_protected_tooling_is_never_touched(self):
        env = _fake_env(protected_tools=("docker",))
        with patch("qz_agent.detect_project_environment", return_value=env), \
             patch("qz_agent.preparation_plan", return_value="Preparation skipped: existing project tooling detected (docker)."), \
             patch("qz_agent._ask_yes_no") as ask, \
             patch("qz_agent.prepare_python_environment") as prepare, \
             patch("qz_agent.configure_project_environment"):
            qz_agent.prepare_environment(auto_setup=True)
            ask.assert_not_called()
            prepare.assert_not_called()

    def test_native_project_is_reported_and_never_prepared(self):
        env = _fake_env(project_type="node", manager="npm", python_interpreter=None,
                         dependency_files=(), test_command="npm test")
        with patch("qz_agent.detect_project_environment", return_value=env), \
             patch("qz_agent.prepare_python_environment") as prepare, \
             patch("qz_agent.configure_project_environment") as configure:
            qz_agent.prepare_environment(auto_setup=True)
            prepare.assert_not_called()
            configure.assert_called_once()

    def test_runs_only_once_per_process(self):
        env = _fake_env(python_interpreter="/fake/workspace/.venv/bin/python")
        with patch("qz_agent.detect_project_environment", return_value=env) as detect, \
             patch("qz_agent.configure_project_environment"):
            qz_agent.prepare_environment(auto_setup=False)
            qz_agent.prepare_environment(auto_setup=False)
            detect.assert_called_once()


if __name__ == "__main__":
    unittest.main()