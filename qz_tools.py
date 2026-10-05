"""
Core agent tools for reading and writing files and running terminal commands.
For safety, every operation is restricted to the CURRENT WORKING DIRECTORY
— the directory where the terminal is opened is the workspace.
"""

import hashlib
import os
import re
import shlex
import subprocess

from qz_sandbox.backend import NO_WINDOW
import sys
import threading
from pathlib import Path

from qz_environment import ProjectEnvironment, detect_project_environment
from qz_indexer import format_search_results, load_or_build_index, search_chunks as find_chunks, search_index as find_in_index
from qz_sandbox.manager import get_manager
from qz_security.gateway import configure as configure_security_gateway, execute as security_execute
from qz_security.workspace_guard import resolve_workspace_path

WORKSPACE = os.getcwd()
_PROJECT_PYTHON: str | None = None
_FILE_SNAPSHOTS: dict[str, str] = {}
_AGENT_CREATED_FILES: set[str] = set()
_AGENT_COMMIT_HASHES: set[str] = set()
# Original bytes of every file the agent modified since begin_change_tracking();
# ``None`` means the agent created the file. Used to undo a failed subtask.
_CHANGE_LOG: dict[str, bytes | None] | None = None
_STATE_LOCK = threading.RLock()

AGENT_COMMIT_TRAILER = "Committed-by: Qazterion"
REDACTION_MARKER = "[REDACTED:"


def current_workspace() -> str:
    """The workspace of the active task (thread-bound context first, then WORKSPACE)."""
    try:
        from qz_core.common import get_task_context
        ctx = get_task_context()
        if ctx is not None:
            return str(ctx.workspace)
    except Exception:
        pass
    return WORKSPACE


_current_workspace = current_workspace


def reset_task_state() -> None:
    """Forget per-task file snapshots so a new task starts from a clean slate."""
    global _CHANGE_LOG
    with _STATE_LOCK:
        _FILE_SNAPSHOTS.clear()
        _AGENT_CREATED_FILES.clear()
        _CHANGE_LOG = None


def begin_change_tracking() -> None:
    global _CHANGE_LOG
    with _STATE_LOCK:
        _CHANGE_LOG = {}


def _remember_original(full: str) -> None:
    with _STATE_LOCK:
        if _CHANGE_LOG is None or full in _CHANGE_LOG:
            return
        try:
            with open(full, "rb") as handle:
                _CHANGE_LOG[full] = handle.read()
        except FileNotFoundError:
            _CHANGE_LOG[full] = None
        except OSError:
            pass


def restore_tracked_changes() -> tuple[list[str], list[str]]:
    """Undo agent edits recorded since begin_change_tracking().

    A file is restored only if it still holds exactly what the agent last wrote,
    so edits made by the user meanwhile are never overwritten. Returns
    ``(restored_paths, skipped_paths)`` relative to the workspace.
    """
    global _CHANGE_LOG
    restored: list[str] = []
    skipped: list[str] = []
    workspace = Path(current_workspace()).resolve()
    with _STATE_LOCK:
        log = _CHANGE_LOG or {}
        for full, original in log.items():
            try:
                rel = Path(full).resolve().relative_to(workspace).as_posix()
            except ValueError:
                rel = full
            current = _file_sha256(full)
            if current is not None and current != _FILE_SNAPSHOTS.get(full):
                skipped.append(rel)
                continue
            try:
                if original is None:
                    if os.path.exists(full):
                        os.remove(full)
                    _FILE_SNAPSHOTS.pop(full, None)
                    _AGENT_CREATED_FILES.discard(full)
                else:
                    with open(full, "wb") as handle:
                        handle.write(original)
                    _FILE_SNAPSHOTS[full] = hashlib.sha256(original).hexdigest()
                restored.append(rel)
            except OSError:
                skipped.append(rel)
        if _CHANGE_LOG is not None:
            _CHANGE_LOG = {}
    return restored, skipped


_UTF8_BOM = b"\xef\xbb\xbf"


def _decode(data: bytes) -> tuple[str, str]:
    """Decode file bytes, returning ``(text, encoding)`` without losing bytes."""
    if data.startswith(_UTF8_BOM):
        try:
            return data[3:].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # Legacy single-byte files: latin-1 maps every byte, so writing back with
        # the same codec reproduces untouched bytes exactly.
        return data.decode("latin-1"), "latin-1"


def _encode(text: str, encoding: str) -> bytes:
    if encoding == "utf-8-sig":
        return _UTF8_BOM + text.encode("utf-8")
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode("utf-8")


def _read_text_file(full: str) -> tuple[str, str, str]:
    """Return ``(text_with_lf_newlines, encoding, newline_style)``."""
    with open(full, "rb") as handle:
        data = handle.read()
    text, encoding = _decode(data)
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), encoding, newline


def _write_text_file(full: str, text: str, encoding: str = "utf-8", newline: str = "\n") -> None:
    normalized = text.replace("\r\n", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    with open(full, "wb") as handle:
        handle.write(_encode(normalized, encoding))


def _redaction_refusal(path: str, content: str) -> str | None:
    if REDACTION_MARKER in content:
        return (
            f"Write refused: content for '{path}' contains a '{REDACTION_MARKER}...]' placeholder. "
            "Secrets are masked in tool output; edit around them with apply_patch instead of "
            "rewriting lines that contain them."
        )
    return None


def _file_sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _refuse_stale_write(full: str, path: str) -> str | None:
    """Refuse overwrite when the file changed since the last agent read/write."""
    if not os.path.exists(full):
        return None
    current = _file_sha256(full)
    expected = _FILE_SNAPSHOTS.get(full)
    if expected is None:
        return (
            f"Write refused: '{path}' exists and was not read or written by the agent in this task. "
            "Read the file first, then write."
        )
    if current is not None and current != expected:
        return (
            f"Write refused: '{path}' changed since the last agent read/write "
            "(concurrent or external modification)."
        )
    return None


def configure_project_environment(workspace: str | os.PathLike[str] | None = None) -> ProjectEnvironment:
    """Select a detected project interpreter without changing the project."""
    global _PROJECT_PYTHON
    environment = detect_project_environment(workspace or _current_workspace())
    _PROJECT_PYTHON = str(environment.python_interpreter) if environment.python_interpreter else None
    return environment

_SENSITIVE_GIT_NAMES = {".env", ".env.local", ".env.production", ".env.development"}
_SENSITIVE_GIT_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _safe_path(path: str) -> str:
    return str(resolve_workspace_path(path, _current_workspace()))


def list_files(directory: str = ".", path: str | None = None) -> str:
    args: dict = {"directory": directory}
    if path is not None:
        args["path"] = path
    return security_execute("list_files", args)


def _impl_list_files(directory: str = ".", path: str | None = None, max_entries: int = 200) -> str:
    """List files/folders under a directory (default: workspace root) with bounded output."""
    if path is not None and directory == ".":
        directory = path
    target = _safe_path(directory)
    if not os.path.exists(target):
        return f"Directory not found: {directory}"
    lines = []
    total_count = 0
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", "venv", ".venv")]
        rel_root = os.path.relpath(root, target)
        for f in sorted(files):
            total_count += 1
            if len(lines) < max_entries:
                rel_path = os.path.join(rel_root, f) if rel_root != "." else f
                lines.append(rel_path)
    if total_count > max_entries:
        lines.append(f"... [Truncated: showing first {max_entries} of {total_count} files/directories. Use subdirectory path to narrow search]")
    return "\n".join(lines) if lines else "(empty)"


def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    args: dict = {"path": path}
    if start_line is not None:
        args["start_line"] = start_line
    if end_line is not None:
        args["end_line"] = end_line
    return security_execute("read_file", args)


def _impl_read_file(path: str, start_line: int | None = None, end_line: int | None = None, max_lines: int = 2000) -> str:
    """Return bounded file content with 1-based line numbers.
    
    Supports pagination with start_line and end_line parameters to protect context budget.
    """
    full = _safe_path(path)
    if not os.path.exists(full):
        return f"File not found: {path}"
    if os.path.isdir(full):
        return f"'{path}' is a directory; use list_files."
    try:
        text, _encoding, _newline = _read_text_file(full)
        lines = text.splitlines(keepends=True)
        with _STATE_LOCK:
            _FILE_SNAPSHOTS[full] = _file_sha256(full)
    except OSError as e:
        return f"Error reading file {path}: {e}"

    total_lines = len(lines)
    s = max(1, int(start_line)) if start_line is not None else 1
    e = min(total_lines, int(end_line)) if end_line is not None else total_lines
    if s > total_lines:
        return f"(empty range: file has {total_lines} lines, requested start line is {s})"
    if e < s:
        e = s

    is_truncated = False
    if (e - s + 1) > max_lines:
        e = s + max_lines - 1
        is_truncated = True

    selected_lines = lines[s - 1:e]
    numbered = "".join(f"{i:>5}\t{line}" for i, line in enumerate(selected_lines, start=s))
    if is_truncated or (start_line is None and end_line is None and total_lines > max_lines):
        numbered += f"\n... [TRUNCATED: Showing lines {s}-{e} of {total_lines}. Use read_file with start_line/end_line to view remaining lines]"
    return numbered if lines else ""


def write_file(path: str, content: str) -> str:
    return security_execute("write_file", {"path": path, "content": content})


def _impl_write_file(path: str, content: str) -> str:
    full = _safe_path(path)
    content = str(content)
    refusal = _redaction_refusal(path, content)
    if refusal:
        return refusal
    existed = os.path.exists(full)
    stale = _refuse_stale_write(full, path)
    if stale:
        return stale
    encoding, newline = "utf-8", "\n"
    if existed:
        try:
            _old_text, encoding, newline = _read_text_file(full)
        except OSError:
            pass
    if os.path.dirname(full):
        os.makedirs(os.path.dirname(full), exist_ok=True)
    _remember_original(full)
    _write_text_file(full, content, encoding, newline)
    with _STATE_LOCK:
        _FILE_SNAPSHOTS[full] = _file_sha256(full)
        if not existed:
            _AGENT_CREATED_FILES.add(full)
    return f"Written: {path} ({len(content)} chars)"


def make_directory(path: str) -> str:
    return security_execute("make_directory", {"path": path})


def _impl_make_directory(path: str) -> str:
    full = _safe_path(path)
    os.makedirs(full, exist_ok=True)
    return f"Directory created: {path}"


def search_index(query: str, limit: int = 3) -> str:
    return security_execute("search_index", {"query": query, "limit": limit})


def _impl_search_index(query: str, limit: int = 3) -> str:
    """Find source files relevant to a task without reading the complete codebase."""
    try:
        index, _ = load_or_build_index(_current_workspace())
        return format_search_results(find_in_index(index, query, limit=int(limit)))
    except (OSError, ValueError, TypeError) as e:
        return f"Index search failed: {e}"


def read_relevant_chunks(query: str, limit: int = 3) -> str:
    return security_execute("read_relevant_chunks", {"query": query, "limit": limit})


def _impl_read_relevant_chunks(query: str, limit: int = 3) -> str:
    """Read only relevant code ranges, leaving full-file reads available by choice."""
    try:
        index, _ = load_or_build_index(_current_workspace())
        chunks = find_chunks(index, query, limit=int(limit))
        if not chunks:
            return "No relevant code chunks found. Try search_index or read_file for broader context."

        rendered = []
        for chunk in chunks:
            full_path = _safe_path(chunk["path"])
            lines = _read_text_file(full_path)[0].splitlines(keepends=True)
            selected_lines = lines[chunk["start_line"] - 1:chunk["end_line"]]
            numbered = "".join(
                f"{i:>5}\t{line}" for i, line in enumerate(selected_lines, start=chunk["start_line"])
            )
            symbols = f"; symbols: {', '.join(chunk['symbols'])}" if chunk.get("symbols") else ""
            rendered.append(
                f"--- {chunk['path']}:{chunk['start_line']}-{chunk['end_line']}{symbols} ---\n{numbered.rstrip()}"
            )
        return "\n\n".join(rendered)
    except (OSError, ValueError, TypeError) as e:
        return f"Chunk retrieval failed: {e}"


# ============================================================
# Diff-based edits
# Dependency-free unified-diff parser and applier; no additional package is required.
# ============================================================

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_hunks(diff_text: str):
    """Extract change hunks from unified-diff text."""
    hunks = []
    current = None
    for line in diff_text.splitlines(keepends=True):
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            current = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "lines": [],
            }
            continue
        if current is None:
            # Ignore file headers (---, +++) before the first hunk.
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file" — ignore
            continue
        if line and line[0] in (" ", "+", "-"):
            current["lines"].append(line)
    if current is not None:
        hunks.append(current)

    for idx, h in enumerate(hunks):
        old_seen = sum(1 for line in h["lines"] if line[:1] in (" ", "-"))
        new_seen = sum(1 for line in h["lines"] if line[:1] in (" ", "+"))
        if old_seen != h["old_count"] or new_seen != h["new_count"]:
            raise ValueError(
                f"Hunk line count mismatch at hunk {idx + 1}: declared "
                f"-{h['old_count']} +{h['new_count']} but found {old_seen} old / {new_seen} new lines"
            )

    # Validate hunks against malformed/overlapping/duplicate ranges
    for idx, h in enumerate(hunks):
        if h["old_start"] < 0 or h["new_start"] < 0:
            raise ValueError(f"Invalid negative line number in hunk {idx + 1}")
        if idx > 0:
            prev = hunks[idx - 1]
            prev_end = prev["old_start"] + max(0, prev["old_count"] - 1)
            if h["old_start"] <= prev_end and not (prev["old_count"] == 0 and h["old_start"] == prev["old_start"]):
                raise ValueError(f"Overlapping or duplicate hunk detected at line {h['old_start']}")
    return hunks


def _apply_hunks(original_lines: list, hunks: list) -> list:
    """Apply hunks to original lines and return the updated lines.

    Validate context (' ') and removed ('-') lines against the actual file
    before applying. A stale or incorrect diff returns a clear error instead
    of silently corrupting the file.
    """
    if not hunks:
        raise ValueError("No valid hunk (@@ ... @@) was found in the diff")

    result = list(original_lines)
    offset = 0  # Line-count shift caused by earlier hunks

    for hunk in hunks:
        # Unified diff uses old_start=0 for an insertion into an empty file.
        # It maps to the first (0-indexed) position rather than -1.
        # A pure insertion ("-N,0") goes *after* original line N.
        if hunk["old_count"] == 0:
            pos = hunk["old_start"] + offset
        else:
            pos = hunk["old_start"] - 1 + offset
        if pos < 0 or pos > len(result):
            raise ValueError(
                f"Hunk line number ({hunk['old_start']}) is outside the file range. "
                "Read the current file content again with read_file."
            )
        new_segment = []
        idx = pos
        for line in hunk["lines"]:
            tag, content = line[0], line[1:]
            if tag in (" ", "-"):
                # The context or removed line must exactly match the current file.
                if idx >= len(result):
                    raise ValueError(
                        f"Could not apply patch: line {idx + 1} is beyond the end of the file "
                        "— the diff is based on an older file state. Read the CURRENT "
                        "content and create a diff with correct line numbers and context."
                    )
                actual = result[idx].rstrip("\n")
                expected = content.rstrip("\n")
                if actual != expected:
                    raise ValueError(
                        f"Context mismatch at line {idx + 1}: the diff expected context/removed line "
                        f"{expected!r} but the actual file line is {actual!r}. The diff is based on an old "
                        "or incorrect file state. Read the CURRENT content again and create a new diff with exact line numbers and context — "
                        "do not guess."
                    )
            if tag == " ":
                new_segment.append(content)
                idx += 1
            elif tag == "-":
                idx += 1  # Removed from the original; do not include in output
            elif tag == "+":
                new_segment.append(content)  # Newly added; do not advance the original pointer

        removed_count = idx - pos
        result[pos:pos + removed_count] = new_segment
        offset += len(new_segment) - removed_count

    return result


def apply_patch(path: str, diff: str) -> str:
    return security_execute("apply_patch", {"path": path, "diff": diff})


def _impl_apply_patch(path: str, diff: str) -> str:
    """Apply a unified-diff patch to an existing file without rewriting the entire file.
    Use write_file for new files; this tool is for existing files only.

        @@ -3,2 +3,3 @@
         unchanged context line
        -old line
        +new line
        +another new line
    """
    full = _safe_path(path)
    if not os.path.exists(full):
        return f"File not found: {path} (use write_file for a new file, not apply_patch)"

    stale = _refuse_stale_write(full, path)
    if stale:
        return stale.replace("Write refused", "Patch refused", 1)
    diff = str(diff).replace("\r\n", "\n")
    added_text = "".join(line[1:] for line in diff.splitlines(keepends=True) if line.startswith("+"))
    refusal = _redaction_refusal(path, added_text)
    if refusal:
        return refusal.replace("Write refused", "Patch refused", 1)

    text, encoding, newline = _read_text_file(full)
    missing_final_newline = bool(text) and not text.endswith("\n")
    original_lines = (text + "\n" if missing_final_newline else text).splitlines(keepends=True)

    try:
        hunks = _parse_hunks(diff)
        patched_lines = _apply_hunks(original_lines, hunks)
    except ValueError as e:
        return f"Could not apply patch: {e}. Read the complete file again with read_file and create a correct diff."

    patched_text = "".join(patched_lines)
    if missing_final_newline and patched_lines and patched_lines[-1] == original_lines[-1]:
        patched_text = patched_text[:-1]  # last line untouched: keep it without a newline
    _remember_original(full)
    _write_text_file(full, patched_text, encoding, newline)
    with _STATE_LOCK:
        _FILE_SNAPSHOTS[full] = _file_sha256(full)
    old_count, new_count = len(original_lines), len(patched_lines)
    return f"Patch applied: {path} ({old_count} → {new_count} lines)"


def run_command(command: str, timeout: int = 60) -> str:
    return security_execute("run_command", {"command": command, "timeout": timeout})


def _impl_run_command(command: str, timeout: int = 60) -> str:
    """Run an agent command on the host, inside the workspace.

    Windows uses PowerShell (matching the system prompt); other platforms use
    /bin/sh. A leading ``python`` is rewritten to the project's interpreter (or
    the one running Qazterion) because a fresh shell's PATH often lacks it.
    """
    try:
        timeout = max(1, min(int(timeout), 300))
    except (TypeError, ValueError):
        return "Invalid timeout: it must be a whole number of seconds."
    try:
        if _PROJECT_PYTHON is None:
            configure_project_environment(_current_workspace())
        is_windows = os.name == "nt"
        workspace = _current_workspace()
        interpreter = _PROJECT_PYTHON or sys.executable
        if re.match(r"^\s*python(?:\.exe)?(?=\s|$)", command, re.IGNORECASE):
            if is_windows:
                quoted = interpreter.replace("'", "''")
                command = re.sub(r"^\s*python(?:\.exe)?", lambda _m: f"& '{quoted}'", command, count=1, flags=re.IGNORECASE)
            else:
                command = re.sub(r"^\s*python(?:\.exe)?", lambda _m: shlex.quote(interpreter), command, count=1, flags=re.IGNORECASE)
        manager = get_manager()
        result = manager.execute(command, workspace, timeout)
        if result.timed_out:
            return f"Command did not complete within {timeout}s (timeout)."
        if result.error and result.exit_code == -1 and not result.stdout and not result.stderr:
            return f"Could not start command: {result.error}"
        # Python 3.14 made ``unittest discover -s tests`` require an
        # ``__init__.py`` in the start directory. Retry that one known case with
        # an in-memory loader instead of changing the user's source tree.
        start_directory = re.search(r"-m\s+unittest\s+discover\s+-s\s+([\w./\\-]+)\s*$", command, re.IGNORECASE)
        if result.exit_code and start_directory and "Start directory is not importable" in result.stderr:
            tests_path = start_directory.group(1)
            runner = (
                "import importlib.util, pathlib, sys, unittest; "
                "sys.path.insert(0, str(pathlib.Path('.').resolve())); "
                f"root = pathlib.Path(r'{tests_path}'); "
                "loader = unittest.defaultTestLoader; suite = unittest.TestSuite(); "
                "[(lambda spec: (lambda module: (spec.loader.exec_module(module), suite.addTests(loader.loadTestsFromModule(module))))(importlib.util.module_from_spec(spec)))(importlib.util.spec_from_file_location(f'_qazterion_test_{i}', path)) "
                "for i, path in enumerate(sorted(root.rglob('test*.py')))]; "
                "outcome = unittest.TextTestRunner(verbosity=1).run(suite); "
                "raise SystemExit(not outcome.wasSuccessful())"
            )
            if is_windows:
                retry = f"& '{interpreter.replace(chr(39), chr(39) * 2)}' -c '{runner.replace(chr(39), chr(39) * 2)}'"
            else:
                retry = f"{shlex.quote(interpreter)} -c {shlex.quote(runner)}"
            result = manager.execute(retry, workspace, timeout)
            if result.timed_out:
                return f"Command did not complete within {timeout}s (timeout)."

        output = f"exit_code={result.exit_code}\n"
        output += f"STDOUT:\n{result.stdout[-3000:]}\n"
        if result.stderr:
            output += f"STDERR:\n{result.stderr[-3000:]}\n"
        return output
    except OSError as e:
        return f"Could not start command: {e}"

# ============================================================
# PHASE 7: Git history and safe rollback
# ============================================================

def _run_git(args: list[str]) -> subprocess.CompletedProcess:
    """Run Git in the agent workspace without a shell with scrubbed environment."""
    from qz_sandbox.backend import sanitize_subprocess_env
    return subprocess.run(
        ["git", *args],
        cwd=_current_workspace(),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env=sanitize_subprocess_env(_current_workspace()),
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )


def _git_error(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or result.stdout or "Unknown Git error").strip()


def ensure_git_repository() -> str:
    """Create a repository for this workspace when it does not have one yet.

    Local author identity is set only if Git has no usable identity, keeping a
    user's existing global or repository identity untouched.
    """
    try:
        inside = _run_git(["rev-parse", "--is-inside-work-tree"])
    except OSError as error:
        return f"Git unavailable: {error}"

    initialized_here = inside.returncode != 0
    if initialized_here:
        workspace = Path(_current_workspace()).resolve()
        if workspace == Path.home().resolve() or workspace.parent == workspace:
            return (
                "Git unavailable: refusing to create a repository in your home directory or a drive root. "
                "Open a project folder instead."
            )
        initialized = _run_git(["init"])
        if initialized.returncode != 0:
            return f"Git init failed: {_git_error(initialized)}"

    # Check every workspace, including an existing repository: a Git clone can
    # legitimately have no local/global author configured yet.
    for key, value in (("user.name", "Qazterion Agent"), ("user.email", "qazterion@local")):
        configured = _run_git(["config", "--get", key])
        if configured.returncode != 0 or not configured.stdout.strip():
            saved = _run_git(["config", key, value])
            if saved.returncode != 0:
                return f"Git identity setup failed: {_git_error(saved)}"
    return "Git repository initialized for this workspace." if initialized_here else "Git repository ready."


def _commit_safe_path(path: str) -> str:
    """Return a validated, non-sensitive pathspec relative to the workspace."""
    full = Path(_safe_path(path))
    workspace = Path(_current_workspace()).resolve()
    relative = full.resolve().relative_to(workspace).as_posix()
    name = full.name.lower()
    if name in _SENSITIVE_GIT_NAMES or name.endswith(_SENSITIVE_GIT_SUFFIXES):
        raise ValueError(f"Sensitive file is not allowed in an automatic Git commit: {relative}")
    if relative == ".qazterion_index.json":
        raise ValueError("Generated index cache is not allowed in an automatic Git commit")
    return relative


def commit_changes(paths: list[str], message: str) -> str:
    """Commit only specified agent-changed files after a successful verification.

    This deliberately never uses ``git add .``: unrelated user edits and API
    credentials stay out of automatic commits.
    """
    ready = ensure_git_repository()
    if ready.startswith(("Git unavailable", "Git init failed", "Git identity setup failed")):
        return ready
    if not isinstance(paths, list) or not paths:
        return "Git commit skipped: no changed files to commit."

    try:
        safe_paths = list(dict.fromkeys(_commit_safe_path(str(path)) for path in paths))
    except ValueError as error:
        return f"Git commit skipped: {error}"

    for path in safe_paths:
        full = _safe_path(path)
        snapshot = _FILE_SNAPSHOTS.get(full)
        if snapshot is None:
            return (
                f"Git commit skipped: '{path}' has no agent snapshot. "
                "Only files read or written by the agent in this task may be committed."
            )
        if os.path.exists(full):
            try:
                with open(full, "rb") as f_bytes:
                    current_h = hashlib.sha256(f_bytes.read()).hexdigest()
                if current_h != snapshot:
                    return (
                        f"Git commit skipped: unexpected concurrent modification in '{path}'. "
                        "Current file state does not match agent changes."
                    )
            except OSError as err:
                return f"Git commit skipped: could not verify file snapshot for '{path}': {err}"

    added = _run_git(["add", "--", *safe_paths])
    if added.returncode != 0:
        return f"Git stage failed: {_git_error(added)}"

    staged = _run_git(["diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return "Git commit skipped: selected files have no changes."
    if staged.returncode != 1:
        return f"Git status check failed: {_git_error(staged)}"

    normalized_message = " ".join(str(message).split())[:120] or "Update project files"
    committed = _run_git(["commit", "-m", normalized_message, "-m", AGENT_COMMIT_TRAILER])
    if committed.returncode != 0:
        return f"Git commit failed: {_git_error(committed)}"
    commit_id = _run_git(["rev-parse", "--short", "HEAD"])
    full_hash = _run_git(["rev-parse", "HEAD"])
    if full_hash.returncode == 0 and full_hash.stdout.strip():
        _AGENT_COMMIT_HASHES.add(full_hash.stdout.strip())
        _AGENT_COMMIT_HASHES.add(commit_id.stdout.strip())
    return f"Git commit created: {commit_id.stdout.strip()} — {normalized_message}"


def is_agent_commit(commit: str) -> bool:
    """True if ``commit`` carries Qazterion's trailer (or the legacy agent author name)."""
    body = _run_git(["log", "-1", "--format=%an%n%B", commit])
    if body.returncode != 0:
        return False
    lines = body.stdout.splitlines()
    return bool(lines) and (lines[0].strip() == "Qazterion Agent" or AGENT_COMMIT_TRAILER in body.stdout)


def rollback_last_change() -> str:
    return security_execute("rollback_last_change", {})


def _impl_rollback_last_change() -> str:
    """Discard the latest automatic commit only when the worktree is clean.

    A clean-tree requirement prevents a rollback from silently deleting later,
    uncommitted user work. The first commit cannot be reset because it has no
    parent; callers receive an actionable explanation instead.
    """
    ready = ensure_git_repository()
    if ready.startswith(("Git unavailable", "Git init failed", "Git identity setup failed")):
        return ready

    status = _run_git(["status", "--porcelain", "--untracked-files=no"])
    if status.returncode != 0:
        return f"Git status check failed: {_git_error(status)}"
    if status.stdout.strip():
        return "Rollback refused: uncommitted changes detected. Commit or stash them first."

    parent = _run_git(["rev-parse", "HEAD~1"])
    if parent.returncode != 0:
        return "Rollback unavailable: the repository needs at least two commits."

    current = _run_git(["rev-parse", "HEAD"])
    if current.returncode != 0 or not current.stdout.strip():
        return "Rollback refused: current HEAD could not be determined."
    head_hash = current.stdout.strip()
    if not (head_hash in _AGENT_COMMIT_HASHES or is_agent_commit(head_hash)):
        return "Rollback refused: latest commit was not created by Qazterion Agent."

    anc = _run_git(["merge-base", "--is-ancestor", "HEAD~1", "HEAD"])
    if anc.returncode != 0:
        return "Rollback refused: target commit is not a valid ancestor of current HEAD."

    reset = _run_git(["reset", "--hard", "HEAD~1"])
    if reset.returncode != 0:
        return f"Rollback failed: {_git_error(reset)}"
    return f"Rollback complete: reset to {parent.stdout.strip()[:7]}."


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_index",
            "description": "Find relevant source files, functions, and classes in the index. Use this before working in an existing project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "Default 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_relevant_chunks",
            "description": "Read relevant code sections with paths and line ranges. Use it first for a local change in a large file; use read_file for broader context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "Default 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in the current project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Default '.' (root). 'path' also accepted as an alias."},
                    "path": {"type": "string", "description": "Alias for 'directory'."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file with 1-based line numbers. Pass start_line/end_line to read part of a large file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a NEW file or overwrite an entire file. Use apply_patch for a small edit to an existing file to avoid wasting tokens.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Make a small targeted unified-diff change to an EXISTING file — "
                "do not rewrite the entire file. Call read_file first: it returns the file "
                "with '  N\\t' line-number prefixes so you can copy exact line numbers into the "
                "hunk header — do NOT type those number prefixes into the diff itself, they are "
                "not part of the file content. Format: '@@ -old_line,count +new_line,count @@' "
                "hunk header, followed by context lines (space prefix), removed lines ('-' prefix), "
                "added lines ('+' prefix)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "diff": {"type": "string", "description": "Unified-diff format text"},
                },
                "required": ["path", "diff"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_directory",
            "description": "Create a new directory in the project.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a terminal command in the current project directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Seconds (default 60, max 300)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback_last_change",
            "description": (
                "Undo the latest automatic Qazterion commit. Use only when the user explicitly asks to "
                "undo the latest agent change. Refuses when there are uncommitted changes."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

configure_security_gateway(
    workspace_getter=_current_workspace,
    handlers={
        "list_files": _impl_list_files,
        "read_file": _impl_read_file,
        "write_file": _impl_write_file,
        "apply_patch": _impl_apply_patch,
        "make_directory": _impl_make_directory,
        "search_index": _impl_search_index,
        "read_relevant_chunks": _impl_read_relevant_chunks,
        "run_command": _impl_run_command,
        "rollback_last_change": _impl_rollback_last_change,
    },
)

TOOL_FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "apply_patch": apply_patch,
    "make_directory": make_directory,
    "search_index": search_index,
    "read_relevant_chunks": read_relevant_chunks,
    "run_command": run_command,
    "rollback_last_change": rollback_last_change,
}
