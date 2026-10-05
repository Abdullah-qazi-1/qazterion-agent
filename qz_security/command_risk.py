"""Keyword/pattern risk classification for agent shell commands.

LOW runs, MEDIUM needs the user's approval, HIGH and FORBIDDEN are refused.
This is a guard-rail against destructive or exfiltrating *commands*; code that
the agent writes into the workspace and then runs is reviewed by the user
through the plan/diff workflow, not by these patterns.
"""

from __future__ import annotations

import re
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    FORBIDDEN = "FORBIDDEN"


_FORBIDDEN: tuple[tuple[str, str], ...] = (
    (r"\bformat\s+[a-zA-Z]:", "disk format"),
    (r"\b(stop-computer|restart-computer|shutdown)\b", "system power control"),
    (r"\bbcdedit\b|\bdiskpart\b|\bvssadmin\s+delete\b", "boot/volume destruction"),
    (r"\bcipher\s+/w\b", "disk wipe"),
    (r"\bset-executionpolicy\b", "execution-policy change"),
    (r"\b(invoke-expression|iex)\b", "dynamic command execution"),
    (r"downloadstring|downloadfile|frombase64string", "remote/encoded payload"),
    (r"\s-encodedcommand\b|\s-enc\s+[A-Za-z0-9+/=]{8,}", "encoded PowerShell"),
    (r"\breg\s+(save|load|restore|delete)\b", "registry hive mutation"),
    (r"\bnet\s+(user|localgroup)\b", "account modification"),
    (r"\b(mimikatz|lsass|ntds\.dit)\b", "credential dump target"),
    (r"\b(new-service|schtasks\s+/create|sc(?:\.exe)?\s+create)\b", "persistence"),
    (r"\b(set-mppreference|add-mppreference|disable-windowsdefender)\b", "defender tamper"),
    (r"remove-item[^\n]*(windows\\system32|\\windows\b|\$env:systemroot)", "OS directory delete"),
)

_HIGH: tuple[tuple[str, str], ...] = (
    (r"\bremove-item\b", "file delete"),
    (r"\brm\s+-[rRf]+", "recursive delete"),
    (r"\b(del|erase)\s+", "file delete"),
    (r"\brd\s+/s\b|\brmdir\s+/s\b", "directory delete"),
    (r"\b(invoke-webrequest|invoke-restmethod|iwr|irm|curl|wget)\b", "network download"),
    (r"\b(bitsadmin|certutil)\b", "transfer/living-off-the-land"),
    (r"\breg\s+(add|query|export)\b", "registry access"),
    (r"hk(lm|cu|cr|u)\\|\\currentversion\\run", "registry run keys"),
    (r"\.ssh[\\/]|[\\/]appdata[\\/]|[\\/]\.aws[\\/]|[\\/]\.gnupg[\\/]|\bcredentials\b|(?<![\w.-])\.env\b", "credential path"),
    (r"\$env:userprofile|\$home\b|~[\\/]|ntuser\.dat|\bSAM\b|unattend\.xml", "credential/store path"),
    (r"\b(cmdkey|whoami\s+/priv|procdump)\b", "credential/privilege probe"),
    (r"\b(icacls|takeown)\b", "acl takeover"),
    (r"start-process[^\n]*-verb\s+runas", "elevation"),
    (r"\bnetsh\b", "network stack change"),
    (r"\b(rundll32|regsvr32|mshta|wscript|cscript)\b", "script host"),
    # Git operations that destroy uncommitted work or rewrite history.
    (r"\bgit\s+reset\s+--(hard|merge|keep)\b", "git reset discards work"),
    (r"\bgit\s+clean\b[^\n]*\s-[a-zA-Z]*[fdxX]", "git clean deletes untracked files"),
    (r"\bgit\s+checkout\s+(?:\S+\s+)?--\s+\S|\bgit\s+checkout\s+(?:-f|--force)\b|\bgit\s+checkout\s+\.(?:\s|$)",
     "git checkout discards changes"),
    (r"\bgit\s+restore\b(?![^\n]*--staged\b)", "git restore discards changes"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "git stash deletion"),
    (r"\bgit\s+branch\s+(-D\b|--delete\s+--force\b)", "git branch force-delete"),
    (r"\bgit\s+push\b[^\n]*(--force|\s-f\b|\s\+\S)", "git force push"),
    (r"\bgit\s+(rebase|filter-branch|filter-repo|update-ref\s+-d|reflog\s+expire|gc\s+--prune)\b", "git history rewrite"),
    # Nested shells / script hosts hide the real command from these checks.
    (r"\b(perl|ruby|php|lua|tclsh|osascript)\b\s+-[a-zA-Z]*e\b", "inline script execution"),
    (r"\b(bash|sh|zsh|ksh|csh|dash)(?:\.exe)?\s+([^\n]*\s)?(-c)\b", "nested shell invocation"),
    (r"\b(powershell|pwsh|cmd)(?:\.exe)?\s+([^\n]*\s)?(-c|-command|/c|/k)\b", "nested shell invocation"),
)

_MEDIUM: tuple[tuple[str, str], ...] = (
    (r"\b(pip|pip3|uv)\s+install\b|\bnpm\s+(install|i|ci)\b|\bpnpm\s+(install|add)\b|\byarn\s+add\b", "package install"),
    (r"\bgit\s+(clone|push|pull|fetch)\b", "git network access"),
    (r"\b(move-item|copy-item|rename-item)\b", "filesystem move/copy"),
    (r"\b(chmod|chown|attrib)\b", "permission change"),
    (r"\bstart-process\b", "new process"),
)

# Inline interpreter code (python -c / node -e) is judged by what it does:
# computation and assertions are fine; network, process spawning or deletion
# inside a one-liner is not.
_INLINE_CODE = re.compile(
    r"\b(?:python\d*(?:\.\d+)?|py|pythonw)(?:\.exe)?['\"]?\s+(?:[^\n]*\s)?(?:-c|--command)\b"
    r"|\b(?:node|nodejs|deno|bun)(?:\.exe)?\s+(?:[^\n]*\s)?(?:-e|--eval|-p|--print)\b",
    re.IGNORECASE,
)
_INLINE_DANGER: tuple[tuple[str, str], ...] = (
    (r"\b(urllib|requests|socket|http\.client|aiohttp|httpx|child_process)\b|\bfetch\(", "network/process access in inline code"),
    (r"\bshutil\.(rmtree|move)\b|\bos\.(remove|unlink|rmdir|removedirs|system|popen)\b|\bfs\.(rm|rmSync|unlink)",
     "file deletion or shell call in inline code"),
    (r"\b(eval|exec)\s*\(|\b__import__\b|\bsubprocess\b", "dynamic code execution in inline code"),
)

_COMPILED = [
    (RiskLevel.FORBIDDEN, [(re.compile(p, re.IGNORECASE), r) for p, r in _FORBIDDEN]),
    (RiskLevel.HIGH, [(re.compile(p, re.IGNORECASE), r) for p, r in _HIGH]),
    (RiskLevel.MEDIUM, [(re.compile(p, re.IGNORECASE), r) for p, r in _MEDIUM]),
]
_COMPILED_INLINE = [(re.compile(p, re.IGNORECASE), r) for p, r in _INLINE_DANGER]


def classify_command(command: str) -> tuple[RiskLevel, list[str]]:
    """Return the highest matching risk level and human-readable reasons."""
    text = command or ""
    for level, patterns in _COMPILED:
        matched = [reason for regex, reason in patterns if regex.search(text)]
        if level is RiskLevel.HIGH and _INLINE_CODE.search(text):
            matched += [reason for regex, reason in _COMPILED_INLINE if regex.search(text)]
        if matched:
            return level, matched
    return RiskLevel.LOW, []


def invokes_unrestricted_interpreter(command: str) -> bool:
    """True when the command runs inline interpreter code (python -c, node -e, ...)."""
    return bool(_INLINE_CODE.search(command or ""))
