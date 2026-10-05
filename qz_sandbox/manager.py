"""Process-wide command executor (host only; Docker is neither required nor used)."""

from __future__ import annotations

from qz_sandbox.backend import ExecutionResult, HostBackend

_manager: "SandboxManager | None" = None


class SandboxManager:
    backend_name = "host"
    docker_available = False  # kept for callers that still check it

    def __init__(self, backend: HostBackend | None = None) -> None:
        self.backend = backend or HostBackend()

    def execute(self, command: str, workspace: str, timeout: int) -> ExecutionResult:
        return self.backend.run(command, workspace, timeout)


def get_manager() -> SandboxManager:
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager


def reset_manager() -> None:
    global _manager
    _manager = None


def execute(command: str, workspace: str, timeout: int) -> ExecutionResult:
    return get_manager().execute(command, workspace, timeout)
