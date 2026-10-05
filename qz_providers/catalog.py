"""Configuration-driven provider/model/role catalog.

Loads the built-in ``default_providers.yaml`` and merges the user's
``<data dir>/providers.yaml`` over it. The catalog only describes *what exists*
(providers, models, roles, settings); keys live in :mod:`qz_providers.keys` and
runtime health in :mod:`qz_providers.health`.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from qz_paths import atomic_write_text, data_dir

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "default_providers.yaml"
USER_CATALOG_FILENAME = "providers.yaml"

KEY_STRATEGIES = ("balanced", "priority")
_STRATEGY_ALIASES = {
    # Names used by the older desktop UI (LiteLLM routing strategies).
    "simple-shuffle": "balanced",
    "least-busy": "balanced",
    "lowest-cost": "priority",
}


class CatalogError(ValueError):
    """The provider configuration is malformed."""


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model_id: str
    context_window: int = 32768
    tools: bool = True
    display_name: str = ""

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.model_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "ref": self.ref,
            "display_name": self.display_name or self.model_id,
            "context_window": self.context_window,
            "supports_tools": self.tools,
        }


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    adapter: str
    base_url: str | None
    key_prefix: str
    enabled: bool = True
    custom: bool = False
    models: dict[str, ModelSpec] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "adapter_type": self.adapter,
            "base_url": self.base_url,
            "key_prefix": self.key_prefix,
            "enabled": self.enabled,
            "custom": self.custom,
            "models": sorted(self.models),
        }


@dataclass(frozen=True)
class CatalogSettings:
    key_strategy: str = "balanced"
    request_timeout_s: float = 60.0
    max_attempts: int = 8
    max_wait_for_cooldown_s: float = 30.0


def parse_model_ref(ref: str) -> tuple[str, str]:
    """Split ``provider/model`` (the model id itself may contain ``/``)."""
    text = str(ref).strip()
    if "/" not in text:
        raise CatalogError(f"Model reference '{ref}' must look like '<provider>/<model>'.")
    provider, model = text.split("/", 1)
    if not provider or not model:
        raise CatalogError(f"Model reference '{ref}' must look like '<provider>/<model>'.")
    return provider.strip().lower(), model.strip()


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise CatalogError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(loaded, dict):
        raise CatalogError(f"{path} must contain a mapping at the top level.")
    return loaded


class ProviderCatalog:
    """Merged view of built-in and user provider configuration."""

    def __init__(self, default_path: Path | None = None, user_path: Path | None = None) -> None:
        self.default_path = Path(default_path) if default_path else DEFAULT_CATALOG_PATH
        self.user_path = Path(user_path) if user_path else data_dir() / USER_CATALOG_FILENAME
        self._lock = threading.RLock()
        self._user_raw: dict = {}
        self.reload()

    # ---- loading ----------------------------------------------------------

    def reload(self) -> None:
        with self._lock:
            defaults = _load_yaml(self.default_path)
            self._user_raw = _load_yaml(self.user_path)
            self._build(_deep_merge(defaults, self._user_raw))

    def _build(self, raw: dict) -> None:
        settings_raw = raw.get("settings") or {}
        strategy = normalize_strategy(str(settings_raw.get("key_strategy", "balanced")))
        try:
            self.settings = CatalogSettings(
                key_strategy=strategy,
                request_timeout_s=float(settings_raw.get("request_timeout_s", 60)),
                max_attempts=max(1, int(settings_raw.get("max_attempts", 8))),
                max_wait_for_cooldown_s=max(0.0, float(settings_raw.get("max_wait_for_cooldown_s", 30))),
            )
        except (TypeError, ValueError) as error:
            raise CatalogError(f"Invalid settings block: {error}") from error

        providers: dict[str, ProviderSpec] = {}
        for provider_id, spec in (raw.get("providers") or {}).items():
            if not isinstance(spec, dict):
                raise CatalogError(f"Provider '{provider_id}' must be a mapping.")
            pid = str(provider_id).strip().lower()
            models: dict[str, ModelSpec] = {}
            for model_id, model_raw in (spec.get("models") or {}).items():
                model_raw = model_raw if isinstance(model_raw, dict) else {}
                try:
                    context = int(model_raw.get("context_window", 32768))
                except (TypeError, ValueError) as error:
                    raise CatalogError(f"Model '{pid}/{model_id}' has an invalid context_window.") from error
                models[str(model_id)] = ModelSpec(
                    provider=pid,
                    model_id=str(model_id),
                    context_window=context,
                    tools=bool(model_raw.get("tools", True)),
                    display_name=str(model_raw.get("display_name") or ""),
                )
            prefix = str(spec.get("key_prefix") or f"{pid.upper().replace('-', '_')}_KEY").upper()
            providers[pid] = ProviderSpec(
                provider_id=pid,
                display_name=str(spec.get("display_name") or pid.capitalize()),
                adapter=str(spec.get("adapter") or "openai_compatible"),
                base_url=(str(spec["base_url"]) if spec.get("base_url") else None),
                key_prefix=prefix,
                enabled=bool(spec.get("enabled", True)),
                custom=bool(spec.get("custom", False)),
                models=models,
            )
        self.providers = providers

        roles: dict[str, tuple[str, ...]] = {}
        for role, refs in (raw.get("roles") or {}).items():
            if isinstance(refs, str):
                refs = [refs]
            if not isinstance(refs, list):
                raise CatalogError(f"Role '{role}' must be a list of '<provider>/<model>' references.")
            for ref in refs:
                parse_model_ref(ref)
            roles[str(role).lower()] = tuple(str(ref) for ref in refs)
        self.roles = roles
        self.role_aliases = {
            str(alias).lower(): str(target).lower()
            for alias, target in (raw.get("role_aliases") or {}).items()
        }

    # ---- queries ----------------------------------------------------------

    def resolve_role(self, name: str) -> str | None:
        key = str(name).strip().lower()
        if key in self.roles:
            return key
        target = self.role_aliases.get(key)
        return target if target in self.roles else None

    def candidates(self, role_or_ref: str) -> list[ModelSpec]:
        """Ordered model candidates for a role name or an explicit ``provider/model``."""
        role = self.resolve_role(role_or_ref)
        refs = self.roles[role] if role else (role_or_ref,)
        result: list[ModelSpec] = []
        for ref in refs:
            try:
                provider_id, model_id = parse_model_ref(ref)
            except CatalogError:
                continue
            provider = self.providers.get(provider_id)
            if provider is None:
                continue
            model = provider.models.get(model_id) or ModelSpec(provider=provider_id, model_id=model_id)
            result.append(model)
        return result

    def provider(self, provider_id: str) -> ProviderSpec | None:
        return self.providers.get(str(provider_id).lower())

    def all_models(self, provider_id: str | None = None) -> list[ModelSpec]:
        providers = [self.provider(provider_id)] if provider_id else list(self.providers.values())
        return [model for spec in providers if spec for model in spec.models.values()]

    # ---- user overrides (persisted to <data dir>/providers.yaml) ----------

    def _update_user(self, mutate) -> None:
        with self._lock:
            user = copy.deepcopy(self._user_raw)
            mutate(user)
            atomic_write_text(self.user_path, yaml.safe_dump(user, sort_keys=False, allow_unicode=True))
            self.reload()

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> None:
        pid = provider_id.lower().strip()
        if pid not in self.providers:
            raise CatalogError(f"Unknown provider '{provider_id}'.")
        self._update_user(lambda user: user.setdefault("providers", {}).setdefault(pid, {}).update(enabled=bool(enabled)))

    def set_key_strategy(self, strategy: str) -> str:
        normalized = normalize_strategy(strategy)
        self._update_user(lambda user: user.setdefault("settings", {}).update(key_strategy=normalized))
        return normalized

    def set_role_preference(self, role: str, provider_id: str, model_id: str) -> None:
        """Move ``provider/model`` to the front of a role's candidate list."""
        name = self.resolve_role(role) or role.strip().lower()
        if not name:
            raise CatalogError("A role name is required.")
        pid = provider_id.lower().strip()
        if pid not in self.providers:
            raise CatalogError(f"Unknown provider '{provider_id}'.")
        ref = f"{pid}/{model_id.strip()}"
        current = [r for r in self.roles.get(name, ()) if r != ref]

        def mutate(user: dict) -> None:
            user.setdefault("roles", {})[name] = [ref, *current]
            if model_id.strip() not in self.providers[pid].models:
                user.setdefault("providers", {}).setdefault(pid, {}).setdefault("models", {})[model_id.strip()] = {}

        self._update_user(mutate)

    def add_models(self, provider_id: str, models: dict[str, dict]) -> None:
        pid = provider_id.lower().strip()

        def mutate(user: dict) -> None:
            target = user.setdefault("providers", {}).setdefault(pid, {}).setdefault("models", {})
            for model_id, meta in models.items():
                target.setdefault(model_id, meta or {})

        self._update_user(mutate)

    def upsert_custom_provider(
        self,
        provider_id: str,
        *,
        base_url: str,
        display_name: str | None = None,
        default_model: str | None = None,
        key_prefix: str | None = None,
    ) -> ProviderSpec:
        pid = provider_id.lower().strip()
        if not pid or "/" in pid:
            raise CatalogError("Provider id must be non-empty and must not contain '/'.")
        if not base_url:
            raise CatalogError("A custom provider needs an OpenAI-compatible base_url.")
        model = (default_model or "").strip()

        def mutate(user: dict) -> None:
            entry = user.setdefault("providers", {}).setdefault(pid, {})
            entry.update(
                display_name=display_name or pid.capitalize(),
                adapter="openai_compatible",
                base_url=base_url,
                key_prefix=(key_prefix or f"{pid.upper().replace('-', '_')}_KEY"),
                custom=True,
                enabled=True,
            )
            if model:
                entry.setdefault("models", {}).setdefault(model, {})
                # Make the new provider usable immediately as a coding fallback.
                roles = user.setdefault("roles", {})
                for role in ("coder", "fast"):
                    refs = list(roles.get(role) or self.roles.get(role, ()))
                    ref = f"{pid}/{model}"
                    if ref not in refs:
                        refs.append(ref)
                    roles[role] = refs

        self._update_user(mutate)
        return self.providers[pid]


def normalize_strategy(strategy: str) -> str:
    value = str(strategy or "").strip().lower()
    value = _STRATEGY_ALIASES.get(value, value)
    if value not in KEY_STRATEGIES:
        raise CatalogError(f"Unsupported key strategy '{strategy}'. Use one of: {', '.join(KEY_STRATEGIES)}.")
    return value
