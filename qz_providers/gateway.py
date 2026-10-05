"""In-process model gateway: role -> (provider, model, key) selection with failover.

The agent asks for a *role* ("coder", "planner", "fast", ...). The gateway walks
that role's ordered model list from the catalog, skips models whose provider is
disabled or has no usable key, picks a healthy key for the provider, and calls
the provider adapter directly. Failures are classified once (see
:mod:`qz_providers.exceptions`) and drive what happens next:

* invalid/expired key      -> key disabled, rotate to the provider's next key
* rate limit / quota       -> key cools down, rotate to the next key
* model missing / bad req  -> skip to the next model in the role
* timeout / 5xx / network  -> try another key once, then the next model
* everything cooling down  -> wait for the soonest key (bounded), then retry

No proxy process, no background threads.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qz_providers.adapters import ProviderAdapter, create_adapter
from qz_providers.catalog import ModelSpec, ProviderCatalog, ProviderSpec
from qz_providers.exceptions import ProviderError, normalize_error
from qz_providers.health import HealthTracker
from qz_providers.keys import ApiKey, KeySource

_KEY_ROTATE_KINDS = {"auth", "rate_limit", "quota"}
_NEXT_MODEL_KINDS = {"model_not_found", "context_length", "invalid_request"}
_TRANSIENT_PER_MODEL = 2


@dataclass
class Attempt:
    route: str
    key_id: str
    ok: bool
    kind: str | None = None
    message: str | None = None
    latency_ms: float = 0.0

    def describe(self) -> str:
        status = "ok" if self.ok else f"{self.kind}: {self.message}"
        return f"{self.route} [{self.key_id}] {status}"


@dataclass
class RouteInfo:
    role: str
    provider: str
    model: str
    key_id: str
    fallback: bool
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.model}"


class NoRouteError(ProviderError):
    """No model in the role could be called (no keys, all disabled, all failed)."""

    kind = "no_route"

    def __init__(self, message: str, attempts: list[Attempt] | None = None, last_error: ProviderError | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []
        self.last_error = last_error


class GatewayResponse:
    """OpenAI-style completion plus the route that produced it."""

    def __init__(self, raw: Any, route: RouteInfo) -> None:
        self._raw = raw
        self.route = route

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    @property
    def raw(self) -> Any:
        return self._raw


def _redact(text: str) -> str:
    try:
        from qz_security.redaction import redact

        return redact(text)
    except Exception:
        return text


def _estimate_tokens(payload: Any) -> int:
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return max(1, len(text) // 4)


class ModelGateway:
    def __init__(
        self,
        catalog: ProviderCatalog | None = None,
        keys: KeySource | None = None,
        health: HealthTracker | None = None,
        usage_tracker: Any | None = None,
        adapter_factory: Callable[[ProviderSpec], ProviderAdapter] = create_adapter,
        event_sink: Callable[[str | None, str, dict], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.catalog = catalog or ProviderCatalog()
        self.keys = keys or KeySource(self.catalog)
        self.health = health or HealthTracker()
        self.usage = usage_tracker
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, ProviderAdapter] = {}
        self._adapters_lock = threading.Lock()
        self._event_sink = event_sink
        self._sleep = sleep
        self._log = log or _stderr_log
        # In-process pick order breaks ties when timestamps are equal.
        self._pick_seq: dict[str, int] = {}
        self._seq = 0

    # ---- wiring -------------------------------------------------------------

    def adapter(self, provider: ProviderSpec) -> ProviderAdapter:
        with self._adapters_lock:
            cached = self._adapters.get(provider.provider_id)
            if cached is not None and cached.spec == provider:
                return cached
            if cached is not None:
                cached.close()
            adapter = self._adapter_factory(provider)
            self._adapters[provider.provider_id] = adapter
            return adapter

    def reload(self) -> None:
        """Pick up catalog/key changes (e.g. after the user adds a key)."""
        self.catalog.reload()
        self.keys.invalidate()
        self.health.refresh()

    def close(self) -> None:
        with self._adapters_lock:
            for adapter in self._adapters.values():
                adapter.close()
            self._adapters.clear()

    # ---- planning -----------------------------------------------------------

    def plan(self, role: str, *, needs_tools: bool = False) -> tuple[list[tuple[ModelSpec, ProviderSpec, list[ApiKey]]], list[str]]:
        """Return usable (model, provider, keys) candidates and reasons for skipped ones."""
        candidates = self.catalog.candidates(role)
        usable: list[tuple[ModelSpec, ProviderSpec, list[ApiKey]]] = []
        skipped: list[str] = []
        if not candidates:
            skipped.append(f"'{role}' is not a known role or '<provider>/<model>' reference")
        for model in candidates:
            provider = self.catalog.provider(model.provider)
            if provider is None:
                skipped.append(f"{model.ref}: provider not configured")
                continue
            if not provider.enabled:
                skipped.append(f"{model.ref}: provider disabled")
                continue
            if needs_tools and not model.tools:
                skipped.append(f"{model.ref}: no tool-calling support")
                continue
            provider_keys = self.keys.keys_for(provider.provider_id)
            if not provider_keys:
                skipped.append(f"{model.ref}: no API key for {provider.provider_id} ({provider.key_prefix}_1)")
                continue
            usable.append((model, provider, provider_keys))
        return usable, skipped

    def _ordered_keys(self, keys: list[ApiKey], route: str) -> list[ApiKey]:
        healthy = [k for k in keys if self.health.key_available(k.key_id, k.fingerprint, route=route)]
        if self.catalog.settings.key_strategy == "priority":
            return healthy
        # balanced: least recently used first spreads load across accounts.
        return sorted(healthy, key=lambda k: (self.health.last_used(k.key_id), self._pick_seq.get(k.key_id, 0), k.index))

    def _soonest_recovery(self, plan: list[tuple[ModelSpec, ProviderSpec, list[ApiKey]]]) -> float | None:
        waits: list[float] = []
        for model, _provider, keys in plan:
            route_wait = self.health.route_cooldown_remaining(model.ref)
            for key in keys:
                remaining = self.health.key_cooldown_remaining(key.key_id, key.fingerprint, route=model.ref)
                if remaining is not None:
                    waits.append(max(route_wait, remaining))
        positive = [w for w in waits if w > 0]
        return min(positive) if positive else None

    # ---- execution ----------------------------------------------------------

    def complete(
        self,
        role: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        task_id: str | None = None,
        timeout: float | None = None,
    ) -> GatewayResponse:
        self.health.refresh()
        plan, skipped = self.plan(role, needs_tools=bool(tools))
        if not plan:
            detail = "; ".join(skipped[:6]) or "no candidates"
            raise NoRouteError(
                f"No usable model for '{role}'. {detail}. "
                "Add a key with `qazterion /keys add <provider>` or set e.g. GEMINI_KEY_1 in .env."
            )

        settings = self.catalog.settings
        timeout = float(timeout or settings.request_timeout_s)
        first_ref = plan[0][0].ref
        attempts: list[Attempt] = []
        last_error: ProviderError | None = None
        waited = 0.0

        while True:
            for model, provider, provider_keys in plan:
                if len(attempts) >= settings.max_attempts:
                    break
                if not self.health.route_available(model.ref):
                    continue
                transient_failures = 0
                for key in self._ordered_keys(provider_keys, model.ref):
                    if len(attempts) >= settings.max_attempts:
                        break
                    secret = self.keys.secret(key.key_id)
                    if not secret:
                        continue
                    self.health.mark_used(key.key_id)
                    self._seq += 1
                    self._pick_seq[key.key_id] = self._seq
                    started = time.monotonic()
                    try:
                        raw = self.adapter(provider).complete(
                            api_key=secret,
                            model=model.model_id,
                            messages=messages,
                            tools=tools or None,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=timeout,
                        )
                        message = raw.choices[0].message
                        if not (getattr(message, "content", None) or getattr(message, "tool_calls", None)):
                            raise normalize_error("provider returned an empty assistant message", status_code=503)
                    except Exception as raw_error:
                        error = normalize_error(raw_error)
                        latency = (time.monotonic() - started) * 1000.0
                        text = _redact(str(error.message))[:300]
                        attempts.append(Attempt(model.ref, key.key_id, False, error.kind, text, latency))
                        last_error = error
                        self.health.record_failure(
                            key.key_id, model.ref, error.kind, text,
                            retry_after=error.retry_after, fingerprint=key.fingerprint,
                        )
                        self._record_usage(task_id, model, key, latency, False, error=f"{error.kind}: {text}",
                                           fallback_from=first_ref, messages=messages, status=error.status_code)
                        self._log(f"[providers] {model.ref} [{key.key_id}] {error.kind}: {text[:120]}")
                        if error.kind in _KEY_ROTATE_KINDS:
                            continue
                        if error.kind in _NEXT_MODEL_KINDS:
                            break
                        transient_failures += 1
                        if transient_failures >= _TRANSIENT_PER_MODEL:
                            break
                        continue

                    latency = (time.monotonic() - started) * 1000.0
                    attempts.append(Attempt(model.ref, key.key_id, True, latency_ms=latency))
                    self.health.record_success(key.key_id, model.ref, latency, fingerprint=key.fingerprint)
                    self._record_usage(task_id, model, key, latency, True, raw=raw, fallback_from=first_ref, messages=messages)
                    fallback = model.ref != first_ref
                    if fallback or len(attempts) > 1:
                        self._emit(task_id, "MODEL_FALLBACK", {
                            "role": role, "from": first_ref, "to": model.ref, "key_id": key.key_id,
                            "attempts": [a.describe() for a in attempts],
                        })
                    return GatewayResponse(raw, RouteInfo(role, model.provider, model.model_id, key.key_id, fallback, attempts))

            if len(attempts) >= settings.max_attempts:
                break
            # Nothing succeeded this pass. If a key/route recovers soon, wait for it.
            wait = self._soonest_recovery(plan)
            budget = settings.max_wait_for_cooldown_s - waited
            if wait is None or wait > budget:
                break
            self._log(f"[providers] all routes for '{role}' cooling down; waiting {wait:.0f}s")
            self._sleep(wait)
            waited += wait

        summary = "; ".join(a.describe() for a in attempts[-6:]) or self._blocked_reasons(plan)
        self._emit(task_id, "MODEL_ROUTES_EXHAUSTED", {"role": role, "attempts": [a.describe() for a in attempts]})
        raise NoRouteError(f"All models for '{role}' failed: {summary}", attempts=attempts, last_error=last_error)

    def _blocked_reasons(self, plan: list[tuple[ModelSpec, ProviderSpec, list[ApiKey]]]) -> str:
        """Explain why nothing was even attempted (all routes/keys benched)."""
        reasons = []
        for model, _provider, keys in plan:
            route_wait = self.health.route_cooldown_remaining(model.ref)
            if route_wait > 0:
                reasons.append(f"{model.ref} benched for {route_wait:.0f}s")
                continue
            key_notes = []
            for key in keys:
                remaining = self.health.key_cooldown_remaining(key.key_id, key.fingerprint, route=model.ref)
                if remaining is None:
                    key_notes.append(f"{key.key_id} rejected (invalid/expired)")
                elif remaining > 0:
                    key_notes.append(f"{key.key_id} cooling {remaining:.0f}s")
            if key_notes:
                reasons.append(f"{model.ref}: {', '.join(key_notes)}")
        return "; ".join(reasons[:6]) or "no candidate could be called"

    # ---- bookkeeping ----------------------------------------------------------

    def _record_usage(
        self,
        task_id: str | None,
        model: ModelSpec,
        key: ApiKey,
        latency_ms: float,
        success: bool,
        *,
        raw: Any = None,
        error: str | None = None,
        fallback_from: str | None = None,
        messages: list[dict] | None = None,
        status: int | None = None,
    ) -> None:
        if self.usage is None:
            return
        usage = getattr(raw, "usage", None) if raw is not None else None
        in_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        out_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        estimated = False
        if not (in_tokens or out_tokens):
            in_tokens = _estimate_tokens(messages or [])
            if raw is not None:
                message = raw.choices[0].message
                out_tokens = _estimate_tokens(str(getattr(message, "content", "") or "") + str(getattr(message, "tool_calls", "") or ""))
            estimated = True
        try:
            self.usage.record_request(
                model=model.ref,
                provider=model.provider,
                key_id=key.key_id,
                task_id=task_id,
                duration=latency_ms / 1000.0,
                success=success,
                error=error,
                input_tokens=in_tokens,
                output_tokens=out_tokens if success else 0,
                is_estimated=estimated,
                fallback_from=fallback_from if fallback_from != model.ref else None,
                http_status=status,
                request_id=str(uuid.uuid4()),
            )
        except Exception:
            pass

    def _emit(self, task_id: str | None, event_type: str, payload: dict) -> None:
        if self._event_sink is None or not task_id:
            return
        try:
            self._event_sink(task_id, event_type, payload)
        except Exception:
            pass

    # ---- inspection (CLI /status, desktop dashboard) -----------------------

    def status(self) -> dict[str, Any]:
        self.health.refresh()
        snapshot = self.health.snapshot()
        providers = []
        for spec in self.catalog.providers.values():
            keys = self.keys.keys_for(spec.provider_id, include_disabled=True)
            key_rows = []
            for key in keys:
                state = snapshot["keys"].get(key.key_id, {})
                cooling = self.health.cooling_routes(key.key_id)
                rejected = not self.health.key_available(key.key_id, key.fingerprint) and self.health.key_cooldown_remaining(key.key_id, key.fingerprint) is None
                key_rows.append({
                    **key.to_dict(),
                    # Usable unless rejected; per-model limits are listed separately.
                    "available": key.enabled and not rejected and not (cooling and len(cooling) >= len(spec.models) > 0),
                    "cooldown_remaining_s": min(cooling.values()) if cooling else 0.0,
                    "cooling_models": cooling,
                    "disabled_reason": state.get("disabled_reason"),
                    "last_error_kind": state.get("last_error_kind"),
                })
            providers.append({
                **spec.to_dict(),
                "key_count": len([k for k in keys if k.enabled]),
                "usable_keys": len([r for r in key_rows if r["available"]]),
                "keys": key_rows,
            })
        roles = {}
        for role in self.catalog.roles:
            plan, skipped = self.plan(role)
            roles[role] = {
                "usable": [
                    model.ref for model, _p, keys in plan
                    if self.health.route_available(model.ref)
                    and any(self.health.key_available(k.key_id, k.fingerprint, route=model.ref) for k in keys)
                ],
                "skipped": skipped,
            }
        return {
            "transport": "direct",
            "key_strategy": self.catalog.settings.key_strategy,
            "providers": providers,
            "roles": roles,
            "routes": snapshot["routes"],
        }


def _stderr_log(message: str) -> None:
    try:
        print(f"\033[90m{message}\033[0m", file=sys.stderr, flush=True)
    except Exception:
        pass


_gateway: ModelGateway | None = None
_gateway_lock = threading.Lock()


def get_gateway() -> ModelGateway:
    """Process-wide gateway wired to the keystore, usage log and task events."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            from qz_paths import load_environment

            load_environment()
            usage = None
            try:
                from qz_usage_tracker import get_usage_tracker

                usage = get_usage_tracker()
            except Exception:
                usage = None
            _gateway = ModelGateway(usage_tracker=usage, event_sink=_task_event_sink)
        return _gateway


def set_gateway(gateway: ModelGateway | None) -> None:
    """Install a specific gateway (tests, embedding) or clear it with ``None``."""
    global _gateway
    with _gateway_lock:
        if _gateway is not None and _gateway is not gateway:
            _gateway.close()
        _gateway = gateway


def reset_gateway() -> None:
    set_gateway(None)


def _task_event_sink(task_id: str | None, event_type: str, payload: dict) -> None:
    if not task_id:
        return
    from qz_tasks.task_manager import log_event

    log_event(task_id, event_type, payload)
