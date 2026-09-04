"""Contain filesystem paths inside a workspace after resolving symlinks and junctions."""

from __future__ import annotations

import os
from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a path is missing, malformed, or outside the workspace."""


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def contained_in(path: Path, root: Path) -> bool:
    path_s = _norm(path)
    root_s = _norm(root)
    if path_s == root_s:
        return True
    prefix = root_s if root_s.endswith(os.sep) else root_s + os.sep
    return path_s.startswith(prefix)


def resolve_workspace_path(path: str | os.PathLike[str], workspace: str | os.PathLike[str]) -> Path:
    """Resolve ``path`` against ``workspace`` and reject anything that escapes the root.

    Existing path components are resolved with ``Path.resolve()``, which follows
    symlinks and Windows junctions. ``..``, absolute paths, and prefix-sibling
    names (``project`` vs ``project-other``) are rejected when they leave the root.
    Environment-variable expansion is intentionally not applied.
    """
    if path is None:
        raise WorkspacePathError("Path is required")
    raw = str(path)
    if "\x00" in raw:
        raise WorkspacePathError("Path contains a NUL byte")

    root = Path(workspace).resolve()
    text = raw.strip() or "."
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise WorkspacePathError(f"Path could not be resolved: {path}") from error

    if not contained_in(resolved, root):
        raise WorkspacePathError(f"Path is outside the workspace: {path}")
    return resolved
