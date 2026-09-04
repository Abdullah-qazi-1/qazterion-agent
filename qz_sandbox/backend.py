"""Execution backends for sandboxed (and unsandboxed fallback) command runs.

Docker invocations use the Docker CLI via ``subprocess`` rather than the
``docker`` Python SDK so this package stays dependency-free.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("qz_sandbox")

DOCKER_IMAGE = "qazterion-sandbox:1"
DOCKERFILE_PATH = Path(__file__).resolve().parent / "Dockerfile"
CONTAINER_WORKSPACE = "/workspace"
CPU_LIMIT = "1"
MEMORY_LIMIT = "512m"
NONROOT_USER = "1000:1000"

# Host paths that must never appear as container mounts.
_DOCKER_SOCKET_MARKERS = (
    "/var/run/docker.sock",
    "\\var\\run\\docker.sock",
    r"\\.\pipe\docker_engine",
    "docker.sock",
)


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    error: str | None = None
    isolation: str = ""
    fallback_reason: str | None = None


class ExecutionBackend(ABC):
    @abstractmethod
    def run(
        self,
        command: str,
        workspace: str,
        timeout: int,
        *,
        allow_network: bool = False,
    ) -> ExecutionResult:
        """Run ``command`` with ``workspace`` as the working directory."""


def build_docker_run_args(
    command: str,
    workspace: str,
    *,
    allow_network: bool = False,
    container_name: str | None = None,
    image: str = DOCKER_IMAGE,
) -> list[str]:
    """Construct ``docker run`` argv. Used by the backend and by unit tests.

    Mounts only ``workspace`` at ``/workspace`` (read-write). Never mounts the
    Docker socket. Network is disabled unless ``allow_network`` is True.
    """
    name = container_name or f"qz-sandbox-{uuid.uuid4().hex[:12]}"
    workspace_path = str(Path(workspace).resolve())
    volume = f"{workspace_path}:{CONTAINER_WORKSPACE}:rw"
    for marker in _DOCKER_SOCKET_MARKERS:
        if marker.lower() in volume.lower():
            raise ValueError("Refusing to construct a Docker mount that includes the Docker socket")

    network = "bridge" if allow_network else "none"
    return [
        "docker",
        "run",
        "--name",
        name,
        "--rm",
        "--network",
        network,
        "--cpus",
        CPU_LIMIT,
        "--memory",
        MEMORY_LIMIT,
        "--user",
        NONROOT_USER,
        "--workdir",
        CONTAINER_WORKSPACE,
        "-v",
        volume,
        image,
        "/bin/sh",
        "-c",
        command,
    ]


def _decode_captured(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def ensure_sandbox_image() -> None:
    """Build ``qazterion-sandbox:1`` once if it is not already present locally."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", DOCKER_IMAGE],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if inspect.returncode == 0:
        return
    logger.info("sandbox building docker image %s", DOCKER_IMAGE)
    built = subprocess.run(
        ["docker", "build", "-t", DOCKER_IMAGE, "-f", str(DOCKERFILE_PATH), str(DOCKERFILE_PATH.parent)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if built.returncode != 0:
        detail = (built.stderr or built.stdout or "docker build failed").strip()
        raise OSError(f"Could not build sandbox image {DOCKER_IMAGE}: {detail}")


class DockerExecutionBackend(ExecutionBackend):
    """Run the command inside a resource-limited Docker container."""

    def build_run_args(
        self,
        command: str,
        workspace: str,
        *,
        allow_network: bool = False,
        container_name: str | None = None,
    ) -> list[str]:
        return build_docker_run_args(
            command,
            workspace,
            allow_network=allow_network,
            container_name=container_name,
        )

    def run(
        self,
        command: str,
        workspace: str,
        timeout: int,
        *,
        allow_network: bool = False,
    ) -> ExecutionResult:
        container_name = f"qz-sandbox-{uuid.uuid4().hex[:12]}"
        try:
            ensure_sandbox_image()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-1,
                timed_out=False,
                error=str(exc),
                isolation="docker",
            )
        args = self.build_run_args(
            command,
            workspace,
            allow_network=allow_network,
            container_name=container_name,
        )
        logger.info(
            "sandbox backend=docker container=%s network=%s workspace=%s timeout=%s",
            container_name,
            "bridge" if allow_network else "none",
            workspace,
            timeout,
        )
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            return ExecutionResult(
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                exit_code=completed.returncode,
                timed_out=False,
                error=None,
                isolation="docker",
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning("sandbox docker timeout; removing container %s", container_name)
            _force_remove_container(container_name)
            return ExecutionResult(
                stdout=_decode_captured(exc.stdout),
                stderr=_decode_captured(exc.stderr),
                exit_code=-1,
                timed_out=True,
                error="timeout",
                isolation="docker",
            )
        except OSError as exc:
            _force_remove_container(container_name)
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-1,
                timed_out=False,
                error=str(exc),
                isolation="docker",
            )
        finally:
            _force_remove_container(container_name)


def _force_remove_container(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("failed to remove sandbox container %s", container_name)


def _split_command_statements(command: str) -> list[tuple[str, str]]:
    """Split compound command into top-level statements respecting quotes."""
    statements: list[tuple[str, str]] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
        elif not in_single and not in_double:
            if ch == "&" and i + 1 < n and command[i + 1] == "&":
                stmt = "".join(current).strip()
                if stmt:
                    statements.append((stmt, "and"))
                current = []
                i += 2
                continue
            elif ch in (";", "\n"):
                stmt = "".join(current).strip()
                if stmt:
                    statements.append((stmt, "seq"))
                current = []
                i += 1
                continue
            else:
                current.append(ch)
                i += 1
        else:
            current.append(ch)
            i += 1

    remaining = "".join(current).strip()
    if remaining:
        statements.append((remaining, "last"))

    return statements


def _prepare_host_command(command: str, is_windows: bool) -> list[str]:
    """Prepare command string with robust multi-command exit code aggregation and PS5 compatibility."""
    statements = _split_command_statements(command)

    if not is_windows:
        if not statements or (len(statements) == 1 and statements[0][1] == "last"):
            return ["/bin/sh", "-c", command]

        # Mirror the PowerShell aggregation below: a later successful
        # statement in a ``;``-separated chain must not mask an earlier
        # non-zero exit code, while ``&&`` chains still short-circuit.
        parts = ["_qz_ec=0;"]
        for stmt, sep in statements:
            if sep == "and":
                parts.append(
                    f"{{ {stmt}; }}; _qz_last=$?; "
                    'if [ "$_qz_last" -ne 0 ]; then '
                    '[ "$_qz_ec" -eq 0 ] && _qz_ec=$_qz_last; '
                    'exit "$_qz_ec"; '
                    "fi;"
                )
            else:
                parts.append(
                    f"{{ {stmt}; }}; _qz_last=$?; "
                    '[ "$_qz_last" -ne 0 ] && [ "$_qz_ec" -eq 0 ] && _qz_ec=$_qz_last;'
                )
        parts.append('if [ "$_qz_ec" -ne 0 ]; then exit "$_qz_ec"; fi')
        sh_script = " ".join(parts)
        return ["/bin/sh", "-c", sh_script]

    if not statements or (len(statements) == 1 and statements[0][1] == "last"):
        raw = statements[0][0] if statements else command
        ps_script = (
            "$ErrorActionPreference = 'Continue'; "
            f"& {{ {raw} }}; "
            "if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
            "if (-not $?) { exit 1 }"
        )
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script]

    parts = ["$ErrorActionPreference = 'Continue';", "$script:__qz_ec = 0;"]
    for stmt, sep in statements:
        if sep == "and":
            parts.append(
                f"& {{ {stmt} }}; "
                "if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { if ($script:__qz_ec -eq 0) { $script:__qz_ec = $LASTEXITCODE }; exit $script:__qz_ec }; "
                "if (-not $?) { if ($script:__qz_ec -eq 0) { $script:__qz_ec = 1 }; exit $script:__qz_ec };"
            )
        else:
            parts.append(
                f"& {{ {stmt} }}; "
                "if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0 -and $script:__qz_ec -eq 0) { $script:__qz_ec = $LASTEXITCODE }; "
                "if ((-not $?) -and $script:__qz_ec -eq 0) { $script:__qz_ec = 1 };"
            )

    parts.append("if ($script:__qz_ec -ne 0) { exit $script:__qz_ec }")
    ps_script = " ".join(parts)
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script]


class RestrictedHostBackend(ExecutionBackend):
    """Direct host subprocess — used only when Docker is unavailable.

    Results are flagged ``UNSANDBOXED`` so callers can tell isolation was skipped.
    """

    def run(
        self,
        command: str,
        workspace: str,
        timeout: int,
        *,
        allow_network: bool = False,
        unsandboxed_reason: str | None = None,
    ) -> ExecutionResult:
        del allow_network  # Host fallback cannot isolate the network.
        is_windows = os.name == "nt"
        shell_args = _prepare_host_command(command, is_windows=is_windows)
        logger.warning(
            "sandbox backend=UNSANDBOXED workspace=%s timeout=%s (%s)",
            workspace,
            timeout,
            unsandboxed_reason or "Docker unavailable; command runs on the host",
        )
        env = dict(os.environ)
        current_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{workspace}{os.pathsep}{current_pp}" if current_pp else str(workspace)

        try:
            completed = subprocess.run(
                shell_args,
                cwd=workspace,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env=env,
            )
            return ExecutionResult(
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                exit_code=completed.returncode,
                timed_out=False,
                error=None,
                isolation="UNSANDBOXED",
                fallback_reason=unsandboxed_reason,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                stdout=_decode_captured(exc.stdout),
                stderr=_decode_captured(exc.stderr),
                exit_code=-1,
                timed_out=True,
                error="timeout",
                isolation="UNSANDBOXED",
                fallback_reason=unsandboxed_reason,
            )
        except OSError as exc:
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-1,
                timed_out=False,
                error=str(exc),
                isolation="UNSANDBOXED",
                fallback_reason=unsandboxed_reason,
            )