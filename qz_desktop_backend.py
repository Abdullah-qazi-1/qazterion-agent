"""Phase 12 — desktop backend: first-run setup, config generation, provider/model management,
and automatic LiteLLM proxy lifecycle.

This module is deliberately UI-free. It is the backend half of Phase 12 —
everything a desktop UI would call into — exposed as a plain Python API, plus a small CLI
(`qazterion-setup`) so the same behavior can be used and verified directly.
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path

import yaml

import generate_config
from qz_keystore import KeyStore, SUPPORTED_PROVIDERS, mask_key
from qz_proxy_manager import ProxyManager
from qz_usage_tracker import UsageTracker, default_usage_log_path

DEFAULT_CONFIG_PATH = "config.yaml"


class SetupError(RuntimeError):
    """A clear, user-facing first-run or configuration problem."""


class DesktopBackend:
    def __init__(
        self,
        keystore: KeyStore | None = None,
        proxy_manager: ProxyManager | None = None,
        usage_tracker: UsageTracker | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        stop_proxy_on_exit: bool = True,
    ) -> None:
        self.config_path = Path(config_path)
        self.keystore = keystore or KeyStore()
        self.proxy_manager = proxy_manager or ProxyManager(config_path=self.config_path)
        self.usage_tracker = usage_tracker or UsageTracker(log_path=default_usage_log_path())
        self.stop_proxy_on_exit = stop_proxy_on_exit
        self._hydrate_runtime_providers()

    def _hydrate_runtime_providers(self) -> None:
        """Restore user-added providers into the in-memory registry after a restart."""
        try:
            from qz_providers.registry import get_provider_registry
            registry = get_provider_registry()
            for custom in self.keystore.list_custom_providers():
                registry.register_provider(
                    custom["provider_id"],
                    display_name=custom.get("display_name"),
                    base_url=custom.get("base_url"),
                    enabled=True,
                )
            known = {p.provider_id for p in registry.list_providers()}
            for entry in self.keystore.list_entries():
                if entry.provider not in known:
                    meta = self.keystore.get_custom_provider(entry.provider) or {}
                    registry.register_provider(
                        entry.provider,
                        display_name=meta.get("display_name"),
                        base_url=meta.get("base_url"),
                        enabled=True,
                    )
                    known.add(entry.provider)
        except Exception:
            pass

    # ---- first-run setup ----------------------------------------------------

    def first_run_setup(
        self,
        keys: dict[str, list[str]] | None = None,
        master_key: str | None = None,
        interactive: bool = False,
    ) -> dict:
        """Store the given keys (e.g. ``{"groq": ["k1", "k2"]}``) and the
        LiteLLM master key. When ``interactive`` is True and ``keys`` is
        None, prompt on stdin for every supported provider instead. A
        missing master key is generated automatically (never left unset)
        unless one is already stored."""
        if keys is None:
            keys = self._prompt_for_keys() if interactive else {}

        configured: dict[str, int] = {}
        for provider, values in keys.items():
            usable = [value.strip() for value in values if value and value.strip()]
            for position, value in enumerate(usable, start=1):
                self.keystore.set_key(provider, position, value, enabled=True)
            if usable:
                configured[provider.lower()] = len(usable)

        if master_key and master_key.strip():
            self.keystore.set_master_key(master_key.strip())
        elif interactive and keys is not None and not self.keystore.has_master_key():
            entered = getpass.getpass("LiteLLM master key (blank to auto-generate): ")
            self.keystore.set_master_key(entered.strip() or secrets.token_urlsafe(32))
        elif not self.keystore.has_master_key():
            self.keystore.set_master_key(secrets.token_urlsafe(32))

        return {
            "backend": self.keystore.backend_name(),
            "configured_providers": configured,
            "unconfigured_providers": [p for p in SUPPORTED_PROVIDERS if p not in configured],
            "master_key_set": self.keystore.has_master_key(),
        }

    def _prompt_for_keys(self) -> dict[str, list[str]]:
        print("Qazterion first-run setup — enter API keys for each provider.")
        print("Leave blank and press Enter to skip a provider. Multiple keys: comma-separated.\n")
        collected: dict[str, list[str]] = {}
        for provider in SUPPORTED_PROVIDERS:
            entered = getpass.getpass(f"{provider} key(s): ")
            if entered.strip():
                collected[provider] = [part.strip() for part in entered.split(",")]
        return collected

    # ---- configuration generation -----------------------------------------

    def generate_configuration(self) -> dict:
        """Regenerate `config.yaml`'s `model_list` from enabled keystore keys."""
        if not self.config_path.is_file():
            raise SetupError(f"Config template not found: {self.config_path}")
        with self.config_path.open(encoding="utf-8") as source:
            config = yaml.safe_load(source) or {}
        model_list = config.get("model_list")
        if not isinstance(model_list, list):
            raise SetupError(f"{self.config_path} must contain a model_list array.")

        groups = self.keystore.enabled_key_groups()
        expanded, untemplated = generate_config.expand_model_list(model_list, groups)

        warnings = [f"No model template for discovered keys: {family}" for family in sorted(untemplated)]
        custom_by_family = {
            str(meta.get("family") or f"{meta['provider_id'].upper()}_KEY"): meta
            for meta in self.keystore.list_custom_providers()
        }
        for family in sorted(untemplated):
            keys = groups.get(family) or []
            meta = custom_by_family.get(family, {})
            provider_id = str(meta.get("provider_id") or family.replace("_KEY", "").lower())
            default_model = str(meta.get("default_model") or "gpt-4o-mini")
            api_base = meta.get("base_url")
            for key_name in keys:
                params = {
                    "model": f"openai/{default_model}",
                    "api_key": f"os.environ/{key_name}",
                }
                if api_base:
                    params["api_base"] = api_base
                expanded.append({
                    "model_name": f"{provider_id}-default",
                    "litellm_params": params,
                })
            warnings = [w for w in warnings if family not in w]
        enabled_env_names = {name for names in groups.values() for name in names}
        kept = []
        for entry in expanded:
            api_key_ref = entry.get("litellm_params", {}).get("api_key", "")
            if api_key_ref.startswith("os.environ/"):
                env_name = api_key_ref.removeprefix("os.environ/")
                if env_name not in enabled_env_names:
                    warnings.append(
                        f"No enabled key for {env_name} — alias '{entry.get('model_name')}' "
                        f"was left out of config.yaml until a key is added and enabled."
                    )
                    continue
            kept.append(entry)

        config["model_list"] = kept
        with self.config_path.open("w", encoding="utf-8", newline="\n") as target:
            yaml.safe_dump(config, target, allow_unicode=True, sort_keys=False, default_flow_style=False)

        return {
            "model_entries": len(kept),
            "configured_families": sorted(groups),
            "warnings": sorted(set(warnings)),
        }

    def validate_configuration(self) -> dict:
        if not self.config_path.is_file():
            return {"valid": False, "errors": [f"Config file not found: {self.config_path}"]}
        try:
            with self.config_path.open(encoding="utf-8") as source:
                config = yaml.safe_load(source) or {}
        except yaml.YAMLError as error:
            return {"valid": False, "errors": [f"Invalid YAML: {error}"]}
        errors = []
        if not isinstance(config.get("model_list"), list) or not config["model_list"]:
            errors.append("model_list is missing or empty.")
        if not self.keystore.has_master_key():
            errors.append("No LiteLLM master key is configured.")
        return {"valid": not errors, "errors": errors}

    def set_routing_strategy(self, strategy: str) -> dict:
        """Persist a UI-selected LiteLLM routing strategy in the real config."""
        allowed = {"least-busy", "lowest-cost", "simple-shuffle"}
        if strategy not in allowed:
            raise SetupError(f"Unsupported routing strategy: {strategy}")
        with self.config_path.open(encoding="utf-8") as source:
            config = yaml.safe_load(source) or {}
        router = config.setdefault("router_settings", {})
        if not isinstance(router, dict):
            raise SetupError("router_settings must be an object.")
        router["routing_strategy"] = strategy
        with self.config_path.open("w", encoding="utf-8", newline="\n") as target:
            yaml.safe_dump(config, target, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return {"strategy": strategy}

    # ---- proxy lifecycle ----------------------------------------------------

    def apply_and_start(self, wait_for_health: float = 0.0) -> dict:
        """Regenerate configuration, validate it, then (re)start the proxy
        with exactly the environment variables it needs.

        Default wait_for_health is 0: spawn the proxy and return immediately
        so the desktop RPC loop is not blocked while LiteLLM boots.
        """
        generation = self.generate_configuration()
        validation = self.validate_configuration()
        if not validation["valid"]:
            raise SetupError("Configuration is invalid: " + "; ".join(validation["errors"]))
        proxy_status = self.proxy_manager.restart(
            env_overrides=self.keystore.enabled_env(), wait_for_health=wait_for_health
        ).to_dict()
        return {"generation": generation, "validation": validation, "proxy": proxy_status}

    def start(self, wait_for_health: float = 15.0) -> dict:
        return self.proxy_manager.start(
            env_overrides=self.keystore.enabled_env(), wait_for_health=wait_for_health
        ).to_dict()

    def stop(self) -> dict:
        return self.proxy_manager.stop().to_dict()

    def shutdown(self) -> None:
        if self.stop_proxy_on_exit:
            self.proxy_manager.stop()

    # ---- status for UI ------------------------------------------------------

    def _aliases_and_fallbacks(self) -> dict:
        if not self.config_path.is_file():
            return {"aliases": [], "fallback_order": {}}
        try:
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {"aliases": [], "fallback_order": {}}
        aliases = sorted({entry.get("model_name") for entry in config.get("model_list", []) if isinstance(entry, dict)})
        fallback_order = {}
        for item in (config.get("router_settings") or {}).get("fallbacks", []) or []:
            if isinstance(item, dict):
                fallback_order.update(item)
        return {"aliases": aliases, "fallback_order": fallback_order}

    def status(self) -> dict:
        proxy_status = self.proxy_manager.status().to_dict()
        config_info = self._aliases_and_fallbacks()
        return {
            "proxy": proxy_status,
            "configured_providers": [
                {"provider": e.provider, "env_name": e.env_name, "masked_value": e.masked_value, "enabled": e.enabled}
                for e in self.keystore.list_entries()
            ],
            "master_key_set": self.keystore.has_master_key(),
            "keystore_backend": self.keystore.backend_name(),
            "aliases": config_info["aliases"],
            "fallback_order": config_info["fallback_order"],
            "usage": self.usage_tracker.summary(),
            "last_error": proxy_status["last_error"],
        }

    # ---- Phase 12: Provider & Model Management ------------------------------

    def get_providers(self) -> list[dict]:
        """Return all registered providers with enablement, adapter info, and key counts."""
        from qz_providers.registry import get_provider_registry
        reg = get_provider_registry()
        entries = self.keystore.list_entries()
        key_counts: dict[str, int] = {}
        for e in entries:
            if e.enabled and e.masked_value != "(not set)":
                key_counts[e.provider] = key_counts.get(e.provider, 0) + 1

        results = []
        seen: set[str] = set()
        custom_meta = {m["provider_id"]: m for m in self.keystore.list_custom_providers()}
        for p in reg.list_providers():
            p_dict = p.to_dict()
            extra = custom_meta.get(p.provider_id, {})
            k_count = key_counts.get(p.provider_id, 0)
            p_dict["key_count"] = k_count
            p_dict["configured"] = bool(k_count > 0)
            p_dict["custom"] = p.provider_id in custom_meta
            if extra.get("default_model"):
                p_dict["default_model"] = extra["default_model"]
            if extra.get("base_url") and not p_dict.get("base_url"):
                p_dict["base_url"] = extra["base_url"]
            results.append(p_dict)
            seen.add(p.provider_id)
        for meta in custom_meta.values():
            if meta["provider_id"] in seen:
                continue
            k_count = key_counts.get(meta["provider_id"], 0)
            results.append({
                "provider_id": meta["provider_id"],
                "display_name": meta.get("display_name") or meta["provider_id"],
                "enabled": True,
                "adapter_type": "OpenAICompatibleAdapter",
                "base_url": meta.get("base_url"),
                "default_concurrency": 2,
                "supported_capabilities": ["chat", "streaming", "tool_calling"],
                "models": [meta["default_model"]] if meta.get("default_model") else [],
                "key_count": k_count,
                "configured": bool(k_count > 0),
                "custom": True,
                "default_model": meta.get("default_model"),
            })
        return results

    def set_provider_enabled(self, provider: str, enabled: bool) -> dict:
        """Enable or disable a provider in ProviderRegistry."""
        from qz_providers.registry import get_provider_registry
        reg = get_provider_registry()
        if enabled:
            reg.enable_provider(provider)
        else:
            reg.disable_provider(provider)
        return {"provider": provider.lower(), "enabled": enabled}

    def get_models(self, provider: str | None = None) -> list[dict]:
        """Return rich metadata for all models in ModelRegistry."""
        from qz_providers.model_registry import get_model_registry
        from qz_providers.registry import get_provider_registry
        reg = get_model_registry()
        prov_reg = get_provider_registry()
        entries = self.keystore.list_entries()
        configured_providers = {e.provider.lower() for e in entries if e.enabled and e.masked_value != "(not set)"}
        models = reg.list_models(provider=provider, include_historical=True)
        results = []
        for m in models:
            m_dict = m.to_dict()
            prov_id = m.provider.lower()
            is_conf = prov_id in configured_providers
            m_dict["is_configured"] = is_conf
            if not is_conf:
                m_dict["state"] = "unconfigured"
            results.append(m_dict)
        return results

    def refresh_models(self, provider: str | None = None, per_provider_timeout: float = 10.0) -> dict:
        """Trigger dynamic model discovery from providers using keystore credentials.

        Runs each provider's discovery concurrently and caps how long any one
        provider can take. Previously this looped over every enabled
        provider one at a time with a 10s network timeout each, so a single
        slow/unreachable provider could hold up "Discover Models" (and, on
        the old synchronous RPC loop, the entire app) for up to ~50s. Now
        the worst case for the whole call is bounded by
        `per_provider_timeout`, not `len(providers) * per_provider_timeout`.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
        from qz_providers.model_registry import get_model_registry
        from qz_providers.registry import get_provider_registry
        model_reg = get_model_registry()
        prov_reg = get_provider_registry()

        providers_to_refresh = [provider.lower()] if provider else [p.provider_id for p in prov_reg.list_providers(enabled_only=True)]
        discovered_summary: dict[str, int] = {}
        errors: dict[str, str] = {}

        runnable: dict[str, str] = {}
        for p_id in providers_to_refresh:
            key_val = self.keystore.get_key(p_id, 1)
            if not key_val or not key_val.strip():
                # Skip unconfigured providers to avoid network timeout hangs
                continue
            runnable[p_id] = key_val

        if runnable:
            with ThreadPoolExecutor(max_workers=min(8, len(runnable)), thread_name_prefix="qz-discover") as pool:
                futures = {
                    pool.submit(model_reg.discover_models_for_provider, p_id, api_key=key_val): p_id
                    for p_id, key_val in runnable.items()
                }
                for future, p_id in futures.items():
                    try:
                        discovered = future.result(timeout=per_provider_timeout)
                        discovered_summary[p_id] = len(discovered)
                    except FutureTimeoutError:
                        errors[p_id] = f"Discovery timed out after {per_provider_timeout:.0f}s."
                    except Exception as error:
                        errors[p_id] = str(error)

        result = {"refreshed": discovered_summary, "total_models": len(model_reg.list_models())}
        if errors:
            result["errors"] = errors
        return result
    
    def set_preferred_model(self, alias: str, provider: str, model_id: str) -> dict:
        """Update preferred model alias mapping."""
        from qz_providers.model_registry import get_model_registry
        get_model_registry().register_alias(alias, provider, model_id)
        return {"alias": alias, "provider": provider, "model_id": model_id}

    def add_provider_key(self, provider: str, index: int, value: str, enabled: bool = True) -> dict:
        """Add or update an API key in the keystore."""
        env_name = self.keystore.set_key(provider, index, value, enabled=enabled)
        try:
            from qz_pool import get_pool
            get_pool().registry.reload()
        except Exception:
            pass
        return {"provider": provider.lower(), "index": index, "env_name": env_name, "masked_value": mask_key(value)}

    def register_custom_provider(
        self,
        provider: str,
        value: str,
        display_name: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        index: int = 1,
        enabled: bool = True,
    ) -> dict:
        """Register a newly released / OpenAI-compatible provider and store its key."""
        meta = self.keystore.upsert_custom_provider(
            provider,
            display_name=display_name,
            base_url=base_url,
            default_model=default_model,
        )
        try:
            from qz_providers.registry import get_provider_registry
            get_provider_registry().register_provider(
                meta["provider_id"],
                display_name=meta.get("display_name"),
                base_url=meta.get("base_url"),
                enabled=True,
            )
        except Exception:
            pass
        key_info = self.add_provider_key(meta["provider_id"], index, value, enabled=enabled)
        try:
            from qz_providers.model_registry import get_model_registry
            from qz_providers.models import ModelMetadata
            model_id = str(meta.get("default_model") or "gpt-4o-mini")
            get_model_registry().register_model(
                ModelMetadata(
                    provider=meta["provider_id"],
                    model_id=model_id,
                    display_name=model_id,
                    capabilities=["chat", "streaming", "tool_calling"],
                    source="user_config",
                )
            )
        except Exception:
            pass
        generation = None
        try:
            generation = self.generate_configuration()
        except Exception as error:
            generation = {"warnings": [str(error)]}
        return {**key_info, "display_name": meta.get("display_name"), "base_url": meta.get("base_url"), "default_model": meta.get("default_model"), "generation": generation}

    def delete_provider_key(self, provider: str, index: int) -> dict:
        """Delete an API key from the keystore."""
        removed = self.keystore.delete_key(provider, index)
        try:
            from qz_pool import get_pool
            get_pool().registry.reload()
        except Exception:
            pass
        return {"provider": provider.lower(), "index": index, "removed": removed}

    def set_key_enabled(self, provider: str, index: int, enabled: bool) -> dict:
        """Set enabled status of an API key in the keystore."""
        self.keystore.set_enabled(provider, index, enabled)
        try:
            from qz_pool import get_pool
            get_pool().registry.reload()
        except Exception:
            pass
        return {"provider": provider.lower(), "index": index, "enabled": enabled}

    def test_key(self, provider: str, value: str, base_url: str | None = None) -> dict:
        """Test validity and connectivity of an API key against the provider."""
        from qz_providers.connectivity import test_provider_connectivity
        res = test_provider_connectivity(provider, value, base_url=base_url)
        return res.to_dict()

    def check_health(self, workspace: str | Path | None = None) -> dict:
        """Run a non-destructive runtime health audit."""
        from qz_health import check_system_health
        report = check_system_health(workspace=workspace, keystore=self.keystore)
        return report.to_dict()

    def intelligence_summary(self) -> dict:
        """Observed operational recommendations; no synthetic provider probes."""
        usage = self.usage_tracker.summary()
        models = usage.get("by_model", {})
        ranked = sorted(models.items(), key=lambda item: (
            item[1].get("success_rate", 0), -item[1].get("latency_ms", float("inf"))
        ), reverse=True)
        fastest = min(models.items(), key=lambda item: item[1].get("latency_ms", float("inf")), default=(None, {}))
        degraded = [model.to_dict() for model in __import__("qz_providers.model_registry", fromlist=["get_model_registry"]).get_model_registry().list_models(include_historical=True)
                    if model.state.value in {"degraded", "unavailable"}]
        return {
            "evidence": "observed",
            "best_overall": ranked[0][0] if ranked else None,
            "fastest": fastest[0],
            "degraded_models": degraded,
            "pool": usage.get("global_pool", {}),
            "providers": usage.get("by_provider", {}),
            "models": models,
            "keys": usage.get("by_key", {}),
            "quota_note": "Quota values are provider-confirmed only when a provider response supplies them; all other health is observed.",
        }


# ---- CLI ------------------------------------------------------------------


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cli_main(argv: list[str] | None = None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    from qz_proxy_manager import RUN_LITELLM_PROXY_FLAG, run_embedded_litellm_proxy
    effective_argv = list(argv if argv is not None else sys.argv[1:])
    if RUN_LITELLM_PROXY_FLAG in effective_argv:
        return run_embedded_litellm_proxy(effective_argv)

    parser = argparse.ArgumentParser(prog="qazterion-setup", description="Qazterion Phase 12 desktop backend CLI.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.yaml (default: config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Interactive first-run API key setup.")
    sub.add_parser("status", help="Print proxy/provider/usage status as JSON.")
    sub.add_parser("config", help="Regenerate config.yaml from enabled keys only.")
    sub.add_parser("apply", help="Regenerate config.yaml and (re)start the proxy.")
    sub.add_parser("start", help="Start the proxy if it is not already running.")
    sub.add_parser("stop", help="Stop the proxy.")
    sub.add_parser("restart", help="Restart the proxy.")
    args = parser.parse_args(argv)

    backend = DesktopBackend(config_path=args.config)
    try:
        if args.command == "setup":
            _print_json(backend.first_run_setup(interactive=True))
        elif args.command == "status":
            _print_json(backend.status())
        elif args.command == "config":
            _print_json(backend.generate_configuration())
        elif args.command == "apply":
            _print_json(backend.apply_and_start())
        elif args.command == "start":
            _print_json(backend.start())
        elif args.command == "stop":
            _print_json(backend.stop())
        elif args.command == "restart":
            _print_json(backend.proxy_manager.restart(env_overrides=backend.keystore.enabled_env()).to_dict())
    except SetupError as error:
        print(f"Setup error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
