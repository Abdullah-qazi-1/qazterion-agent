"""End-to-end: a task goes through the real Qazterion pipeline and real gateway.

Everything is real (task DB, classifier, planner, DAG executor, tools, security
gateway, host command execution, validation, git commits, key rotation and
health tracking) except the provider's HTTP endpoint, which is replaced by a
scripted adapter so the test is deterministic and offline. The first API key
is rate limited, so the run also proves key rotation inside a real task.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from openai.types.chat import ChatCompletion

from qz_core.autonomous_loop import AutonomousRunner
from qz_providers import exceptions as px
from qz_providers.catalog import ProviderCatalog
from qz_providers.gateway import ModelGateway, set_gateway
from qz_providers.health import HealthTracker
from qz_providers.keys import KeySource
from qz_tasks import task_manager
from qz_usage_tracker import UsageTracker

CATALOG = """
providers:
  alpha:
    base_url: https://alpha.example/v1
    key_prefix: ALPHA_KEY
    models:
      alpha-coder: {context_window: 128000, tools: true}
roles:
  coder: [alpha/alpha-coder]
  fast: [alpha/alpha-coder]
  reasoner: [alpha/alpha-coder]
  planner: [alpha/alpha-coder]
  classify: [alpha/alpha-coder]
"""

NEW_CALC = "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
NEW_TEST = (
    "import unittest\n\nfrom calc import add, multiply\n\n\n"
    "class CalcTests(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_multiply(self):\n        self.assertEqual(multiply(6, 7), 42)\n"
)


def completion(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(tool_calls)
        ]
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "alpha-coder",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    })


class ScriptedProvider:
    """Answers like a well-behaved model, keyed on what each pipeline step asks for."""

    def __init__(self):
        self.calls = []
        self.rate_limited_once = False

    def factory(self, spec):
        provider = self

        class Adapter:
            def __init__(self):
                self.spec = spec

            def complete(self, *, api_key, model, messages, tools=None, **_kw):
                provider.calls.append(api_key)
                if api_key == "alpha-key-1" and not provider.rate_limited_once:
                    provider.rate_limited_once = True
                    raise px.RateLimitError("429 Rate limit reached for requests", retry_after=60)
                return provider.respond(messages, tools)

            def list_models(self, **_kw):
                return []

            def close(self):
                pass

        return Adapter()

    def respond(self, messages, tools):
        system = str(messages[0].get("content", ""))
        if tools:
            tool_results = [m for m in messages if m.get("role") == "tool"]
            if not tool_results:
                return completion(tool_calls=[
                    ("read_file", {"path": "calc.py"}),
                ])
            if len(tool_results) == 1:
                return completion(tool_calls=[
                    ("write_file", {"path": "calc.py", "content": NEW_CALC}),
                    ("write_file", {"path": "tests/test_calc.py", "content": NEW_TEST}),
                ])
            if len(tool_results) == 3:
                return completion(tool_calls=[("run_command", {"command": "python -m unittest discover -s tests"})])
            return completion("Added multiply() to calc.py with tests; all tests pass.")
        if "task-complexity classifier" in system:
            return completion("simple")
        if "Classify this coding task" in system:
            return completion("standard")
        if "ambiguity" in system:
            return completion("NONE")
        if "planning assistant" in system:
            return completion("1. Add multiply to calc.py\n2. Add a unit test\n3. Run the tests")
        if "software architect" in system:
            return completion("calc.py (modified)\ntests/test_calc.py (modified)")
        if "project planner" in system:
            return completion(json.dumps([{"id": 0, "title": "Add multiply with tests",
                                           "description": "Implement multiply(a, b) and test it.", "depends_on": []}]))
        if "Git commit subject" in system:
            return completion("Add multiply function with tests")
        if "code reviewer" in system:
            return completion("NO_ISSUES")
        return completion("ok")


def git(ws, *args):
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=False).stdout.strip()


class WorkflowEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "project"
        (self.ws / "tests").mkdir(parents=True)
        (self.ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.ws / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.ws / "tests" / "test_calc.py").write_text(
            "import unittest\n\nfrom calc import add\n\n\nclass CalcTests(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
            encoding="utf-8",
        )
        git(self.ws, "init")
        git(self.ws, "config", "user.name", "Real User")
        git(self.ws, "config", "user.email", "user@example.com")
        git(self.ws, "add", ".")
        git(self.ws, "commit", "-m", "initial")

        user = root / "providers.yaml"
        user.write_text(CATALOG, encoding="utf-8")
        empty = root / "empty.yaml"
        empty.write_text("{}", encoding="utf-8")
        catalog = ProviderCatalog(default_path=empty, user_path=user)
        self.provider = ScriptedProvider()
        self.usage = UsageTracker(log_path=root / "usage.jsonl")
        self.health = HealthTracker(path=root / "health.json")
        set_gateway(ModelGateway(
            catalog=catalog,
            keys=KeySource(catalog, keystore_factory=None, environ={"ALPHA_KEY_1": "alpha-key-1", "ALPHA_KEY_2": "alpha-key-2"}),
            health=self.health,
            usage_tracker=self.usage,
            adapter_factory=self.provider.factory,
            sleep=lambda _s: None,
            log=lambda _m: None,
        ))

    def tearDown(self):
        set_gateway(None)
        self.tmp.cleanup()

    def test_task_runs_from_prompt_to_verified_commit(self):
        events = []
        task_manager.subscribe_events(lambda tid, kind, payload, ts: events.append(kind))
        try:
            runner = AutonomousRunner(workspace=self.ws, approval_policy="never")
            result = runner.run_task("Add a multiply function to calc.py and test it.")
        finally:
            task_manager._global_event_subscribers.clear()

        self.assertEqual(result["status"], "completed", result)
        self.assertIn("multiply", result["result"])

        # Code changed and verified by the real test command.
        self.assertIn("def multiply(a, b):", (self.ws / "calc.py").read_text(encoding="utf-8"))
        unit = subprocess.run(["python", "-m", "unittest", "discover", "-s", "tests"], cwd=self.ws,
                              capture_output=True, text=True)
        self.assertEqual(unit.returncode, 0, unit.stderr)

        # One agent commit with the Qazterion trailer, and a checkpoint for it.
        self.assertEqual(git(self.ws, "log", "-1", "--format=%s"), "Add multiply function with tests")
        self.assertIn("Committed-by: Qazterion", git(self.ws, "log", "-1", "--format=%B"))
        self.assertEqual(git(self.ws, "status", "--porcelain", "--untracked-files=no"), "")
        checkpoint = task_manager.get_latest_checkpoint(result["task_id"])
        self.assertEqual(checkpoint["git_commit_hash"], git(self.ws, "rev-parse", "--short", "HEAD"))

        # Key rotation happened inside the real run and was remembered.
        self.assertEqual(self.provider.calls[0], "alpha-key-1")
        self.assertTrue(all(key == "alpha-key-2" for key in self.provider.calls[1:3]))
        self.assertFalse(self.health.key_available("ALPHA_KEY_1"))
        usage = self.usage.get_task_usage(result["task_id"])
        self.assertGreater(usage["success_count"], 3)
        self.assertEqual(usage["error_count"], 1)

        for expected in ("TASK_CLASSIFIED", "PLAN_CREATED", "NODE_STARTED", "FILE_EDITED", "TOOL_FINISHED",
                         "CHECKPOINT_CREATED", "NODE_COMPLETED", "DAG_COMPLETED"):
            self.assertIn(expected, events)
        self.assertEqual(task_manager.get_task(result["task_id"])["status"], "COMPLETED")
        # Nothing generated by Qazterion was left inside the project.
        self.assertFalse((self.ws / ".qazterion_index.json").exists())
        self.assertFalse((self.ws / ".qazterion").exists())


if __name__ == "__main__":
    unittest.main()
