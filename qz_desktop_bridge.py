"""Trusted stdio bridge between Electron's main process and the Python backend."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qz_paths import load_environment  # noqa: E402
from qz_sandbox.backend import NO_WINDOW  # noqa: E402

load_environment()

import qz_agent  # noqa: E402
import qz_tools  # noqa: E402
from qz_core.common import TaskContext, task_context_scope  # noqa: E402
from qz_desktop_backend import DesktopBackend  # noqa: E402
from qz_recovery import get_resume_manager  # noqa: E402
from qz_security.gateway import configure as configure_security_gateway  # noqa: E402
from qz_tasks.task_manager import (
    announce_interrupted_tasks,
    find_interrupted_tasks,
    format_interrupted_notice,
    get_subtasks,
    log_event,
    subscribe_events,
)


# The protocol stream to Electron. Captured once at import so nothing that later
# reassigns sys.stdout (or prints from worker threads) can corrupt the framing.
_PROTOCOL_OUT = sys.stdout
_PROTOCOL_LOCK = threading.Lock()


def _protocol_write(line: str) -> None:
    with _PROTOCOL_LOCK:
        _PROTOCOL_OUT.write(line + "\n")
        _PROTOCOL_OUT.flush()


def _emit_task_event(task_id: str, event_type: str, payload: dict | None, timestamp: str) -> None:
    """Emit a structured task activity event line for the Electron main process."""
    event_data = {
        "taskId": task_id,
        "eventType": event_type,
        "timestamp": timestamp,
        "payload": payload or {},
    }
    try:
        _protocol_write("__QZ_EVENT__" + json.dumps(event_data, default=str))
    except Exception:
        pass


subscribe_events(_emit_task_event)

import queue
import threading
import time

_active_task_id: str | None = None
_always_allowed_commands_by_task: dict[str, set[str]] = {}


class _StreamLineReader:
    """A single persistent background reader for one stdin-like stream.

    `queue.Queue.get(timeout=...)` can't cancel the underlying blocking
    `stream.readline()` call that feeds it. Earlier this module spawned a
    *fresh* reader thread on every call; when a call timed out, that
    thread didn't stop -- it stayed alive, still blocked in `readline()`
    on the *same* shared stream (in production this is always `sys.stdin`,
    reused across every permission request in a task's lifetime). The next
    call would spawn its own thread and race the orphaned one for whatever
    line arrived next, so a user's response could be silently stolen by a
    stale thread from an earlier, already-timed-out request -- causing the
    *new* request to time out too, even though the user did respond.

    The fix is to own the stream with exactly one long-lived thread per
    stream, so there is never more than one reader in flight and no line
    can be delivered to the wrong caller.
    """

    _EOF = object()  # sentinel distinct from a real "" line and from a timeout

    _registry: dict[int, "_StreamLineReader"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                line = self._stream.readline()
                if line == "":
                    self._queue.put(self._EOF)
                    return
                self._queue.put(line)
        except Exception:
            self._queue.put(self._EOF)

    def get_line(self, timeout: float | None) -> str | None:
        """Return the next line, "" on EOF, or None if `timeout` elapses first."""
        try:
            item = self._queue.get(timeout=timeout) if timeout else self._queue.get()
        except queue.Empty:
            return None
        if item is self._EOF:
            # Put it back so any later waiter on this (now-closed) stream
            # also gets an immediate EOF instead of hanging.
            self._queue.put(self._EOF)
            return ""
        return item

    @classmethod
    def for_stream(cls, stream: Any) -> "_StreamLineReader":
        key = id(stream)
        with cls._registry_lock:
            reader = cls._registry.get(key)
            if reader is None:
                reader = cls(stream)
                cls._registry[key] = reader
            return reader


def _read_line_with_timeout(stream: Any, timeout: float | None = 300.0) -> str | None:
    """Read a single line from stream with a bounded timeout.

    Delegates to the single persistent reader thread for this stream (see
    `_StreamLineReader`) instead of spawning a new thread per call, so a
    timed-out wait never leaves a stale reader racing a later one for the
    same stream.
    """
    if timeout is None or timeout <= 0:
        return _StreamLineReader.for_stream(stream).get_line(None)
    return _StreamLineReader.for_stream(stream).get_line(timeout)


def create_desktop_ask_handler(
    task_id: str | None = None,
    input_stream=None,
    output_stream=None,
    timeout: float = 300.0,
):
    """Factory for interactive permission requests communicating over bridge stdout/stdin with bounded timeout."""
    def desktop_ask_handler(tool_name: str, payload: dict, risk: Any, reasons: list[str]) -> bool:
        current_task = task_id or _active_task_id or ""
        cmd = str(payload.get("command", "")).strip()

        # Scoped strictly to current_task only
        if current_task:
            allowed_set = _always_allowed_commands_by_task.get(current_task, set())
            if cmd in allowed_set or tool_name in allowed_set or "*" in allowed_set:
                return True

        req_id = str(uuid.uuid4())
        risk_str = getattr(risk, "value", str(risk))
        perm_data = {
            "requestId": req_id,
            "taskId": current_task,
            "tool": tool_name,
            "command": cmd,
            "risk": risk_str,
            "reasons": reasons,
        }

        if current_task:
            try:
                log_event(current_task, "PERMISSION_REQUESTED", perm_data)
            except Exception:
                pass

        try:
            line = "__QZ_PERMISSION__" + json.dumps(perm_data, default=str)
            if output_stream is not None:
                print(line, file=output_stream, flush=True)
            else:
                _protocol_write(line)
        except Exception:
            pass

        inp = input_stream or sys.stdin
        try:
            # Loop rather than a single read: because the stream is owned by
            # one persistent reader shared across every request in this
            # task's lifetime (see _StreamLineReader), a stray/late line left
            # over from an earlier, already-resolved request could otherwise
            # be handed to us. Skip anything that isn't a well-formed
            # response to *this* requestId, within the original timeout
            # budget.
            deadline = None if timeout is None or timeout <= 0 else time.monotonic() + timeout
            while True:
                remaining = timeout if deadline is None else max(0.0, deadline - time.monotonic())
                if deadline is not None and remaining <= 0:
                    line = None
                else:
                    line = _read_line_with_timeout(inp, timeout=remaining)

                if line is None:
                    # Timed out waiting for response
                    if current_task:
                        try:
                            log_event(current_task, "PERMISSION_TIMEOUT", {"requestId": req_id, "timeout_seconds": timeout})
                            log_event(current_task, "PERMISSION_DENIED", {"requestId": req_id, "reason": "timeout"})
                        except Exception:
                            pass
                    return False

                if line == "":
                    # EOF / stream closed
                    if current_task:
                        try:
                            log_event(current_task, "PERMISSION_DENIED", {"requestId": req_id, "reason": "eof"})
                        except Exception:
                            pass
                    return False

                try:
                    resp = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    # Not a parseable response line; keep waiting within budget.
                    continue
                if not isinstance(resp, dict):
                    # Valid JSON but not a response object (e.g. a bare number);
                    # keep waiting within budget.
                    continue

                incoming_request_id = resp.get("requestId")
                if incoming_request_id is not None and incoming_request_id != req_id:
                    # A late response to a request we already timed out on
                    # (or otherwise resolved). Discard it and keep waiting
                    # for *our* requestId within the remaining budget.
                    continue

                break

            decision = str(resp.get("decision", "deny")).lower()
            if decision == "allow_once":
                if current_task:
                    try:
                        log_event(current_task, "PERMISSION_GRANTED", {"requestId": req_id, "decision": "allow_once"})
                    except Exception:
                        pass
                return True
            elif decision == "always_allow":
                if current_task:
                    _always_allowed_commands_by_task.setdefault(current_task, set()).add(cmd or tool_name or "*")
                    try:
                        log_event(current_task, "PERMISSION_GRANTED", {"requestId": req_id, "decision": "always_allow"})
                    except Exception:
                        pass
                return True
            else:
                if current_task:
                    try:
                        log_event(current_task, "PERMISSION_DENIED", {"requestId": req_id, "decision": "deny"})
                    except Exception:
                        pass
                return False
        except Exception as e:
            if current_task:
                try:
                    log_event(current_task, "PERMISSION_DENIED", {"requestId": req_id, "reason": str(e)})
                except Exception:
                    pass
            return False

    return desktop_ask_handler


def _interrupted_task_payload() -> list[dict[str, Any]]:
    try:
        tasks = find_interrupted_tasks()
    except Exception:
        return []
    notices = []
    for task in tasks:
        notices.append({
            "id": task.get("id"),
            "status": task.get("status"),
            "currentStep": task.get("current_step"),
            "message": format_interrupted_notice(task),
        })
    return notices
APP_VERSION = "0.1.0"
def _approve_changes(ws: Path, task_id: str, files: list) -> dict[str, Any]:
    """Record the user's review of a task's changed files in the task's audit log.

    This is an acknowledgement only: it validates the files (inside the workspace,
    belonging to a known task of that workspace) and persists a CHANGES_APPROVED
    event. It never stages, commits or modifies anything.
    """
    from qz_security.workspace_guard import WorkspacePathError, resolve_workspace_path
    from qz_tasks.task_manager import get_task

    task = get_task(task_id)
    if task is None:
        raise ValueError(f"Unknown task '{task_id}'.")
    if Path(task.get("workspace_path") or "").resolve() != ws:
        raise ValueError("The task belongs to a different workspace.")
    approved: list[str] = []
    for item in files:
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            full = resolve_workspace_path(item, ws)
        except WorkspacePathError as error:
            raise ValueError(f"Refusing to approve a path outside the workspace: {item}") from error
        approved.append(full.relative_to(ws).as_posix())
    approved = sorted(set(approved))
    approved_at = datetime.now().isoformat(timespec="seconds")
    log_event(task_id, "CHANGES_APPROVED", {"files": approved, "approved_at": approved_at})
    return {"message": f"Recorded approval for {len(approved)} changed file(s).", "files": approved, "approvedAt": approved_at}


def _workspace(value: str | None) -> Path:
    """Validate a workspace path. RPC handlers run concurrently, so this never
    touches process-wide state; code that needs an implicit workspace runs inside
    ``task_context_scope`` instead."""
    if not value:
        raise ValueError("A workspace is required.")
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError("Selected project directory does not exist.")
    return path


def _in_workspace(ws: Path, fn):
    with task_context_scope(TaskContext(task_id="rpc", workspace=ws)):
        return fn()


def _git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    from qz_sandbox.backend import sanitize_subprocess_env
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env=sanitize_subprocess_env(str(workspace)),
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )


def _diffs(workspace: Path) -> dict[str, list[dict[str, Any]]]:
    changed, commits = [], []
    result = _git(workspace, ["diff", "--numstat"])
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.split("\t", 2)
            if len(fields) == 3:
                changed.append({"additions": int(fields[0]) if fields[0].isdigit() else 0, "deletions": int(fields[1]) if fields[1].isdigit() else 0, "path": fields[2]})
    history = _git(workspace, ["log", "-30", "--format=%H%x1f%an%x1f%aI%x1f%s"])
    if history.returncode == 0:
        for line in history.stdout.splitlines():
            fields = line.split("\x1f", 3)
            if len(fields) == 4:
                commits.append({"hash": fields[0], "author": fields[1], "timestamp": fields[2], "message": fields[3]})
    return {"changedFiles": changed, "commits": commits}


def _usage(backend: DesktopBackend) -> dict[str, Any]:
    all_events = backend.usage_tracker.recent_events(limit=1000)
    events = all_events[-50:]
    total = len(events)
    total_tokens = sum(int(e.get("total_tokens") or 0) for e in all_events)
    prompt_tokens = sum(int(e.get("input_tokens") or 0) for e in all_events)
    completion_tokens = sum(int(e.get("output_tokens") or 0) for e in all_events)
    estimated_cost = sum(float(e.get("estimated_cost") or 0.0) for e in all_events)

    return {
        "totalRequests": total,
        "totalTokens": total_tokens,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "estimatedCost": round(estimated_cost, 4),
        "successRate": round(100 * sum(bool(event.get("success")) for event in events) / total, 1) if total else 0,
        "fallbackCount": sum(bool(event.get("fallback_from")) for event in events),
        "lastActiveModel": events[-1].get("model") if events else None,
        "recentRequests": [
            {
                "time": datetime.fromtimestamp(event.get("ts", 0)).isoformat(timespec="seconds"),
                "model": event.get("model", "unknown"),
                "tokens": int(event.get("total_tokens") or 0),
                "cost": round(float(event.get("estimated_cost") or 0.0), 4),
                "latencyMs": round(float(event.get("duration", 0)) * 1000),
                "status": "success" if event.get("success") else "error",
                "fallbackFrom": event.get("fallback_from"),
            }
            for event in reversed(events)
        ],
        # Aggregate views are derived solely from real request history. They
        # intentionally contain masked key labels and "observed" evidence,
        # never guessed provider quotas or raw credentials.
        "breakdown": backend.usage_tracker.summary(),
    }


def _diagnostics(backend: DesktopBackend) -> dict[str, Any]:
    st = backend.status()
    routing = st.get("routing", {})
    logs = [
        f"{e.get('model')} [{e.get('key_id')}] {e.get('error')}"
        for e in backend.usage_tracker.recent_events(limit=200) if not e.get("success")
    ][-30:]
    notifications: list[dict[str, str]] = []

    if st["proxy"].get("healthy"):
        usable_roles = [role for role, info in routing.get("roles", {}).items() if info.get("usable")]
        notifications.append({
            "level": "success",
            "title": "Providers Connected",
            "detail": f"Direct provider access ready; {len(usable_roles)} role(s) have usable models.",
        })
    else:
        notifications.append({
            "level": "info",
            "title": "No Usable API Key",
            "detail": "Add a provider key in API & Models to start.",
        })
    for provider in routing.get("providers", []):
        for key in provider.get("keys", []):
            if key.get("disabled_reason"):
                notifications.append({
                    "level": "warning",
                    "title": f"{key['key_id']} rejected",
                    "detail": "The provider rejected this key (invalid or expired). Replace it to use it again.",
                })

    entries = backend.keystore.list_entries()
    enabled_keys = sum(1 for e in entries if e.enabled)
    if enabled_keys > 0:
        notifications.append({
            "level": "info",
            "title": f"{enabled_keys} API Key(s) Stored",
            "detail": f"Encrypted in local OS Keystore ({backend.keystore.backend_name()}).",
        })

    return {
        "notifications": notifications,
        "logs": logs,
    }


def _import_env_keys(backend: DesktopBackend, workspace: Path) -> dict[str, Any]:
    env_files = [workspace / ".env", ROOT / ".env"]
    found_keys: dict[str, list[str]] = {}

    for env_file in env_files:
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if not v:
                        continue
                    for prov in ["groq", "gemini", "mistral", "openrouter", "deepseek"]:
                        if prov in k.lower() and ("key" in k.lower() or "token" in k.lower()):
                            found_keys.setdefault(prov, []).append(v)
            except Exception:
                pass

    if found_keys:
        res = backend.first_run_setup(keys=found_keys)
        return {
            "imported": {p: len(v) for p, v in found_keys.items()},
            "configured": sorted(res["configured_providers"]),
            "count": sum(len(v) for v in found_keys.values()),
        }
    return {"imported": {}, "configured": [], "count": 0}


def _dispatch(backend: DesktopBackend, method: str, params: dict[str, Any]) -> Any:
    if method == "status":
        st = backend.status()
        is_healthy = bool(st["proxy"].get("healthy", False))
        aliases = st.get("aliases", [])
        configured_providers = {}
        raw_configured = st.get("configured_providers")
        if isinstance(raw_configured, list):
            for entry in raw_configured:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("provider") or "").lower()
                if not name:
                    continue
                bucket = configured_providers.setdefault(name, {"count": 0, "masked": entry.get("masked_value") or "••••••••••••"})
                bucket["count"] += 1
        elif isinstance(raw_configured, dict):
            for prov_name, prov_info in raw_configured.items():
                if not isinstance(prov_info, dict):
                    continue
                if int(prov_info.get("configured") or 0) <= 0:
                    continue
                keys_list = prov_info.get("keys") or []
                first_key = keys_list[0].get("masked_value", "••••••••••••") if keys_list else "••••••••••••"
                configured_providers[str(prov_name)] = {"count": int(prov_info.get("configured") or 0), "masked": first_key}

        coder = st.get("routing", {}).get("roles", {}).get("coder", {}).get("usable") or []
        active_model = coder[0] if coder else ("No Active Model" if not is_healthy else "Ready")

        return {
            "proxyStatus": "healthy" if is_healthy else "offline",
            "transport": "direct",
            "model": active_model,
            "connected": is_healthy,
            "aliases": aliases,
            "configuredProviders": configured_providers,
            "interruptedTasks": _interrupted_task_payload(),
        }
    if method == "first_run_setup":
        providers = params.get("providers", {})
        if not isinstance(providers, dict): raise ValueError("providers must be an object.")
        keys = {str(name).lower(): [key] for name, key in providers.items() if isinstance(key, str) and key.strip()}
        return {"configured": sorted(backend.first_run_setup(keys=keys)["configured_providers"])}
    if method == "generate_configuration": return backend.generate_configuration()
    if method == "validate_configuration": return backend.validate_configuration()
    if method == "apply_and_start":
        result = backend.apply_and_start()
        return {"status": result["proxy"].get("state", "requested")}
    if method == "set_routing_strategy":
        return backend.set_routing_strategy(str(params.get("strategy", "")))
    if method == "start": return backend.start()
    if method == "stop": return backend.stop()
    if method == "get_usage": return _usage(backend)
    if method == "import_env_keys":
        raw_ws = params.get("workspace")
        if not raw_ws:
            return {"imported": {}, "configured": [], "count": 0}
        return _import_env_keys(backend, _workspace(str(raw_ws)))
    if method == "get_diffs":
        raw_ws = params.get("workspace")
        if not raw_ws:
            return {"changedFiles": [], "commits": []}
        return _diffs(_workspace(str(raw_ws)))
    if method == "rollback_commit":
        raw_workspace = params.get("workspace")
        if not raw_workspace:
            raise ValueError("A workspace is required to roll back a commit.")
        ws = _workspace(str(raw_workspace))
        requested, head = str(params.get("commitHash", "")), _git(ws, ["rev-parse", "HEAD"])
        if head.returncode or not requested or not head.stdout.strip().startswith(requested): raise ValueError("Only the current HEAD commit can be rolled back.")
        result = _in_workspace(ws, qz_tools.rollback_last_change)
        if not result.startswith("Rollback complete:"): raise RuntimeError(result)
        return {"message": result}
    if method == "approve_changes":
        raw_workspace, task_id, files = params.get("workspace"), str(params.get("taskId", "")), params.get("files", [])
        if not raw_workspace or not task_id or not isinstance(files, list):
            raise ValueError("A workspace, task ID, and changed-file list are required to approve changes.")
        ws = _workspace(str(raw_workspace))
        return _approve_changes(ws, task_id, files)
    if method == "check_for_updates":
        # This build has no signed release feed configured. Return an honest backend result instead of simulating an update.
        return {"available": False, "currentVersion": APP_VERSION, "latestVersion": None, "message": "No signed update feed is configured for this build."}
    if method == "install_update":
        raise RuntimeError("Automatic installation is unavailable because this build has no signed update feed.")
    if method in ("get_task_subtasks", "getTaskSubtasks"):
        raw_task_id = params.get("taskId")
        if not raw_task_id:
            raise ValueError("A taskId is required to get task subtasks.")
        return get_subtasks(str(raw_task_id))
    if method in ("get_ready_subtasks", "getReadySubtasks"):
        raw_task_id = params.get("taskId")
        if not raw_task_id:
            raise ValueError("A taskId is required to get ready subtasks.")
        from qz_tasks.task_manager import get_ready_subtasks
        return get_ready_subtasks(str(raw_task_id))
    if method in ("cancel_task", "cancelTask"):
        raw_task_id = params.get("taskId")
        if not raw_task_id:
            raise ValueError("A taskId is required to cancel a task.")
        from qz_tasks.task_manager import update_status, log_event
        update_status(str(raw_task_id), "CANCELLED", current_step="cancelled")
        log_event(str(raw_task_id), "TASK_CANCELLED", {"reason": "rpc_cancel"})
        return {"cancelled": True, "taskId": str(raw_task_id)}
    if method == "getResumeOptions":
        raw_task_id = params.get("taskId")
        if not raw_task_id:
            raise ValueError("A taskId is required to get resume options.")
        raw_ws = params.get("workspace")
        ws = _workspace(str(raw_ws)) if raw_ws else None
        mgr = get_resume_manager(ws)
        plan = mgr.get_resumable_state(str(raw_task_id))
        integrity = mgr.verify_workspace_integrity(str(raw_task_id), workspace=ws)
        allowed = ["RESUME", "RESTART_CURRENT_SUBTASK", "ROLLBACK", "DISCARD"] if integrity.status == "CLEAN" else ["RESTART_CURRENT_SUBTASK", "ROLLBACK", "DISCARD"]
        return {
            "plan": plan.to_dict(),
            "integrity": integrity.to_dict(),
            "allowedDecisions": allowed,
        }
    if method == "resumeTask":
        raw_task_id = params.get("taskId")
        if not raw_task_id:
            raise ValueError("A taskId is required to resume a task.")
        decision = str(params.get("decision", "RESUME"))
        raw_ws = params.get("workspace")
        ws = _workspace(str(raw_ws)) if raw_ws else None
        mgr = get_resume_manager(ws)
        if ws is None:
            return mgr.resume_task(str(raw_task_id), decision=decision).to_dict()
        return _in_workspace(ws, lambda: mgr.resume_task(str(raw_task_id), workspace=ws, decision=decision)).to_dict()
    if method in ("get_providers", "getProviders"):
        return backend.get_providers()
    if method in ("set_provider_enabled", "setProviderEnabled"):
        prov = str(params.get("provider", ""))
        en = bool(params.get("enabled", True))
        return backend.set_provider_enabled(prov, en)
    if method in ("get_models", "getModels"):
        prov = params.get("provider")
        return backend.get_models(provider=str(prov) if prov else None)
    if method in ("refresh_models", "refreshModels"):
        prov = params.get("provider")
        return backend.refresh_models(provider=str(prov) if prov else None)
    if method in ("set_preferred_model", "setPreferredModel"):
        alias = str(params.get("alias", ""))
        prov = str(params.get("provider", ""))
        model_id = str(params.get("modelId", params.get("model_id", "")))
        return backend.set_preferred_model(alias, prov, model_id)
    if method in ("get_enabled_env", "getEnabledEnv"):
        # Decrypted secrets never leave the backend process.
        raise PermissionError("get_enabled_env was removed: API keys are never sent to the UI.")
    if method in ("add_provider_key", "addProviderKey"):
        prov = str(params.get("provider", ""))
        index = int(params.get("index", 1))
        val = str(params.get("value", params.get("key", "")))
        en = bool(params.get("enabled", True))
        return backend.add_provider_key(prov, index, val, enabled=en)
    if method in ("register_custom_provider", "registerCustomProvider"):
        return backend.register_custom_provider(
            provider=str(params.get("provider", "")),
            value=str(params.get("value", params.get("key", ""))),
            display_name=str(params.get("displayName", params.get("display_name", "")) or "") or None,
            base_url=str(params.get("baseUrl", params.get("base_url", "")) or "") or None,
            default_model=str(params.get("defaultModel", params.get("default_model", "")) or "") or None,
            index=int(params.get("index", 1)),
            enabled=bool(params.get("enabled", True)),
        )
    if method in ("delete_provider_key", "deleteProviderKey"):
        prov = str(params.get("provider", ""))
        index = int(params.get("index", 1))
        return backend.delete_provider_key(prov, index)
    if method in ("set_key_enabled", "setKeyEnabled"):
        prov = str(params.get("provider", ""))
        index = int(params.get("index", 1))
        en = bool(params.get("enabled", True))
        return backend.set_key_enabled(prov, index, en)
    if method in ("get_usage_metrics", "getUsageMetrics"):
        raw_task_id = params.get("taskId")
        if raw_task_id:
            return backend.usage_tracker.get_task_usage(str(raw_task_id))
        return backend.usage_tracker.summary()
    if method in ("get_intelligence", "getIntelligence"):
        return backend.intelligence_summary()
    if method in ("get_benchmark_report", "getBenchmarkReport"):
        return backend.benchmark_report()
    if method in ("preview_rollback", "previewRollback"):
        raw_task_id = str(params.get("taskId", ""))
        ckpt_id = params.get("checkpointId")
        raw_ws = params.get("workspace")
        ws = _workspace(str(raw_ws)) if raw_ws else None
        mgr = get_resume_manager(ws)
        return mgr.preview_rollback(raw_task_id, checkpoint_id=int(ckpt_id) if ckpt_id is not None else None, workspace=ws)
    if method in ("safe_rollback", "safeRollback"):
        raw_task_id = str(params.get("taskId", ""))
        ckpt_id = params.get("checkpointId")
        raw_ws = params.get("workspace")
        force = bool(params.get("force", False))
        ws = _workspace(str(raw_ws)) if raw_ws else None
        mgr = get_resume_manager(ws)
        outcome = mgr.safe_rollback_to_checkpoint(raw_task_id, checkpoint_id=int(ckpt_id) if ckpt_id is not None else None, workspace=ws, force=force)
        return outcome.to_dict()
    if method in ("get_project_rules", "getProjectRules"):
        raw_ws = params.get("workspace")
        ws = _workspace(str(raw_ws)) if raw_ws else None
        from qz_security.rules_loader import load_project_rules
        return load_project_rules(ws).to_dict()
    if method in ("get_context_summary", "getContextSummary"):
        query = str(params.get("query", ""))
        raw_ws = params.get("workspace")
        ws = _workspace(str(raw_ws)) if raw_ws else None
        from qz_context import ContextBudgetManager
        ctx = ContextBudgetManager().select_context(query, workspace=ws or Path.cwd())
        return {
            "query": query,
            "totalTokens": ctx.total_tokens,
            "budgetLimit": ctx.budget_limit,
            "truncated": ctx.truncated,
            "files": [f.get("path") for f in ctx.files],
            "snippetsCount": len(ctx.snippets),
            "dependencies": ctx.dependencies,
        }
    if method in ("check_system_health", "checkSystemHealth"):
        raw_ws = params.get("workspace")
        ws = _workspace(str(raw_ws)) if raw_ws else None
        res = backend.check_health(workspace=str(ws) if ws else None)
        # Kept in desktop RPC so desktop builds reading components.docker keep working; Docker is not used.
        res.setdefault("components", {})["docker"] = {
            "name": "Docker (not used)",
            "status": "healthy",
            "available": False,
            "details": "Not required: Qazterion runs commands on the host and never uses Docker.",
            "metadata": {"isolation": "host", "required": False},
        }
        return res
    if method in ("test_provider_connectivity", "testProviderConnectivity"):
        prov = str(params.get("provider", ""))
        val = str(params.get("key", params.get("value", "")))
        base_url = params.get("baseUrl", params.get("base_url"))
        return backend.test_key(prov, val, base_url=str(base_url) if base_url else None)
    if method in ("diagnostics", "get_diagnostics", "getDiagnostics"):
                return _diagnostics(backend)
    raise ValueError(f"Unsupported bridge method: {method}")


# Methods that mutate the keystore or config.yaml on disk. Two of these
# running at once (e.g. two "Save Key" clicks) could interleave a
# read-modify-write and lose one of them, so they're serialized with
# _WRITE_LOCK even though everything else now runs concurrently.
_KEYSTORE_WRITE_METHODS = {
    "add_provider_key", "addProviderKey",
    "delete_provider_key", "deleteProviderKey",
    "set_key_enabled", "setKeyEnabled",
    "register_custom_provider", "registerCustomProvider",
    "first_run_setup", "import_env_keys",
    "generate_configuration", "set_routing_strategy",
    "set_provider_enabled", "setProviderEnabled",
    "set_preferred_model", "setPreferredModel",
    "refresh_models", "refreshModels",
}

# Slow / network-bound methods. These are the ones that used to freeze the
# entire single-threaded stdin loop (and therefore every other screen) for
# up to tens of seconds. They're still routed through the same thread pool
# as everything else, but calling them out here documents *why* concurrency
# was required in the first place.
_NETWORK_METHODS = {
    "test_provider_connectivity", "testProviderConnectivity",
    "refresh_models", "refreshModels",
}

_WRITE_LOCK = threading.Lock()


def _json_safe(value: Any) -> Any:
    """Make a result strictly JSON-parseable (JavaScript rejects NaN/Infinity)."""
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _send_response(response: dict[str, Any]) -> None:
    try:
        line = json.dumps(_json_safe(response), default=str, allow_nan=False)
    except (TypeError, ValueError) as error:
        line = json.dumps({"id": response.get("id"), "ok": False,
                           "error": {"message": f"Result could not be serialized: {error}"}}, default=str)
    _protocol_write(line)


def _handle_request(backend: DesktopBackend, line: str) -> None:
    request: dict[str, Any] = {}
    try:
        request = json.loads(line.lstrip("\ufeff"))
        if not isinstance(request, dict):
            raise ValueError("Request must be an object.")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("params must be an object.")
        method = str(request.get("method", ""))
        sys.stderr.write(f"[RPC REQ] id={request.get('id')} method={method}\n")
        if method in _KEYSTORE_WRITE_METHODS:
            with _WRITE_LOCK:
                result = _dispatch(backend, method, params)
        else:
            result = _dispatch(backend, method, params)
        response = {"id": request.get("id"), "ok": True, "result": result}
        sys.stderr.write(f"[RPC OK] id={request.get('id')} method={method}\n")
    except Exception as error:
        from qz_security.redaction import redact

        message = redact(str(error)) or type(error).__name__
        sys.stderr.write(f"[RPC ERR] id={request.get('id')} method={request.get('method')} error={message}\n")
        response = {"id": request.get("id") if isinstance(request, dict) else None, "ok": False,
                    "error": {"message": message, "type": type(error).__name__}}
    _send_response(response)


def rpc_main() -> int:
    # Anything printed by library code goes to stderr; replies use _PROTOCOL_OUT.
    sys.stdout = sys.stderr
    backend = DesktopBackend()
    announce_interrupted_tasks(stream=sys.stderr)
    # Each request now runs on its own worker thread. Previously this was a
    # bare `for line in sys.stdin: ... dispatch synchronously ... print()`
    # loop, so ANY slow call (a provider connectivity test, model discovery
    # against a slow/unreachable API) blocked every other in-flight request
    # -- including the plain local "status"/"get_providers" reads the UI
    # fires on every tab switch. That head-of-line blocking is what made
    # keys look like they'd "reverted" and made Save/Test modals hang.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="qz-rpc") as pool:
        for line in sys.stdin:
            if not line.strip():
                continue
            pool.submit(_handle_request, backend, line)
        pool.shutdown(wait=True)
    backend.shutdown()
    return 0


def _configure_stdio() -> None:
    """Use UTF-8 on the pipes to Electron.

    On Windows, piped stdio otherwise uses the ANSI code page (cp1252): non-ASCII
    paths, prompts or keys from the UI would be mis-decoded, and printing model
    output with non-ASCII characters could raise UnicodeEncodeError.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> int:
    global _active_task_id
    _configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace"); parser.add_argument("--task"); parser.add_argument("--rpc", action="store_true")
    parser.add_argument("--mode", choices=qz_agent.TASK_MODES); parser.add_argument("--approval", choices=("always", "complex", "never"), default="never")
    args = parser.parse_args()
    if args.rpc: return rpc_main()
    if not args.workspace or not args.task: parser.error("--workspace and --task are required unless --rpc is used")
    announce_interrupted_tasks(stream=sys.stderr)
    workspace = _workspace(args.workspace)
    qz_agent.WORKSPACE = str(workspace)
    qz_tools.WORKSPACE = str(workspace)
    qz_agent.PLAN_APPROVAL_SETTING = args.approval
    qz_agent.FORCED_TASK_MODE = args.mode

    # Desktop approval protocol: generate the real plan, then pause on stdin
    # instead of using qz_agent's terminal prompt. This preserves the plan
    # produced by the agent and lets Electron resume the same task process.
    task_id = None
    try:
        task_id = qz_agent.create_task(args.task, qz_agent.WORKSPACE)
    except Exception:
        task_id = None
    _active_task_id = task_id
    configure_security_gateway(ask_handler=create_desktop_ask_handler(task_id))
    qz_agent._persist_task(task_id, lambda: qz_agent.update_status(task_id, qz_agent.TaskStatus.ANALYZING, current_step="analyzing"))
    qz_agent.prepare_environment(auto_setup=False, workspace=str(workspace))
    qz_agent.ensure_git_repository()
    baseline = qz_agent.capture_pre_existing_test_failures()
    index, _ = qz_agent.load_or_build_index(qz_agent.WORKSPACE)
    mode = args.mode or qz_agent.classify_task_mode(args.task)
    qz_agent._persist_task(task_id, lambda: qz_agent.log_event(task_id, "TASK_CLASSIFIED", {"mode": mode}))
    outcome = qz_agent.resolve_plan(
        args.task, qz_agent.format_index_summary(index), "never", mode,
        interactive_clarifications=False, task_id=task_id, build_dag=False,
    )
    should_pause = qz_agent.requires_plan_approval(mode, args.approval)
    if should_pause:
        qz_agent._persist_task(task_id, lambda: qz_agent.update_status(
            task_id, qz_agent.TaskStatus.WAITING_APPROVAL, current_step="waiting_approval",
        ))
        qz_agent._persist_task(task_id, lambda: qz_agent.log_event(task_id, "WAITING_APPROVAL", {"mode": mode}))
        _protocol_write("__QZ_PLAN__" + json.dumps({"task": outcome["task"], "plan": outcome["plan"], "architecture": outcome["architecture"], "mode": mode}))
        try:
            # Same single stdin reader as permission prompts, so no line is lost.
            response = json.loads(_read_line_with_timeout(sys.stdin, timeout=None) or "{}")
            if not isinstance(response, dict):
                response = {"decision": "reject"}
        except Exception:
            response = {"decision": "reject"}
        # Same contract as earlier desktop builds: anything but an explicit
        # rejection proceeds; a missing/unparseable reply counts as rejection.
        decision = str(response.get("decision") or "reject").strip().lower()
        if decision in ("reject", "rejected", "cancel", "deny", "no"):
            qz_agent._persist_task(task_id, lambda: qz_agent.update_status(task_id, qz_agent.TaskStatus.CANCELLED, current_step="cancelled"))
            qz_agent._persist_task(task_id, lambda: qz_agent.log_event(task_id, "TASK_CANCELLED", {"reason": "plan_rejected"}))
            print("Task rejected by user before implementation.", flush=True)
            return 0
        edited = response.get("plan") if decision == "edit" and isinstance(response.get("plan"), str) else None
        qz_agent.finalize_plan(outcome, task_id=task_id, plan=edited)
        qz_agent._persist_task(task_id, lambda: qz_agent.log_event(task_id, "APPROVAL_RECEIVED", {"decision": decision}))
    else:
        qz_agent.finalize_plan(outcome, task_id=task_id)
    print(qz_agent.run_executor(
        outcome["task"], outcome["plan"], outcome["architecture"],
        test_baseline=baseline, task_id=task_id,
    ), flush=True)
    return 0


if __name__ == "__main__": raise SystemExit(main())
