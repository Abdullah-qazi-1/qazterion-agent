
"""Unit and integration tests for qz_router: scoring, SmartRouter, fallbacks, and executor escalation."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qz_validation.pipeline import ValidationReport
from qz_validation.checks import CheckResult, CheckStatus

from qz_keystore import KeyEntry, KeyStore
from qz_pool.concurrency import ConcurrencyLimiter
from qz_pool.health_manager import HealthManager
from qz_pool.models import KeyHealth
from qz_pool.pool import LLMPool, reset_pool
from qz_pool.registry import KeyRegistry
from qz_router.scoring import (
    CAPABILITY_MATCH_WEIGHT,
    HEALTH_WEIGHT,
    LATENCY_WEIGHT,
    CONCURRENCY_WEIGHT,
    calculate_route_score,
    get_capability_fit,
)
from qz_router.router import SmartRouter, reset_router
from qz_core.executor import run_executor


class ScoringTests(unittest.TestCase):
    def test_scoring_weights_named_constants_sum_to_one(self):
        total_weight = (
            CAPABILITY_MATCH_WEIGHT
            + HEALTH_WEIGHT
            + LATENCY_WEIGHT
            + CONCURRENCY_WEIGHT
        )
        self.assertAlmostEqual(total_weight, 1.0)

    def test_capability_fit_returns_exact_and_fallback_scores(self):
        self.assertEqual(get_capability_fit("simple", "groq-fast"), 1.0)
        self.assertEqual(get_capability_fit("simple", "coder-backup"), 0.85)
        self.assertEqual(get_capability_fit("complex", "coder-strong"), 1.0)
        self.assertEqual(get_capability_fit("complex", "reasoner"), 0.90)
        self.assertEqual(get_capability_fit("reasoner", "reasoner"), 1.0)

    def test_score_calculation_prefers_zero_error_low_latency_low_load(self):
        healthy = KeyHealth(
            key_id="K1",
            success_count=10,
            error_count=0,
            average_latency_ms=100.0,
        )
        score_healthy = calculate_route_score(
            "simple",
            "groq-fast",
            "K1",
            healthy,
            current_load_fraction=0.0,
        )

        unhealthy = KeyHealth(
            key_id="K2",
            success_count=5,
            error_count=5,
            average_latency_ms=100.0,
        )
        score_unhealthy = calculate_route_score(
            "simple",
            "groq-fast",
            "K2",
            unhealthy,
            current_load_fraction=0.0,
        )

        self.assertGreater(score_healthy, score_unhealthy)

    def test_score_calculation_penalizes_high_concurrency_load(self):
        idle = KeyHealth(
            key_id="K1",
            success_count=10,
            error_count=0,
            average_latency_ms=100.0,
        )
        score_idle = calculate_route_score(
            "complex",
            "coder-strong",
            "K1",
            idle,
            current_load_fraction=0.0,
        )

        busy = KeyHealth(
            key_id="K2",
            success_count=10,
            error_count=0,
            average_latency_ms=100.0,
        )
        score_busy = calculate_route_score(
            "complex",
            "coder-strong",
            "K2",
            busy,
            current_load_fraction=1.0,
        )

        self.assertGreater(score_idle, score_busy)


class SmartRouterTests(unittest.TestCase):
    def setUp(self):
        reset_pool()
        reset_router()

        self.mock_keystore = MagicMock(spec=KeyStore)
        self.mock_keystore.list_entries.return_value = [
            KeyEntry(
                provider="groq",
                family="GROQ_KEY",
                index=1,
                env_name="GROQ_KEY_1",
                masked_value="******1111",
                enabled=True,
            ),
            KeyEntry(
                provider="groq",
                family="GROQ_KEY",
                index=2,
                env_name="GROQ_KEY_2",
                masked_value="******2222",
                enabled=True,
            ),
            KeyEntry(
                provider="mistral",
                family="MISTRAL_KEY",
                index=1,
                env_name="MISTRAL_KEY_1",
                masked_value="******3333",
                enabled=True,
            ),
        ]

        self.registry = KeyRegistry(
            keystore=self.mock_keystore,
            config_path="nonexistent.yaml",
        )

        self.registry._alias_to_keys["groq-fast"] = [
            "GROQ_KEY_1",
            "GROQ_KEY_2",
        ]
        self.registry._alias_to_keys["coder-strong"] = ["MISTRAL_KEY_1"]

        self.health_mgr = HealthManager(
            registry=self.registry,
            base_cooldown_seconds=30.0,
        )
        self.concurrency = ConcurrencyLimiter(default_max_concurrency=2)
        self.pool = LLMPool(
            registry=self.registry,
            health_manager=self.health_mgr,
            concurrency=self.concurrency,
        )
        self.router = SmartRouter(pool=self.pool)

    def tearDown(self):
        reset_pool()
        reset_router()

    def test_select_route_picks_healthy_low_load_key_over_unhealthy(self):
        for _ in range(5):
            self.health_mgr.record_failure(
                "GROQ_KEY_1",
                error_type="server_error",
                latency_ms=2500.0,
            )

        for _ in range(5):
            self.health_mgr.record_success(
                "GROQ_KEY_1",
                latency_ms=2500.0,
            )

        for _ in range(10):
            self.health_mgr.record_success(
                "GROQ_KEY_2",
                latency_ms=120.0,
            )

        best_alias, best_key = self.router.select_route("simple")

        self.assertEqual(best_alias, "groq-fast")
        self.assertEqual(best_key, "GROQ_KEY_2")

    def test_key_in_cooldown_or_disabled_is_excluded(self):
        self.health_mgr.record_failure(
            "GROQ_KEY_1",
            error_type="rate_limit",
            error_message="429 Rate limit",
        )
        self.assertFalse(self.health_mgr.is_available("GROQ_KEY_1"))

        self.health_mgr.record_success("GROQ_KEY_2", latency_ms=100.0)

        best_alias, best_key = self.router.select_route("simple")
        self.assertEqual(best_key, "GROQ_KEY_2")

        self.registry.disable_key("GROQ_KEY_2")
        self.assertFalse(self.health_mgr.is_available("GROQ_KEY_2"))

        next_alias, next_key = self.router.select_route("simple")
        self.assertIn(
            next_alias,
            ("coder-backup", "coder-strong", "reasoner"),
        )

    def test_key_with_no_free_concurrency_slot_is_excluded(self):
        self.concurrency.acquire("GROQ_KEY_1")
        self.concurrency.acquire("GROQ_KEY_1")

        self.assertFalse(
            self.concurrency.can_acquire("GROQ_KEY_1")
        )

        self.assertTrue(
            self.concurrency.can_acquire("GROQ_KEY_2")
        )

        best_alias, best_key = self.router.select_route("simple")
        self.assertEqual(best_key, "GROQ_KEY_2")

    def test_all_candidates_unavailable_falls_back_to_default_and_logs(self):
        self.registry.disable_key("GROQ_KEY_1")
        self.registry.disable_key("GROQ_KEY_2")
        self.registry.disable_key("MISTRAL_KEY_1")

        with patch("qz_router.router._persist_event") as mock_persist:
            best_alias, best_key = self.router.select_route(
                "simple",
                task_id="test-task-123",
            )

        self.assertEqual(best_alias, "groq-fast")
        self.assertTrue(mock_persist.called)

        call_args = mock_persist.call_args[0]
        self.assertEqual(call_args[0], "test-task-123")
        self.assertEqual(call_args[1], "ROUTER_FALLBACK")
        self.assertEqual(
            call_args[2]["reason"],
            "all_candidates_unavailable",
        )


class ExecutorEscalationIntegrationTests(unittest.TestCase):
    def test_three_consecutive_failures_escalates_to_reasoner_capability(self):
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content="Working on task",
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                function=SimpleNamespace(
                                    name="run_command",
                                    arguments='{"command": "python -m pytest"}',
                                ),
                            )
                        ],
                        model_dump=lambda exclude_none=True: {
                            "role": "assistant",
                            "content": "Working on task",
                        },
                    )
                )
            ]
        )

        select_route_calls = []

        def mock_select_route(capability, **kwargs):
            select_route_calls.append(capability)

            if capability == "reasoner":
                return "reasoner", "MISTRAL_KEY_1"

            return "groq-fast", "GROQ_KEY_1"

        failing_validation = ValidationReport(
            status="FAIL",
            checks={
                "tests": CheckResult(
                    name="tests",
                    status=CheckStatus.FAIL,
                    summary="Simulated test failure",
                    is_required=True,
                )
            },
            summary="Validation Gate: FAIL",
            required_checks={"tests"},
            advisory_checks={
                "lint",
                "typecheck",
                "build",
                "security",
                "requirements",
            },
        )

        mock_validation_pipeline = MagicMock()
        mock_validation_pipeline.run.return_value = failing_validation

        with (
            patch(
                "qz_agent.classify_task_complexity",
                return_value="simple",
                create=True,
            ),
            patch(
                "qz_agent.select_route",
                side_effect=mock_select_route,
                create=True,
            ),
            patch(
                "qz_agent.request_completion",
                return_value=(mock_response, "groq-fast"),
                create=True,
            ),
            patch(
                "qz_core.executor.classify_task_complexity",
                return_value="simple",
            ),
            patch(
                "qz_core.executor.select_route",
                side_effect=mock_select_route,
            ),
            patch(
                "qz_core.executor.request_completion",
                return_value=(mock_response, "groq-fast"),
            ),
            patch(
                "qz_core.dag_executor.executor_mod.ValidationPipeline",
                return_value=mock_validation_pipeline,
            ),
            patch.dict(
                "qz_core.dag_executor.TOOL_FUNCTIONS",
                {
                    "run_command": lambda **kwargs: (
                        "exit_code=1\n"
                        "STDOUT:\n"
                        "FAILED test_math.py\n"
                    )
                },
            ),
            patch(
                "qz_core.executor.self_review",
                return_value="",
            ),
            patch(
                "qz_core.executor._task_requires_test_changes",
                return_value=False,
            ),
        ):
            result = run_executor(
                task="Fix bug in math",
                plan="1. Fix bug",
                architecture="src/math.py",
                max_iterations=4,
                task_id="task-escalate-test",
            )

        self.assertIn("simple", select_route_calls)
        self.assertIn("reasoner", select_route_calls)
        self.assertEqual(select_route_calls[0], "simple")
        self.assertEqual(select_route_calls[1], "reasoner")


if __name__ == "__main__":
    unittest.main()
