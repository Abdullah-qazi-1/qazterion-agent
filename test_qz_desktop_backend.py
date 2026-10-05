"""Desktop backend: key setup, status, configuration and model management (no proxy)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qz_desktop_backend import DesktopBackend, SetupError
from qz_keystore import KeyStore
from qz_providers.catalog import ProviderCatalog
from qz_providers.gateway import ModelGateway
from qz_providers.health import HealthTracker
from qz_providers.keys import KeySource
from qz_usage_tracker import UsageTracker


class _FakeAdapter:
    models = [{"id": "brand-new-model", "context_window": 65536, "tools": True}]

    def __init__(self, spec):
        self.spec = spec

    def complete(self, **kwargs):
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None, model=kwargs["model"])

    def list_models(self, **_kwargs):
        return list(self.models)

    def check_key(self, **_kwargs):
        return 1

    def close(self):
        pass


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.keystore = KeyStore(path=root / "keystore.dat", backend="fernet")
        self.catalog = ProviderCatalog(user_path=root / "providers.yaml")
        self.gateway = ModelGateway(
            catalog=self.catalog,
            keys=KeySource(self.catalog, keystore_factory=lambda: self.keystore, environ={}, cache_ttl_s=0),
            health=HealthTracker(persist=False),
            adapter_factory=_FakeAdapter,
            sleep=lambda _s: None,
            log=lambda _m: None,
        )
        self.usage = UsageTracker(log_path=None)
        self.backend = DesktopBackend(keystore=self.keystore, usage_tracker=self.usage, gateway=self.gateway)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_first_run_setup_stores_multiple_keys_per_provider(self):
        result = self.backend.first_run_setup(keys={"groq": ["g1-aaaaaaaa", "g2-bbbbbbbb"], "gemini": ["gem1-cccccc"]})
        self.assertEqual(result["configured_providers"], {"groq": 2, "gemini": 1})
        self.assertIn("mistral", result["unconfigured_providers"])
        self.assertEqual([k.key_id for k in self.gateway.keys.keys_for("groq")], ["GROQ_KEY_1", "GROQ_KEY_2"])

    def test_first_run_setup_appends_without_duplicating(self):
        self.backend.first_run_setup(keys={"groq": ["g1-aaaaaaaa"]})
        self.backend.first_run_setup(keys={"groq": ["g1-aaaaaaaa", "g2-bbbbbbbb"]})
        names = [e.env_name for e in self.keystore.list_entries(provider="groq")]
        self.assertEqual(names, ["GROQ_KEY_1", "GROQ_KEY_2"])

    def test_status_reports_masked_keys_roles_and_connection(self):
        self.assertFalse(self.backend.status()["proxy"]["healthy"])
        self.backend.first_run_setup(keys={"groq": ["sk-abcdefghijklmno"]})
        self.usage.record_request(model="groq/x", duration=0.2, success=True)
        status = self.backend.status()
        self.assertTrue(status["proxy"]["healthy"])
        self.assertEqual(status["proxy"]["state"], "direct")
        self.assertIn("coder", status["aliases"])
        self.assertTrue(status["fallback_order"]["coder"])
        self.assertNotIn("sk-abcdefghijklmno", str(status))
        self.assertEqual(status["usage"]["request_count"], 1)

    def test_validate_configuration_requires_a_usable_key(self):
        self.assertFalse(self.backend.validate_configuration()["valid"])
        self.backend.first_run_setup(keys={"gemini": ["AIza-test-key-1234"]})
        self.assertEqual(self.backend.validate_configuration(), {"valid": True, "errors": []})
        warnings = self.backend.generate_configuration()["warnings"]
        self.assertFalse(any("'coder'" in w for w in warnings))

    def test_disabled_key_and_provider_are_not_used(self):
        self.backend.first_run_setup(keys={"groq": ["g1-aaaaaaaa"]})
        self.backend.set_key_enabled("groq", 1, False)
        self.assertEqual(self.gateway.keys.keys_for("groq"), [])
        self.backend.set_key_enabled("groq", 1, True)
        self.assertEqual(len(self.gateway.keys.keys_for("groq")), 1)
        self.backend.set_provider_enabled("groq", False)
        plan, skipped = self.gateway.plan("fast")
        self.assertFalse(any(model.provider == "groq" for model, _p, _k in plan))
        self.assertTrue(any("provider disabled" in s for s in skipped))

    def test_set_preferred_model_reorders_role_and_persists(self):
        result = self.backend.set_preferred_model("coder-strong", "groq", "llama-3.1-8b-instant")
        self.assertEqual(result["alias"], "coder")
        self.assertEqual(result["order"][0], "groq/llama-3.1-8b-instant")
        reloaded = ProviderCatalog(user_path=self.catalog.user_path)
        self.assertEqual(reloaded.roles["coder"][0], "groq/llama-3.1-8b-instant")

    def test_routing_strategy_accepts_legacy_names(self):
        self.assertEqual(self.backend.set_routing_strategy("simple-shuffle"), {"strategy": "simple-shuffle", "effective_strategy": "balanced"})
        self.assertEqual(self.backend.set_routing_strategy("lowest-cost"), {"strategy": "lowest-cost", "effective_strategy": "priority"})
        with self.assertRaises(SetupError):
            self.backend.set_routing_strategy("random")

    def test_register_custom_provider_makes_it_routable(self):
        result = self.backend.register_custom_provider(
            "acme", "acme-secret-key-123", display_name="Acme AI",
            base_url="https://api.acme.example/v1", default_model="acme-coder",
        )
        self.assertEqual(result["env_name"], "ACME_KEY_1")
        self.assertIn("acme/acme-coder", self.catalog.roles["coder"])
        response = self.gateway.complete("acme/acme-coder", [{"role": "user", "content": "hi"}])
        self.assertEqual(response.route.provider, "acme")

    def test_refresh_models_adds_discovered_models_to_catalog(self):
        self.backend.first_run_setup(keys={"groq": ["g1-aaaaaaaa"]})
        result = self.backend.refresh_models("groq")
        self.assertEqual(result["refreshed"], {"groq": 1})
        self.assertIn("brand-new-model", self.catalog.providers["groq"].models)
        models = {m["ref"]: m for m in self.backend.get_models("groq")}
        self.assertTrue(models["groq/brand-new-model"]["is_configured"])

    def test_check_health_reports_host_execution_without_docker(self):
        report = self.backend.check_health()
        self.assertEqual(report["components"]["execution"]["metadata"]["isolation"], "host")
        self.assertNotIn("docker", report["components"])


if __name__ == "__main__":
    unittest.main()
