"""Registry for discovering and indexing physical API keys from the keystore."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from qz_keystore import KeyStore, KeyEntry, SUPPORTED_PROVIDERS
from qz_pool.models import APIKey, Provider

KEY_ENV_NAME_RE = re.compile(r"^(?:os\.environ/)?([A-Z][A-Z0-9_]*_\d+)$")


class KeyRegistry:
    """Discovers physical API keys configured in the keystore and maps aliases to keys."""

    def __init__(
        self,
        keystore: KeyStore | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self._keystore = keystore
        self._config_path = Path(config_path) if config_path else Path("config.yaml")
        self._keys: dict[str, APIKey] = {}                       # key_id -> APIKey
        self._keys_by_provider: dict[str, list[str]] = {}        # provider -> [key_id]
        self._alias_to_provider: dict[str, str] = {}             # alias -> provider
        self._alias_to_keys: dict[str, list[str]] = {}           # alias -> [key_id]
        self._providers: dict[str, Provider] = {}
        self.reload()

    def reload(self) -> None:
        """Reload physical keys from the keystore and alias mappings from config."""
        self._keys.clear()
        self._keys_by_provider.clear()
        self._alias_to_provider.clear()
        self._alias_to_keys.clear()
        self._providers.clear()

        # Initialize known supported providers
        for p in SUPPORTED_PROVIDERS:
            self._providers[p] = Provider(name=p, display_name=p.capitalize())

        # 1. Discover physical keys from the keystore
        if self._keystore is not None:
            entries = self._keystore.list_entries()
        else:
            try:
                ks = KeyStore()
                entries = ks.list_entries()
            except Exception:
                entries = []

        for entry in entries:
            key_id = entry.env_name
            api_key = APIKey(
                id=key_id,
                provider=entry.provider.lower(),
                key_reference=entry.env_name,
                label=f"{entry.provider.capitalize()} Key #{entry.index}",
                enabled=entry.enabled,
            )
            self._keys[key_id] = api_key
            self._keys_by_provider.setdefault(api_key.provider, []).append(key_id)

        # 2. Parse config.yaml for alias -> provider / key mappings
        self._load_config_mappings()

    def _load_config_mappings(self) -> None:
        if not self._config_path.is_file():
            return
        try:
            with open(self._config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            model_list = data.get("model_list", [])
            for item in model_list:
                if not isinstance(item, dict):
                    continue
                alias = item.get("model_name")
                params = item.get("litellm_params", {})
                model_str = str(params.get("model", ""))
                api_key_ref = str(params.get("api_key", ""))

                provider = model_str.split("/")[0].lower() if "/" in model_str else None
                if alias and provider:
                    self._alias_to_provider[alias] = provider

                match = KEY_ENV_NAME_RE.match(api_key_ref)
                if match and alias:
                    key_id = match.group(1)
                    if key_id not in self._alias_to_keys.get(alias, []):
                        self._alias_to_keys.setdefault(alias, []).append(key_id)
        except Exception:
            pass

    def register_key(self, api_key: APIKey) -> None:
        """Register or update an APIKey record."""
        self._keys[api_key.id] = api_key
        if api_key.id not in self._keys_by_provider.get(api_key.provider, []):
            self._keys_by_provider.setdefault(api_key.provider, []).append(api_key.id)

    def disable_key(self, key_id: str) -> bool:
        """Disable a key in memory."""
        if key_id in self._keys:
            self._keys[key_id].enabled = False
            return True
        return False

    def enable_key(self, key_id: str) -> bool:
        """Enable a key in memory."""
        if key_id in self._keys:
            self._keys[key_id].enabled = True
            return True
        return False

    def get_key(self, key_id: str) -> APIKey | None:
        return self._keys.get(key_id)

    def get_keys_by_provider(self, provider: str) -> list[APIKey]:
        key_ids = self._keys_by_provider.get(provider.lower(), [])
        return [self._keys[kid] for kid in key_ids if kid in self._keys]

    def get_all_keys(self) -> list[APIKey]:
        return list(self._keys.values())

    def get_provider_for_alias(self, alias: str) -> str | None:
        if alias in self._alias_to_provider:
            return self._alias_to_provider[alias]
        try:
            from qz_providers.model_registry import DEFAULT_ALIAS_MAP
            if alias.lower() in DEFAULT_ALIAS_MAP:
                return DEFAULT_ALIAS_MAP[alias.lower()][0]
        except ImportError:
            pass
        if alias.lower() in self._keys_by_provider or alias.lower() in self._providers:
            return alias.lower()
        return None

    def get_keys_for_alias(self, alias: str) -> list[APIKey]:
        """Return physical keys mapped to an alias, falling back to or combining with provider keys."""
        provider = self.get_provider_for_alias(alias)
        provider_keys = self.get_keys_by_provider(provider) if provider else []
        key_ids = self._alias_to_keys.get(alias, [])
        alias_keys = [self._keys[kid] for kid in key_ids if kid in self._keys]

        combined: list[APIKey] = []
        seen: set[str] = set()
        for k in (alias_keys + provider_keys):
            if k.id not in seen:
                seen.add(k.id)
                combined.append(k)
        return combined
