"""Tests for Phase 13: Hybrid Context Retrieval, Ranking, and Context Budgeting."""

import tempfile
import unittest
from pathlib import Path

import qz_context
import qz_indexer


class Phase13RetrievalContextTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "src").mkdir()

        (self.workspace / "src" / "auth.py").write_text(
            '"""Authentication module."""\n\n'
            "from src.token_service import generate_token\n\n"
            "def authenticate_user(username, password):\n"
            "    token = generate_token(username)\n"
            "    return token\n",
            encoding="utf-8",
        )

        (self.workspace / "src" / "token_service.py").write_text(
            '"""JWT token generation."""\n\n'
            "def generate_token(user_id):\n"
            "    return f'token_{user_id}'\n",
            encoding="utf-8",
        )

        (self.workspace / "src" / "billing.py").write_text(
            '"""Billing and subscription logic."""\n\n'
            "def process_subscription_payment(customer_id, amount):\n"
            "    return {'status': 'paid', 'amount': amount}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_hybrid_retrieval_finds_relevant_files_snippets_and_dependencies(self):
        retriever = qz_context.HybridRetriever(self.workspace)
        files, snippets, deps = retriever.retrieve("authenticate user token login")

        file_paths = [f["path"] for f in files]
        self.assertIn("src/auth.py", file_paths)
        # auth.py imports token_service.py so token_service is in dependencies
        self.assertIn("src/token_service.py", deps)

        # Snippets should contain the function
        snippet_paths = [s.path for s in snippets]
        self.assertIn("src/auth.py", snippet_paths)

    def test_context_budget_manager_enforces_budget_limits_and_formats_prompt(self):
        mgr = qz_context.ContextBudgetManager(default_budget_tokens=150)
        ctx = mgr.select_context("authenticate user and billing", workspace=self.workspace)

        self.assertLessEqual(ctx.total_tokens, 150)
        self.assertIsInstance(ctx.snippets, list)

        formatted = ctx.format_for_prompt()
        self.assertIn("=== RETRIEVED CODEBASE CONTEXT ===", formatted)
        self.assertIn("src/auth.py", formatted)
        self.assertIn("trusted=\"false\"", formatted)

    def test_token_estimation(self):
        text = "def hello_world():\n    return 'Hello, Qazterion!'\n"
        tokens = qz_context.estimate_tokens(text)
        self.assertGreater(tokens, 0)
        self.assertLess(tokens, 50)
        self.assertEqual(qz_context.estimate_tokens(""), 0)


if __name__ == "__main__":
    unittest.main()
