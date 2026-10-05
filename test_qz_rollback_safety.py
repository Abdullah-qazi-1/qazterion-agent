"""Checkpoint rollback against a real git repository: only Qazterion commits are ever discarded."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qz_tools
from qz_recovery.resume_manager import ResumeManager
from qz_tasks import task_manager


def git(ws, *args):
    result = subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=False)
    return result.stdout.strip()


class RollbackSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()
        self.patch = patch.object(qz_tools, "WORKSPACE", str(self.ws))
        self.patch.start()
        qz_tools.reset_task_state()
        git(self.ws, "init")
        git(self.ws, "config", "user.name", "Real User")
        git(self.ws, "config", "user.email", "user@example.com")
        (self.ws / "app.py").write_text("v = 0\n", encoding="utf-8")
        git(self.ws, "add", "app.py")
        git(self.ws, "commit", "-m", "user baseline")
        self.base = git(self.ws, "rev-parse", "HEAD")
        self.task_id = task_manager.create_task("add features", str(self.ws))
        self.manager = ResumeManager(self.ws)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _agent_step(self, content: str, message: str) -> int:
        qz_tools._impl_read_file("app.py")
        qz_tools._impl_write_file("app.py", content)
        result = qz_tools.commit_changes(["app.py"], message)
        self.assertTrue(result.startswith("Git commit created"), result)
        return task_manager.create_checkpoint(self.task_id, 1, git_commit_hash=git(self.ws, "rev-parse", "HEAD"), summary=message)

    def test_whole_task_rollback_restores_the_pre_task_state(self):
        self._agent_step("v = 1\n", "step one")
        self._agent_step("v = 2\n", "step two")
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, workspace=self.ws)
        self.assertTrue(outcome.success, outcome.message)
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), self.base)
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "v = 0\n")

    def test_rolling_back_to_a_checkpoint_undoes_that_step_and_later_ones(self):
        first = self._agent_step("v = 1\n", "step one")
        second = self._agent_step("v = 2\n", "step two")
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, checkpoint_id=second, workspace=self.ws)
        self.assertTrue(outcome.success, outcome.message)
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "v = 1\n")
        self.assertTrue(first)

    def test_user_commits_after_the_task_block_rollback(self):
        self._agent_step("v = 1\n", "step one")
        (self.ws / "mine.txt").write_text("precious\n", encoding="utf-8")
        git(self.ws, "add", "mine.txt")
        git(self.ws, "commit", "-m", "user commit on top")
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, workspace=self.ws, force=True)
        self.assertFalse(outcome.success)
        self.assertIn("not made by Qazterion", outcome.message)
        self.assertTrue((self.ws / "mine.txt").exists())

    def test_uncommitted_work_blocks_rollback_even_with_force(self):
        self._agent_step("v = 1\n", "step one")
        (self.ws / "app.py").write_text("v = 1  # user tweak, not committed\n", encoding="utf-8")
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, workspace=self.ws, force=True)
        self.assertFalse(outcome.success)
        self.assertIn("uncommitted", outcome.message)
        self.assertIn("user tweak", (self.ws / "app.py").read_text(encoding="utf-8"))

    def test_untracked_files_survive_rollback(self):
        self._agent_step("v = 1\n", "step one")
        (self.ws / "draft.txt").write_text("unsaved idea\n", encoding="utf-8")
        (self.ws / "__pycache__").mkdir()
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, workspace=self.ws)
        self.assertTrue(outcome.success, outcome.message)
        self.assertEqual((self.ws / "draft.txt").read_text(encoding="utf-8"), "unsaved idea\n")

    def test_rollback_never_overwrites_an_untracked_file(self):
        (self.ws / "old.txt").write_text("tracked\n", encoding="utf-8")
        git(self.ws, "add", "old.txt")
        git(self.ws, "commit", "-m", "user adds old.txt")
        (self.ws / "old.txt").unlink()
        git(self.ws, "rm", "-q", "--cached", "old.txt")
        git(self.ws, "commit", "-m", "remove old.txt", "-m", qz_tools.AGENT_COMMIT_TRAILER)
        task_manager.create_checkpoint(self.task_id, 1, git_commit_hash=git(self.ws, "rev-parse", "HEAD"))
        (self.ws / "old.txt").write_text("new user file\n", encoding="utf-8")  # untracked now
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, workspace=self.ws)
        self.assertFalse(outcome.success)
        self.assertIn("overwrite untracked", outcome.message)
        self.assertEqual((self.ws / "old.txt").read_text(encoding="utf-8"), "new user file\n")

    def test_missing_or_foreign_checkpoints_never_fall_back_to_head_minus_one(self):
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, workspace=self.ws)
        self.assertFalse(outcome.success)
        self.assertIn("no recorded checkpoint", outcome.message)
        outcome = self.manager.safe_rollback_to_checkpoint(self.task_id, checkpoint_id=999_999, workspace=self.ws)
        self.assertFalse(outcome.success)
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), self.base)

    def test_generated_index_does_not_make_the_tree_dirty(self):
        from qz_indexer import load_or_build_index

        self._agent_step("v = 1\n", "step one")
        load_or_build_index(self.ws)
        self.assertEqual(git(self.ws, "status", "--porcelain"), "")
        preview = self.manager.preview_rollback(self.task_id, workspace=self.ws)
        self.assertTrue(preview["can_rollback"], preview["message"])


if __name__ == "__main__":
    unittest.main()
