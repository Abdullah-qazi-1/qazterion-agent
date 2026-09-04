"""Phase 12 — automatic LiteLLM proxy lifecycle management.

Responsibilities (IMPLEMENTATION_PLAN.md, "Proxy lifecycle"):
- Check whether a proxy is already answering on `localhost:4000`.
- Start it hidden in the background when it is not.
- Restart it safely after a key/configuration change.
- Report a clear error and support a Restart action after a crash.
- Support a "stop the proxy when the app closes" setting.

This module never talks to a specific provider; it only manages the local
`litellm --config config.yaml` process and asks it whether it is healthy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Frozen (PyInstaller) backends re-invoke this same executable with this flag
# so LiteLLM can be imported in-process. Looking for litellm.exe next to
# sys.executable is wrong when sys.executable *is* qz_backend.exe.
RUN_LITELLM_PROXY_FLAG = "--run-litellm-proxy"


def is_frozen_executable() -> bool:
    return bool(getattr(sys, "frozen", False))


def resolve_proxy_command(
    config_path: Path,
    port: int,
    command: list[str] | None = None,
) -> list[str]:
    """Build the argv used to spawn the LiteLLM proxy.

    Dev (venv): prefer the venv's litellm launcher beside python.exe.
    Frozen exe: re-invoke this executable with RUN_LITELLM_PROXY_FLAG so the
    bundled litellm package runs without requiring a PATH install.
    """
    if command is not None:
        return list(command)
    if is_frozen_executable():
        return [
            sys.executable,
            RUN_LITELLM_PROXY_FLAG,
            "--config",
            str(config_path),
            "--port",
            str(port),
        ]
    venv_bin = Path(sys.executable).resolve().parent if sys.executable else Path(".")
    launcher_candidates = [
        venv_bin / ("litellm.exe" if sys.platform == "win32" else "litellm"),
        venv_bin / "litellm-script.py",
    ]
    chosen = next((str(candidate) for candidate in launcher_candidates if candidate.exists()), "litellm")
    return [chosen, "--config", str(config_path), "--port", str(port)]


def run_embedded_litellm_proxy(argv: list[str] | None = None) -> int:
    """Entry used by the frozen executable's proxy child process.

    Mirrors ``multiprocessing.freeze_support``: the parent spawns
    ``[qz_backend.exe, --run-litellm-proxy, ...]`` and this function
    imports LiteLLM in-process instead of searching PATH for litellm.exe.
    """
    import multiprocessing

    multiprocessing.freeze_support()
    args = list(argv if argv is not None else sys.argv[1:])
    args = [item for item in args if item != RUN_LITELLM_PROXY_FLAG]
    sys.argv = ["litellm", *args]
    try:
        from litellm.proxy.proxy_cli import run_server
    except ImportError as error:
        print(f"LiteLLM is not available in this build: {error}", file=sys.stderr)
        return 1
    run_server()
    return 0


@dataclass
class ProxyStatus:
    running: bool
    healthy: bool
    pid: int | None
    crashed: bool
    last_error: str | None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "healthy": self.healthy,
            "pid": self.pid,
            "crashed": self.crashed,
            "last_error": self.last_error,
        }


class ProxyManager:
    def __init__(
        self,
        config_path: str | os.PathLike[str] = "config.yaml",
        host: str = "127.0.0.1",
        port: int = 4000,
        log_path: str | os.PathLike[str] | None = None,
        pid_path: str | os.PathLike[str] | None = None,
        command: list[str] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.host = host
        self.port = port
        self.log_path = Path(log_path) if log_path else Path(".qazterion_proxy.log")
        # Persisted alongside the log so a *different* ProxyManager instance
        # (e.g. the next `qazterion-setup` CLI invocation, which is a brand
        # new process with nothing in memory) can still find and stop the
        # proxy a previous invocation started. Without this, `stop`/`status`
        # only ever know about a process this exact instance launched.
        self.pid_path = Path(pid_path) if pid_path else self.log_path.with_suffix(".pid")
        self._command = resolve_proxy_command(self.config_path, self.port, command)
        self._process: subprocess.Popen | None = None
        self._expected_stop = False
        self._last_error: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ---- health -----------------------------------------------------------

    def health_check(self, timeout: float = 2.0) -> bool:
        """True only if the proxy answers an HTTP request. Any connection
        failure, timeout, or non-2xx response counts as unhealthy — this
        function never raises."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/health/readiness", timeout=timeout) as response:
                return 200 <= response.status < 300
        except Exception:
            pass
        # Some LiteLLM builds only expose the root route; treat any HTTP
        # response (even an error page) as "something is listening".
        try:
            with urllib.request.urlopen(self.base_url, timeout=timeout) as response:
                return 200 <= response.status < 500
        except urllib.error.HTTPError as error:
            return error.code < 500
        except Exception:
            return False

    def _process_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def crashed(self) -> bool:
        """True when a process we started has exited without us calling
        stop()/restart() first."""
        if self._process is None or self._expected_stop:
            return False
        return self._process.poll() is not None

    def _tail_log(self, lines: int = 30) -> str | None:
        if not self.log_path.is_file():
            return None
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        return "\n".join(content[-lines:]) if content else None

    # ---- pid file (cross-process lifecycle) --------------------------------

    def _read_pid_file(self) -> int | None:
        try:
            text = self.pid_path.read_text(encoding="utf-8").strip()
            return int(text) if text else None
        except (OSError, ValueError):
            return None

    def _write_pid_file(self, pid: int) -> None:
        try:
            self.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(pid), encoding="utf-8")
        except OSError:
            pass

    def _clear_pid_file(self) -> None:
        try:
            self.pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """True if a process with this PID currently exists. Works without
        extra dependencies (no psutil) on both Windows and POSIX."""
        if pid is None or pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, just owned by someone else
        except OSError:
            return False
        return True

    @staticmethod
    def _kill_external_pid(pid: int, timeout: float = 5.0) -> None:
        """Terminate a process we didn't spawn ourselves (no Popen handle),
        recovered instead from the pid file. Best-effort: never raises."""
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and ProxyManager._pid_alive(pid):
            time.sleep(0.2)
        if ProxyManager._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def status(self) -> ProxyStatus:
        if self._process is not None:
            # Read the process's exit state exactly once up front.
            # `health_check()` below can take several seconds (two HTTP
            # attempts with their own timeouts) — if we asked "did it exit?"
            # again afterwards, a process that dies during that window would
            # show up as not-alive without ever being flagged as crashed, and
            # last_error would stay whatever was set before this call (e.g. a
            # stale "timed out" message from start()) instead of the real
            # reason. Taking one snapshot here keeps crashed/running/
            # last_error consistent with each other.
            exited = self._process.poll() is not None
            crashed = exited and not self._expected_stop
            if crashed:
                # Always refresh from the log tail so a stale message from an
                # earlier timeout never masks the real crash reason.
                self._last_error = self._tail_log() or "The proxy process exited unexpectedly."
            healthy = False if exited else self.health_check()
            running = not exited
            pid = self._process.pid
        else:
            # No in-memory handle — either nothing has been started from
            # this instance, or (the common case for the CLI) this is a
            # fresh process and a *previous* `qazterion-setup` invocation
            # started the proxy. Recover its pid from disk so status/stop
            # still work across separate CLI invocations, falling back to
            # "is anything answering on the port" if there's no pid file
            # (e.g. a proxy started entirely outside Qazterion).
            crashed = False  # we have no reliable "we started this" context here
            pid = self._read_pid_file()
            if pid is not None and not self._pid_alive(pid):
                self._clear_pid_file()
                pid = None
            healthy = self.health_check()
            running = pid is not None or healthy
        return ProxyStatus(
            running=running,
            healthy=healthy,
            pid=pid,
            crashed=crashed,
            last_error=self._last_error,
        )

    # ---- lifecycle ----------------------------------------------------------

    def start(self, env_overrides: dict[str, str] | None = None, wait_for_health: float = 30.0) -> ProxyStatus:
        if self.health_check():
            return self.status()

        wait_for_health = float(wait_for_health)

        # LiteLLM's startup banner (and some log lines) can contain characters
        # that Windows' default console codepage (cp1252) can't encode,
        # crashing the proxy with a UnicodeEncodeError before it ever binds
        # the port. Forcing UTF-8 I/O for the child process sidesteps that
        # regardless of what codepage the parent's console is using.
        venv_bin = Path(sys.executable).resolve().parent if sys.executable else Path(".")
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            **(env_overrides or {}),
        }
        path_entries = [str(venv_bin)]
        if env.get("PATH"):
            path_entries.append(env["PATH"])
        env["PATH"] = os.pathsep.join(path_entries)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._expected_stop = False
        self._last_error = None

        popen_kwargs: dict = {
            "cwd": str(self.config_path.parent) if self.config_path.parent != Path("") else None,
            "env": env,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        log_handle = open(self.log_path, "a", encoding="utf-8")
        try:
            try:
                self._process = subprocess.Popen(
                    self._command, stdout=log_handle, stderr=subprocess.STDOUT, **popen_kwargs
                )
            except OSError as error:
                self._last_error = (
                    f"Failed to start LiteLLM proxy ({self._command[0]}): {error}. "
                    "API keys remain saved; start the proxy later from API & Models."
                )
                return self.status()
        finally:
            log_handle.close()
        self._write_pid_file(self._process.pid)

        # wait_for_health <= 0 means spawn-and-return so the RPC thread is not
        # blocked for tens of seconds (which made key-save and status reloads
        # appear stuck / empty).
        if wait_for_health <= 0:
            return self.status()

        deadline = time.monotonic() + wait_for_health
        while time.monotonic() < deadline:
            if self.crashed():
                self._last_error = self._tail_log() or "The proxy exited before becoming healthy."
                return self.status()
            if self.health_check():
                break
            time.sleep(0.5)
        else:
            self._last_error = "Timed out waiting for the proxy to become healthy."
        return self.status()

    def stop(self, timeout: float = 5.0) -> ProxyStatus:
        self._expected_stop = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=timeout)
            self._clear_pid_file()
            return self.status()

        # No in-memory handle — recover the pid a previous invocation
        # recorded on disk (see status()) and terminate that process
        # directly, so `stop` actually works when run as a separate CLI
        # command from the one that started the proxy.
        pid = self._read_pid_file()
        if pid is not None and self._pid_alive(pid):
            self._kill_external_pid(pid, timeout=timeout)
        self._clear_pid_file()
        return self.status()

    def restart(self, env_overrides: dict[str, str] | None = None, wait_for_health: float = 15.0) -> ProxyStatus:
        self.stop()
        return self.start(env_overrides=env_overrides, wait_for_health=wait_for_health)