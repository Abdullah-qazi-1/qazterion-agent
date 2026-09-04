"""Execution sandbox: isolate ALLOWED run_command invocations from the host.

Isolation is applied only after the security gateway has already decided
ALLOW. This package does not classify risk or override policy.
"""

from qz_sandbox.backend import ExecutionBackend, ExecutionResult
from qz_sandbox.manager import SandboxManager, execute

__all__ = [
    "ExecutionBackend",
    "ExecutionResult",
    "SandboxManager",
    "execute",
]
