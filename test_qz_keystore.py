import tempfile
import unittest
from pathlib import Path

import qz_keystore
from qz_keystore import KeyStore, SUPPORTED_PROVIDERS, UnsupportedProviderError, mask_key


class MaskKeyTests(unittest.TestCase):
    def test_short_value_is_fully_masked(self):
        self.assertEqual(mask_key("abcd"), "****")

    def test_long_value_hides_everything_but_last_four(self):
        masked = mask_key("sk-abcdefghijklmnop")
        self.assertTrue(masked.startswith("******"))
        self.assertTrue(masked.endswith("mnop"))
        self.assertNotIn("abcdefgh", masked)

    def test_missing_value(self):
        self.assertEqual(mask_key(None), "(not set)")
        self.assertEqual(mask_key(""), "(not set)")


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "keystore.dat"
        # Force the cross-platform fallback backend so this test suite is
        # deterministic on every OS it runs on (DPAPI only exists on Windows).
        self.store = KeyStore(path=self.path, backend="fernet")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_every_supported_provider_can_be_configured(self):
        for index, provider in enumerate(SUPPORTED_PROVIDERS, start=1):
            env_name = self.store.set_key(provider, 1, f"secret-{provider}-{index}")
            self.assertEqual(env_name, f"{qz_keystore.PROVIDER_FAMILIES[provider]}_1")
        self.assertEqual(sorted(self.store.configured_providers()), sorted(SUPPORTED_PROVIDERS))

    def test_unsupported_provider_raises_clear_error(self):
        with self.assertRaises(UnsupportedProviderError) as ctx:
            self.store.set_key("openai", 1, "sk-whatever")
        self.assertIn("Unsupported provider", str(ctx.exception))
        self.assertIn("groq", str(ctx.exception))

    def test_multiple_keys_expand_into_sorted_env_group(self):
        self.store.set_key("groq", 1, "one")
        self.store.set_key("groq", 2, "two")
        groups = self.store.enabled_key_groups()
        self.assertEqual(groups["GROQ_KEY"], ["GROQ_KEY_1", "GROQ_KEY_2"])

    def test_disabled_key_is_excluded_from_groups_and_env(self):
        self.store.set_key("groq", 1, "one")
        self.store.set_key("groq", 2, "two", enabled=False)
        groups = self.store.enabled_key_groups()
        self.assertEqual(groups["GROQ_KEY"], ["GROQ_KEY_1"])
        env = self.store.enabled_env()
        self.assertIn("GROQ_KEY_1", env)
        self.assertNotIn("GROQ_KEY_2", env)

    def test_stored_file_never_contains_plaintext_key(self):
        secret = "sk-super-secret-value-12345"
        self.store.set_key("mistral", 1, secret)
        self.store.set_master_key("master-secret-value")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        self.assertNotIn("master-secret-value", raw)

    def test_masking_in_list_entries_never_reveals_full_value(self):
        secret = "sk-abcdefghijklmno"
        self.store.set_key("deepseek", 1, secret)
        entries = self.store.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertNotEqual(entries[0].masked_value, secret)
        self.assertNotIn(secret, entries[0].masked_value)

    def test_reloading_the_store_reads_back_the_same_decrypted_value(self):
        self.store.set_key("gemini", 1, "reload-me")
        self.store.set_master_key("master-reload")
        reloaded = KeyStore(path=self.path, backend="fernet")
        self.assertEqual(reloaded.get_key("gemini", 1), "reload-me")
        self.assertEqual(reloaded.get_master_key(), "master-reload")

    def test_delete_key_removes_it_from_groups(self):
        self.store.set_key("groq", 1, "one")
        self.assertTrue(self.store.delete_key("groq", 1))
        self.assertEqual(self.store.enabled_key_groups(), {})
        self.assertFalse(self.store.delete_key("groq", 1))

    def test_upgrade_style_reload_preserves_existing_keys_after_adding_a_new_one(self):
        self.store.set_key("groq", 1, "one")
        # Simulate an app upgrade: a new KeyStore instance opens the same file.
        reopened = KeyStore(path=self.path, backend="fernet")
        reopened.set_key("groq", 2, "two")
        self.assertEqual(reopened.enabled_key_groups()["GROQ_KEY"], ["GROQ_KEY_1", "GROQ_KEY_2"])
        self.assertEqual(reopened.get_key("groq", 1), "one")

    def test_backend_name_reports_fallback(self):
        self.assertEqual(self.store.backend_name(), "fernet-fallback")

    def test_custom_provider_registration_and_persistence(self):
        meta = self.store.upsert_custom_provider(
            "together",
            display_name="Together AI",
            base_url="https://api.together.xyz/v1",
            default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        )
        self.assertEqual(meta["provider_id"], "together")
        self.store.set_key("together", 1, "together-secret-key-1234")
        self.assertEqual(self.store.get_key("together", 1), "together-secret-key-1234")

        # Reload into a fresh KeyStore instance simulating restart
        reloaded = KeyStore(path=self.path, backend="fernet")
        custom_list = reloaded.list_custom_providers()
        self.assertTrue(any(c["provider_id"] == "together" for c in custom_list))
        self.assertEqual(reloaded.get_key("together", 1), "together-secret-key-1234")

    @unittest.skipUnless(qz_keystore.sys.platform == "win32", "DPAPI only available on Windows")
    def test_dpapi_roundtrip_on_windows(self):
        dpapi_path = Path(self.temp_dir.name) / "keystore_dpapi.dat"
        dpapi_store = KeyStore(path=dpapi_path, backend="dpapi")
        self.assertEqual(dpapi_store.backend_name(), "dpapi")
        dpapi_store.set_key("groq", 1, "gsk_test_12345678")
        dpapi_store.set_master_key("master_secret_dpapi")
        
        # Verify encrypted on disk
        raw_text = dpapi_path.read_text(encoding="utf-8")
        self.assertNotIn("gsk_test_12345678", raw_text)
        self.assertNotIn("master_secret_dpapi", raw_text)

        # Reopen with fresh KeyStore instance (simulating app restart)
        reopened = KeyStore(path=dpapi_path, backend="dpapi")
        self.assertEqual(reopened.get_key("groq", 1), "gsk_test_12345678")
        self.assertEqual(reopened.get_master_key(), "master_secret_dpapi")


if __name__ == "__main__":
    unittest.main()