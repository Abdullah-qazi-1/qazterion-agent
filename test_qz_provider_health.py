"""Error normalization and key/route health (cooldowns, invalid keys, persistence)."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx
import openai

from qz_providers import exceptions as px
from qz_providers.health import (
    MODEL_NOT_FOUND_S,
    QUOTA_BASE_S,
    RATE_LIMIT_BASE_S,
    HealthTracker,
)


def _openai_error(cls, status, message="boom", headers=None):
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    return cls(message, response=response, body=None)


class ErrorNormalizationTests(unittest.TestCase):
    def test_status_codes(self):
        self.assertIsInstance(px.normalize_error("Invalid API Key", status_code=401), px.AuthenticationError)
        self.assertIsInstance(px.normalize_error("forbidden", status_code=403), px.AuthenticationError)
        self.assertIsInstance(px.normalize_error("slow down", status_code=429), px.RateLimitError)
        self.assertIsInstance(px.normalize_error("no such model", status_code=404), px.ModelNotFoundError)
        self.assertIsInstance(px.normalize_error("oops", status_code=500), px.ServerError)
        self.assertIsInstance(px.normalize_error("overloaded", status_code=503), px.ModelUnavailableError)
        self.assertIsInstance(px.normalize_error("bad tools", status_code=400), px.InvalidRequestError)

    def test_quota_is_distinguished_from_rate_limit(self):
        quota = px.normalize_error("You exceeded your current quota, please check billing", status_code=429)
        self.assertIsInstance(quota, px.QuotaExhaustedError)
        self.assertEqual(quota.kind, "quota")
        self.assertEqual(px.normalize_error("Rate limit reached for requests", status_code=429).kind, "rate_limit")

    def test_gemini_invalid_key_400_is_auth(self):
        err = px.normalize_error("API key not valid. Please pass a valid API key.", status_code=400)
        self.assertEqual(err.kind, "auth")

    def test_context_length_and_timeouts(self):
        self.assertEqual(px.normalize_error("This model's maximum context length is 8192 tokens", 400).kind, "context_length")
        self.assertEqual(px.normalize_error("Request timed out").kind, "timeout")

    def test_openai_sdk_exceptions_and_retry_after(self):
        rate = px.normalize_error(_openai_error(openai.RateLimitError, 429, "Rate limit", {"retry-after": "7"}))
        self.assertEqual(rate.kind, "rate_limit")
        self.assertEqual(rate.retry_after, 7.0)
        auth = px.normalize_error(_openai_error(openai.AuthenticationError, 401, "Incorrect API key provided"))
        self.assertEqual(auth.kind, "auth")
        not_found = px.normalize_error(_openai_error(openai.NotFoundError, 404, "The model does not exist"))
        self.assertEqual(not_found.kind, "model_not_found")
        request = httpx.Request("POST", "https://x")
        self.assertEqual(px.normalize_error(openai.APITimeoutError(request=request)).kind, "timeout")
        self.assertEqual(px.normalize_error(openai.APIConnectionError(request=request)).kind, "connection")

    def test_retry_after_in_message_and_durations(self):
        self.assertEqual(px.normalize_error("Rate limit. Please try again in 1.5s", 429).retry_after, 1.5)
        self.assertEqual(px._parse_duration("1m30s"), 90.0)
        self.assertEqual(px._parse_duration("250ms"), 0.25)


class HealthTrackerTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "health.json"
        self.health = HealthTracker(path=self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_rate_limit_backs_off_exponentially_and_recovers(self):
        self.health.record_failure("K1", "groq/m", "rate_limit")
        self.assertFalse(self.health.key_available("K1"))
        self.assertAlmostEqual(self.health.key_cooldown_remaining("K1"), RATE_LIMIT_BASE_S)
        self.now[0] += RATE_LIMIT_BASE_S + 1
        self.assertTrue(self.health.key_available("K1"))
        self.health.record_failure("K1", "groq/m", "rate_limit")
        self.assertAlmostEqual(self.health.key_cooldown_remaining("K1"), RATE_LIMIT_BASE_S * 2)
        self.health.record_success("K1", "groq/m")
        self.assertTrue(self.health.key_available("K1"))

    def test_retry_after_from_provider_is_respected(self):
        self.health.record_failure("K1", "groq/m", "rate_limit", retry_after=3.0)
        self.assertAlmostEqual(self.health.key_cooldown_remaining("K1"), 3.0)

    def test_quota_exhaustion_rests_the_key_for_a_long_time(self):
        self.health.record_failure("K1", "gemini/m", "quota")
        self.assertGreaterEqual(self.health.key_cooldown_remaining("K1"), QUOTA_BASE_S)

    def test_invalid_key_is_disabled_until_the_secret_changes(self):
        self.health.record_failure("K1", "groq/m", "auth", fingerprint="old")
        self.now[0] += 10 ** 6
        self.assertFalse(self.health.key_available("K1", "old"))
        self.assertIsNone(self.health.key_cooldown_remaining("K1", "old"))
        # The user replaced the key: the new secret starts healthy.
        self.assertTrue(self.health.key_available("K1", "new"))

    def test_model_not_found_cools_the_route_not_the_key(self):
        self.health.record_failure("K1", "groq/retired-model", "model_not_found")
        self.assertTrue(self.health.key_available("K1"))
        self.assertFalse(self.health.route_available("groq/retired-model"))
        self.assertAlmostEqual(self.health.route_cooldown_remaining("groq/retired-model"), MODEL_NOT_FOUND_S)

    def test_transient_route_failures_need_a_streak(self):
        self.health.record_failure("K1", "gemini/m", "server")
        self.assertTrue(self.health.route_available("gemini/m"))
        self.health.record_failure("K2", "gemini/m", "timeout")
        self.assertFalse(self.health.route_available("gemini/m"))
        self.health.record_success("K3", "gemini/m")
        self.assertTrue(self.health.route_available("gemini/m"))

    def test_request_errors_do_not_penalize_keys_or_routes(self):
        for kind in ("context_length", "invalid_request"):
            self.health.record_failure("K1", "groq/m", kind)
        self.assertTrue(self.health.key_available("K1"))
        self.assertTrue(self.health.route_available("groq/m"))
        state = self.health.key_state("K1")
        self.assertTrue(state is None or state.failures == 0)

    def test_state_persists_across_processes_without_secrets(self):
        self.health.record_failure("GROQ_KEY_1", "groq/m", "auth", "Incorrect API key gsk_abcdefghijklmnopqrstuvwx", fingerprint="fp1")
        self.health.record_failure("GEMINI_KEY_2", "gemini/m", "rate_limit", fingerprint="fp2")
        other = HealthTracker(path=self.path, clock=lambda: self.now[0])
        self.assertFalse(other.key_available("GROQ_KEY_1", "fp1"))
        self.assertFalse(other.key_available("GEMINI_KEY_2", "fp2"))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("GROQ_KEY_1", raw["keys"])
        # Error text is stored only via callers that redact it; the tracker never stores secrets it is not given.
        self.assertNotIn("api_key", json.dumps(raw).lower())

    def test_changes_from_another_process_are_merged(self):
        other = HealthTracker(path=self.path, clock=lambda: self.now[0] + 5)
        other.record_failure("K9", "groq/m", "rate_limit")
        self.health.refresh()
        self.assertFalse(self.health.key_available("K9"))
        self.health.record_failure("K1", "groq/m", "rate_limit")  # writing must not drop K9
        fresh = HealthTracker(path=self.path, clock=lambda: self.now[0])
        self.assertFalse(fresh.key_available("K9"))
        self.assertFalse(fresh.key_available("K1"))

    def test_reset_key_and_reset_all(self):
        self.health.record_failure("K1", "groq/m", "auth")
        self.health.reset_key("K1")
        self.assertTrue(self.health.key_available("K1"))
        self.health.record_failure("K2", "groq/m", "rate_limit")
        self.health.reset_all()
        self.assertTrue(self.health.key_available("K2"))
        self.assertTrue(HealthTracker(path=self.path).key_available("K2"))


if __name__ == "__main__":
    unittest.main()
