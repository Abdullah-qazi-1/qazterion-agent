"""Tests for Phase 12 Secure API Key Management, Masking, Keystore Integration, and Secret Redaction."""

import os
import pytest
from pathlib import Path

from qz_keystore import KeyStore, mask_key, register_provider_family
from qz_pool import LLMPool, reset_pool
from qz_pool.models import APIKey
from qz_security.redaction import redact
from qz_security.secret_scanner import scan


class TestSecureKeyManagement:
    def setup_method(self, tmp_path=None):
        reset_pool()

    def teardown_method(self):
        reset_pool()

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
        pool = LLMPool()
        k1 = APIKey(id="GROQ_KEY_1", provider="groq", key_reference="GROQ_KEY_1", label="Key 1")
        k2 = APIKey(id="GROQ_KEY_2", provider="groq", key_reference="GROQ_KEY_2", label="Key 2")
        pool.registry.register_key(k1)
        pool.registry.register_key(k2)

        # Record failure on Key 1
        pool.health_manager.record_failure("GROQ_KEY_1", error_type="rate_limit", latency_ms=150.0)

        # Key 1 is in cooldown, Key 2 remains completely healthy and available
        assert not pool.health_manager.is_available("GROQ_KEY_1")
        assert pool.health_manager.is_available("GROQ_KEY_2")

        h1 = pool.health_manager.get_health("GROQ_KEY_1")
        h2 = pool.health_manager.get_health("GROQ_KEY_2")
        assert h1.error_count == 1
        assert h2.error_count == 0

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
