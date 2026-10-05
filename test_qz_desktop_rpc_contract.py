"""Desktop RPC contract: every method, strict JSON, error propagation, and no secret leakage.

The Electron front-end is not part of this repository, so these tests pin the
wire contract it relies on: method names, reply framing, result keys that
earlier builds returned, and the guarantee that API keys never leave the backend.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qz_desktop_bridge as bridge
from qz_desktop_backend import DesktopBackend
from qz_keystore import KeyStore
from qz_providers.catalog import ProviderCatalog
from qz_providers.gateway import ModelGateway
from qz_providers.health import HealthTracker
from qz_providers.keys import KeySource
from qz_tasks import task_manager
from qz_usage_tracker import UsageTracker

SECRET = "gsk_contract_secret_value_9f8e7d6c5b4a"
ROOT = Path(__file__).resolve().parent


class _Adapter:
    def __init__(self, spec):
        self.spec = spec

    def complete(self, **kwargs):
        msg = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None, model=kwargs["model"])

    def list_models(self, **_kw):
        return [{"id": "discovered-model", "context_window": 32000, "tools": True}]

    def check_key(self, **_kw):
        return 3

    def close(self):
        pass


class RpcContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = (root / "project").resolve()
        self.ws.mkdir()
        (self.ws / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.ws, check=True)
        subprocess.run(["git", "-c", "user.name=U", "-c", "user.email=u@example.com", "add", "."], cwd=self.ws, check=True)
        subprocess.run(["git", "-c", "user.name=U", "-c", "user.email=u@example.com", "commit", "-qm", "init"], cwd=self.ws, check=True)
        self.keystore = KeyStore(path=root / "keystore.dat", backend="fernet")
        catalog = ProviderCatalog(user_path=root / "providers.yaml")
        gateway = ModelGateway(
            catalog=catalog,
            keys=KeySource(catalog, keystore_factory=lambda: self.keystore, environ={}, cache_ttl_s=0),
            health=HealthTracker(persist=False), adapter_factory=_Adapter,
            sleep=lambda _s: None, log=lambda _m: None,
        )
        self.backend = DesktopBackend(keystore=self.keystore, usage_tracker=UsageTracker(log_path=None), gateway=gateway)
        self.task_id = task_manager.create_task("demo", str(self.ws))
        task_manager.create_subtasks(self.task_id, [{"id": 0, "title": "t", "description": "d", "depends_on": []}])

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, params=None, raw=None):
        out = io.StringIO()
        line = raw if raw is not None else json.dumps({"id": 7, "method": method, "params": params or {}})
        with patch.object(bridge, "_PROTOCOL_OUT", out):
            bridge._handle_request(self.backend, line)
        text = out.getvalue()
        self.assertNotIn(SECRET, text)
        # Task events (__QZ_EVENT__ lines) may precede the reply; exactly one reply line.
        replies = [line for line in text.splitlines() if line.strip() and not line.startswith("__QZ_")]
        self.assertEqual(len(replies), 1, text)
        for line in text.splitlines():
            if line.startswith("__QZ_EVENT__"):
                json.loads(line[len("__QZ_EVENT__"):])
        return json.loads(replies[0], parse_constant=lambda c: self.fail(f"non-JSON constant {c}"))

    def test_every_method_replies_with_strict_json_and_never_leaks_keys(self):
        ws = str(self.ws)
        calls = [
            ("add_provider_key", {"provider": "groq", "index": 1, "value": SECRET}),
            ("status", {}), ("first_run_setup", {"providers": {"gemini": "AIza_contract_other_key_123"}}),
            ("generate_configuration", {}), ("validate_configuration", {}), ("apply_and_start", {}),
            ("set_routing_strategy", {"strategy": "least-busy"}), ("start", {}), ("stop", {}), ("get_usage", {}),
            ("import_env_keys", {"workspace": ws}), ("get_diffs", {"workspace": ws}),
            ("approve_changes", {"workspace": ws, "taskId": self.task_id, "files": ["app.py"]}),
            ("check_for_updates", {}), ("get_task_subtasks", {"taskId": self.task_id}),
            ("get_ready_subtasks", {"taskId": self.task_id}),
            ("getResumeOptions", {"taskId": self.task_id, "workspace": ws}),
            ("get_providers", {}), ("set_provider_enabled", {"provider": "mistral", "enabled": False}),
            ("get_models", {}), ("refresh_models", {"provider": "groq"}),
            ("set_preferred_model", {"alias": "coder-strong", "provider": "groq", "modelId": "discovered-model"}),
            ("register_custom_provider", {"provider": "acme", "value": "acme_contract_key_456", "baseUrl": "https://acme.example/v1", "defaultModel": "acme-1"}),
            ("set_key_enabled", {"provider": "groq", "index": 1, "enabled": True}),
            ("get_usage_metrics", {}), ("get_usage_metrics", {"taskId": self.task_id}),
            ("get_intelligence", {}), ("get_benchmark_report", {}),
            ("preview_rollback", {"taskId": self.task_id, "workspace": ws}),
            ("get_project_rules", {"workspace": ws}), ("get_context_summary", {"query": "app", "workspace": ws}),
            ("check_system_health", {"workspace": ws}),
            ("test_provider_connectivity", {"provider": "groq", "key": SECRET}),
            ("diagnostics", {}), ("cancel_task", {"taskId": self.task_id}),
            ("delete_provider_key", {"provider": "groq", "index": 1}),
        ]
        for method, params in calls:
            with self.subTest(method=method):
                reply = self.call(method, params)
                self.assertEqual(reply["id"], 7)
                self.assertTrue(reply["ok"], reply.get("error"))

    def test_results_keep_the_keys_earlier_desktop_builds_used(self):
        self.call("add_provider_key", {"provider": "groq", "index": 1, "value": SECRET})
        status = self.call("status")["result"]
        for key in ("proxyStatus", "model", "connected", "aliases", "configuredProviders", "interruptedTasks"):
            self.assertIn(key, status)
        self.assertEqual(status["configuredProviders"]["groq"]["count"], 1)
        provider = self.call("get_providers")["result"][0]
        for key in ("provider_id", "display_name", "enabled", "adapter_type", "base_url", "default_concurrency",
                    "supported_capabilities", "models", "key_count", "configured", "custom", "default_model"):
            self.assertIn(key, provider)
        model = self.call("get_models")["result"][0]
        for key in ("provider", "model_id", "display_name", "capabilities", "context_window", "supports_tools",
                    "supports_vision", "supports_streaming", "supports_reasoning", "state", "is_configured"):
            self.assertIn(key, model)
        self.assertIn(model["state"], ("active", "degraded", "unavailable", "unconfigured"))
        self.assertEqual(self.call("set_routing_strategy", {"strategy": "least-busy"})["result"]["strategy"], "least-busy")
        report = self.call("get_benchmark_report")["result"]
        for key in ("total_tasks", "completed_tasks", "task_success_rate", "total_nodes", "node_success_rate",
                    "repair_attempts", "successful_repairs", "total_tokens", "total_cost", "failure_categories",
                    "models", "validation_pass_rates"):
            self.assertIn(key, report)
        self.assertGreaterEqual(report["total_tasks"], 1)
        intel = self.call("get_intelligence")["result"]
        for key in ("evidence", "best_overall", "fastest", "degraded_models", "pool", "providers", "models", "keys", "quota_note"):
            self.assertIn(key, intel)
        health = self.call("check_system_health", {"workspace": str(self.ws)})["result"]
        self.assertFalse(health["components"]["docker"]["metadata"]["required"])
        conn = self.call("test_provider_connectivity", {"provider": "groq", "key": SECRET})["result"]
        for key in ("provider", "connected", "status", "message", "models_found", "latency_ms", "masked_key"):
            self.assertIn(key, conn)

    def test_errors_are_returned_not_raised_and_are_redacted(self):
        bad_json = self.call(None, raw="{not json")
        self.assertFalse(bad_json["ok"])
        self.assertIsNone(bad_json["id"])
        unknown = self.call("no_such_method")
        self.assertFalse(unknown["ok"])
        self.assertIn("Unsupported bridge method", unknown["error"]["message"])
        with patch.object(self.backend, "status", side_effect=RuntimeError(f"boom with key {SECRET}")):
            failing = self.call("status")
        self.assertFalse(failing["ok"])
        self.assertIn("[REDACTED", failing["error"]["message"])
        self.assertEqual(self.call("get_enabled_env")["error"]["type"], "PermissionError")
        bom = self.call(None, raw="﻿" + json.dumps({"id": 9, "method": "check_for_updates", "params": {}}))
        self.assertTrue(bom["ok"])

    def test_non_finite_numbers_never_reach_the_ui(self):
        with patch.object(self.backend, "status", return_value={"proxy": {"healthy": True, "x": float("inf")},
                                                                 "aliases": [], "configured_providers": [], "routing": {}}):
            reply = self.call("status")
        self.assertTrue(reply["ok"])

    def test_approve_changes_is_validated_and_persisted_without_touching_git(self):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.ws, capture_output=True, text=True).stdout
        reply = self.call("approve_changes", {"workspace": str(self.ws), "taskId": self.task_id, "files": ["app.py", "./app.py"]})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["files"], ["app.py"])
        events = [e for e in task_manager.get_task_events(self.task_id) if e["event_type"] == "CHANGES_APPROVED"]
        self.assertEqual(events[-1]["payload"]["files"], ["app.py"])
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.ws, capture_output=True, text=True).stdout, head)
        outside = self.call("approve_changes", {"workspace": str(self.ws), "taskId": self.task_id, "files": ["..\\..\\secret.txt"]})
        self.assertFalse(outside["ok"])
        unknown = self.call("approve_changes", {"workspace": str(self.ws), "taskId": "nope", "files": []})
        self.assertFalse(unknown["ok"])


class RpcProcessTests(unittest.TestCase):
    def test_rpc_process_stdout_carries_only_protocol_lines(self):
        with tempfile.TemporaryDirectory() as data:
            env = {**os.environ, "QAZTERION_DATA_DIR": data, "QAZTERION_SKIP_DOTENV": "1",
                   "QAZTERION_TASKS_DB": str(Path(data) / "t.db")}
            requests = "\n".join([
                "﻿" + json.dumps({"id": 1, "method": "status", "params": {}}),
                json.dumps({"id": 2, "method": "add_provider_key", "params": {"provider": "groq", "index": 1, "value": SECRET}}),
                json.dumps({"id": 3, "method": "get_providers", "params": {}}),
                json.dumps({"id": 4, "method": "get_enabled_env", "params": {}}),
                "garbage",
            ]) + "\n"
            proc = subprocess.run([sys.executable, str(ROOT / "qz_desktop_bridge.py"), "--rpc"], input=requests,
                                  capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, cwd=data)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        replies = [json.loads(line) for line in lines]  # every stdout line must be protocol JSON
        self.assertEqual(sorted(str(r["id"]) for r in replies), ["1", "2", "3", "4", "None"])
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
