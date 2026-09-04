import tempfile
import unittest
from pathlib import Path

import yaml

import generate_config


class GenerateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.env_path = self.workspace / ".env"
        self.config_path = self.workspace / "config.yaml"
        self.config_path.write_text(
            """model_list:
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
""",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_discover_key_groups_ignores_empty_and_sorts_numeric_suffixes(self):
        self.env_path.write_text("GROQ_KEY_10=ten\nGROQ_KEY_2=two\nGROQ_KEY_1=one\nMISTRAL_KEY_1=\n", encoding="utf-8")
        self.assertEqual(
            generate_config.discover_key_groups(self.env_path),
            {"GROQ_KEY": ["GROQ_KEY_1", "GROQ_KEY_2", "GROQ_KEY_10"]},
        )

    def test_generate_config_expands_every_template_for_each_key_and_preserves_router(self):
        self.env_path.write_text("GROQ_KEY_1=one\nGROQ_KEY_2=two\nMISTRAL_KEY_1=one\nMISTRAL_KEY_3=three\n", encoding="utf-8")
        config, groups = generate_config.generate_config(self.env_path, self.config_path)
        entries = config["model_list"]

        self.assertEqual(groups["GROQ_KEY"], ["GROQ_KEY_1", "GROQ_KEY_2"])
        self.assertEqual(len(entries), 6)
        self.assertEqual(
            [entry["litellm_params"]["api_key"] for entry in entries if entry["model_name"] == "groq-fast"],
            ["os.environ/GROQ_KEY_1", "os.environ/GROQ_KEY_2"],
        )
        self.assertEqual(
            [entry["litellm_params"]["api_key"] for entry in entries if entry["model_name"] == "reasoner"],
            ["os.environ/MISTRAL_KEY_1", "os.environ/MISTRAL_KEY_3"],
        )
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["router_settings"]["fallbacks"], [{"coder-strong": ["groq-fast"]}])

    def test_generate_config_keeps_template_when_provider_has_no_usable_key(self):
        self.env_path.write_text("GROQ_KEY_1=one\n", encoding="utf-8")
        config, _ = generate_config.generate_config(self.env_path, self.config_path)
        mistral_entries = [entry for entry in config["model_list"] if entry["model_name"] in {"coder-strong", "reasoner"}]
        self.assertEqual(len(mistral_entries), 2)
        self.assertTrue(all(entry["litellm_params"]["api_key"] == "os.environ/MISTRAL_KEY_1" for entry in mistral_entries))


if __name__ == "__main__":
    unittest.main()
