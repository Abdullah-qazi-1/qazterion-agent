"""Map tool name + args + risk onto ALLOW / ASK / DENY."""

from __future__ import annotations

from enum import Enum

from qz_security.command_risk import RiskLevel


class Decision(str, Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


_PATH_TOOLS = {
    "list_files",
    "read_file",
    "write_file",
    "apply_patch",
    "make_directory",
    "read_relevant_chunks",
    "search_index",
    "rollback_last_change",
}


def decide(tool_name: str, args: dict, risk: RiskLevel, *, path_ok: bool = True) -> Decision:
    if not path_ok:
        return Decision.DENY
    if tool_name == "run_command":
        if risk is RiskLevel.FORBIDDEN or risk is RiskLevel.HIGH:
            return Decision.DENY
        if risk is RiskLevel.MEDIUM:
            return Decision.ASK
        return Decision.ALLOW
    if tool_name in _PATH_TOOLS:
        return Decision.ALLOW
    return Decision.DENY
