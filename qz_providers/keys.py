"""Discover API keys/accounts per provider from the keystore and environment.

A provider can have any number of keys. Keys are identified by
``<key_prefix>_<N>`` (e.g. ``GEMINI_KEY_2``); the prefix comes from the provider
catalog, so adding a provider never requires code changes here.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from qz_providers.catalog import ProviderCatalog


@dataclass(frozen=True)
class ApiKey:
    key_id: str
    provider: str
    index: int
    source: str          # "keystore" | "env"
    enabled: bool
    fingerprint: str     # short hash of the secret; changes when the key is replaced
    masked: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "provider": self.provider,
            "index": self.index,
            "source": self.source,
            "enabled": self.enabled,
            "masked_value": self.masked,
        }


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def mask(secret: str) -> str:
    if not secret:
        return "(not set)"
    return "*" * 6 + secret[-4:] if len(secret) > 4 else "*" * len(secret)


class KeySource:
    """Read-through view over keystore + environment keys with a short cache."""

    def __init__(
        self,
        catalog: ProviderCatalog,
        keystore_factory: Callable[[], Any] | None = None,
        environ: Mapping[str, str] | None = None,
        cache_ttl_s: float = 10.0,
    ) -> None:
        self._catalog = catalog
        self._keystore_factory = keystore_factory if keystore_factory is not None else _default_keystore
        self._environ = environ
        self._ttl = cache_ttl_s
        self._lock = threading.RLock()
        self._cache: dict[str, list[ApiKey]] | None = None
        self._secrets: dict[str, str] = {}
        self._loaded_at = 0.0

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _env(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    def _load(self) -> dict[str, list[ApiKey]]:
        with self._lock:
            if self._cache is not None and (time.monotonic() - self._loaded_at) < self._ttl:
                return self._cache
            by_provider: dict[str, dict[str, ApiKey]] = {}
            secrets: dict[str, str] = {}

            # 1. Keystore entries (encrypted at rest).
            store = None
            try:
                store = self._keystore_factory() if self._keystore_factory else None
            except Exception:
                store = None
            if store is not None:
                try:
                    entries = store.list_entries()
                except Exception:
                    entries = []
                for entry in entries:
                    try:
                        secret = store.get_key(entry.provider, entry.index) or ""
                    except Exception:
                        secret = ""
                    if not secret.strip():
                        continue
                    key = ApiKey(
                        key_id=entry.env_name,
                        provider=entry.provider.lower(),
                        index=int(entry.index),
                        source="keystore",
                        enabled=bool(entry.enabled),
                        fingerprint=fingerprint(secret),
                        masked=mask(secret),
                    )
                    by_provider.setdefault(key.provider, {})[key.key_id] = key
                    secrets[key.key_id] = secret.strip()

            # 2. Environment variables (.env or shell); keystore wins on clashes.
            env = self._env()
            for provider in self._catalog.providers.values():
                pattern = re.compile(rf"^{re.escape(provider.key_prefix)}_(\d+)$")
                found = by_provider.setdefault(provider.provider_id, {})
                candidates = [(name, int(m.group(1))) for name in env for m in [pattern.match(name)] if m]
                alt = f"{provider.provider_id.upper().replace('-', '_')}_API_KEY"
                if alt in env:
                    candidates.append((alt, 0))
                for name, index in candidates:
                    secret = (env.get(name) or "").strip()
                    if not secret or name in found:
                        continue
                    found[name] = ApiKey(
                        key_id=name,
                        provider=provider.provider_id,
                        index=index,
                        source="env",
                        enabled=True,
                        fingerprint=fingerprint(secret),
                        masked=mask(secret),
                    )
                    secrets[name] = secret

            self._cache = {
                provider: sorted(keys.values(), key=lambda k: (k.index, k.key_id))
                for provider, keys in by_provider.items()
            }
            self._secrets = secrets
            self._loaded_at = time.monotonic()
            return self._cache

    def keys_for(self, provider_id: str, *, include_disabled: bool = False) -> list[ApiKey]:
        keys = self._load().get(str(provider_id).lower(), [])
        return [k for k in keys if include_disabled or k.enabled]

    def all_keys(self) -> list[ApiKey]:
        return [key for keys in self._load().values() for key in keys]

    def secret(self, key_id: str) -> str | None:
        self._load()
        with self._lock:
            return self._secrets.get(key_id)


def _default_keystore() -> Any:
    from qz_keystore import KeyStore

    return KeyStore()
