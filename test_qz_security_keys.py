"""Tests for Phase 12 Secure API Key Management, Masking, Keystore Integration, and Secret Redaction."""

import os
import pytest
from pathlib import Path

from qz_keystore import KeyStore, mask_key, register_provider_family
from qz_providers.health import HealthTracker
from qz_security.redaction import redact
from qz_security.secret_scanner import scan


class TestSecureKeyManagement:
    def test_mask_key_never_reveals_plaintext(self):
        assert mask_key("gsk_1234567890abcdef123456") == "******23456"[-10:] or mask_key("gsk_1234567890abcdef123456").startswith("******")
        assert "gsk_1234567890abcdef" not in mask_key("gsk_1234567890abcdef123456")
        assert mask_key(None) == "(not set)"
        assert mask_key("") == "(not set)"
        assert mask_key("abc") == "***"

    def test_keystore_multi_key_and_dynamic_providers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ks_path = Path(td) / "keystore.dat"
            ks = KeyStore(path=ks_path, backend="fernet")

            # Add 2 Groq keys and 1 DeepSeek key
            ks.set_key("groq", 1, "gsk_live_secret_key_number_1")
            ks.set_key("groq", 2, "gsk_live_secret_key_number_2")
            ks.set_key("deepseek", 1, "sk_deepseek_secret_key_1")

            entries = ks.list_entries()
            assert len(entries) == 3

            # Keys returned by list_entries are always masked
            for e in entries:
                assert "live_secret" not in e.masked_value
                assert "deepseek_secret" not in e.masked_value

            # Dynamic custom provider family registration
            family = register_provider_family("cohere", "COHERE_KEY")
            assert family == "COHERE_KEY"
            ks.set_key("cohere", 1, "cohere_secret_key_value")
            cohere_entries = ks.list_entries(provider="cohere")
            assert len(cohere_entries) == 1
            assert cohere_entries[0].env_name == "COHERE_KEY_1"

    def test_physical_key_isolation(self):
        health = HealthTracker(persist=False)
        health.record_failure("GROQ_KEY_1", "groq/m", "rate_limit", "429")
        # Key 1 cools down; key 2 of the same provider stays fully available.
        assert not health.key_available("GROQ_KEY_1")
        assert health.key_available("GROQ_KEY_2")
        assert health.key_state("GROQ_KEY_1").failures == 1
        assert health.key_state("GROQ_KEY_2") is None or health.key_state("GROQ_KEY_2").failures == 0

    def test_keystore_writes_are_atomic_and_listing_does_not_need_secrets(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ks = KeyStore(path=Path(td) / "keystore.dat", backend="fernet")
            ks.set_key("groq", 1, "gsk_live_secret_key_number_1")
            # A second instance (another process) adds a key; the first must not lose it.
            other = KeyStore(path=Path(td) / "keystore.dat", backend="fernet")
            other.set_key("gemini", 1, "AIza_second_process_key")
            ks.set_key("groq", 2, "gsk_live_secret_key_number_2")
            names = sorted(e.env_name for e in KeyStore(path=Path(td) / "keystore.dat", backend="fernet").list_entries())
            assert names == ["GEMINI_KEY_1", "GROQ_KEY_1", "GROQ_KEY_2"]
            assert not list(Path(td).glob("*.tmp"))
            raw = (Path(td) / "keystore.dat").read_text(encoding="utf-8")
            assert "gsk_live_secret" not in raw and "AIza_second" not in raw

    def test_keystore_accepts_providers_defined_in_the_catalog_only(self):
        import tempfile
        from qz_keystore import UnsupportedProviderError
        with tempfile.TemporaryDirectory() as td:
            ks = KeyStore(path=Path(td) / "keystore.dat", backend="fernet")
            with pytest.raises(UnsupportedProviderError):
                ks.set_key("no-such-provider", 1, "x" * 20)
            with pytest.raises(UnsupportedProviderError):
                ks.set_key("../evil", 1, "x" * 20)

    def test_secret_scanner_and_redaction(self):
        raw_prompt = "Using apiKey: gsk_abcdef12345678901234567890 to call model"
        redacted = redact(raw_prompt)
        assert "gsk_" not in redacted
        assert "[REDACTED:" in redacted

        # OpenAI style key
        raw_openai = "Bearer sk-1234567890abcdef1234567890"
        redacted_openai = redact(raw_openai)
        assert "sk-1234567890" not in redacted_openai
        assert "[REDACTED:" in redacted_openai
