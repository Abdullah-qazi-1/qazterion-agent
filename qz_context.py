"""Hybrid context retrieval and context budget management for Qazterion."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qz_indexer import (
    INDEX_FILENAME,
    _doc_tokens,
    _get_embedding_model,
    _normalized_embeddings,
    _query_embedding,
    _symbol_tokens,
    _cosine_similarity,
    load_or_build_index,
    search_chunks,
    search_index,
)
from qz_security.injection_guard import wrap_untrusted_content
from qz_tools import WORKSPACE


_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def estimate_tokens(text: str) -> int:
    """Safe, fast token estimation (approx 3.75 chars per token with word-boundary heuristic)."""
    if not text:
        return 0
    words = len(text.split())
    chars = len(text)
    return max(1, int(chars / 3.8 + words * 0.1))


@dataclass
class RetrievedSnippet:
    path: str
    start_line: int
    end_line: int
    content: str
    symbols: list[str] = field(default_factory=list)
    score: float = 0.0
    reason: str = "match"
    estimated_tokens: int = 0

    def __post_init__(self):
        if not self.estimated_tokens:
            self.estimated_tokens = estimate_tokens(self.content)


@dataclass
class RetrievedContext:
    query: str
    files: list[dict[str, Any]] = field(default_factory=list)
    snippets: list[RetrievedSnippet] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    total_tokens: int = 0
    budget_limit: int = 8000
    truncated: bool = False

    def format_for_prompt(self) -> str:
        """Format retrieved context safely as untrusted repository data with line references."""
        if not self.files and not self.snippets:
            return ""

        sections = []
        if self.files:
            file_lines = ["Relevant Files:"]
            for f in self.files[:8]:
                syms = f.get("functions", []) + f.get("classes", [])
                sym_str = f" (symbols: {', '.join(syms[:6])})" if syms else ""
                file_lines.append(f"- {f['path']}{sym_str}: {f.get('summary', '')}")
            sections.append("\n".join(file_lines))

        if self.snippets:
            snippet_lines = ["Relevant Code Snippets:"]
            for s in self.snippets:
                sym_info = f" [symbols: {', '.join(s.symbols)}]" if s.symbols else ""
                header = f"--- {s.path}:{s.start_line}-{s.end_line}{sym_info} ({s.reason}) ---"
                snippet_lines.append(f"{header}\n{s.content}")
            sections.append("\n\n".join(snippet_lines))

        if self.dependencies:
            sections.append(f"Connected Dependencies: {', '.join(self.dependencies[:10])}")

        raw_context = "\n\n".join(sections)
        wrapped = wrap_untrusted_content(raw_context, label="retrieved_codebase_context")
        return (
            "=== RETRIEVED CODEBASE CONTEXT ===\n"
            f"{wrapped}\n"
            "===================================\n"
        )


class HybridRetriever:
    """Combines exact text, symbol search, path relevance, AST dependencies, and semantic search."""

    def __init__(self, workspace: str | Path | None = None) -> None:
        self.workspace = Path(workspace or WORKSPACE).resolve()

    def retrieve(
        self,
        query: str,
        index: dict[str, Any] | None = None,
        limit_files: int = 5,
        limit_snippets: int = 5,
        recent_files: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[RetrievedSnippet], list[str]]:
        """Retrieve most relevant files, line snippets, and connected dependencies."""
        if index is None:
            index, _ = load_or_build_index(self.workspace)

        tokens = set(_TOKEN_RE.findall(query.lower()))
        files = index.get("files", [])
        if not files or not tokens:
            return [], [], []

        # 1. Scored files via search_index
        matched_files = search_index(index, query, limit=limit_files * 2)

        # 2. Boost recently modified/touched files
        if recent_files:
            recent_set = {f.replace("\\", "/").lower() for f in recent_files}
            for f in files:
                if f["path"].lower() in recent_set and f not in matched_files:
                    matched_files.insert(0, f)

        # 3. Collect dependency paths
        dependencies: set[str] = set()
        for f in matched_files[:limit_files]:
            for dep in f.get("dependencies", []):
                dependencies.add(dep)

        # 4. Scored chunks / snippets
        raw_chunks = search_chunks(index, query, limit=limit_snippets * 2)
        snippets: list[RetrievedSnippet] = []

        for chunk in raw_chunks:
            rel_path = chunk["path"]
            full_path = self.workspace / rel_path
            if not full_path.is_file():
                continue
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
                    lines = handle.readlines()
                s_line = max(1, chunk["start_line"])
                e_line = min(len(lines), chunk["end_line"])
                selected = lines[s_line - 1:e_line]
                numbered = "".join(f"{i:>5}\t{line}" for i, line in enumerate(selected, start=s_line))
                snippets.append(
                    RetrievedSnippet(
                        path=rel_path,
                        start_line=s_line,
                        end_line=e_line,
                        content=numbered.rstrip(),
                        symbols=chunk.get("symbols", []),
                        score=1.0,
                        reason="lexical/semantic relevance",
                    )
                )
            except OSError:
                continue

        return matched_files[:limit_files], snippets[:limit_snippets], sorted(dependencies)


class ContextBudgetManager:
    """Manages token budgets dynamically adapting to model context limits."""

    def __init__(
        self,
        default_budget_tokens: int = 8000,
        max_context_ratio: float = 0.60,
    ) -> None:
        self.default_budget = default_budget_tokens
        self.max_context_ratio = max_context_ratio

    def calculate_budget(self, model_alias: str | None = None) -> int:
        """Derive safe context token budget from model metadata context window."""
        if not model_alias:
            return self.default_budget

        try:
            from qz_providers.model_registry import get_model_registry
            meta = get_model_registry().get_model_by_alias(model_alias)
            if meta and meta.context_window:
                # Reserve 40% for system prompt, user instructions, conversation turns, and generation
                budget = int(meta.context_window * self.max_context_ratio)
                return max(2000, min(budget, 32000))
        except Exception:
            pass
        return self.default_budget

    def select_context(
        self,
        query: str,
        workspace: str | Path | None = None,
        model_alias: str | None = None,
        recent_files: list[str] | None = None,
        critical_files: list[str] | None = None,
    ) -> RetrievedContext:
        """Retrieve, rank, deduplicate, and fit context within the token budget."""
        ws = Path(workspace or WORKSPACE).resolve()
        budget = self.calculate_budget(model_alias)
        retriever = HybridRetriever(ws)

        files, snippets, deps = retriever.retrieve(
            query,
            limit_files=8,
            limit_snippets=8,
            recent_files=recent_files,
        )

        selected_snippets: list[RetrievedSnippet] = []
        seen_ranges: set[tuple[str, int, int]] = set()
        accumulated_tokens = 0
        truncated = False

        # If critical files are specified, prioritize chunks from them
        if critical_files:
            crit_set = {f.replace("\\", "/").lower() for f in critical_files}
            for s in snippets:
                if s.path.lower() in crit_set:
                    range_key = (s.path, s.start_line, s.end_line)
                    if range_key not in seen_ranges:
                        if accumulated_tokens + s.estimated_tokens <= budget:
                            selected_snippets.append(s)
                            seen_ranges.add(range_key)
                            accumulated_tokens += s.estimated_tokens
                        else:
                            truncated = True

        for s in snippets:
            range_key = (s.path, s.start_line, s.end_line)
            if range_key in seen_ranges:
                continue
            if accumulated_tokens + s.estimated_tokens <= budget:
                selected_snippets.append(s)
                seen_ranges.add(range_key)
                accumulated_tokens += s.estimated_tokens
            else:
                truncated = True
                break

        return RetrievedContext(
            query=query,
            files=files,
            snippets=selected_snippets,
            dependencies=deps,
            total_tokens=accumulated_tokens,
            budget_limit=budget,
            truncated=truncated,
        )
