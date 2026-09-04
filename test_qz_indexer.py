import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qz_indexer
import qz_tools
import qz_agent


class CodebaseIndexerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "auth.py").write_text(
            '"""User login and session management."""\n\n'
            "class UserSession:\n    pass\n\n"
            "def login_user(email):\n    return email\n\n"
            "async def logout_user():\n    return None\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "database.py").write_text(
            "def connect_database():\n    return True\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "broken.py").write_text("def incomplete(:\n", encoding="utf-8")
        for number in range(50):
            (self.workspace / "src" / f"module_{number}.py").write_text(
                f"def feature_{number}():\n    return {number}\n", encoding="utf-8"
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_build_index_extracts_symbols_and_writes_cache(self):
        index = qz_indexer.build_index(self.workspace)
        entries = {entry["path"]: entry for entry in index["files"]}

        self.assertEqual(len(entries), 53)
        self.assertEqual(entries["src/auth.py"]["functions"], ["login_user", "logout_user"])
        self.assertEqual(entries["src/auth.py"]["classes"], ["UserSession"])
        self.assertIn("syntax errors", entries["src/broken.py"]["summary"])
        self.assertTrue((self.workspace / qz_indexer.INDEX_FILENAME).is_file())

    def test_cache_reuses_then_rebuilds_when_a_file_changes(self):
        first_index, rebuilt = qz_indexer.load_or_build_index(self.workspace)
        self.assertTrue(rebuilt)
        second_index, rebuilt = qz_indexer.load_or_build_index(self.workspace)
        self.assertFalse(rebuilt)
        self.assertEqual(first_index, second_index)

        target = self.workspace / "src" / "database.py"
        target.write_text("def connect_database():\n    return 'updated'\n", encoding="utf-8")
        _, rebuilt = qz_indexer.load_or_build_index(self.workspace)
        self.assertTrue(rebuilt)

    def test_search_finds_relevant_file_and_tool_returns_a_compact_result(self):
        index = qz_indexer.build_index(self.workspace)
        result = qz_indexer.search_index(index, "fix login session bug")
        self.assertEqual(result[0]["path"], "src/auth.py")

        with patch.object(qz_tools, "WORKSPACE", str(self.workspace)):
            tool_result = qz_tools.search_index("login session")
        self.assertIn("src/auth.py", tool_result)
        self.assertIn("login_user", tool_result)

    def test_index_cache_is_valid_json(self):
        qz_indexer.build_index(self.workspace)
        with (self.workspace / qz_indexer.INDEX_FILENAME).open(encoding="utf-8") as cache:
            cache = json.load(cache)
        self.assertEqual(cache["version"], qz_indexer.INDEX_VERSION)

    def test_semantic_search_uses_cached_embeddings_for_vague_queries(self):
        (self.workspace / "src" / "billing.py").write_text(
            '"""Invoice settlement and charge processing."""\n\n'
            "def settle_invoice(invoice_id):\n    return invoice_id\n",
            encoding="utf-8",
        )

        class FakeEmbeddingModel:
            def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
                vectors = []
                for text in texts:
                    text = text.lower()
                    vectors.append([1.0, 0.0] if any(word in text for word in ("payment", "invoice", "billing", "charge")) else [0.0, 1.0])
                return vectors

        with patch.object(qz_indexer, "_get_embedding_model", return_value=FakeEmbeddingModel()):
            index = qz_indexer.build_index(self.workspace)
            result = qz_indexer.search_index(index, "payment bug", limit=1)

        self.assertEqual(index["embedding_model"], qz_indexer.EMBEDDING_MODEL_NAME)
        self.assertIn("embedding", next(item for item in index["files"] if item["path"] == "src/billing.py"))
        self.assertEqual(result[0]["path"], "src/billing.py")

    def test_chunk_search_returns_only_the_relevant_section_of_a_large_file(self):
        source_lines = [f"unused_setting_{number} = {number}\n" for number in range(240)]
        source_lines.extend([
            'def authenticate_request(token):\n',
            '    """Validate a login token and create a user session."""\n',
            '    return token\n',
        ])
        source_lines.extend(f"later_setting_{number} = {number}\n" for number in range(240))
        (self.workspace / "src" / "large_service.py").write_text("".join(source_lines), encoding="utf-8")

        index = qz_indexer.build_index(self.workspace)
        chunks = qz_indexer.search_chunks(index, "login token authentication", limit=1)

        self.assertEqual(chunks[0]["path"], "src/large_service.py")
        self.assertLessEqual(chunks[0]["end_line"] - chunks[0]["start_line"] + 1, qz_indexer.CHUNK_LINES)
        with patch.object(qz_tools, "WORKSPACE", str(self.workspace)):
            result = qz_tools.read_relevant_chunks("login token authentication", limit=1)
        self.assertIn("authenticate_request", result)
        self.assertNotIn("later_setting_239", result)

    def test_agent_startup_passes_index_summary_to_planner_and_architect(self):
        with (
            patch.object(qz_agent, "WORKSPACE", str(self.workspace)),
            # Phase 11: mode classification now decides whether the planner/architect
            # are even called (a "quick" task bypasses them). Force "standard" so this
            # test still exercises the plan/architecture wiring it was written for.
            patch.object(qz_agent, "classify_task_mode", return_value="standard"),
            patch.object(qz_agent, "generate_clarifying_questions", return_value=[]),
            patch.object(qz_agent, "call_planner", return_value="plan") as planner,
            patch.object(qz_agent, "call_architect", return_value="architecture") as architect,
            patch.object(qz_agent, "run_executor") as executor,
            patch.object(qz_agent, "capture_pre_existing_test_failures", return_value=None),
        ):
            qz_agent.run_task("Fix the login flow")

        summary = planner.call_args.args[1]
        self.assertIn("src/auth.py", summary)
        self.assertEqual(architect.call_args.args, ("Fix the login flow", "plan", summary))
        self.assertEqual(executor.call_args.args, ("Fix the login flow", "plan", "architecture"))
        self.assertEqual(executor.call_args.kwargs.get("test_baseline"), None)
        self.assertTrue(executor.call_args.kwargs.get("task_id"))


if __name__ == "__main__":
    unittest.main()
