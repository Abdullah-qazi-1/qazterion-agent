import itertools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qz_proxy_manager import ProxyManager


def _fake_process(pid=1234, alive=True, returncode=None):
    process = MagicMock()
    process.pid = pid
    process.poll.return_value = None if alive else (returncode if returncode is not None else 1)
    return process


class ProxyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "config.yaml").write_text("model_list: []\n", encoding="utf-8")
        self.manager = ProxyManager(
            config_path=self.workspace / "config.yaml",
            log_path=self.workspace / "proxy.log",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_start_skips_launch_when_already_healthy(self):
        with patch.object(self.manager, "health_check", return_value=True), \
             patch("qz_proxy_manager.subprocess.Popen") as popen:
            status = self.manager.start()
            popen.assert_not_called()
            self.assertTrue(status.healthy)

    def test_start_launches_process_and_waits_for_health(self):
        process = _fake_process()
        health_calls = iter([False, False, True])
        with patch("qz_proxy_manager.subprocess.Popen", return_value=process) as popen, \
             patch.object(self.manager, "health_check", side_effect=lambda *a, **k: next(health_calls, True)), \
             patch("qz_proxy_manager.time.sleep"):
            status = self.manager.start(env_overrides={"GROQ_KEY_1": "x"}, wait_for_health=5)
            popen.assert_called_once()
            self.assertTrue(status.healthy)
            self.assertEqual(status.pid, 1234)
            self.assertFalse(status.crashed)

    def test_start_reports_clear_error_when_process_crashes_immediately(self):
        process = _fake_process(alive=False, returncode=1)
        self.manager.log_path.write_text("Traceback: bad config\n", encoding="utf-8")
        with patch("qz_proxy_manager.subprocess.Popen", return_value=process), \
             patch.object(self.manager, "health_check", return_value=False), \
             patch("qz_proxy_manager.time.sleep"):
            status = self.manager.start(wait_for_health=1)
            self.assertFalse(status.healthy)
            self.assertTrue(status.crashed)
            self.assertIn("bad config", status.last_error)

    def test_crash_after_successful_start_is_detected_on_next_status_call(self):
        process = _fake_process(alive=True)
        with patch("qz_proxy_manager.subprocess.Popen", return_value=process), \
             patch.object(self.manager, "health_check", side_effect=itertools.chain([False], itertools.repeat(True))), \
             patch("qz_proxy_manager.time.sleep"):
            self.manager.start()
        # Now the process dies unexpectedly, without stop() being called.
        process.poll.return_value = 1
        self.manager.log_path.write_text("segfault\n", encoding="utf-8")
        with patch.object(self.manager, "health_check", return_value=False):
            status = self.manager.status()
        self.assertTrue(status.crashed)
        self.assertIn("segfault", status.last_error)

    def test_stop_terminates_and_does_not_report_a_crash(self):
        process = _fake_process(alive=True)
        with patch("qz_proxy_manager.subprocess.Popen", return_value=process), \
             patch.object(self.manager, "health_check", side_effect=itertools.chain([False], itertools.repeat(True))), \
             patch("qz_proxy_manager.time.sleep"):
            self.manager.start()

        def _poll_after_terminate():
            process.poll.return_value = 0
            return 0

        process.wait.side_effect = lambda timeout=None: _poll_after_terminate()
        with patch.object(self.manager, "health_check", return_value=False):
            status = self.manager.stop()
        process.terminate.assert_called_once()
        self.assertFalse(status.crashed)

    def test_restart_stops_then_starts_with_fresh_env(self):
        with patch.object(self.manager, "stop") as stop, \
             patch.object(self.manager, "start", return_value="new-status") as start:
            result = self.manager.restart(env_overrides={"MISTRAL_KEY_1": "y"})
            stop.assert_called_once()
            start.assert_called_once_with(env_overrides={"MISTRAL_KEY_1": "y"}, wait_for_health=15.0)
            self.assertEqual(result, "new-status")

    def test_health_check_false_on_connection_error(self):
        with patch("qz_proxy_manager.urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertFalse(self.manager.health_check())

    def test_health_check_true_on_200(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch("qz_proxy_manager.urllib.request.urlopen", return_value=response):
            self.assertTrue(self.manager.health_check())


if __name__ == "__main__":
    unittest.main()