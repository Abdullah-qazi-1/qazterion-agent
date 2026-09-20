"""Unit tests for Phase 2:
- Router-selected physical API key passed to transport
- Concurrency limiter slot acquisition
- Actual model attribution
- Rate limit immediate failover without redundant retries
- Non-tool model capability filtering
- Request ID propagation and telemetry correlation
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import qz_agent
from qz_core.executor import request_completion
from qz_pool.concurrency import ConcurrencyLimiter
from qz_pool.models import APIKey
from qz_pool.pool import LLMPool
from qz_providers.adapters.openrouter import OpenRouterAdapter
from qz_providers.models import ModelCapability
from qz_usage_tracker import UsageTracker


def make_completion(content="done", model="actual-provider-model-v1"):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
    )


class Phase2RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tracker = UsageTracker(log_path=None, cooldown_seconds=60)

    def test_router_selected_key_passed_to_transport(self):
        with patch("qz_core.executor.get_pool") as mock_get_pool, \
             patch("qz_core.executor.get_router") as mock_get_router, \
             patch.object(qz_agent.client.chat.completions, "create", return_value=make_completion()) as mock_create:

            mock_pool = MagicMock()
            mock_pool.registry.get_key_value.return_value = "gsk_selected_secret_key_2"
            mock_pool.concurrency = ConcurrencyLimiter()
            mock_get_pool.return_value = mock_pool

            mock_router = MagicMock()
            mock_router.rank_routes.return_value = [("groq-fast", "GROQ_KEY_2", 0.95)]
            mock_get_router.return_value = mock_router

            resp, active = request_completion(
                model="groq-fast",
                messages=[{"role": "user", "content": "hello"}],
                usage_tracker=self.tracker,
            )

            mock_create.assert_called_once()
            called_kwargs = mock_create.call_args.kwargs
            self.assertEqual(called_kwargs.get("api_key"), "gsk_selected_secret_key_2")
            mock_pool.registry.get_key_value.assert_called_once_with("GROQ_KEY_2")

    def test_actual_model_attribution_recorded_in_telemetry(self):
        with patch.object(
            qz_agent.client.chat.completions,
            "create",
            return_value=make_completion(content="ok", model="groq/llama-3.3-70b-versatile"),
        ):
            _, active = request_completion(
                model="groq-fast",
                messages=[],
                usage_tracker=self.tracker,
                task_id="test-attrib-task",
            )
            task_usage = self.tracker.get_task_usage("test-attrib-task")
            self.assertIn("groq/llama-3.3-70b-versatile", task_usage["models_used"])

    def test_rate_limit_breaks_immediate_retry_and_fails_over(self):
        with patch.object(
            qz_agent.client.chat.completions,
            "create",
            side_effect=[RuntimeError("429 rate limit"), make_completion()],
        ) as mock_create:
            _, active = request_completion(
                model="groq-fast",
                messages=[],
                fallbacks=("groq-fast", "coder-backup"),
                usage_tracker=self.tracker,
            )
            # groq-fast should only be called once, then immediately fail over to coder-backup
            self.assertEqual(mock_create.call_count, 2)
            self.assertEqual(active, "coder-backup")

    def test_openrouter_deepseek_r1_metadata_has_no_tools(self):
        adapter = OpenRouterAdapter()
        models = {m.model_id: m for m in adapter.get_static_models()}
        r1 = models.get("deepseek/deepseek-r1")
        self.assertIsNotNone(r1)
        self.assertFalse(r1.supports_tools)
        self.assertNotIn("tool_calling", r1.capabilities)

    def test_concurrency_limiter_acquired_during_request(self):
        limiter = ConcurrencyLimiter(default_max_concurrency=1)
        with patch("qz_core.executor.get_pool") as mock_get_pool, \
             patch("qz_core.executor.get_router") as mock_get_router:

            mock_pool = MagicMock()
            mock_pool.registry.get_key_value.return_value = "dummy_key"
            mock_pool.concurrency = limiter
            mock_get_pool.return_value = mock_pool

            mock_router = MagicMock()
            mock_router.rank_routes.return_value = [("groq-fast", "KEY_1", 0.95)]
            mock_get_router.return_value = mock_router

            def check_in_flight(**kwargs):
                self.assertEqual(limiter.get_active_count("KEY_1"), 1)
                return make_completion()

            with patch.object(qz_agent.client.chat.completions, "create", side_effect=check_in_flight):
                request_completion(
                    model="groq-fast",
                    messages=[],
                    usage_tracker=self.tracker,
                )
            self.assertEqual(limiter.get_active_count("KEY_1"), 0)


if __name__ == "__main__":
    unittest.main()
