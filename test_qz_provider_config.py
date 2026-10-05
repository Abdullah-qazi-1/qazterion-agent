"""Provider catalog (configuration loading/merging) and key discovery."""

import tempfile
import unittest
from pathlib import Path

from qz_keystore import KeyStore
from qz_providers.catalog import CatalogError, ProviderCatalog, normalize_strategy, parse_model_ref
from qz_providers.keys import KeySource


def _catalog(tmp: str, user_yaml: str | None = None) -> ProviderCatalog:
    user = Path(tmp) / "providers.yaml"
    if user_yaml is not None:
        user.write_text(user_yaml, encoding="utf-8")
    return ProviderCatalog(user_path=user)


class CatalogLoadingTests(unittest.TestCase):
    def test_defaults_define_providers_models_and_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(tmp)
        for provider in ("gemini", "groq", "mistral", "openrouter", "deepseek"):
            self.assertIn(provider, catalog.providers)
            self.assertTrue(catalog.providers[provider].base_url)
        for role in ("fast", "coder", "reasoner", "planner", "classify"):
            self.assertTrue(catalog.roles[role], role)
        self.assertEqual(catalog.settings.key_strategy, "balanced")
        self.assertEqual(catalog.providers["gemini"].key_prefix, "GEMINI_KEY")

    def test_every_default_role_reference_points_to_a_known_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(tmp)
        for role, refs in catalog.roles.items():
            for ref in refs:
                provider, _model = parse_model_ref(ref)
                self.assertIn(provider, catalog.providers, f"{role}: {ref}")

    def test_user_file_overrides_and_extends_defaults(self):
        user_yaml = """
settings:
  key_strategy: priority
  max_attempts: 3
providers:
  groq:
    enabled: false
  localai:
    display_name: Local AI
    base_url: http://127.0.0.1:8080/v1
    key_prefix: LOCALAI_KEY
    models:
      tiny-coder: {context_window: 8192, tools: false}
roles:
  coder:
    - localai/tiny-coder
    - gemini/gemini-2.0-flash
"""
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(tmp, user_yaml)
        self.assertEqual(catalog.settings.key_strategy, "priority")
        self.assertEqual(catalog.settings.max_attempts, 3)
        self.assertFalse(catalog.providers["groq"].enabled)
        self.assertEqual(catalog.providers["groq"].base_url, "https://api.groq.com/openai/v1")  # merged, not replaced
        self.assertEqual([m.ref for m in catalog.candidates("coder")], ["localai/tiny-coder", "gemini/gemini-2.0-flash"])
        self.assertFalse(catalog.providers["localai"].models["tiny-coder"].tools)
        self.assertTrue(catalog.roles["fast"])  # untouched roles keep their defaults

    def test_invalid_configuration_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogError):
                _catalog(tmp, "roles: [unclosed")
            with self.assertRaises(CatalogError):
                _catalog(tmp, "roles:\n  coder:\n    - not-a-model-ref\n")
            with self.assertRaises(CatalogError):
                _catalog(tmp, "settings:\n  key_strategy: random\n")

    def test_legacy_aliases_and_explicit_refs_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(tmp)
        self.assertEqual(catalog.resolve_role("coder-strong"), "coder")
        self.assertEqual(catalog.resolve_role("groq-fast"), "fast")
        refs = [m.ref for m in catalog.candidates("openrouter/meta-llama/llama-3.3-70b-instruct")]
        self.assertEqual(refs, ["openrouter/meta-llama/llama-3.3-70b-instruct"])
        self.assertEqual(catalog.candidates("unknown-role"), [])
        self.assertEqual(parse_model_ref("openrouter/a/b"), ("openrouter", "a/b"))

    def test_mutations_persist_to_the_user_file_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(tmp)
            catalog.set_provider_enabled("mistral", False)
            catalog.set_role_preference("fast", "gemini", "gemini-2.0-flash")
            catalog.set_key_strategy("least-busy")
            catalog.upsert_custom_provider("acme", base_url="https://acme.example/v1", default_model="acme-1")
            reloaded = _catalog(tmp)
            default_text = catalog.default_path.read_text(encoding="utf-8")
        self.assertFalse(reloaded.providers["mistral"].enabled)
        self.assertEqual(reloaded.roles["fast"][0], "gemini/gemini-2.0-flash")
        self.assertEqual(reloaded.settings.key_strategy, "balanced")
        self.assertIn("acme/acme-1", reloaded.roles["coder"])
        self.assertEqual(reloaded.providers["acme"].key_prefix, "ACME_KEY")
        self.assertNotIn("acme", default_text)

    def test_strategy_names(self):
        self.assertEqual(normalize_strategy("PRIORITY"), "priority")
        self.assertEqual(normalize_strategy("simple-shuffle"), "balanced")
        with self.assertRaises(CatalogError):
            normalize_strategy("fastest")


class KeyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = _catalog(self.tmp.name)
        self.store = KeyStore(path=Path(self.tmp.name) / "keystore.dat", backend="fernet")

    def tearDown(self):
        self.tmp.cleanup()

    def _source(self, environ):
        return KeySource(self.catalog, keystore_factory=lambda: self.store, environ=environ, cache_ttl_s=0)

    def test_multiple_accounts_per_provider_from_env(self):
        source = self._source({"GROQ_KEY_1": "g1", "GROQ_KEY_3": "g3", "GEMINI_KEY_1": "a", "GEMINI_KEY_2": "b", "UNRELATED": "x"})
        self.assertEqual([k.key_id for k in source.keys_for("groq")], ["GROQ_KEY_1", "GROQ_KEY_3"])
        self.assertEqual([k.key_id for k in source.keys_for("gemini")], ["GEMINI_KEY_1", "GEMINI_KEY_2"])
        self.assertEqual(source.keys_for("mistral"), [])
        self.assertEqual(source.secret("GROQ_KEY_3"), "g3")

    def test_keystore_and_env_combine_with_keystore_winning_on_clash(self):
        self.store.set_key("groq", 1, "from-keystore-1111")
        self.store.set_key("groq", 2, "disabled-key-2222", enabled=False)
        source = self._source({"GROQ_KEY_1": "from-env", "GROQ_KEY_5": "env-five"})
        keys = source.keys_for("groq")
        self.assertEqual([(k.key_id, k.source) for k in keys], [("GROQ_KEY_1", "keystore"), ("GROQ_KEY_5", "env")])
        self.assertEqual(source.secret("GROQ_KEY_1"), "from-keystore-1111")
        self.assertEqual([k.key_id for k in source.keys_for("groq", include_disabled=True)],
                         ["GROQ_KEY_1", "GROQ_KEY_2", "GROQ_KEY_5"])

    def test_conventional_api_key_variable_and_blank_values(self):
        source = self._source({"GEMINI_API_KEY": "conventional", "GROQ_KEY_1": "   "})
        self.assertEqual([k.key_id for k in source.keys_for("gemini")], ["GEMINI_API_KEY"])
        self.assertEqual(source.keys_for("groq"), [])

    def test_new_catalog_provider_needs_no_code_change(self):
        catalog = _catalog(self.tmp.name, "providers:\n  newco:\n    base_url: https://newco.example/v1\n    key_prefix: NEWCO_TOKEN\n")
        source = KeySource(catalog, keystore_factory=None, environ={"NEWCO_TOKEN_1": "n1", "NEWCO_TOKEN_2": "n2"})
        self.assertEqual(len(source.keys_for("newco")), 2)
        # The keystore accepts it too, using the catalog's key prefix.
        self.assertEqual(KeyStore(path=Path(self.tmp.name) / "k2.dat", backend="fernet")._family_for("deepseek"), "DEEPSEEK_KEY")

    def test_key_metadata_never_contains_the_secret(self):
        source = self._source({"MISTRAL_KEY_1": "mistral-super-secret-value"})
        key = source.keys_for("mistral")[0]
        self.assertNotIn("super-secret", repr(key))
        self.assertNotIn("super-secret", str(key.to_dict()))
        self.assertTrue(key.masked.endswith("alue"))


if __name__ == "__main__":
    unittest.main()
