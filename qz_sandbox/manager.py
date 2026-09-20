"""Pick a Docker backend when the daemon is reachable; otherwise fail closed."""

from __future__ import annotations

import logging
import subprocess

from qz_sandbox.backend import (
    DockerExecutionBackend,
    ExecutionBackend,
    ExecutionResult,
    RestrictedHostBackend,
    UnsandboxedHostBackend,
)
from qz_sandbox.translate import translate_for_docker
from qz_security.command_risk import invokes_unrestricted_interpreter

logger = logging.getLogger("qz_sandbox")

NOT_TRANSLATABLE = "ISOLATION_DENIED — command is not translatable for the sandbox"
DOCKER_UNAVAILABLE = "ISOLATION_DENIED — Docker sandbox is unavailable"
INTERPRETER_UNISOLATED = "ISOLATION_DENIED — unrestricted interpreter requires container isolation"

_manager: SandboxManager | None = None


def probe_docker() -> bool:
    """Return True when ``docker info`` succeeds and the daemon runs Linux containers."""
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    out = completed.stdout.strip().lower()
    return out == "linux" or "server version" in out or "linux" in out


class SandboxManager:
    def __init__(self, docker_available: bool | None = None) -> None:
        available = probe_docker() if docker_available is None else docker_available
        self.docker_available = bool(available)
        self._host = UnsandboxedHostBackend()
        self.backend: ExecutionBackend = (
            DockerExecutionBackend() if self.docker_available else self._host
        )
        self.backend_name = "docker" if self.docker_available else "UNSANDBOXED"
        logger.info("sandbox manager started backend=%s", self.backend_name)

    def _deny(self, reason: str) -> ExecutionResult:
        logger.warning("sandbox execute denied: %s", reason)
        return ExecutionResult(
            stdout="",
            stderr=reason,
            exit_code=-1,
            timed_out=False,
            error=reason,
            isolation="DENIED",
            fallback_reason=reason,
        )

    def execute(
        self,
        command: str,
        workspace: str,
        timeout: int,
        *,
        allow_network: bool = False,
        allow_unsandboxed: bool = False,
    ) -> ExecutionResult:
        if self.docker_available:
            posix = translate_for_docker(command)
            if posix is not None:
                logger.info(
                    "sandbox execute backend=docker timeout=%s allow_network=%s posix=%r",
                    timeout,
                    allow_network,
                    posix,
                )
                return self.backend.run(
                    posix,
                    workspace,
                    timeout,
                    allow_network=allow_network,
                )
            logger.warning("%s command=%r", NOT_TRANSLATABLE, command)
            return self._host.run(
                command,
                workspace,
                timeout,
                allow_network=allow_network,
                unsandboxed_reason=NOT_TRANSLATABLE,
            )

        logger.warning(
            "sandbox execute backend=UNSANDBOXED timeout=%s (Docker unavailable)",
            timeout,
        )
        return self._host.run(
            command,
            workspace,
            timeout,
            allow_network=allow_network,
            unsandboxed_reason=DOCKER_UNAVAILABLE,
        )


def get_manager() -> SandboxManager:
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager


def reset_manager() -> None:
    """Drop the process-wide manager so the next call re-probes Docker."""
    global _manager
    _manager = None


def execute(
    command: str,
    workspace: str,
    timeout: int,
    *,
    allow_network: bool = False,
    allow_unsandboxed: bool = False,
) -> ExecutionResult:
    return get_manager().execute(
        command,
        workspace,
        timeout,
        allow_network=allow_network,
        allow_unsandboxed=allow_unsandboxed,
    )
