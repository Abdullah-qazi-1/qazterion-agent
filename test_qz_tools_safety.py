"""File-editing safety: encodings, line endings, secret placeholders, undo of agent edits, git guards."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qz_tools


def _git(ws, *args):
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=False)


class EncodingAndNewlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.patch = patch.object(qz_tools, "WORKSPACE", str(self.ws))
        self.patch.start()
        qz_tools.reset_task_state()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_patch_preserves_utf8_text_and_crlf_line_endings(self):
        path = self.ws / "greet.py"
        path.write_bytes("# café ✓ — naïve\r\ndef hi():\r\n    return 'héllo'\r\n".encode("utf-8"))
        self.assertIn("café ✓", qz_tools._impl_read_file("greet.py"))
        result = qz_tools._impl_apply_patch(
            "greet.py",
            "@@ -2,2 +2,3 @@\n def hi():\n-    return 'héllo'\n+    # ünïcode stays intact\n+    return 'héllo wörld'\n",
        )
        self.assertTrue(result.startswith("Patch applied"), result)
        data = path.read_bytes()
        self.assertEqual(
            data.decode("utf-8"),
            "# café ✓ — naïve\r\ndef hi():\r\n    # ünïcode stays intact\r\n    return 'héllo wörld'\r\n",
        )
        self.assertNotIn(b"\r\r", data)

    def test_legacy_single_byte_file_round_trips_untouched_bytes(self):
        path = self.ws / "legacy.txt"
        original = "caf\xe9 line\nsecond\n".encode("latin-1")
        path.write_bytes(original)
        qz_tools._impl_read_file("legacy.txt")
        qz_tools._impl_apply_patch("legacy.txt", "@@ -2,1 +2,1 @@\n-second\n+changed\n")
        self.assertEqual(path.read_bytes(), "caf\xe9 line\nchanged\n".encode("latin-1"))

    def test_appending_after_a_last_line_without_newline(self):
        path = self.ws / "a.txt"
        path.write_bytes(b"one\ntwo")
        qz_tools._impl_read_file("a.txt")
        result = qz_tools._impl_apply_patch("a.txt", "@@ -2,0 +3,1 @@\n+three\n")
        self.assertTrue(result.startswith("Patch applied"), result)
        self.assertEqual(path.read_bytes(), b"one\ntwo\nthree\n")

    def test_untouched_last_line_keeps_missing_newline(self):
        path = self.ws / "b.txt"
        path.write_bytes(b"one\ntwo")
        qz_tools._impl_read_file("b.txt")
        qz_tools._impl_apply_patch("b.txt", "@@ -1,1 +1,1 @@\n-one\n+ONE\n")
        self.assertEqual(path.read_bytes(), b"ONE\ntwo")

    def test_write_file_keeps_existing_crlf_style(self):
        path = self.ws / "c.txt"
        path.write_bytes(b"x\r\ny\r\n")
        qz_tools._impl_read_file("c.txt")
        qz_tools._impl_write_file("c.txt", "x\nz\n")
        self.assertEqual(path.read_bytes(), b"x\r\nz\r\n")

    def test_redaction_placeholders_are_never_written_back(self):
        path = self.ws / "config.py"
        path.write_text("TOKEN = 'abc'\n", encoding="utf-8")
        qz_tools._impl_read_file("config.py")
        refused = qz_tools._impl_write_file("config.py", "TOKEN = '[REDACTED:generic_secret]'\n")
        self.assertTrue(refused.startswith("Write refused"))
        refused_patch = qz_tools._impl_apply_patch("config.py", "@@ -1,1 +1,1 @@\n-TOKEN = 'abc'\n+TOKEN = '[REDACTED:x]'\n")
        self.assertTrue(refused_patch.startswith("Patch refused"))
        self.assertEqual(path.read_text(encoding="utf-8"), "TOKEN = 'abc'\n")


class ChangeTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.patch = patch.object(qz_tools, "WORKSPACE", str(self.ws))
        self.patch.start()
        qz_tools.reset_task_state()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_restore_undoes_only_the_agents_own_edits(self):
        existing = self.ws / "app.py"
        existing.write_text("user work in progress\n", encoding="utf-8")
        user_file = self.ws / "notes.txt"
        user_file.write_text("mine\n", encoding="utf-8")

        qz_tools.begin_change_tracking()
        qz_tools._impl_read_file("app.py")
        qz_tools._impl_write_file("app.py", "agent rewrite\n")
        qz_tools._impl_write_file("new_module.py", "x = 1\n")
        qz_tools._impl_read_file("notes.txt")
        qz_tools._impl_write_file("notes.txt", "agent edit\n")
        user_file.write_text("user changed it again\n", encoding="utf-8")  # concurrent user edit

        restored, skipped = qz_tools.restore_tracked_changes()
        self.assertEqual(sorted(restored), ["app.py", "new_module.py"])
        self.assertEqual(skipped, ["notes.txt"])
        self.assertEqual(existing.read_text(encoding="utf-8"), "user work in progress\n")
        self.assertFalse((self.ws / "new_module.py").exists())
        self.assertEqual(user_file.read_text(encoding="utf-8"), "user changed it again\n")

    def test_no_tracking_means_nothing_is_restored(self):
        qz_tools._impl_write_file("x.py", "1\n")
        self.assertEqual(qz_tools.restore_tracked_changes(), ([], []))
        self.assertTrue((self.ws / "x.py").exists())


class GitGuardTests(unittest.TestCase):
    def test_refuses_to_init_a_repository_in_the_home_directory(self):
        home = Path.home().resolve()
        with patch.object(qz_tools, "WORKSPACE", str(home)), patch.object(qz_tools, "_run_git") as run_git:
            run_git.return_value = subprocess.CompletedProcess([], 128, "", "not a git repository")
            message = qz_tools.ensure_git_repository()
        self.assertIn("refusing", message)
        self.assertFalse(any(call.args[0][:1] == ["init"] for call in run_git.call_args_list))

    def test_agent_commits_carry_a_trailer_and_only_they_can_be_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(qz_tools, "WORKSPACE", tmp):
            qz_tools.reset_task_state()
            _git(tmp, "init")
            _git(tmp, "config", "user.name", "Real User")
            _git(tmp, "config", "user.email", "user@example.com")
            Path(tmp, "base.txt").write_text("base\n", encoding="utf-8")
            _git(tmp, "add", "base.txt")
            _git(tmp, "commit", "-m", "user base")
            qz_tools._impl_write_file("feature.py", "x = 1\n")
            result = qz_tools.commit_changes(["feature.py"], "Add feature")
            self.assertTrue(result.startswith("Git commit created"), result)
            head = _git(tmp, "rev-parse", "HEAD").stdout.strip()
            self.assertTrue(qz_tools.is_agent_commit(head))
            self.assertIn(qz_tools.AGENT_COMMIT_TRAILER, _git(tmp, "log", "-1", "--format=%B").stdout)

            # A user commit on top must never be discarded by the agent's undo.
            Path(tmp, "user.txt").write_text("user\n", encoding="utf-8")
            _git(tmp, "add", "user.txt")
            _git(tmp, "commit", "-m", "user work")
            qz_tools._AGENT_COMMIT_HASHES.clear()  # simulate a new process
            refused = qz_tools._impl_rollback_last_change()
            self.assertIn("not created by Qazterion", refused)
            self.assertTrue(Path(tmp, "user.txt").exists())


if __name__ == "__main__":
    unittest.main()
