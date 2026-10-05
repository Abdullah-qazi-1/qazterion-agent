"""Encrypted local storage for provider API keys.

Design goals:
- Keys are never written to disk in plaintext and are never printed in full.
- On Windows, keys are protected with DPAPI (`CryptProtectData` /
  `CryptUnprotectData`) via `ctypes`, so no extra OS-level dependency is
  required beyond the standard library.
- On non-Windows platforms (used for local development and this project's
  automated tests, since DPAPI does not exist there) a Fernet-encrypted file
  fallback is used instead. This is clearly reported by `backend_name()` so a
  desktop build can warn if it ever runs on a non-Windows host.
- The store lives outside the project directory (a per-user config folder),
  so it is never at risk of being committed to Git, and is additionally
  covered by `.gitignore` as a defence in depth.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

# Built-in key families. A family is the env-var prefix of a provider's keys
# (GROQ_KEY_1, GROQ_KEY_2, ...). Providers added to providers.yaml register
# their own family (its ``key_prefix``) on first use.
PROVIDER_FAMILIES: dict[str, str] = {
    "groq": "GROQ_KEY",
    "gemini": "GEMINI_KEY",
    "mistral": "MISTRAL_KEY",
    "openrouter": "OPENROUTER_KEY",
    "deepseek": "DEEPSEEK_KEY",
}
SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(PROVIDER_FAMILIES)


def register_provider_family(provider: str, family: str | None = None) -> str:
    """Dynamically register a new provider family in the keystore."""
    global SUPPORTED_PROVIDERS
    p_norm = provider.lower().strip()
    f_norm = family.upper().strip() if family else f"{p_norm.upper()}_KEY"
    PROVIDER_FAMILIES[p_norm] = f_norm
    SUPPORTED_PROVIDERS = tuple(PROVIDER_FAMILIES)
    return f_norm


def mask_key(value: str | None) -> str:
    """Return a display-safe form of a secret. Never reveals full length or
    full content: a fixed number of stars plus the last 4 characters."""
    if not value:
        return "(not set)"
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * 6 + value[-4:]


class UnsupportedProviderError(ValueError):
    """Raised for a provider family Qazterion's desktop setup does not know."""


@dataclass(frozen=True)
class KeyEntry:
    provider: str          # e.g. "groq"
    family: str             # e.g. "GROQ_KEY"
    index: int               # e.g. 1
    env_name: str            # e.g. "GROQ_KEY_1"
    masked_value: str
    enabled: bool


def _default_store_path() -> Path:
    override = os.environ.get("QAZTERION_KEYSTORE_PATH")
    if override:
        return Path(override)
    from qz_paths import data_dir

    primary = data_dir() / "keystore.dat"
    if primary.is_file() or os.environ.get("QAZTERION_DATA_DIR") or sys.platform != "win32":
        return primary
    # Older desktop builds kept their keystore under %APPDATA%.
    appdata_base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    legacy = Path(appdata_base) / "qazterion-desktop" / "backend-data" / "keystore.dat"
    return legacy if legacy.is_file() else primary


class _DpapiCipher:
    """Windows DPAPI encryption, current-user scope, via ctypes only."""

    name = "dpapi"

    def __init__(self) -> None:
        import ctypes
        import ctypes.wintypes as wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._DATA_BLOB = DATA_BLOB

    def _blob(self, data: bytes):
        buf = self._ctypes.create_string_buffer(data, len(data))
        return self._DATA_BLOB(len(data), self._ctypes.cast(buf, self._ctypes.POINTER(self._ctypes.c_char)))

    def encrypt(self, plaintext: bytes) -> bytes:
        blob_in = self._blob(plaintext)
        blob_out = self._DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            self._ctypes.byref(blob_in), None, None, None, None, 0, self._ctypes.byref(blob_out)
        )
        if not ok:
            raise RuntimeError("DPAPI CryptProtectData failed")
        try:
            return self._ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)

    def decrypt(self, ciphertext: bytes) -> bytes:
        blob_in = self._blob(ciphertext)
        blob_out = self._DATA_BLOB()
        ok = self._crypt32.CryptUnprotectData(
            self._ctypes.byref(blob_in), None, None, None, None, 0, self._ctypes.byref(blob_out)
        )
        if not ok:
            raise RuntimeError("DPAPI CryptUnprotectData failed")
        try:
            return self._ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)


class _FernetCipher:
    """Fallback used on non-Windows hosts (dev machines, CI, this test
    suite). Backed by a locally generated key file created with owner-only
    permissions the first time it is needed."""

    name = "fernet-fallback"

    def __init__(self, key_file: Path) -> None:
        from cryptography.fernet import Fernet

        key_file.parent.mkdir(parents=True, exist_ok=True)
        if key_file.is_file():
            secret = key_file.read_bytes()
        else:
            secret = Fernet.generate_key()
            key_file.write_bytes(secret)
            if os.name != "nt":
                os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
        self._fernet = Fernet(secret)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)


class KeyStore:
    """Encrypted-at-rest storage for provider API keys and the LiteLLM
    master key. Values only ever exist as plaintext in memory."""

    def __init__(self, path: str | os.PathLike[str] | None = None, backend: str | None = None):
        self.path = Path(path) if path is not None else _default_store_path()
        chosen = backend or ("dpapi" if sys.platform == "win32" else "fernet")
        if chosen == "dpapi":
            self._cipher = _DpapiCipher()
        elif chosen == "fernet":
            self._cipher = _FernetCipher(self.path.with_suffix(".keyfile"))
        else:
            raise ValueError(f"Unknown keystore backend: {backend!r}")
        self._plain_cache: dict[str, str] = {}
        self._data: dict = self._load_raw()
        self._hydrate_provider_families()

    def _hydrate_provider_families(self) -> None:
        """Re-register custom families so a new process can read keys saved
        for providers that were added after the built-in list was shipped."""
        for meta in self._data.get("custom_providers", {}).values():
            if isinstance(meta, dict) and meta.get("provider_id"):
                register_provider_family(str(meta["provider_id"]), meta.get("family"))
        for entry in self._data.get("entries", {}).values():
            if not isinstance(entry, dict):
                continue
            provider = entry.get("provider")
            family = entry.get("family")
            if provider and family and str(provider).lower() not in PROVIDER_FAMILIES:
                register_provider_family(str(provider), str(family))

    def _reload(self) -> None:
        """Reload from disk if file exists to ensure multi-process and multi-query consistency."""
        try:
            self._data = self._load_raw()
            self._hydrate_provider_families()
        except Exception as e:
            sys.stderr.write(f"[KeyStore _reload error] {e}\n")

    # ---- persistence -----------------------------------------------------

    def backend_name(self) -> str:
        return self._cipher.name

    def _load_raw(self) -> dict:
        empty = {"entries": {}, "master_key": None, "custom_providers": {}}
        if not self.path.is_file():
            return empty
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return empty
        if not isinstance(loaded, dict):
            return empty
        loaded.setdefault("entries", {})
        loaded.setdefault("master_key", None)
        loaded.setdefault("custom_providers", {})
        return loaded

    def _save(self) -> None:
        from qz_paths import atomic_write_text

        # Atomic replace: a crash mid-write never leaves a truncated keystore.
        atomic_write_text(self.path, json.dumps(self._data, indent=2))
        if os.name != "nt":
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def _encrypt_str(self, value: str) -> str:
        return base64.b64encode(self._cipher.encrypt(value.encode("utf-8"))).decode("ascii")

    def _decrypt_str(self, blob: str) -> str:
        cached = self._plain_cache.get(blob)
        if cached is None:
            cached = self._cipher.decrypt(base64.b64decode(blob)).decode("utf-8")
            self._plain_cache[blob] = cached
        return cached

    # ---- provider keys -----------------------------------------------------

    @staticmethod
    def _family_for(provider: str) -> str:
        """Key family (env-var prefix) for a provider: built-in, registered, or from the catalog."""
        pid = str(provider or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", pid):
            raise UnsupportedProviderError(f"Invalid provider id '{provider}'.")
        family = PROVIDER_FAMILIES.get(pid)
        if family:
            return family
        try:
            from qz_providers.catalog import ProviderCatalog

            spec = ProviderCatalog().provider(pid)
        except Exception:
            spec = None
        if spec is None:
            raise UnsupportedProviderError(
                f"Unknown provider '{provider}'. Supported providers: {', '.join(SUPPORTED_PROVIDERS)}. "
                "Add new providers to providers.yaml or register them as a custom provider first."
            )
        return register_provider_family(pid, spec.key_prefix)

    def set_key(self, provider: str, index: int, value: str, enabled: bool = True) -> str:
        """Store (or replace) one numbered key for a provider. Returns the
        `<FAMILY>_<N>` env var name it will be exposed as."""
        if not value or not value.strip():
            raise ValueError("A key value is required.")
        if index < 1:
            raise ValueError("Key index must be 1 or greater.")
        self._reload()
        family = self._family_for(provider)
        env_name = f"{family}_{index}"
        self._data.setdefault("entries", {})[env_name] = {
            "provider": provider.lower(),
            "family": family,
            "index": index,
            "blob": self._encrypt_str(value.strip()),
            "masked": mask_key(value.strip()),
            "enabled": bool(enabled),
        }
        self._save()
        return env_name

    def delete_key(self, provider: str, index: int) -> bool:
        self._reload()
        family = self._family_for(provider)
        env_name = f"{family}_{index}"
        removed = self._data.get("entries", {}).pop(env_name, None) is not None
        if removed:
            self._save()
        return removed

    def set_enabled(self, provider: str, index: int, enabled: bool) -> None:
        self._reload()
        family = self._family_for(provider)
        env_name = f"{family}_{index}"
        entry = self._data.get("entries", {}).get(env_name)
        if entry is None:
            raise KeyError(f"No stored key {env_name!r}.")
        entry["enabled"] = bool(enabled)
        self._save()

    def get_key(self, provider: str, index: int) -> str | None:
        self._reload()
        family = self._family_for(provider)
        entry = self._data.get("entries", {}).get(f"{family}_{index}")
        if not entry:
            return None
        try:
            return self._decrypt_str(entry["blob"])
        except Exception:
            return None

    def list_entries(self, provider: str | None = None) -> list[KeyEntry]:
        self._reload()
        results = []
        for env_name, entry in sorted(self._data.get("entries", {}).items()):
            if not isinstance(entry, dict) or not entry.get("provider"):
                continue
            if provider and entry["provider"] != provider.lower():
                continue
            masked = entry.get("masked")
            if not masked:
                # Entries written by older versions have no stored mask.
                try:
                    decrypted = self._decrypt_str(entry["blob"])
                except Exception:
                    decrypted = ""
                if not decrypted.strip():
                    continue
                masked = mask_key(decrypted)
            results.append(
                KeyEntry(
                    provider=entry["provider"],
                    family=entry.get("family") or self._family_for(entry["provider"]),
                    index=int(entry.get("index") or 1),
                    env_name=env_name,
                    masked_value=masked,
                    enabled=bool(entry.get("enabled", True)),
                )
            )
        return results

    def configured_providers(self) -> list[str]:
        return sorted({entry.provider for entry in self.list_entries() if entry.enabled})

    # ---- custom / newly-released providers --------------------------------

    def upsert_custom_provider(
        self,
        provider: str,
        display_name: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        family: str | None = None,
    ) -> dict:
        """Persist metadata for a user-added OpenAI-compatible provider."""
        p_id = provider.lower().strip()
        if not p_id:
            raise ValueError("Provider id is required.")
        self._reload()
        family_name = register_provider_family(p_id, family)
        record = {
            "provider_id": p_id,
            "display_name": (display_name or p_id).strip() or p_id.capitalize(),
            "base_url": (base_url or "").strip() or None,
            "default_model": (default_model or "").strip() or "gpt-4o-mini",
            "family": family_name,
        }
        self._data.setdefault("custom_providers", {})[p_id] = record
        self._save()
        return dict(record)

    def list_custom_providers(self) -> list[dict]:
        result = []
        for meta in self._data.get("custom_providers", {}).values():
            if isinstance(meta, dict) and meta.get("provider_id"):
                result.append(dict(meta))
        return sorted(result, key=lambda item: str(item.get("display_name") or item["provider_id"]))

    def get_custom_provider(self, provider: str) -> dict | None:
        meta = self._data.get("custom_providers", {}).get(provider.lower().strip())
        return dict(meta) if isinstance(meta, dict) else None