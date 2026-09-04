"""System runtime health inspection and diagnostic validation for Qazterion."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from qz_keystore import KeyStore
from qz_sandbox.manager import probe_docker


@dataclass
class ComponentHealth:
    name: str
    status: str  # "healthy" | "advisory" | "error" | "unavailable"
    available: bool
    details: str
    action_needed: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemHealthReport:
    overall_status: str  # "healthy" | "ready_with_advisories" | "action_required"
    components: dict[str, ComponentHealth] = field(default_factory=dict)
    advisories: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overallStatus": self.overall_status,
            "components": {k: asdict(v) for k, v in self.components.items()},
            "advisories": self.advisories,
            "timestamp": self.timestamp,
        }


def check_system_health(workspace: str | Path | None = None, keystore: KeyStore | None = None) -> SystemHealthReport:
    """Perform a comprehensive, non-destructive health audit of all runtime dependencies."""
    components: dict[str, ComponentHealth] = {}
    advisories: list[str] = []

    # 1. Python Runtime
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_healthy = sys.version_info >= (3, 10)
    components["python"] = ComponentHealth(
        name="Python Interpreter",
        status="healthy" if py_healthy else "error",
        available=True,
        details=f"Python {py_version} ({sys.executable})",
        action_needed=None if py_healthy else "Upgrade to Python 3.10 or higher.",
        metadata={"version": py_version, "path": sys.executable},
    )

    # 2. Git
    git_path = shutil.which("git")
    if git_path:
        try:
            res = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=3, check=False)
            git_ver = res.stdout.strip() if res.returncode == 0 else "git detected"
            components["git"] = ComponentHealth(
                name="Git Version Control",
                status="healthy",
                available=True,
                details=git_ver,
                metadata={"path": git_path, "version": git_ver},
            )
        except Exception:
            components["git"] = ComponentHealth(
                name="Git Version Control",
                status="healthy",
                available=True,
                details=f"Available at {git_path}",
                metadata={"path": git_path},
            )
    else:
        components["git"] = ComponentHealth(
            name="Git Version Control",
            status="error",
            available=False,
            details="Git is not found on system PATH.",
            action_needed="Install Git from https://git-scm.com/ and add it to your PATH.",
        )
        advisories.append("Git is required for atomic checkpoints, diff history, and recovery.")

    # 3. Docker Sandbox
    docker_available = probe_docker()
    if docker_available:
        components["docker"] = ComponentHealth(
            name="Docker Sandbox Isolation",
            status="healthy",
            available=True,
            details="Docker daemon is running. Container isolation active.",
            metadata={"isolation": "CONTAINER"},
        )
    else:
        components["docker"] = ComponentHealth(
            name="Docker Sandbox Isolation",
            status="advisory",
            available=False,
            details="Docker daemon unreachable. Restricted host fallback active.",
            action_needed="Start Docker Desktop to enable full containerized sandbox isolation.",
            metadata={"isolation": "UNSANDBOXED_HOST_FALLBACK"},
        )
        advisories.append("Docker is not running; commands will run in restricted host mode.")

    # 4. OS Keystore
    ks = keystore or KeyStore()
    ks_backend = ks.backend_name()
    components["keystore"] = ComponentHealth(
        name="Secure Keystore",
        status="healthy",
        available=True,
        details=f"Operating with {ks_backend} encryption.",
        metadata={"backend": ks_backend},
    )

    # 5. Configured Providers & Keys
    key_groups = ks.enabled_key_groups()
    configured_count = sum(len(v) for v in key_groups.values())
    if configured_count > 0:
        providers_list = ", ".join(f"{p} ({len(keys)})" for p, keys in sorted(key_groups.items()))
        components["providers"] = ComponentHealth(
            name="LLM Providers & Keys",
            status="healthy",
            available=True,
            details=f"{configured_count} key(s) configured across: {providers_list}",
            metadata={"configured_count": configured_count, "providers": list(key_groups.keys())},
        )
    else:
        components["providers"] = ComponentHealth(
            name="LLM Providers & Keys",
            status="advisory",
            available=False,
            details="No API keys have been configured yet.",
            action_needed="Add at least one free or commercial provider key (e.g. Groq, Gemini, Mistral, OpenRouter).",
            metadata={"configured_count": 0},
        )
        advisories.append("No API keys configured. Set up keys in the Onboarding / API Models screen.")

    # 6. Workspace check if provided
    if workspace:
        ws_path = Path(workspace).resolve()
        if ws_path.is_dir():
            is_git = (ws_path / ".git").is_dir()
            components["workspace"] = ComponentHealth(
                name="Active Workspace",
                status="healthy",
                available=True,
                details=f"{ws_path} ({'Git repository' if is_git else 'Non-git folder'})",
                metadata={"path": str(ws_path), "is_git": is_git},
            )
        else:
            components["workspace"] = ComponentHealth(
                name="Active Workspace",
                status="error",
                available=False,
                details=f"Workspace path does not exist: {ws_path}",
                action_needed="Select a valid existing directory.",
            )

    # Determine overall status
    has_error = any(c.status == "error" for c in components.values())
    has_advisory = any(c.status == "advisory" for c in components.values())

    if has_error:
        overall = "action_required"
    elif has_advisory:
        overall = "ready_with_advisories"
    else:
        overall = "healthy"

    return SystemHealthReport(
        overall_status=overall,
        components=components,
        advisories=advisories,
    )
