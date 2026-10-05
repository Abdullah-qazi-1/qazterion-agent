"""ModelGateway: provider/model/key selection, rotation, failover and bookkeeping.

All provider calls go to a scripted fake adapter; no network is used.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qz_providers import exceptions as px
from qz_providers.catalog import ProviderCatalog
from qz_providers.gateway import GatewayResponse, ModelGateway, NoRouteError
from qz_providers.health import HealthTracker
from qz_providers.keys import KeySource
from qz_usage_tracker import UsageTracker

CATALOG = """
settings:
  key_strategy: balanced
  max_attempts: 8
  max_wait_for_cooldown_s: 30
providers:
  alpha:
    base_url: https://alpha.example/v1
    key_prefix: ALPHA_KEY
    models:
      a-large: {context_window: 128000, tools: true}
      a-small: {context_window: 8000, tools: true}
  beta:
    base_url: https://beta.example/v1
    key_prefix: BETA_KEY
    models:
      b-chat: {context_window: 64000, tools: true}
      b-notools: {context_window: 64000, tools: false}
roles:
  coder: [alpha/a-large, beta/b-chat, alpha/a-small]
  reasoner: [beta/b-notools, alpha/a-large]
  fast: [alpha/a-small]
"""


def ok(content="ok", tokens=(10, 5)):
    usage = SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1], total_tokens=sum(tokens))
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))], usage=usage)


class ScriptedProviders:
    """Fake adapter factory. ``script[(provider, model, secret)]`` is a list of results/exceptions."""

    def __init__(self):
        self.script: dict[tuple[str, str, str], list] = {}
        self.calls: list[tuple[str, str, str]] = []

    def factory(self, spec):
        outer = self

        class Adapter:
            def __init__(self):
                self.spec = spec

            def complete(self, *, api_key, model, messages, tools=None, temperature=None, max_tokens=None, timeout=60):
                key = (spec.provider_id, model, api_key)
                outer.calls.append(key)
                queue = outer.script.get(key) or outer.script.get((spec.provider_id, model, "*")) or [ok()]
                result = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(result, BaseException):
                    raise result
                return result

            def list_models(self, **_kw):
                return []

            def close(self):
                pass

        return Adapter()


class GatewayTestBase(unittest.TestCase):
    env = {"ALPHA_KEY_1": "alpha-secret-1", "ALPHA_KEY_2": "alpha-secret-2", "BETA_KEY_1": "beta-secret-1"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        user = Path(self.tmp.name) / "providers.yaml"
        user.write_text(CATALOG, encoding="utf-8")
        # Use only the test catalog: an empty defaults file keeps real providers out.
        empty = Path(self.tmp.name) / "empty.yaml"
        empty.write_text("{}", encoding="utf-8")
        self.catalog = ProviderCatalog(default_path=empty, user_path=user)
        self.now = [1000.0]
        self.health = HealthTracker(path=Path(self.tmp.name) / "health.json", clock=lambda: self.now[0])
        self.providers = ScriptedProviders()
        self.usage = UsageTracker(log_path=Path(self.tmp.name) / "usage.jsonl")
        self.events = []
        self.slept = []

        def sleep(seconds):
            self.slept.append(seconds)
            self.now[0] += seconds

        self.gateway = ModelGateway(
            catalog=self.catalog,
            keys=KeySource(self.catalog, keystore_factory=None, environ=dict(self.env)),
            health=self.health,
            usage_tracker=self.usage,
            adapter_factory=self.providers.factory,
            event_sink=lambda task_id, kind, payload: self.events.append((task_id, kind, payload)),
            sleep=sleep,
            log=lambda _m: None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def complete(self, role="coder", **kwargs):
        return self.gateway.complete(role, [{"role": "user", "content": "hello " * 50}], **kwargs)


class SelectionTests(GatewayTestBase):
    def test_success_uses_first_model_and_reports_route(self):
        response = self.complete(task_id="t1")
        self.assertIsInstance(response, GatewayResponse)
        self.assertEqual((response.route.provider, response.route.model), ("alpha", "a-large"))
        self.assertFalse(response.route.fallback)
        self.assertEqual(response.choices[0].message.content, "ok")
        usage = self.usage.get_task_usage("t1")
        self.assertEqual((usage["request_count"], usage["total_tokens"]), (1, 15))
        self.assertEqual(usage["providers_used"], ["alpha"])

    def test_balanced_strategy_rotates_across_accounts(self):
        used = [self.complete().route.key_id for _ in range(4)]
        self.assertEqual(used, ["ALPHA_KEY_1", "ALPHA_KEY_2", "ALPHA_KEY_1", "ALPHA_KEY_2"])

    def test_priority_strategy_sticks_to_first_healthy_key(self):
        self.catalog.set_key_strategy("priority")
        used = {self.complete().route.key_id for _ in range(3)}
        self.assertEqual(used, {"ALPHA_KEY_1"})

    def test_tool_requests_skip_models_without_tool_support(self):
        response = self.gateway.complete("reasoner", [{"role": "user", "content": "x"}], tools=[{"type": "function"}])
        self.assertEqual(response.route.ref, "alpha/a-large")
        plain = self.gateway.complete("reasoner", [{"role": "user", "content": "x"}])
        self.assertEqual(plain.route.ref, "beta/b-notools")

    def test_explicit_provider_model_reference(self):
        self.assertEqual(self.gateway.complete("beta/b-chat", [{"role": "user", "content": "x"}]).route.ref, "beta/b-chat")

    def test_disabled_provider_and_missing_keys_are_skipped(self):
        self.catalog.set_provider_enabled("alpha", False)
        self.assertEqual(self.complete().route.ref, "beta/b-chat")
        _plan, skipped = self.gateway.plan("coder")
        self.assertTrue(any("provider disabled" in s for s in skipped))

    def test_no_keys_gives_actionable_error(self):
        self.gateway.keys = KeySource(self.catalog, keystore_factory=None, environ={})
        with self.assertRaises(NoRouteError) as ctx:
            self.complete()
        self.assertIn("ALPHA_KEY_1", str(ctx.exception))
        self.assertIn("/keys add", str(ctx.exception))
        self.assertEqual(self.providers.calls, [])

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(NoRouteError):
            self.complete(role="does-not-exist")


class FailoverTests(GatewayTestBase):
    def test_rate_limited_key_rotates_to_next_account_of_same_provider(self):
        self.providers.script[("alpha", "a-large", "alpha-secret-1")] = [px.RateLimitError("429 slow down")]
        response = self.complete(task_id="t2")
        self.assertEqual((response.route.ref, response.route.key_id), ("alpha/a-large", "ALPHA_KEY_2"))
        self.assertFalse(self.health.key_available("ALPHA_KEY_1"))
        # The cooling key is skipped on the next request instead of being retried.
        self.providers.calls.clear()
        self.complete()
        self.assertNotIn(("alpha", "a-large", "alpha-secret-1"), self.providers.calls)
        self.assertTrue(any(kind == "MODEL_FALLBACK" for _t, kind, _p in self.events))

    def test_invalid_key_is_disabled_and_never_reused(self):
        self.providers.script[("alpha", "a-large", "alpha-secret-1")] = [px.AuthenticationError("401 Incorrect API key")]
        self.complete()
        self.now[0] += 10 ** 6  # even much later
        self.providers.calls.clear()
        for _ in range(3):
            self.complete()
        self.assertNotIn("alpha-secret-1", [c[2] for c in self.providers.calls])
        status = self.gateway.status()
        alpha = next(p for p in status["providers"] if p["provider_id"] == "alpha")
        key1 = next(k for k in alpha["keys"] if k["key_id"] == "ALPHA_KEY_1")
        self.assertEqual(key1["disabled_reason"], "invalid_or_expired_key")

    def test_all_accounts_of_a_provider_failing_falls_over_to_another_provider(self):
        for secret in ("alpha-secret-1", "alpha-secret-2"):
            self.providers.script[("alpha", "a-large", secret)] = [px.QuotaExhaustedError("quota exceeded")]
        response = self.complete(task_id="t3")
        self.assertEqual(response.route.ref, "beta/b-chat")
        self.assertTrue(response.route.fallback)
        usage = self.usage.get_task_usage("t3")
        self.assertEqual(usage["error_count"], 2)
        self.assertEqual(usage["fallback_count"], 1)

    def test_missing_model_skips_to_next_model_and_cools_that_route(self):
        self.providers.script[("alpha", "a-large", "*")] = [px.ModelNotFoundError("model does not exist")]
        self.assertEqual(self.complete().route.ref, "beta/b-chat")
        self.assertFalse(self.health.route_available("alpha/a-large"))
        self.assertTrue(self.health.key_available("ALPHA_KEY_1"))
        # Only one call was spent on the missing model, not one per key.
        self.assertEqual(sum(1 for c in self.providers.calls if c[1] == "a-large"), 1)

    def test_context_length_moves_on_without_penalizing(self):
        self.providers.script[("alpha", "a-large", "*")] = [px.ContextLengthExceededError("maximum context length")]
        self.assertEqual(self.complete().route.ref, "beta/b-chat")
        self.assertTrue(self.health.route_available("alpha/a-large"))
        self.assertTrue(self.health.key_available("ALPHA_KEY_1"))

    def test_transient_errors_retry_another_key_then_next_model(self):
        self.providers.script[("alpha", "a-large", "*")] = [px.ServerError("502 bad gateway")]
        response = self.complete()
        self.assertEqual(response.route.ref, "beta/b-chat")
        self.assertEqual([c[1] for c in self.providers.calls], ["a-large", "a-large", "b-chat"])

    def test_empty_response_is_a_failure(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=None))], usage=None)
        self.providers.script[("alpha", "a-large", "*")] = [empty]
        self.assertEqual(self.complete().route.ref, "beta/b-chat")

    def test_waits_for_a_short_rate_limit_instead_of_failing(self):
        self.gateway.keys = KeySource(self.catalog, keystore_factory=None, environ={"ALPHA_KEY_1": "alpha-secret-1"})
        self.providers.script[("alpha", "a-small", "alpha-secret-1")] = [px.RateLimitError("429", retry_after=4.0), ok("after wait")]
        response = self.complete(role="fast")
        self.assertEqual(response.choices[0].message.content, "after wait")
        self.assertEqual(self.slept, [4.0])

    def test_long_cooldowns_fail_fast_with_a_summary(self):
        self.gateway.keys = KeySource(self.catalog, keystore_factory=None, environ={"ALPHA_KEY_1": "alpha-secret-1"})
        self.providers.script[("alpha", "a-small", "alpha-secret-1")] = [px.QuotaExhaustedError("daily quota exceeded")]
        with self.assertRaises(NoRouteError) as ctx:
            self.complete(role="fast", task_id="t4")
        self.assertIn("quota", str(ctx.exception))
        self.assertEqual(self.slept, [])
        self.assertTrue(any(kind == "MODEL_ROUTES_EXHAUSTED" for _t, kind, _p in self.events))

    def test_attempts_are_bounded(self):
        self.catalog._update_user(lambda user: user["settings"].update(max_attempts=2))
        for model in ("a-large", "a-small"):
            self.providers.script[("alpha", model, "*")] = [px.ServerError("500")]
        self.providers.script[("beta", "b-chat", "*")] = [px.ServerError("500")]
        with self.assertRaises(NoRouteError):
            self.complete()
        self.assertEqual(len(self.providers.calls), 2)

    def test_errors_and_usage_never_contain_secrets(self):
        self.providers.script[("alpha", "a-large", "alpha-secret-1")] = [
            px.AuthenticationError("Incorrect API key provided: sk-proj-abcdefghijklmnopqrstuvwxyz0123")
        ]
        self.complete(task_id="t5")
        log_text = Path(self.usage.log_path).read_text(encoding="utf-8")
        health_text = (Path(self.tmp.name) / "health.json").read_text(encoding="utf-8")
        for secret in ("alpha-secret-1", "sk-proj-abcdefghijklmnopqrstuvwxyz0123"):
            self.assertNotIn(secret, log_text)
            self.assertNotIn(secret, health_text)

    def test_quota_on_one_model_does_not_bench_the_key_for_other_models(self):
        # Live behaviour seen on free Gemini keys: Pro has no quota, Flash works.
        self.providers.script[("alpha", "a-large", "*")] = [px.QuotaExhaustedError("You exceeded your current quota")]
        self.assertEqual(self.complete().route.ref, "beta/b-chat")
        self.providers.calls.clear()
        response = self.complete(role="fast")  # alpha/a-small on the very same keys
        self.assertEqual(response.route.ref, "alpha/a-small")
        self.assertFalse(self.health.key_available("ALPHA_KEY_1", route="alpha/a-large"))
        self.assertTrue(self.health.key_available("ALPHA_KEY_1", route="alpha/a-small"))
        cooling = self.health.cooling_routes("ALPHA_KEY_1")
        self.assertIn("alpha/a-large", cooling)

    def test_blocked_error_explains_each_candidate(self):
        self.gateway.keys = KeySource(self.catalog, keystore_factory=None, environ={"ALPHA_KEY_1": "alpha-secret-1"})
        self.health.record_failure("ALPHA_KEY_1", "alpha/a-small", "quota")
        with self.assertRaises(NoRouteError) as ctx:
            self.complete(role="fast")
        self.assertIn("ALPHA_KEY_1 cooling", str(ctx.exception))
        self.assertEqual(self.providers.calls, [])

    def test_health_is_shared_with_a_new_process(self):
        self.providers.script[("alpha", "a-large", "alpha-secret-1")] = [px.AuthenticationError("401")]
        self.complete()
        fresh = HealthTracker(path=Path(self.tmp.name) / "health.json", clock=lambda: self.now[0])
        key = self.gateway.keys.keys_for("alpha")[0]
        self.assertFalse(fresh.key_available(key.key_id, key.fingerprint))


if __name__ == "__main__":
    unittest.main()
