"""OpenAI-compatible adapter and key check against a real local HTTP server.

The server is the standard-library http.server running in a thread on
127.0.0.1, so the actual wire protocol (URL, auth header, JSON body, status
codes, Retry-After) is exercised without any external network access.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qz_providers import exceptions as px
from qz_providers.adapters import create_adapter
from qz_providers.catalog import ProviderSpec
from qz_providers.connectivity import test_provider_connectivity
from qz_providers.gateway import ModelGateway
from qz_providers.health import HealthTracker
from qz_providers.keys import KeySource
from qz_providers.catalog import ProviderCatalog

VALID_KEYS = {"good-key-1111", "good-key-2222"}


class FakeProvider(BaseHTTPRequestHandler):
    requests: list = []
    mode = {"chat": "ok"}

    def log_message(self, *_args):
        pass

    def _send(self, status, body, headers=None):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _auth_ok(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in VALID_KEYS:
            self._send(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}})
            return False
        return True

    def do_GET(self):
        if not self._auth_ok():
            return
        if self.path.endswith("/models"):
            self._send(200, {"object": "list", "data": [
                {"id": "fake-model", "object": "model", "context_window": 32000},
                {"id": "models/other", "object": "model"},
            ]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        FakeProvider.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if not self._auth_ok():
            return
        mode = FakeProvider.mode["chat"]
        if mode == "rate_limited":
            self._send(429, {"error": {"message": "Rate limit reached"}}, {"Retry-After": "2"})
        elif mode == "missing_model":
            self._send(404, {"error": {"message": "The model `x` does not exist"}})
        else:
            self._send(200, {
                "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            })


class AdapterWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeProvider.requests.clear()
        FakeProvider.mode["chat"] = "ok"
        self.spec = ProviderSpec("fakeco", "Fake Co", "openai_compatible", self.base_url, "FAKECO_KEY")
        self.adapter = create_adapter(self.spec)

    def tearDown(self):
        self.adapter.close()

    def test_completion_request_on_the_wire(self):
        response = self.adapter.complete(
            api_key="good-key-1111", model="fake-model",
            messages=[{"role": "system", "content": "a"}, {"role": "system", "content": "b"}, {"role": "user", "content": "ping"}],
            tools=[{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {}}}}],
            temperature=0.2, max_tokens=50,
        )
        self.assertEqual(response.choices[0].message.content, "pong")
        sent = FakeProvider.requests[-1]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer good-key-1111")
        self.assertEqual(sent["body"]["model"], "fake-model")
        self.assertEqual(sent["body"]["max_tokens"], 50)
        self.assertEqual(sent["body"]["tools"][0]["function"]["name"], "t")
        self.assertEqual(sent["body"]["messages"][1]["role"], "user")  # mid-conversation system note

    def test_errors_are_normalized_with_retry_after(self):
        with self.assertRaises(px.AuthenticationError):
            self.adapter.complete(api_key="bad-key", model="fake-model", messages=[{"role": "user", "content": "x"}])
        FakeProvider.mode["chat"] = "rate_limited"
        with self.assertRaises(px.RateLimitError) as ctx:
            self.adapter.complete(api_key="good-key-1111", model="fake-model", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.retry_after, 2.0)
        FakeProvider.mode["chat"] = "missing_model"
        with self.assertRaises(px.ModelNotFoundError):
            self.adapter.complete(api_key="good-key-1111", model="nope", messages=[{"role": "user", "content": "x"}])

    def test_unreachable_provider_is_a_connection_error(self):
        dead = create_adapter(ProviderSpec("dead", "Dead", "openai_compatible", "http://127.0.0.1:9/v1", "DEAD_KEY"))
        with self.assertRaises(px.ProviderError) as ctx:
            dead.complete(api_key="k", model="m", messages=[{"role": "user", "content": "x"}], timeout=2)
        self.assertIn(ctx.exception.kind, ("connection", "timeout"))

    def test_model_listing_strips_gemini_style_prefix(self):
        models = self.adapter.list_models(api_key="good-key-1111")
        self.assertEqual([m["id"] for m in models], ["fake-model", "other"])
        self.assertEqual(models[0]["context_window"], 32000)

    def test_connectivity_check_reports_valid_and_invalid_keys(self):
        good = test_provider_connectivity("fakeco", "good-key-1111", base_url=self.base_url)
        self.assertTrue(good.connected)
        self.assertEqual(good.models_found, 2)
        bad = test_provider_connectivity("fakeco", "expired-key-9999", base_url=self.base_url)
        self.assertFalse(bad.connected)
        self.assertEqual(bad.status, "invalid_key")
        self.assertNotIn("expired-key-9999", json.dumps(bad.to_dict()))

    def test_gateway_rotates_past_an_expired_key_over_real_http(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "providers.yaml"
            user.write_text(
                f"providers:\n  fakeco:\n    base_url: {self.base_url}\n    key_prefix: FAKECO_KEY\n"
                "    models:\n      fake-model: {}\nroles:\n  coder: [fakeco/fake-model]\n",
                encoding="utf-8",
            )
            empty = Path(tmp) / "empty.yaml"
            empty.write_text("{}", encoding="utf-8")
            catalog = ProviderCatalog(default_path=empty, user_path=user)
            gateway = ModelGateway(
                catalog=catalog,
                keys=KeySource(catalog, keystore_factory=None,
                               environ={"FAKECO_KEY_1": "expired-key-9999", "FAKECO_KEY_2": "good-key-2222"}),
                health=HealthTracker(persist=False),
                log=lambda _m: None,
            )
            try:
                response = gateway.complete("coder", [{"role": "user", "content": "ping"}])
            finally:
                gateway.close()
        self.assertEqual(response.choices[0].message.content, "pong")
        self.assertEqual(response.route.key_id, "FAKECO_KEY_2")
        self.assertEqual([r["auth"] for r in FakeProvider.requests], ["Bearer expired-key-9999", "Bearer good-key-2222"])

    def test_sdk_clients_are_reused_per_key(self):
        first = self.adapter._client("good-key-1111", 60)
        self.assertIs(first, self.adapter._client("good-key-1111", 60))
        self.assertIsNot(first, self.adapter._client("good-key-2222", 60))


if __name__ == "__main__":
    unittest.main()
