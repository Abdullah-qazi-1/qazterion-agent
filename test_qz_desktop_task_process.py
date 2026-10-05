"""Desktop task mode end to end in a real child process over real pipes (Windows-safe).

The bridge runs `qz_desktop_bridge.py --workspace ... --task ...` exactly as the
desktop app launches it. A local OpenAI-compatible HTTP server (stdlib only)
plays the model provider, configured through providers.yaml + an env key, so the
whole stack runs: catalog, keys, gateway, adapter HTTP, planner, plan pause on
stdout, approval on stdin, tools, tests, git commit and __QZ_EVENT__ streaming.
The task text and approval include non-ASCII characters to prove UTF-8 pipes.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

NEW_CALC = "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
NEW_TEST = (
    "import unittest\n\nfrom calc import add, multiply\n\n\nclass T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_multiply(self):\n        self.assertEqual(multiply(6, 7), 42)\n"
)


def _reply(body):
    messages, tools = body["messages"], body.get("tools")
    system = str(messages[0].get("content", ""))
    content, calls = "ok", None
    if tools:
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            calls = [("read_file", {"path": "calc.py"}), ("read_file", {"path": "tests/test_calc.py"})]
        elif len(tool_msgs) == 2:
            calls = [("write_file", {"path": "calc.py", "content": NEW_CALC}),
                     ("write_file", {"path": "tests/test_calc.py", "content": NEW_TEST})]
        elif len(tool_msgs) == 4:
            calls = [("run_command", {"command": "python -m unittest discover -s tests"})]
        else:
            content = "Added multiply() — tests pass ✓"
    elif "task-complexity classifier" in system:
        content = "simple"
    elif "Classify this coding task" in system:
        content = "standard"
    elif "ambiguity" in system:
        content = "NONE"
    elif "planning assistant" in system:
        content = "1. Add multiply to calc.py\n2. Test it"
    elif "software architect" in system:
        content = "calc.py\ntests/test_calc.py"
    elif "project planner" in system:
        content = json.dumps([{"id": 0, "title": "Add multiply", "description": "Implement and test multiply", "depends_on": []}])
    elif "Git commit subject" in system:
        content = "Add multiply function"
    elif "code reviewer" in system:
        content = "NO_ISSUES"
    message = {"role": "assistant", "content": None if calls else content}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                                 for i, (n, a) in enumerate(calls)]
    return {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class _Provider(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = json.dumps(_reply(body)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def git(ws, *args):
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=False).stdout.strip()


class DesktopTaskProcessTests(unittest.TestCase):
    def test_task_mode_plan_pause_approval_and_completion(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ws = root / "projekt-ü"
                (ws / "tests").mkdir(parents=True)
                (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
                (ws / "tests" / "__init__.py").write_text("", encoding="utf-8")
                (ws / "tests" / "test_calc.py").write_text(
                    "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8")
                git(ws, "init")
                git(ws, "config", "user.name", "U")
                git(ws, "config", "user.email", "u@example.com")
                git(ws, "add", ".")
                git(ws, "commit", "-m", "init")
                data = root / "data"
                data.mkdir()
                port = server.server_address[1]
                (data / "providers.yaml").write_text(
                    "providers:\n  local:\n    base_url: http://127.0.0.1:%d/v1\n    key_prefix: LOCAL_KEY\n"
                    "    models:\n      m1: {}\nroles:\n" % port
                    + "".join(f"  {r}: [local/m1]\n" for r in ("coder", "fast", "planner", "reasoner", "classify")),
                    encoding="utf-8")
                env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY",)) and "_KEY_" not in k}
                env.update(QAZTERION_DATA_DIR=str(data), QAZTERION_SKIP_DOTENV="1", LOCAL_KEY_1="local-test-key",
                           QAZTERION_TASKS_DB=str(data / "tasks.db"), PYTHONIOENCODING="")
                proc = subprocess.Popen(
                    [sys.executable, str(ROOT / "qz_desktop_bridge.py"), "--workspace", str(ws),
                     "--task", "Add multiply() — with a test ✓", "--approval", "always", "--mode", "standard"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(root),
                )
                events, plan, output, err_chunks = [], None, [], []
                # Drain stderr concurrently (a full pipe would block the child) and
                # kill the child if it ever hangs, so a failure is reported, not a hang.
                threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True).start()
                watchdog = threading.Timer(150, proc.kill)
                watchdog.start()
                for raw in proc.stdout:
                    line = raw.decode("utf-8")
                    if line.startswith("__QZ_PLAN__"):
                        plan = json.loads(line[len("__QZ_PLAN__"):])
                        proc.stdin.write((json.dumps({"decision": "approve", "note": "ok ✓"}) + "\n").encode("utf-8"))
                        proc.stdin.flush()
                    elif line.startswith("__QZ_EVENT__"):
                        events.append(json.loads(line[len("__QZ_EVENT__"):])["eventType"])
                    else:
                        output.append(line)
                proc.wait(timeout=30)
                watchdog.cancel()
                stderr = b"".join(err_chunks).decode("utf-8", "replace")

                self.assertEqual(proc.returncode, 0, stderr[-2000:])
                self.assertIsNotNone(plan, "".join(output)[-2000:])
                self.assertIn("multiply", plan["plan"])
                self.assertIn("WAITING_APPROVAL", events)
                self.assertIn("APPROVAL_RECEIVED", events)
                self.assertIn("NODE_COMPLETED", events)
                self.assertIn("def multiply", (ws / "calc.py").read_text(encoding="utf-8"))
                self.assertIn("Committed-by: Qazterion", git(ws, "log", "-1", "--format=%B"))
                self.assertIn("tests pass ✓", "".join(output))
                self.assertNotIn("local-test-key", "".join(output) + stderr)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
