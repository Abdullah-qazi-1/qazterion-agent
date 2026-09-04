"""Tests for Phase 13: Advanced Codebase Intelligence and Incremental Indexing."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import qz_indexer


class Phase13IndexingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "src").mkdir()
        (self.workspace / "tests").mkdir()

        (self.workspace / "src" / "user.py").write_text(
            '"""User model and management."""\n\n'
            "import os\n"
            "from src.db import get_connection\n\n"
            "DEFAULT_ROLE = 'user'\n\n"
            "class User:\n"
            "    def __init__(self, username: str):\n"
            "        self.username = username\n\n"
            "    def get_role(self) -> str:\n"
            "        return DEFAULT_ROLE\n\n"
            "    async def save_async(self):\n"
            "        return True\n\n"
            "def create_user(username: str) -> User:\n"
            "    return User(username)\n",
            encoding="utf-8",
        )

        (self.workspace / "src" / "db.py").write_text(
            "import sqlite3\n\n"
            "def get_connection():\n"
            "    return sqlite3.connect(':memory:')\n",
            encoding="utf-8",
        )

        (self.workspace / "src" / "app.ts").write_text(
            "import { User } from './user';\n"
            "export interface ServerConfig { port: number; }\n"
            "export class AppServer {\n"
            "    start() { return true; }\n"
            "}\n"
            "export function launch() { return new AppServer(); }\n",
            encoding="utf-8",
        )

        (self.workspace / "src" / "service.go").write_text(
            "package main\n\n"
            "import \"fmt\"\n\n"
            "type Handler struct {}\n\n"
            "func HandleRequest() {\n"
            "    fmt.Println(\"ok\")\n"
            "}\n",
            encoding="utf-8",
        )

        (self.workspace / "src" / "malformed.py").write_text("def invalid_syntax(:\n", encoding="utf-8")

        # Binary file
        with open(self.workspace / "src" / "image.png", "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

        # Ignored directory content
        node_modules = self.workspace / "node_modules" / "pkg"
        node_modules.mkdir(parents=True)
        (node_modules / "index.js").write_text("console.log('vendor');", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initial_indexing_extracts_symbols_methods_and_dependencies(self):
        index = qz_indexer.build_index(self.workspace)
        files = {e["path"]: e for e in index["files"]}

        self.assertIn("src/user.py", files)
        self.assertIn("src/db.py", files)
        self.assertIn("src/app.ts", files)
        self.assertIn("src/service.go", files)
        self.assertNotIn("node_modules/pkg/index.js", files)
        self.assertNotIn("src/image.png", files)

        user_entry = files["src/user.py"]
        self.assertEqual(user_entry["classes"], ["User"])
        self.assertEqual(user_entry["functions"], ["create_user"])
        self.assertIn("User.get_role", user_entry["methods"])
        self.assertIn("User.save_async", user_entry["methods"])
        self.assertIn("os", user_entry["imports"])
        self.assertIn("src.db.get_connection", user_entry["imports"])
        self.assertIn("DEFAULT_ROLE", user_entry["symbols"])

        # Dependencies graph
        self.assertIn("src/db.py", user_entry["dependencies"])

        # TypeScript symbols
        ts_entry = files["src/app.ts"]
        self.assertIn("AppServer", ts_entry["classes"])
        self.assertIn("ServerConfig", ts_entry["symbols"])
        self.assertIn("launch", ts_entry["functions"])

        # Go symbols
        go_entry = files["src/service.go"]
        self.assertIn("Handler", go_entry["classes"])
        self.assertIn("HandleRequest", go_entry["functions"])

        # Resilient handling of malformed file
        malformed_entry = files["src/malformed.py"]
        self.assertIn("syntax errors", malformed_entry["summary"].lower())

    def test_incremental_indexing_reuses_unchanged_entries(self):
        first_index, rebuilt = qz_indexer.load_or_build_index(self.workspace)
        self.assertTrue(rebuilt)
        self.assertEqual(len(first_index["files"]), 5)

        # Second load without edits should be cached
        cached_index, rebuilt = qz_indexer.load_or_build_index(self.workspace)
        self.assertFalse(rebuilt)

        # Modify only one file
        db_file = self.workspace / "src" / "db.py"
        db_file.write_text("def get_connection():\n    return 'connected'\n", encoding="utf-8")

        updated_index, rebuilt = qz_indexer.load_or_build_index(self.workspace)
        self.assertTrue(rebuilt)

        files = {e["path"]: e for e in updated_index["files"]}
        self.assertEqual(len(files), 5)
        self.assertEqual(files["src/user.py"]["classes"], ["User"])

    def test_incremental_indexing_handles_deleted_and_new_files(self):
        qz_indexer.build_index(self.workspace)

        # Add new file
        (self.workspace / "src" / "new_module.py").write_text("def new_func(): pass\n", encoding="utf-8")
        # Delete old file
        (self.workspace / "src" / "malformed.py").unlink()

        updated_index, rebuilt = qz_indexer.load_or_build_index(self.workspace)
        self.assertTrue(rebuilt)

        files = {e["path"]: e for e in updated_index["files"]}
        self.assertIn("src/new_module.py", files)
        self.assertNotIn("src/malformed.py", files)


if __name__ == "__main__":
    unittest.main()
