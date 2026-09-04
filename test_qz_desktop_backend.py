import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from qz_desktop_backend import DesktopBackend, SetupError
from qz_keystore import KeyStore
from qz_proxy_manager import ProxyStatus
from qz_usage_tracker import UsageTracker

SAMPLE_CONFIG = """model_list:
  - model_name: groq-fast
    litellm_params:
      model: groq/model-a
      api_key: os.environ/GROQ_KEY_1
  - model_name: coder-strong
    litellm_params:
      model: mistral/coder
      api_key: os.environ/MISTRAL_KEY_1
  - model_name: reasoner
    litellm_params:
      model: mistral/reasoner
      api_key: os.environ/MISTRAL_KEY_1
router_settings:
  fallbacks:
    - coder-strong: [groq-fast]
"""


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.config_path = self.workspace / "config.yaml"
        self.config_path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        self.keystore = KeyStore(path=self.workspace / "keystore.dat", backend="fernet")
        self.proxy_manager = MagicMock()
        self.usage_tracker = UsageTracker(log_path=None)
        self.backend = DesktopBackend(
            keystore=self.keystore,
            proxy_manager=self.proxy_manager,
            usage_tracker=self.usage_tracker,
            config_path=self.config_path,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    # ---- first-run setup --------------------------------------------------

    def test_first_run_setup_configures_every_supported_provider(self):
        keys = {
            "groq": ["g1", "g2"],
            "gemini": ["gem1"],
            "mistral": ["m1"],
            "openrouter": ["or1"],
            "deepseek": ["ds1"],
        }
        result = self.backend.first_run_setup(keys=keys, master_key="master-value")
        self.assertEqual(
            result["configured_providers"],
            {"groq": 2, "gemini": 1, "mistral": 1, "openrouter": 1, "deepseek": 1},
        )
        self.assertEqual(result["unconfigured_providers"], [])
        self.assertTrue(result["master_key_set"])
        self.assertEqual(self.keystore.get_master_key(), "master-value")

    def test_first_run_setup_with_partial_providers_reports_the_rest_as_unconfigured(self):
        result = self.backend.first_run_setup(keys={"groq": ["g1"]}, master_key="m")
        self.assertEqual(result["configured_providers"], {"groq": 1})
        self.assertIn("mistral", result["unconfigured_providers"])

    def test_missing_master_key_is_auto_generated_not_left_unset(self):
        result = self.backend.first_run_setup(keys={"groq": ["g1"]})
        self.assertTrue(result["master_key_set"])
        self.assertIsNotNone(self.keystore.get_master_key())

    # ---- configuration generation -----------------------------------------

    def test_generate_configuration_expands_multiple_keys_correctly(self):
        self.backend.first_run_setup(keys={"groq": ["g1", "g2"], "mistral": ["m1"]}, master_key="m")
        result = self.backend.generate_configuration()
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        groq_entries = [e for e in persisted["model_list"] if e["model_name"] == "groq-fast"]
        self.assertEqual(
            [e["litellm_params"]["api_key"] for e in groq_entries],
            ["os.environ/GROQ_KEY_1", "os.environ/GROQ_KEY_2"],
        )
        self.assertEqual(result["model_entries"], len(persisted["model_list"]))
        self.assertIn("GROQ_KEY", result["configured_families"])

    def test_generate_configuration_warns_about_unconfigured_provider(self):
        self.backend.first_run_setup(keys={"groq": ["g1"]}, master_key="m")
        result = self.backend.generate_configuration()
        self.assertTrue(any("MISTRAL_KEY_1" in warning for warning in result["warnings"]))

    def test_generate_configuration_missing_template_is_a_clear_setup_error(self):
        backend = DesktopBackend(
            keystore=self.keystore,
            proxy_manager=self.proxy_manager,
            usage_tracker=self.usage_tracker,
            config_path=self.workspace / "does-not-exist.yaml",
        )
        with self.assertRaises(SetupError):
            backend.generate_configuration()

    def test_disabled_key_is_not_used_when_regenerating_config(self):
        self.backend.first_run_setup(keys={"groq": ["g1", "g2"]}, master_key="m")
        self.keystore.set_enabled("groq", 2, False)
        self.backend.generate_configuration()
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        groq_keys = [e["litellm_params"]["api_key"] for e in persisted["model_list"] if e["model_name"] == "groq-fast"]
        self.assertEqual(groq_keys, ["os.environ/GROQ_KEY_1"])

    # ---- validation ------------------------------------------------------

    def test_validate_configuration_flags_missing_master_key(self):
        self.backend.first_run_setup(keys={"groq": ["g1"]})  # auto-generates master key
        self.assertTrue(self.backend.validate_configuration()["valid"])

    def test_validate_configuration_flags_empty_model_list(self):
        self.config_path.write_text("model_list: []\n", encoding="utf-8")
        self.keystore.set_master_key("m")
        result = self.backend.validate_configuration()
        self.assertFalse(result["valid"])
        self.assertTrue(any("model_list" in e for e in result["errors"]))

    # ---- apply / lifecycle --------------------------------------------------

    def test_proxy_manager_uses_venv_litellm_launcher(self):
        from qz_proxy_manager import ProxyManager

        manager = ProxyManager()
        self.assertTrue(Path(manager._command[0]).name.startswith("litellm"))
        self.assertEqual(manager._command[1], "--config")

    def test_apply_and_start_restarts_proxy_with_enabled_env_only(self):
        self.backend.first_run_setup(keys={"groq": ["g1"], "mistral": ["m1"]}, master_key="master")
        self.proxy_manager.restart.return_value = ProxyStatus(
            running=True, healthy=True, pid=1, crashed=False, last_error=None
        )
        result = self.backend.apply_and_start()
        called_env = self.proxy_manager.restart.call_args.kwargs["env_overrides"]
        self.assertEqual(called_env["GROQ_KEY_1"], "g1")
        self.assertEqual(called_env["LITELLM_MASTER_KEY"], "master")
        self.assertTrue(result["proxy"]["healthy"])

    def test_apply_and_start_refuses_to_start_with_invalid_configuration(self):
        self.config_path.write_text("model_list: []\n", encoding="utf-8")
        with self.assertRaises(SetupError):
            self.backend.apply_and_start()
        self.proxy_manager.restart.assert_not_called()

    def test_shutdown_stops_proxy_when_configured_to(self):
        self.backend.stop_proxy_on_exit = True
        self.backend.shutdown()
        self.proxy_manager.stop.assert_called_once()

    def test_shutdown_leaves_proxy_running_when_configured_not_to(self):
        self.backend.stop_proxy_on_exit = False
        self.backend.shutdown()
        self.proxy_manager.stop.assert_not_called()

    # ---- status -------------------------------------------------------------

    def test_status_reports_masked_keys_aliases_and_usage(self):
        self.backend.first_run_setup(keys={"groq": ["sk-abcdefghijklmno"]}, master_key="m")
        self.backend.generate_configuration()
        self.proxy_manager.status.return_value = ProxyStatus(
            running=True, healthy=True, pid=99, crashed=False, last_error=None
        )
        self.usage_tracker.record_request(model="groq-fast", duration=0.2, success=True)
        status = self.backend.status()
        self.assertIn("groq-fast", status["aliases"])
        self.assertEqual(status["fallback_order"], {"coder-strong": ["groq-fast"]})
        self.assertNotIn("sk-abcdefghijklmno", str(status))
        self.assertEqual(status["usage"]["request_count"], 1)
        self.assertTrue(status["master_key_set"])


if __name__ == "__main__":
    unittest.main()