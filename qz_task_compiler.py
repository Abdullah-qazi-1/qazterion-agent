"""Deterministic task analysis that augments, never replaces, the agent flow."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any

from qz_context import ContextBudgetManager
from qz_memory import ProjectMemory


@dataclass
class CompiledTask:
    goal: str
    requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    affected_areas: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    complexity: str = "simple"
    capabilities: list[str] = field(default_factory=lambda: ["coding"])
    clarifications: list[str] = field(default_factory=list)
    project_memory: list[dict[str, Any]] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskCompiler:
    """Local compiler: no model calls and no prior-conversation injection."""

    def compile(self, request: str, workspace: str | Path) -> CompiledTask:
        text = request.strip()
        lower = text.lower()
        requirements = [line.strip(" -•\t") for line in text.splitlines()
                        if line.strip().startswith(("-", "*", "•"))][:12]
        constraints = [phrase for phrase in (
            "preserve existing architecture" if any(x in lower for x in ("do not redesign", "preserve", "backward compatible")) else "",
            "avoid unnecessary API requests" if any(x in lower for x in ("zero-waste", "do not waste", "no extra ai")) else "",
            "do not expose secrets" if any(x in lower for x in ("api key", "secret", "credential")) else "",
        ) if phrase]
        validation = [name for name, markers in {
            "tests": ("test", "pytest", "vitest"), "build": ("build", "package"),
            "lint": ("lint",), "typecheck": ("typecheck", "typescript"),
            "startup": ("startup", "exe", "install"),
        }.items() if any(marker in lower for marker in markers)]
        complexity = "complex" if len(text) > 800 or len(requirements) > 4 else "simple"
        capabilities = ["coding"]
        if any(x in lower for x in ("debug", "root cause", "failure")): capabilities.append("reasoning")
        if "image" in lower or "vision" in lower: capabilities.append("vision")
        memory = ProjectMemory(workspace).relevant(text)
        try:
            selected = ContextBudgetManager().select_context(text, workspace=workspace)
            files = [str(item.get("path")) for item in selected.files if item.get("path")][:16]
        except Exception:
            files = []
        # Ask only where the request explicitly leaves a high-impact choice open.
        clarifications: list[str] = []
        if re.search(r"\b(migrate|delete|remove)\b", lower) and not re.search(r"\b(yes|approved|safe)\b", lower):
            clarifications.append("Should this destructive/data-migration change preserve existing user data?")
        return CompiledTask(text, requirements, constraints, files, validation, complexity,
                            capabilities, clarifications, memory, files)

