"""Key and route (provider/model) health with cooldowns, persisted to a small JSON file.

Policy, by error kind:

* ``auth``             -> the key is disabled until its secret changes (re-added/replaced).
* ``rate_limit``       -> (key, model) cooldown: Retry-After, else 20s doubling per streak (max 5 min).
* ``quota``            -> (key, model) cooldown: Retry-After, else 30 min doubling (max 6 h).

Rate limits and quotas are tracked per key *and* model because providers apply
them that way (e.g. a free Gemini key has no Pro quota but plenty of Flash).
* ``model_not_found``  -> route cooldown for 6 h (the model is misconfigured or retired).
* ``timeout``/``connection``/``server``/``model_unavailable``
                       -> route cooldown after 2 consecutive failures (30 s doubling, max 10 min).
* ``context_length``/``invalid_request`` -> no penalty (the request, not the key, was at fault).

State is shared between processes (desktop backend and task runner) through
``<data dir>/provider_health.json``; each writer merges entries by timestamp.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from qz_paths import atomic_write_text, data_dir

HEALTH_FILENAME = "provider_health.json"

RATE_LIMIT_BASE_S = 20.0
RATE_LIMIT_MAX_S = 300.0
QUOTA_BASE_S = 1800.0
QUOTA_MAX_S = 6 * 3600.0
ROUTE_BASE_S = 30.0
ROUTE_MAX_S = 600.0
MODEL_NOT_FOUND_S = 6 * 3600.0
ROUTE_FAILURE_THRESHOLD = 2

_TRANSIENT_KINDS = {"timeout", "connection", "server", "model_unavailable", "error"}
_NO_PENALTY_KINDS = {"context_length", "invalid_request"}


@dataclass
class KeyState:
    fingerprint: str = ""
    cooldown_until: float = 0.0
    disabled_reason: str | None = None
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    last_used: float = 0.0
    last_error_kind: str | None = None
    last_error: str | None = None
    avg_latency_ms: float = 0.0
    updated_at: float = 0.0


@dataclass
class RouteState:
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_error_kind: str | None = None
    last_error: str | None = None
    updated_at: float = 0.0


@dataclass
class _Snapshot:
    keys: dict[str, KeyState] = field(default_factory=dict)
    routes: dict[str, RouteState] = field(default_factory=dict)
    limits: dict[str, RouteState] = field(default_factory=dict)  # "<key_id>|<provider/model>"


def _limit_id(key_id: str, route: str) -> str:
    return f"{key_id}|{route}"


class HealthTracker:
    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
        persist: bool = True,
    ) -> None:
        self.path = Path(path) if path else data_dir() / HEALTH_FILENAME
        self._clock = clock
        self._persist = persist
        self._lock = threading.RLock()
        self._state = _Snapshot()
        self._mtime = 0.0
        self._load()

    # ---- persistence ------------------------------------------------------

    def _read_file(self) -> _Snapshot:
        snapshot = _Snapshot()
        if not self._persist or not self.path.is_file():
            return snapshot
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return snapshot
        for key_id, data in (raw.get("keys") or {}).items():
            try:
                snapshot.keys[key_id] = KeyState(**{k: v for k, v in data.items() if k in KeyState.__dataclass_fields__})
            except TypeError:
                continue
        for section, target in (("routes", snapshot.routes), ("limits", snapshot.limits)):
            for name, data in (raw.get(section) or {}).items():
                try:
                    target[name] = RouteState(**{k: v for k, v in data.items() if k in RouteState.__dataclass_fields__})
                except TypeError:
                    continue
        return snapshot

    def _load(self) -> None:
        with self._lock:
            self._state = self._read_file()
            self._mtime = self._file_mtime()

    def _file_mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def refresh(self) -> None:
        """Merge changes written by another process since the last read."""
        if not self._persist:
            return
        with self._lock:
            mtime = self._file_mtime()
            if mtime and mtime != self._mtime:
                self._merge(self._read_file())
                self._mtime = mtime

    def _merge(self, other: _Snapshot) -> None:
        for key_id, theirs in other.keys.items():
            mine = self._state.keys.get(key_id)
            if mine is None or theirs.updated_at > mine.updated_at:
                self._state.keys[key_id] = theirs
        for mine_map, theirs_map in ((self._state.routes, other.routes), (self._state.limits, other.limits)):
            for name, theirs in theirs_map.items():
                mine = mine_map.get(name)
                if mine is None or theirs.updated_at > mine.updated_at:
                    mine_map[name] = theirs

    def _save(self) -> None:
        if not self._persist:
            return
        with self._lock:
            self._merge_from_disk_before_write()
            payload = {
                "keys": {k: asdict(v) for k, v in self._state.keys.items()},
                "routes": {k: asdict(v) for k, v in self._state.routes.items()},
                "limits": {k: asdict(v) for k, v in self._state.limits.items() if v.cooldown_until > self._clock()},
            }
            try:
                atomic_write_text(self.path, json.dumps(payload, indent=1))
                self._mtime = self._file_mtime()
            except OSError:
                pass

    def _merge_from_disk_before_write(self) -> None:
        mtime = self._file_mtime()
        if mtime and mtime != self._mtime:
            self._merge(self._read_file())

    # ---- queries ----------------------------------------------------------

    def _key(self, key_id: str, fingerprint: str = "") -> KeyState:
        state = self._state.keys.get(key_id)
        if state is None:
            state = KeyState(fingerprint=fingerprint)
            self._state.keys[key_id] = state
        elif fingerprint and state.fingerprint and state.fingerprint != fingerprint:
            # The secret was replaced: forget everything learned about the old key.
            state = KeyState(fingerprint=fingerprint)
            self._state.keys[key_id] = state
        elif fingerprint and not state.fingerprint:
            state.fingerprint = fingerprint
        return state

    def _route(self, route: str) -> RouteState:
        return self._state.routes.setdefault(route, RouteState())

    def key_available(self, key_id: str, fingerprint: str = "", now: float | None = None, route: str | None = None) -> bool:
        """False if the key was rejected, or (with ``route``) is rate limited for that model."""
        remaining = self.key_cooldown_remaining(key_id, fingerprint, route=route, now=now)
        return remaining is not None and remaining <= 0

    def key_cooldown_remaining(self, key_id: str, fingerprint: str = "", route: str | None = None,
                               now: float | None = None) -> float | None:
        """Seconds until the key can serve ``route`` again; ``None`` if the key was rejected."""
        with self._lock:
            state = self._key(key_id, fingerprint)
            if state.disabled_reason:
                return None
            current = now if now is not None else self._clock()
            remaining = max(0.0, state.cooldown_until - current)
            if route is not None:
                limit = self._state.limits.get(_limit_id(key_id, route))
                if limit is not None:
                    remaining = max(remaining, limit.cooldown_until - current)
            else:
                prefix = f"{key_id}|"
                for name, limit in self._state.limits.items():
                    if name.startswith(prefix):
                        remaining = max(remaining, limit.cooldown_until - current)
            return max(0.0, remaining)

    def cooling_routes(self, key_id: str) -> dict[str, float]:
        """Models this key is currently rate limited / out of quota for, with seconds left."""
        now = self._clock()
        prefix = f"{key_id}|"
        with self._lock:
            return {
                name[len(prefix):]: round(limit.cooldown_until - now, 1)
                for name, limit in self._state.limits.items()
                if name.startswith(prefix) and limit.cooldown_until > now
            }

    def route_available(self, route: str, now: float | None = None) -> bool:
        with self._lock:
            state = self._state.routes.get(route)
            return state is None or state.cooldown_until <= (now if now is not None else self._clock())

    def route_cooldown_remaining(self, route: str) -> float:
        with self._lock:
            state = self._state.routes.get(route)
            return 0.0 if state is None else max(0.0, state.cooldown_until - self._clock())

    def last_used(self, key_id: str) -> float:
        with self._lock:
            state = self._state.keys.get(key_id)
            return state.last_used if state else 0.0

    def key_state(self, key_id: str) -> KeyState | None:
        with self._lock:
            return self._state.keys.get(key_id)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            return {
                "keys": {
                    key_id: {
                        **{k: v for k, v in asdict(state).items() if k not in ("fingerprint", "last_error")},
                        "available": not state.disabled_reason and state.cooldown_until <= now,
                        "cooldown_remaining_s": round(max(0.0, state.cooldown_until - now), 1),
                    }
                    for key_id, state in self._state.keys.items()
                },
                "routes": {
                    route: {
                        "available": state.cooldown_until <= now,
                        "cooldown_remaining_s": round(max(0.0, state.cooldown_until - now), 1),
                        "consecutive_failures": state.consecutive_failures,
                        "last_error_kind": state.last_error_kind,
                    }
                    for route, state in self._state.routes.items()
                },
                "limits": {
                    name: {"cooldown_remaining_s": round(state.cooldown_until - now, 1), "last_error_kind": state.last_error_kind}
                    for name, state in self._state.limits.items() if state.cooldown_until > now
                },
            }

    # ---- updates ----------------------------------------------------------

    def record_success(self, key_id: str, route: str, latency_ms: float = 0.0, fingerprint: str = "") -> None:
        with self._lock:
            now = self._clock()
            state = self._key(key_id, fingerprint)
            had_problem = state.consecutive_failures > 0 or state.cooldown_until > 0
            state.successes += 1
            state.consecutive_failures = 0
            state.cooldown_until = 0.0
            state.last_used = now
            state.avg_latency_ms = latency_ms if state.successes == 1 else (state.avg_latency_ms * 0.8 + latency_ms * 0.2)
            state.updated_at = now
            for route_state in (self._state.routes.get(route), self._state.limits.get(_limit_id(key_id, route))):
                if route_state is not None and (route_state.consecutive_failures or route_state.cooldown_until):
                    route_state.consecutive_failures = 0
                    route_state.cooldown_until = 0.0
                    route_state.updated_at = now
                    had_problem = True
            if had_problem:
                self._save()

    def mark_used(self, key_id: str) -> None:
        with self._lock:
            self._key(key_id).last_used = self._clock()

    def record_failure(
        self,
        key_id: str | None,
        route: str,
        kind: str,
        message: str = "",
        retry_after: float | None = None,
        fingerprint: str = "",
    ) -> None:
        if kind in _NO_PENALTY_KINDS:
            return
        with self._lock:
            now = self._clock()
            short = (message or "")[:200]
            if key_id:
                state = self._key(key_id, fingerprint)
                state.failures += 1
                state.consecutive_failures += 1
                state.last_used = now
                state.last_error_kind = kind
                state.last_error = short
                state.updated_at = now
                if kind == "auth":
                    state.disabled_reason = "invalid_or_expired_key"
                elif kind in ("rate_limit", "quota"):
                    limit = self._state.limits.setdefault(_limit_id(key_id, route), RouteState())
                    limit.consecutive_failures += 1
                    streak = limit.consecutive_failures - 1
                    if kind == "rate_limit":
                        backoff = min(RATE_LIMIT_MAX_S, RATE_LIMIT_BASE_S * 2 ** streak)
                        limit.cooldown_until = now + (retry_after if retry_after else backoff)
                    else:
                        backoff = min(QUOTA_MAX_S, QUOTA_BASE_S * 2 ** streak)
                        limit.cooldown_until = now + max(retry_after or 0.0, backoff)
                    limit.last_error_kind = kind
                    limit.last_error = short
                    limit.updated_at = now

            if kind == "model_not_found":
                route_state = self._route(route)
                route_state.cooldown_until = now + MODEL_NOT_FOUND_S
                route_state.consecutive_failures += 1
            elif kind in _TRANSIENT_KINDS:
                route_state = self._route(route)
                route_state.consecutive_failures += 1
                if route_state.consecutive_failures >= ROUTE_FAILURE_THRESHOLD:
                    streak = route_state.consecutive_failures - ROUTE_FAILURE_THRESHOLD
                    route_state.cooldown_until = now + min(ROUTE_MAX_S, ROUTE_BASE_S * 2 ** streak)
            else:
                route_state = None
            if route_state is not None:
                route_state.last_error_kind = kind
                route_state.last_error = short
                route_state.updated_at = now
            self._save()

    def reset_key(self, key_id: str) -> None:
        """Forget all state for a key (e.g. after the user re-enables or replaces it)."""
        with self._lock:
            prefix = f"{key_id}|"
            for name in [n for n in self._state.limits if n.startswith(prefix)]:
                self._state.limits[name] = RouteState(updated_at=self._clock())
            if key_id in self._state.keys:
                fp = self._state.keys[key_id].fingerprint
                self._state.keys[key_id] = KeyState(fingerprint=fp, updated_at=self._clock())
            self._save()

    def reset_all(self) -> None:
        with self._lock:
            now = self._clock()
            self._state = _Snapshot()
            # Write explicit timestamps so other processes don't resurrect old entries on merge.
            self._save_empty(now)

    def _save_empty(self, now: float) -> None:
        if not self._persist:
            return
        try:
            atomic_write_text(self.path, json.dumps({"keys": {}, "routes": {}, "limits": {}, "reset_at": now}))
            self._mtime = self._file_mtime()
        except OSError:
            pass
