"""Tests for Phase 14: Production Hardening, Health Diagnostics, Packaging & Security."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from qz_health import check_system_health
from qz_keystore import KeyStore
from qz_providers.connectivity import test_provider_connectivity
from qz_desktop_backend import DesktopBackend


class Phase14ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / ".git").mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_system_health_audit_inspects_components(self):
        ks = KeyStore()
        report = check_system_health(workspace=self.workspace, keystore=ks)
        d = report.to_dict()

        self.assertIn("overallStatus", d)
        self.assertIn("components", d)
        self.assertIn("python", d["components"])
        self.assertIn("git", d["components"])
        self.assertIn("docker", d["components"])
        self.assertIn("keystore", d["components"])
        self.assertIn("providers", d["components"])
        self.assertIn("workspace", d["components"])

        self.assertTrue(d["components"]["python"]["available"])
        self.assertEqual(d["components"]["workspace"]["status"], "healthy")

    def test_provider_connectivity_empty_key_rejected(self):
        res = test_provider_connectivity("groq", "   ")
        self.assertFalse(res.connected)
        self.assertEqual(res.status, "invalid_key")
        self.assertIn("cannot be empty", res.message)

    @patch("urllib.request.urlopen")
    def test_provider_connectivity_valid_response(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": [{"id": "llama-3.3-70b-versatile"}]}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = test_provider_connectivity("groq", "gsk_valid_secret_key_1234567890")
        self.assertTrue(res.connected)
        self.assertEqual(res.status, "valid")
        self.assertEqual(res.models_found, 1)
        self.assertNotIn("valid_secret_key", res.message)
        self.assertTrue(res.masked_key.endswith("7890"))
        self.assertTrue(res.masked_key.startswith("******"))

    def test_packaging_and_branding_assets_exist(self):
        root = Path(__file__).parent
        pyproject_path = root / "pyproject.toml"
        self.assertTrue(pyproject_path.is_file(), "pyproject.toml missing")
        pyproject_text = pyproject_path.read_text(encoding="utf-8")
        self.assertIn("qazterion = \"qz_cli.app:main\"", pyproject_text)
        self.assertIn("qz = \"qz_cli.app:main\"", pyproject_text)

        setup_path = root / "setup.py"
        self.assertTrue(setup_path.is_file(), "setup.py missing")
        setup_text = setup_path.read_text(encoding="utf-8")
        self.assertIn("\"qazterion = qz_cli.app:main\"", setup_text)


    def test_release_infrastructure_files_exist(self):
        root = Path(__file__).parent

        self.assertTrue((root / "LICENSE").is_file())
        self.assertTrue((root / "SECURITY.md").is_file())
        self.assertTrue((root / "CONTRIBUTING.md").is_file())
        self.assertTrue((root / "CHANGELOG.md").is_file())
        self.assertTrue((root / ".github" / "workflows" / "ci.yml").is_file())
        self.assertTrue((root / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").is_file())
        self.assertTrue((root / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").is_file())

    def test_desktop_backend_health_and_test_key_integration(self):
        backend = DesktopBackend()
        health = backend.check_health(workspace=self.workspace)
        self.assertIsInstance(health, dict)
        self.assertIn("overallStatus", health)

        # test_key with empty key
        t_res = backend.test_key("gemini", "")
        self.assertFalse(t_res["connected"])


if __name__ == "__main__":
    unittest.main()
