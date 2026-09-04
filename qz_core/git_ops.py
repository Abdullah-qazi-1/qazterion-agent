from __future__ import annotations

import re
from openai import OpenAI
from qz_core.client import get_client
from qz_tools import commit_changes, ensure_git_repository


def _commit_hash_from_result(commit_result: str) -> str | None:
    match = re.match(r"Git commit created:\s+(\S+)", commit_result)
    return match.group(1) if match else None


def generate_commit_message(task: str, changes: list[dict], client: OpenAI | None = None) -> str:
    """Create a short commit subject, with a deterministic offline fallback."""
    paths = ", ".join(str(change.get("path", "file")) for change in changes[:5])
    c = get_client(client)
    try:
        response = c.chat.completions.create(
            model="groq-fast",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write a Git commit subject for the supplied coding-task changes. "
                        "Return only one imperative English line, 72 characters or fewer; no quotes, prefix, or punctuation."
                    ),
                },
                {"role": "user", "content": f"Task: {task}\nChanged files: {paths}"},
            ],
            temperature=0,
            max_tokens=30,
        )
        message = " ".join((response.choices[0].message.content or "").split())
        if message:
            return message[:72]
    except Exception as error:
        print(f"\033[90m[git] commit message generation unavailable ({error}); using fallback\033[0m")
    return f"Update {paths or 'project files'}"[:72]


def get_current_head(workspace: str | None = None) -> str | None:
    """Return the current short or full git HEAD commit hash, or None if unavailable."""
    import subprocess
    from qz_tools import WORKSPACE
    from pathlib import Path
    ws = Path(workspace or WORKSPACE).resolve()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ws,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_uncommitted_files(workspace: str | None = None) -> list[str]:
    """Return list of modified, staged, or untracked files in the workspace."""
    import subprocess
    from qz_tools import WORKSPACE
    from pathlib import Path
    ws = Path(workspace or WORKSPACE).resolve()
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ws,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


def is_worktree_clean(workspace: str | None = None) -> bool:
    """Return True if there are no uncommitted changes in the workspace."""
    return len(get_uncommitted_files(workspace)) == 0


def commit_exists(commit_hash: str, workspace: str | None = None) -> bool:
    """Return True if the specified commit exists in git repository history."""
    import subprocess
    from qz_tools import WORKSPACE
    from pathlib import Path
    if not commit_hash:
        return False
    ws = Path(workspace or WORKSPACE).resolve()
    try:
        res = subprocess.run(
            ["git", "cat-file", "-t", commit_hash],
            cwd=ws,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        return res.returncode == 0 and res.stdout.strip() == "commit"
    except Exception:
        return False

