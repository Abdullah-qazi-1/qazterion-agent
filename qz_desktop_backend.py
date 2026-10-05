"""Desktop backend: key management, provider/model configuration and status.

UI-free Python API used by the desktop bridge (and the ``qazterion-setup`` CLI).
Model calls go straight from the agent to providers through the in-process
gateway, so there is no proxy process to start, stop or keep healthy; the
``start``/``stop``/``apply_and_start`` methods remain for UI compatibility and
simply reload configuration and report status.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from qz_keystore import KeyStore, SUPPORTED_PROVIDERS, mask_key
from qz_paths import load_environment
from qz_providers.catalog import CatalogError, ProviderCatalog
from qz_providers.gateway import ModelGateway, get_gateway
from qz_usage_tracker import UsageTracker, get_usage_tracker


class SetupError(RuntimeError):
    """A clear, user-facing first-run or configuration problem."""


class DesktopBackend:
    def __init__(
        self,
        keystore: KeyStore | None = None,
        usage_tracker: UsageTracker | None = None,
        gateway: ModelGateway | None = None,
        **_legacy: Any,
    ) -> None:
        load_environment()
        self.keystore = keystore or KeyStore()
        self.usage_tracker = usage_tracker or get_usage_tracker()
        self.gateway = gateway or get_gateway()

    @property
    def catalog(self) -> ProviderCatalog:
        return self.gateway.catalog

    def _keys_changed(self, key_id: str | None = None) -> None:
        self.gateway.keys.invalidate()
        if key_id:
            self.gateway.health.reset_key(key_id)

    # ---- keys -------------------------------------------------------------------

    def first_run_setup(self, keys: dict[str, list[str]] | None = None, master_key: str | None = None,
                        interactive: bool = False) -> dict:
        """Store keys, e.g. ``{"groq": ["k1", "k2"], "gemini": ["k3"]}``.

        New keys are appended after the provider's existing ones; a key that is
        already stored is not duplicated. ``master_key`` is ignored (no proxy).
        """
        del master_key
        if keys is None:
            keys = self._prompt_for_keys() if interactive else {}
        configured: dict[str, int] = {}
        for provider, values in keys.items():
            pid = provider.lower().strip()
            existing = self.keystore.list_entries(provider=pid)
            used = {e.index for e in existing}
            known = {e.masked_value for e in existing}
            added = 0
            for value in (v.strip() for v in values if v and v.strip()):
                if mask_key(value) in known and any(self.keystore.get_key(pid, e.index) == value for e in existing):
                    continue
                index = 1
                while index in used:
                    index += 1
                env_name = self.keystore.set_key(pid, index, value, enabled=True)
                self._keys_changed(env_name)
                used.add(index)
                added += 1
            if added or existing:
                configured[pid] = len(existing) + added
        return {
            "backend": self.keystore.backend_name(),
            "configured_providers": configured,
            "unconfigured_providers": [p for p in self.catalog.providers if p not in configured],
            "master_key_set": True,
        }

    def _prompt_for_keys(self) -> dict[str, list[str]]:
        print("Qazterion setup - enter API keys per provider (comma-separate several keys; blank skips).")
        collected: dict[str, list[str]] = {}
        for provider in self.catalog.providers:
            entered = getpass.getpass(f"{provider} key(s): ")
            if entered.strip():
                collected[provider] = [part.strip() for part in entered.split(",")]
        return collected

    def add_provider_key(self, provider: str, index: int, value: str, enabled: bool = True) -> dict:
        env_name = self.keystore.set_key(provider, index, value, enabled=enabled)
        self._keys_changed(env_name)
        return {"provider": provider.lower(), "index": index, "env_name": env_name, "masked_value": mask_key(value)}

    def delete_provider_key(self, provider: str, index: int) -> dict:
        removed = self.keystore.delete_key(provider, index)
        self._keys_changed()
        return {"provider": provider.lower(), "index": index, "removed": removed}

    def set_key_enabled(self, provider: str, index: int, enabled: bool) -> dict:
        self.keystore.set_enabled(provider, index, enabled)
        env_name = f"{self.keystore._family_for(provider)}_{index}"
        # Re-enabling is an explicit "try this key again", so forget past failures.
        self._keys_changed(env_name if enabled else None)
        return {"provider": provider.lower(), "index": index, "enabled": enabled}

    def register_custom_provider(self, provider: str, value: str, display_name: str | None = None,
                                 base_url: str | None = None, default_model: str | None = None,
                                 index: int = 1, enabled: bool = True) -> dict:
        """Add an OpenAI-compatible provider (base URL + model) and store its key."""
        try:
            spec = self.catalog.upsert_custom_provider(
                provider, base_url=base_url or "", display_name=display_name, default_model=default_model,
            )
        except CatalogError as error:
            raise SetupError(str(error)) from error
        self.keystore.upsert_custom_provider(
            spec.provider_id, display_name=spec.display_name, base_url=spec.base_url,
            default_model=default_model, family=spec.key_prefix,
        )
        key_info = self.add_provider_key(spec.provider_id, index, value, enabled=enabled)
        self.gateway.reload()
        return {**key_info, "display_name": spec.display_name, "base_url": spec.base_url,
                "default_model": default_model, "generation": self.generate_configuration()}

    def test_key(self, provider: str, value: str, base_url: str | None = None) -> dict:
        from qz_providers.connectivity import test_provider_connectivity

        return test_provider_connectivity(provider, value, base_url=base_url, catalog=self.catalog).to_dict()

    # ---- configuration --------------------------------------------------------

    def generate_configuration(self) -> dict:
        """Summarize which roles can be served by the configured keys (no files are written)."""
        status = self.gateway.status()
        warnings = [
            f"Role '{role}' has no usable model: " + "; ".join(info["skipped"][:3])
            for role, info in status["roles"].items() if not info["usable"]
        ]
        return {
            "model_entries": sum(len(info["usable"]) for info in status["roles"].values()),
            "configured_families": sorted(p["provider_id"] for p in status["providers"] if p["key_count"]),
            "warnings": warnings,
        }

    def validate_configuration(self) -> dict:
        errors: list[str] = []
        try:
            self.catalog.reload()
        except CatalogError as error:
            errors.append(str(error))
        else:
            status = self.gateway.status()
            if not any(p["key_count"] for p in status["providers"]):
                errors.append("No API key is configured for any provider.")
            elif not status["roles"].get("coder", {}).get("usable"):
                errors.append("No configured provider can serve the 'coder' role.")
        return {"valid": not errors, "errors": errors}

    def set_routing_strategy(self, strategy: str) -> dict:
        """Persist the key strategy. Echoes the requested name (as older UIs expect)
        and reports the effective Qazterion strategy separately."""
        try:
            normalized = self.catalog.set_key_strategy(strategy)
        except CatalogError as error:
            raise SetupError(str(error)) from error
        return {"strategy": strategy, "effective_strategy": normalized}

    # ---- "proxy" lifecycle (compatibility: there is no proxy any more) -------

    def _connection_status(self) -> dict:
        status = self.gateway.status()
        usable = any(p["usable_keys"] for p in status["providers"] if p["enabled"])
        return {
            "running": True,
            "healthy": usable,
            "state": "direct" if usable else "no_keys",
            "pid": None,
            "crashed": False,
            "last_error": None if usable else "No usable API key. Add a provider key to start.",
        }

    def apply_and_start(self, wait_for_health: float = 0.0) -> dict:
        del wait_for_health
        self.gateway.reload()
        return {"generation": self.generate_configuration(), "validation": self.validate_configuration(),
                "proxy": self._connection_status()}

    def start(self, wait_for_health: float = 0.0) -> dict:
        del wait_for_health
        self.gateway.reload()
        return self._connection_status()

    def stop(self) -> dict:
        return self._connection_status()

    def shutdown(self) -> None:
        self.gateway.close()

    # ---- status for UI ------------------------------------------------------------

    def status(self) -> dict:
        gateway_status = self.gateway.status()
        return {
            "proxy": self._connection_status(),
            "configured_providers": [
                {"provider": k["provider"], "env_name": k["key_id"], "masked_value": k["masked_value"], "enabled": k["enabled"]}
                for p in gateway_status["providers"] for k in p["keys"]
            ],
            "master_key_set": True,
            "keystore_backend": self.keystore.backend_name(),
            "aliases": sorted(self.catalog.roles),
            "fallback_order": {role: list(refs) for role, refs in self.catalog.roles.items()},
            "routing": gateway_status,
            "usage": self.usage_tracker.summary(),
            "last_error": None,
        }

    def get_providers(self) -> list[dict]:
        rows = []
        for provider in self.gateway.status()["providers"]:
            rows.append({
                **{k: v for k, v in provider.items() if k != "keys"},
                "default_model": provider["models"][0] if provider["models"] else None,
                "configured": provider["key_count"] > 0,
                "supported_capabilities": ["chat", "tool_calling"],
                "default_concurrency": 1,
                "keys": provider["keys"],
            })
        return rows

    def set_provider_enabled(self, provider: str, enabled: bool) -> dict:
        try:
            self.catalog.set_provider_enabled(provider, enabled)
        except CatalogError as error:
            raise SetupError(str(error)) from error
        return {"provider": provider.lower(), "enabled": enabled}

    def get_models(self, provider: str | None = None) -> list[dict]:
        configured = {p for p in self.catalog.providers if self.gateway.keys.keys_for(p)}
        results = []
        for model in self.catalog.all_models(provider):
            row = model.to_dict()
            available = self.gateway.health.route_available(model.ref)
            row["is_configured"] = model.provider in configured
            # Same lifecycle vocabulary the UI has always received.
            row["state"] = ("active" if available else "degraded") if row["is_configured"] else "unconfigured"
            row["roles"] = [role for role, refs in self.catalog.roles.items() if model.ref in refs]
            row.update(_legacy_model_fields(model))
            results.append(row)
        return results

    def refresh_models(self, provider: str | None = None, per_provider_timeout: float = 10.0) -> dict:
        """Ask each configured provider which models it offers and add new ones to the catalog."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

        targets = [provider.lower()] if provider else [p for p, spec in self.catalog.providers.items() if spec.enabled]
        jobs = {}
        for pid in targets:
            spec = self.catalog.provider(pid)
            keys = self.gateway.keys.keys_for(pid)
            secret = self.gateway.keys.secret(keys[0].key_id) if keys else None
            if spec and secret:
                jobs[pid] = (spec, secret)

        refreshed: dict[str, int] = {}
        errors: dict[str, str] = {}
        if jobs:
            with ThreadPoolExecutor(max_workers=min(4, len(jobs)), thread_name_prefix="qz-discover") as pool:
                futures = {
                    pid: pool.submit(self.gateway.adapter(spec).list_models, api_key=secret, timeout=per_provider_timeout)
                    for pid, (spec, secret) in jobs.items()
                }
                for pid, future in futures.items():
                    try:
                        models = future.result(timeout=per_provider_timeout + 2)
                    except FutureTimeoutError:
                        errors[pid] = f"Discovery timed out after {per_provider_timeout:.0f}s."
                        continue
                    except Exception as error:
                        errors[pid] = getattr(error, "kind", "error") + ": " + str(error)[:200]
                        continue
                    new = {
                        m["id"]: {k: v for k, v in (("context_window", m.get("context_window")), ("tools", m.get("tools"))) if v is not None}
                        for m in models if m.get("id")
                    }
                    if new:
                        self.catalog.add_models(pid, new)
                    refreshed[pid] = len(new)
        result: dict[str, Any] = {"refreshed": refreshed, "total_models": len(self.catalog.all_models())}
        if errors:
            result["errors"] = errors
        return result

    def set_preferred_model(self, alias: str, provider: str, model_id: str) -> dict:
        """Put ``provider/model_id`` first in a role (``alias`` may be a legacy alias name)."""
        try:
            self.catalog.set_role_preference(alias, provider, model_id)
        except CatalogError as error:
            raise SetupError(str(error)) from error
        role = self.catalog.resolve_role(alias) or alias
        return {"alias": role, "provider": provider.lower(), "model_id": model_id, "order": list(self.catalog.roles.get(role, ()))}

    # ---- diagnostics ------------------------------------------------------------

    def check_health(self, workspace: str | None = None) -> dict:
        from qz_health import check_system_health

        return check_system_health(workspace=workspace, keystore=self.keystore).to_dict()

    def intelligence_summary(self) -> dict:
        """Observed routing evidence: success/latency per model and key health."""
        usage = self.usage_tracker.summary()
        models = usage.get("by_model", {})
        ranked = sorted(models.items(), key=lambda item: (item[1].get("success_rate", 0), -item[1].get("latency_ms", 0)), reverse=True)
        fastest = min(models.items(), key=lambda item: item[1].get("latency_ms", float("inf")), default=(None, {}))
        routing = self.gateway.status()
        degraded = []
        for route, info in routing["routes"].items():
            if info["available"]:
                continue
            provider_id, _, model_id = route.partition("/")
            degraded.append({
                "provider": provider_id, "model_id": model_id, "display_name": route, "ref": route,
                "state": "unavailable" if info.get("last_error_kind") == "model_not_found" else "degraded",
                "consecutive_failures": info.get("consecutive_failures", 0),
                "cooldown_remaining_s": info.get("cooldown_remaining_s", 0.0),
            })
        return {
            "evidence": "observed",
            "best_overall": ranked[0][0] if ranked else None,
            "fastest": fastest[0],
            "degraded_models": degraded,
            "pool": usage.get("global_pool", {}),
            "providers": usage.get("by_provider", {}),
            "models": models,
            "keys": usage.get("by_key", {}),
            "routing": routing,
            "quota_note": "Quota values are provider-confirmed only when a provider response supplies them; all other health is observed.",
        }

    def benchmark_report(self) -> dict:
        """Task/subtask/repair/validation statistics plus per-model usage (same shape as before)."""
        import json as _json
        from collections import defaultdict

        from qz_tasks.task_manager import get_manager

        report: dict[str, Any] = {
            "total_tasks": 0, "completed_tasks": 0, "failed_tasks": 0, "task_success_rate": 0.0,
            "total_nodes": 0, "completed_nodes": 0, "failed_nodes": 0, "node_success_rate": 0.0,
            "repair_attempts": 0, "successful_repairs": 0, "total_tokens": 0, "total_cost": 0.0,
            "average_task_duration": 0.0, "failure_categories": {}, "models": {}, "validation_pass_rates": {},
        }
        try:
            with get_manager()._session() as conn:
                for status, count in conn.execute("SELECT upper(status), count(*) FROM tasks GROUP BY upper(status)"):
                    report["total_tasks"] += count
                    if status == "COMPLETED":
                        report["completed_tasks"] += count
                    elif status in ("FAILED", "CANCELLED"):
                        report["failed_tasks"] += count
                for status, count in conn.execute("SELECT upper(status), count(*) FROM subtasks GROUP BY upper(status)"):
                    report["total_nodes"] += count
                    if status == "COMPLETED":
                        report["completed_nodes"] += count
                    elif status in ("FAILED", "CANCELLED", "BLOCKED"):
                        report["failed_nodes"] += count
                report["repair_attempts"] = conn.execute(
                    "SELECT count(*) FROM task_events WHERE event_type IN ('NODE_RETRYING', 'REPAIR_ATTEMPT')"
                ).fetchone()[0]
                validation: dict[str, dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0})
                failures: dict[str, int] = defaultdict(int)
                rows = conn.execute(
                    "SELECT event_type, payload FROM task_events WHERE event_type IN ('VALIDATION_CHECK', 'NODE_COMPLETED')"
                )
                for event_type, raw in rows:
                    try:
                        payload = _json.loads(raw) if raw else {}
                    except ValueError:
                        payload = {}
                    if event_type == "NODE_COMPLETED":
                        if int(payload.get("attempts") or 1) > 1:
                            report["successful_repairs"] += 1
                        continue
                    name, status = payload.get("name", "unknown"), payload.get("status", "")
                    if status == "PASS":
                        validation[name]["pass"] += 1
                    elif status in ("FAIL", "ERROR"):
                        validation[name]["fail"] += 1
                        failures[str(name).upper()] += 1
                report["validation_pass_rates"] = {
                    name: round(c["pass"] / (c["pass"] + c["fail"]) * 100.0, 1)
                    for name, c in validation.items() if c["pass"] + c["fail"]
                }
                report["failure_categories"] = dict(failures)
        except Exception:
            pass
        if report["total_tasks"]:
            report["task_success_rate"] = round(report["completed_tasks"] / report["total_tasks"] * 100.0, 1)
        if report["total_nodes"]:
            report["node_success_rate"] = round(report["completed_nodes"] / report["total_nodes"] * 100.0, 1)

        usage = self.usage_tracker.summary()
        report["total_tokens"] = usage.get("total_tokens", 0)
        report["total_cost"] = usage.get("estimated_cost", 0.0)
        by_model: dict[str, list[dict]] = defaultdict(list)
        for event in self.usage_tracker.recent_events(limit=1000):
            by_model[str(event.get("model") or "unknown")].append(event)
        for name, events in by_model.items():
            total = len(events)
            ok = sum(1 for e in events if e.get("success"))
            duration = sum(float(e.get("duration") or 0.0) for e in events)
            report["models"][name] = {
                "model": name,
                "provider": events[0].get("provider") or name.split("/")[0],
                "total_requests": total,
                "successful_requests": ok,
                "failed_requests": total - ok,
                "fallback_count": sum(1 for e in events if e.get("fallback_from")),
                "total_tokens": sum(int(e.get("total_tokens") or 0) for e in events),
                "total_cost": round(sum(float(e.get("estimated_cost") or 0.0) for e in events), 6),
                "average_latency": round(duration / total, 3) if total else 0.0,
                "success_rate": round(ok / total * 100.0, 1) if total else 0.0,
            }
        return report


def _legacy_model_fields(model) -> dict:
    """Fields the desktop UI received from the previous model registry."""
    capabilities = ["chat", "streaming"] + (["tool_calling"] if model.tools else [])
    if model.context_window >= 65536:
        capabilities.append("large_context")
    return {
        "capabilities": capabilities,
        "max_output_tokens": None,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "supports_vision": False,
        "supports_streaming": True,
        "supports_reasoning": False,
        "supports_structured_output": True,
        "consecutive_failures": 0,
        "last_discovery_time": None,
        "pricing_input_per_1m": None,
        "pricing_output_per_1m": None,
        "source": "catalog",
        "historical": False,
    }


# ---- CLI --------------------------------------------------------------------------


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qazterion-setup", description="Qazterion provider/key setup.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Interactively store API keys.")
    sub.add_parser("status", help="Print providers, keys (masked), roles and usage as JSON.")
    args = parser.parse_args(argv)
    backend = DesktopBackend()
    try:
        if args.command == "setup":
            print(json.dumps(backend.first_run_setup(interactive=True), indent=2))
        else:
            print(json.dumps(backend.status(), indent=2, default=str))
    except (SetupError, ValueError) as error:
        print(f"Setup error: {error}", file=sys.stderr)
        return 1
    finally:
        backend.shutdown()
    return 0


__all__ = ["DesktopBackend", "SetupError", "SUPPORTED_PROVIDERS", "cli_main"]


if __name__ == "__main__":
    raise SystemExit(cli_main())
