"""Pick a Docker backend when the daemon is reachable, else unsandboxed host."""

from __future__ import annotations

import logging
import subprocess

from qz_sandbox.backend import (
    DockerExecutionBackend,
    ExecutionBackend,
    ExecutionResult,
    RestrictedHostBackend,
)
from qz_sandbox.translate import translate_for_docker

logger = logging.getLogger("qz_sandbox")

NOT_TRANSLATABLE = "UNSANDBOXED — not translatable"

_manager: SandboxManager | None = None


def probe_docker() -> bool:
    """Return True when ``docker info`` succeeds (daemon reachable)."""
    try:
        completed = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class SandboxManager:
    def __init__(self, docker_available: bool | None = None) -> None:
        available = probe_docker() if docker_available is None else docker_available
        self.docker_available = bool(available)
        self._host = RestrictedHostBackend()
        self.backend: ExecutionBackend = (
            DockerExecutionBackend() if self.docker_available else self._host
        )
        self.backend_name = "docker" if self.docker_available else "UNSANDBOXED"
        logger.info("sandbox manager started backend=%s", self.backend_name)

    def execute(
        self,
        command: str,
        workspace: str,
        timeout: int,
        *,
        allow_network: bool = False,
    ) -> ExecutionResult:
        if not self.docker_available:
            logger.info(
                "sandbox execute backend=UNSANDBOXED timeout=%s (Docker unavailable)",
                timeout,
            )
            return self._host.run(
                command,
                workspace,
                timeout,
                allow_network=allow_network,
            )

        posix = translate_for_docker(command)
        if posix is None:
            logger.warning("%s command=%r", NOT_TRANSLATABLE, command)
            return self._host.run(
                command,
                workspace,
                timeout,
                allow_network=allow_network,
                unsandboxed_reason=NOT_TRANSLATABLE,
            )

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
) -> ExecutionResult:
    return get_manager().execute(
        command,
        workspace,
        timeout,
        allow_network=allow_network,
    )
