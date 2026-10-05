"""Per-user storage locations and one-time ``.env`` loading.

Every module that persists state (keystore, task database, usage log, provider
health, index cache, project memory) resolves its location here, so state never
lands inside the user's project or the installed package directory.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
from pathlib import Path

_ENV_LOCK = threading.Lock()
_ENV_LOADED = False


def data_dir() -> Path:
    """Return (and create) the per-user Qazterion data directory.

    ``QAZTERION_DATA_DIR`` overrides the default, which is
    ``%LOCALAPPDATA%\\Qazterion`` on Windows and ``~/.qazterion`` elsewhere.
    """
    override = os.environ.get("QAZTERION_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Qazterion"
    else:
        base = Path.home() / ".qazterion"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir()) / "qazterion"
        base.mkdir(parents=True, exist_ok=True)
    return base


def workspace_key(workspace: str | os.PathLike[str]) -> str:
    """Stable short identifier for a workspace path (used for per-project caches)."""
    normalized = os.path.normcase(str(Path(workspace).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def workspace_state_dir(workspace: str | os.PathLike[str]) -> Path:
    """Per-project state directory kept outside the project tree."""
    path = data_dir() / "workspaces" / workspace_key(workspace)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_environment(force: bool = False) -> None:
    """Load ``.env`` files once per process (never overriding real env vars).

    Looks in the current directory and in the Qazterion installation directory.
    Safe to call repeatedly; only the first call does any work.
    """
    global _ENV_LOADED
    with _ENV_LOCK:
        if _ENV_LOADED and not force:
            return
        _ENV_LOADED = True
        if os.environ.get("QAZTERION_SKIP_DOTENV", "").strip().lower() in ("1", "true", "yes"):
            return
        try:
            from dotenv import load_dotenv
        except ImportError:
            return
        candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
        seen: set[str] = set()
        for candidate in candidates:
            key = os.path.normcase(str(candidate))
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            load_dotenv(dotenv_path=candidate, override=False)
