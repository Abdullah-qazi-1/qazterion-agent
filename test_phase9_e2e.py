import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qz_agent
import qz_tools


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none=True):
        result = {"role": self.role}
        if self.content is not None:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        return result


def response(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage(content, tool_calls))])


def tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


class Phase9EndToEndTests(unittest.TestCase):
    def test_real_ten_file_project_runs_index_to_review_and_git_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "src"
            tests = workspace / "tests"
            source.mkdir()
            tests.mkdir()
            (source / "__init__.py").write_text("", encoding="utf-8")
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (source / "calculator.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8"
            )
            for name in ("cache", "config", "formatters", "models", "parsers", "service", "validators"):
                (source / f"{name}.py").write_text(
                    f"def {name}_status():\n    return '{name}'\n", encoding="utf-8"
                )
            (tests / "test_calculator.py").write_text(
                "import unittest\n\nfrom src.calculator import multiply\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_multiply_positive_numbers(self):\n"
                "        self.assertEqual(multiply(6, 7), 42)\n",
                encoding="utf-8",
            )

            executor_responses = iter([
                response(tool_calls=[tool_call("call-1", "search_index", {"query": "calculator multiply", "limit": 3})]),
                response(tool_calls=[tool_call("call-2", "read_file", {"path": "src/calculator.py"})]),
                response(tool_calls=[tool_call("call-3", "apply_patch", {
                    "path": "src/calculator.py",
                    "diff": "@@ -1,2 +1,6 @@\n def add(a, b):\n     return a + b\n+\n+\n+def multiply(a, b):\n+    return a * b\n",
                })]),
                response(tool_calls=[tool_call("call-4", "run_command", {
                    "command": "python -m unittest discover -s tests",
                    "timeout": 60,
                })]),
                response("Implemented multiply(), ran the tests, and committed the verified change."),
            ])
            def fake_create(*, model, messages, **kwargs):
                system_content = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""

                # Phase 11: classify_task_mode also calls the "classify" alias — route by
                # system-prompt content so this fixture still answers the Phase 2 executor
                # complexity classifier ("simple") and the Phase 11 mode classifier
                # ("standard") independently, without relying on call order.
                if model == "classify" and "one mode: quick, standard, or complex" in system_content:
                    return response("standard")
                if model == "classify":
                    return response("simple")

                # Phase 11: generate_clarifying_questions also uses the "planner" alias —
                # keep this deterministic fixture unambiguous so it never pauses for input.
                if model == "planner" and "ambiguity before planning" in system_content:
                    return response("NONE")
                if model == "planner" and "planning assistant" in system_content:
                    return response("1. Inspect calculator\n2. Add multiply\n3. Run tests")
                if model == "planner":
                    return response("src/calculator.py")

                if model == "reasoner":
                    return response("NO_ISSUES")
                if model == "groq-fast" and "Git commit subject" in messages[0]["content"]:
                    return response("Add calculator multiply function")
                if model == "groq-fast":
                    return next(executor_responses)
                raise AssertionError(f"Unexpected model call: {model}")

            with (
                patch.object(qz_tools, "WORKSPACE", str(workspace)),
                patch.object(qz_agent, "WORKSPACE", str(workspace)),
                patch.object(qz_agent.client.chat.completions, "create", side_effect=fake_create),
            ):
                qz_agent.run_task("Add a multiply function to the calculator and test it.")

            calculator = (source / "calculator.py").read_text(encoding="utf-8")
            self.assertIn("def multiply(a, b):", calculator)
            with patch.object(qz_tools, "WORKSPACE", str(workspace)):
                self.assertEqual(qz_tools.run_command("python -m unittest discover -s tests").splitlines()[0], "exit_code=0")
                git_log = qz_tools._run_git(["log", "--oneline", "-1"])
            self.assertEqual(git_log.returncode, 0)
            self.assertIn("Add calculator multiply function", git_log.stdout)
            self.assertFalse((workspace / ".qazterion" / "index.json").exists())


if __name__ == "__main__":
    unittest.main()
