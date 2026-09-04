"""Keyword/pattern risk classification for agent shell commands."""

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
    (r"\.ssh\\|\\appdata\\|\\\.aws\\|\\\.gnupg\\|\bcredentials\b|\.env\b", "credential path"),
    (r"\$env:userprofile|ntuser\.dat|\bSAM\b|unattend\.xml", "credential/store path"),
    (r"\b(cmdkey|whoami\s+/priv|procdump)\b", "credential/privilege probe"),
    (r"\b(icacls|takeown)\b", "acl takeover"),
    (r"start-process[^\n]*-verb\s+runas", "elevation"),
    (r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-", "destructive git"),
    (r"\bnetsh\b", "network stack change"),
    (r"\b(rundll32|regsvr32|mshta|wscript|cscript)\b", "script host"),
)

_MEDIUM: tuple[tuple[str, str], ...] = (
    (r"\b(pip|pip3|uv)\s+install\b|\bnpm\s+install\b|\bpnpm\s+install\b|\byarn\s+add\b", "package install"),
    (r"\bgit\s+(clone|push|filter-branch|filter-repo)\b", "git network/history rewrite"),
    (r"\b(move-item|copy-item|rename-item)\b", "filesystem move/copy"),
    (r"\b(chmod|chown|attrib)\b", "permission change"),
    (r"\bstart-process\b", "new process"),
)

_COMPILED = [
    (RiskLevel.FORBIDDEN, [(re.compile(p, re.IGNORECASE), r) for p, r in _FORBIDDEN]),
    (RiskLevel.HIGH, [(re.compile(p, re.IGNORECASE), r) for p, r in _HIGH]),
    (RiskLevel.MEDIUM, [(re.compile(p, re.IGNORECASE), r) for p, r in _MEDIUM]),
]


def classify_command(command: str) -> tuple[RiskLevel, list[str]]:
    """Return the highest matching risk level and human-readable reasons."""
    text = command or ""
    reasons: list[str] = []
    level = RiskLevel.LOW
    for candidate, patterns in _COMPILED:
        matched = [reason for regex, reason in patterns if regex.search(text)]
        if matched:
            level = candidate
            reasons = matched
            break
    return level, reasons
