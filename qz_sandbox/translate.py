"""Conservative PowerShell/Windows → POSIX translation for Docker Linux containers.

Only patterns we are confident about are translated. Anything else returns None
so the caller can fall back to the host backend.
"""

from __future__ import annotations

import re
import shlex

_WIN_ABS = re.compile(r"[A-Za-z]:[\\/]")
_PS_MARKERS = re.compile(
    r"Write-|Get-(?!ChildItem\b)|Set-|Remove-Item|Start-Sleep|Invoke-|"
    r"Out-File|\$env:|\$_|`n|&\s+'",
    re.IGNORECASE,
)
_UNSAFE_SHELL = re.compile(r"[;|&]|&&|\|\|")


def translate_for_docker(command: str) -> str | None:
    """Return a POSIX ``sh -c`` command, or None if translation is not confident."""
    text = command.strip()
    if not text or _WIN_ABS.search(text) or _PS_MARKERS.search(text):
        return None
    if _UNSAFE_SHELL.search(text):
        return None

    listing = _translate_listing(text)
    if listing is not None:
        return listing

    python = _translate_python(text)
    if python is not None:
        return python

    if re.match(r"^pytest(\s|$)", text, re.IGNORECASE):
        return _posix_paths(text)

    if re.match(r"^pip(?:3)?\s+install\b", text, re.IGNORECASE):
        return _posix_paths(text)

    if re.match(r"^npm\s+(install|ci|run|test|exec)\b", text, re.IGNORECASE):
        return _posix_paths(text)

    if re.match(r"^git\s+[A-Za-z]", text):
        return _posix_paths(text)

    return None


def _posix_paths(text: str) -> str:
    return text.replace("\\", "/")


def _translate_listing(text: str) -> str | None:
    if re.fullmatch(r"ls(\s+\S+)?", text, re.IGNORECASE):
        return _ls_args(text.split(None, 1))
    if re.fullmatch(r"dir(\s+\S+)?", text, re.IGNORECASE):
        return _ls_args(text.split(None, 1))
    if re.fullmatch(r"Get-ChildItem(\s+-Name)?(\s+\S+)?", text, re.IGNORECASE):
        parts = [p for p in text.split() if not p.lower().startswith("-")]
        return _ls_args(parts)
    return None


def _ls_args(parts: list[str]) -> str | None:
    if len(parts) == 1:
        return "ls -la"
    path = parts[1].replace("\\", "/")
    if path.startswith("/") or ".." in path.split("/"):
        return None
    return f"ls -la {shlex.quote(path)}"


def _translate_python(text: str) -> str | None:
    text = re.sub(r"^python\.exe\b", "python", text, count=1, flags=re.IGNORECASE)
    if re.match(r"^python\s+-c\s+", text, re.IGNORECASE):
        return text
    if re.match(r"^python\s+-m\s+(pytest|unittest|pip)\b", text, re.IGNORECASE):
        return _posix_paths(text)
    if re.match(r"^python\s+-m\s+pip\s+install\b", text, re.IGNORECASE):
        return _posix_paths(text)
    return None
