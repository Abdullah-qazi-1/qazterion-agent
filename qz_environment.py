"""Project type detection and safe environment preparation."""
from __future__ import annotations
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ProjectEnvironment:
    workspace: Path
    project_type: str
    manager: str | None
    python_interpreter: Path | None
    dependency_files: tuple[str, ...]
    protected_tools: tuple[str, ...]
    test_command: str | None
    @property
    def can_create_venv(self) -> bool:
        return self.project_type == "python" and not self.python_interpreter and not self.protected_tools
    def summary(self) -> str:
        return "; ".join(item for item in (f"type={self.project_type}", f"manager={self.manager}" if self.manager else None, f"python={self.python_interpreter}" if self.python_interpreter else None, "protected=" + ", ".join(self.protected_tools) if self.protected_tools else None) if item)

def _interpreter(root: Path) -> Path | None:
    for name in (".venv", "venv"):
        value = root / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if value.is_file(): return value.resolve()
    return None

def detect_project_environment(path: str | os.PathLike[str]) -> ProjectEnvironment:
    """Read project markers only; no environment or package changes occur."""
    root = Path(path).resolve(); has = lambda name: (root / name).exists()
    protected = tuple(name for name, present in (("conda", has("environment.yml") or has("environment.yaml")), ("docker", has("Dockerfile") or has("docker-compose.yml") or has("compose.yml"))) if present)
    python = _interpreter(root); deps = tuple(name for name in ("requirements.txt", "pyproject.toml", "poetry.lock", "Pipfile") if has(name))
    if python or deps or has("setup.py") or has("uv.lock") or has("tox.ini"):
        manager = "poetry" if has("poetry.lock") else "pipenv" if has("Pipfile") else "uv" if has("uv.lock") else "tox" if has("tox.ini") else "pip" if deps else "existing-venv"
        return ProjectEnvironment(root, "python", manager, python, deps, protected, f"& '{python}' -m pytest" if python else "python -m pytest")
    for marker, kind, manager, test in (("package.json", "node", "npm", "npm test"), ("Cargo.toml", "rust", "cargo", "cargo test"), ("go.mod", "go", "go", "go test ./..."), ("pom.xml", "java", "maven", "mvn test"), ("build.gradle", "java", "gradle", "gradle test"), ("build.gradle.kts", "java", "gradle", "gradle test")):
        if has(marker): return ProjectEnvironment(root, kind, manager, None, (), protected, test)
    if any(root.glob("*.sln")) or any(root.glob("*.csproj")): return ProjectEnvironment(root, "dotnet", "dotnet", None, (), protected, "dotnet test")
    return ProjectEnvironment(root, "unknown", None, None, (), protected, None)

def preparation_plan(env: ProjectEnvironment) -> str:
    if env.project_type != "python": return "No Python environment preparation is needed for this project type."
    if env.python_interpreter: return f"Reuse existing project interpreter: {env.python_interpreter}"
    if env.protected_tools: return "Preparation skipped: existing project tooling detected (" + ", ".join(env.protected_tools) + ")."
    return f"Approval required: create {env.workspace / '.venv'} and install declared dependencies."

def prepare_python_environment(env: ProjectEnvironment, *, approved: bool = False) -> str:
    """Create `.venv` and install requirements only after explicit approval."""
    if not env.can_create_venv or not approved: return preparation_plan(env)
    target = env.workspace / ".venv"
    made = subprocess.run([sys.executable, "-m", "venv", str(target)], cwd=env.workspace, capture_output=True, text=True, check=False)
    if made.returncode: return "Environment creation failed: " + (made.stderr or made.stdout or "unknown error").strip()
    interpreter = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python"); requirements = env.workspace / "requirements.txt"
    if not requirements.is_file(): return f"Environment created: {interpreter}. No requirements.txt was installed."
    installed = subprocess.run([str(interpreter), "-m", "pip", "install", "-r", str(requirements)], cwd=env.workspace, capture_output=True, text=True, check=False)
    if installed.returncode: return "Dependencies failed to install: " + (installed.stderr or installed.stdout or "unknown error").strip()
    return f"Environment created and dependencies installed: {interpreter}"
