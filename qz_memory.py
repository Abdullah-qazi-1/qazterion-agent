"""Small, local project-memory store; repository files remain the authority."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _memory_path(workspace: str | Path) -> Path:
    root = Path(workspace).resolve()
    # A project-local store is portable with the project, while a user-level
    # fallback keeps read-only workspaces usable.
    local = root / ".qazterion" / "project-memory.jsonl"
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        return local
    except OSError:
        base = Path(os.environ.get("QAZTERION_DATA_DIR", Path.home() / ".qazterion"))
        base.mkdir(parents=True, exist_ok=True)
        return base / "project-memory.jsonl"


class ProjectMemory:
    """Append-only, relevance-filtered project facts (never chat transcripts)."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.path = _memory_path(self.workspace)

    def remember(self, category: str, content: str, *, tags: list[str] | None = None,
                 task_id: str | None = None) -> None:
        text = content.strip()
        if not text:
            return
        event = {"timestamp": time.time(), "category": category, "content": text,
                 "tags": sorted(set(tags or [])), "task_id": task_id}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def relevant(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        terms = {term.lower() for term in query.replace("/", " ").replace("_", " ").split()
                 if len(term) > 2}
        ranked: list[tuple[int, dict[str, Any]]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            haystack = (item["content"] + " " + " ".join(item.get("tags", []))).lower()
            score = sum(term in haystack for term in terms)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1].get("timestamp", 0)), reverse=True)
        return [item for _, item in ranked[:limit]]

