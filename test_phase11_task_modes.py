"""Tests for Phase 11: task modes, plan approval, and clarification.

These cover qz_agent.classify_task_mode / requires_plan_approval /
generate_clarifying_questions / prompt_plan_approval / resolve_plan, without
touching a real LiteLLM proxy — every model call is mocked.
"""
import unittest
from unittest.mock import patch, MagicMock

import qz_agent


def _fake_completion(content: str):
    """Build a minimal object shaped like an OpenAI chat completion response."""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


class ClassifyTaskModeTests(unittest.TestCase):
    def setUp(self):
        qz_agent.FORCED_TASK_MODE = None

    def tearDown(self):
        qz_agent.FORCED_TASK_MODE = None

    def test_forced_mode_short_circuits_classification(self):
        qz_agent.FORCED_TASK_MODE = "complex"
        with patch.object(qz_agent.client.chat.completions, "create") as create:
            mode = qz_agent.classify_task_mode("Create a basic calculator")
            create.assert_not_called()
            self.assertEqual(mode, "complex")

    def test_llm_classification_is_used_when_available(self):
        with patch.object(
            qz_agent.client.chat.completions, "create",
            return_value=_fake_completion("standard"),
        ):
            self.assertEqual(qz_agent.classify_task_mode("Add validation to login"), "standard")

    def test_falls_back_to_heuristic_on_provider_failure(self):
        with patch.object(
            qz_agent.client.chat.completions, "create",
            side_effect=RuntimeError("proxy down"),
        ):
            self.assertEqual(qz_agent.classify_task_mode("Create a basic calculator"), "quick")
            self.assertEqual(
                qz_agent.classify_task_mode("Refactor authentication and migrate the schema"),
                "complex",
            )


class RequiresPlanApprovalTests(unittest.TestCase):
    def test_never_setting_never_asks(self):
        for mode in qz_agent.TASK_MODES:
            self.assertFalse(qz_agent.requires_plan_approval(mode, "never"))

    def test_quick_mode_never_asks_regardless_of_setting(self):
        for setting in qz_agent._VALID_APPROVAL_SETTINGS:
            self.assertFalse(qz_agent.requires_plan_approval("quick", setting))

    def test_complex_setting_default_only_pauses_for_complex_tasks(self):
        self.assertFalse(qz_agent.requires_plan_approval("standard", "complex"))
        self.assertTrue(qz_agent.requires_plan_approval("complex", "complex"))

    def test_always_setting_pauses_for_standard_and_complex(self):
        self.assertTrue(qz_agent.requires_plan_approval("standard", "always"))
        self.assertTrue(qz_agent.requires_plan_approval("complex", "always"))

    def test_auto_setting_currently_pauses_only_for_complex(self):
        self.assertFalse(qz_agent.requires_plan_approval("standard", "auto"))
        self.assertTrue(qz_agent.requires_plan_approval("complex", "auto"))


class ResolvePlanTests(unittest.TestCase):
    def test_quick_task_bypasses_visible_plan_entirely(self):
        with patch("qz_agent.call_planner") as planner, \
             patch("qz_agent.call_architect") as architect, \
             patch("qz_agent.generate_clarifying_questions") as clarify, \
             patch("qz_agent.prompt_plan_approval") as approval:
            outcome = qz_agent.resolve_plan("Create a basic calculator", "idx", "complex", "quick")

            planner.assert_not_called()
            architect.assert_not_called()
            clarify.assert_not_called()
            approval.assert_not_called()
            self.assertEqual(outcome["status"], "ready")
            self.assertEqual(outcome["mode"], "quick")

    def test_complex_task_pauses_for_approval_by_default(self):
        with patch("qz_agent.call_planner", return_value="1. Do the thing"), \
             patch("qz_agent.call_architect", return_value="tree"), \
             patch("qz_agent.generate_clarifying_questions", return_value=[]), \
             patch("qz_agent.prompt_plan_approval", return_value=("approved", "1. Do the thing", "tree")) as approval:
            outcome = qz_agent.resolve_plan("Refactor auth and migrate schema", "idx", "complex", "complex")

            approval.assert_called_once()
            self.assertEqual(outcome["status"], "ready")
            self.assertEqual(outcome["decision"], "approved")

    def test_standard_task_does_not_pause_under_default_setting(self):
        with patch("qz_agent.call_planner", return_value="plan text"), \
             patch("qz_agent.call_architect", return_value="tree"), \
             patch("qz_agent.generate_clarifying_questions", return_value=[]), \
             patch("qz_agent.prompt_plan_approval") as approval:
            outcome = qz_agent.resolve_plan("Add endpoint validation", "idx", "complex", "standard")

            approval.assert_not_called()
            self.assertEqual(outcome["status"], "ready")
            self.assertIsNone(outcome["decision"])

    def test_standard_task_pauses_when_setting_is_always(self):
        with patch("qz_agent.call_planner", return_value="plan text"), \
             patch("qz_agent.call_architect", return_value="tree"), \
             patch("qz_agent.generate_clarifying_questions", return_value=[]), \
             patch("qz_agent.prompt_plan_approval", return_value=("approved", "plan text", "tree")) as approval:
            outcome = qz_agent.resolve_plan("Add endpoint validation", "idx", "always", "standard")

            approval.assert_called_once()
            self.assertEqual(outcome["status"], "ready")

    def test_rejected_plan_stops_before_execution(self):
        with patch("qz_agent.call_planner", return_value="plan text"), \
             patch("qz_agent.call_architect", return_value="tree"), \
             patch("qz_agent.generate_clarifying_questions", return_value=[]), \
             patch("qz_agent.prompt_plan_approval", return_value=("rejected", "plan text", "tree")):
            outcome = qz_agent.resolve_plan("Refactor auth and migrate schema", "idx", "complex", "complex")

            self.assertEqual(outcome["status"], "rejected")

    def test_edited_plan_replaces_the_plan_text_used_later(self):
        edited_text = "1. Do it this other way instead"
        with patch("qz_agent.call_planner", return_value="original plan"), \
             patch("qz_agent.call_architect", return_value="tree"), \
             patch("qz_agent.generate_clarifying_questions", return_value=[]), \
             patch("qz_agent.prompt_plan_approval", return_value=("edited", edited_text, "tree")):
            outcome = qz_agent.resolve_plan("Refactor auth and migrate schema", "idx", "complex", "complex")

            self.assertEqual(outcome["status"], "ready")
            self.assertEqual(outcome["plan"], edited_text)
            self.assertEqual(outcome["decision"], "edited")

    def test_proceed_with_best_judgment_only_prompts_once(self):
        with patch("qz_agent.call_planner", return_value="plan text"), \
             patch("qz_agent.call_architect", return_value="tree"), \
             patch("qz_agent.generate_clarifying_questions", return_value=[]), \
             patch("qz_agent.prompt_plan_approval",
                   return_value=("proceed_best_judgment", "plan text", "tree")) as approval:
            outcome = qz_agent.resolve_plan("Refactor auth and migrate schema", "idx", "complex", "complex")

            approval.assert_called_once()
            self.assertEqual(outcome["status"], "ready")
            self.assertEqual(outcome["decision"], "proceed_best_judgment")

    def test_ambiguous_task_folds_answers_into_task_text_used_for_planning(self):
        questions = ["Should deleted notes be soft-deleted or removed permanently?"]
        qa_pairs = [(questions[0], "Soft-delete them")]
        with patch("qz_agent.call_planner", return_value="plan text") as planner, \
             patch("qz_agent.call_architect", return_value="tree"), \
             patch("qz_agent.generate_clarifying_questions", return_value=questions), \
             patch("qz_agent.ask_clarifying_questions", return_value=qa_pairs) as ask, \
             patch("qz_agent.prompt_plan_approval", return_value=("approved", "plan text", "tree")):
            outcome = qz_agent.resolve_plan("Add a delete-note endpoint", "idx", "complex", "complex")

            ask.assert_called_once_with(questions)
            # The planner must see the resolved answer, not just the raw task.
            planner_task_arg = planner.call_args[0][0]
            self.assertIn("Soft-delete them", planner_task_arg)
            self.assertIn("Soft-delete them", outcome["task"])


class GenerateClarifyingQuestionsTests(unittest.TestCase):
    def test_none_response_yields_no_questions(self):
        with patch.object(qz_agent.client.chat.completions, "create", return_value=_fake_completion("NONE")):
            self.assertEqual(qz_agent.generate_clarifying_questions("Add a health endpoint", "idx"), [])

    def test_numbered_questions_are_parsed_and_capped_at_three(self):
        raw = "1. Should X happen?\n2. What about Y?\n3. And Z?\n4. One too many?"
        with patch.object(qz_agent.client.chat.completions, "create", return_value=_fake_completion(raw)):
            questions = qz_agent.generate_clarifying_questions("Add a delete-note endpoint", "idx")
            self.assertEqual(len(questions), 3)
            self.assertEqual(questions[0], "Should X happen?")

    def test_provider_failure_yields_no_questions(self):
        with patch.object(qz_agent.client.chat.completions, "create", side_effect=RuntimeError("down")):
            self.assertEqual(qz_agent.generate_clarifying_questions("Add a delete-note endpoint", "idx"), [])


class PromptPlanApprovalTests(unittest.TestCase):
    def test_approve_default_on_empty_input(self):
        with patch("builtins.input", return_value=""):
            decision, plan, architecture = qz_agent.prompt_plan_approval("plan", "tree")
        self.assertEqual((decision, plan, architecture), ("approved", "plan", "tree"))

    def test_reject(self):
        with patch("builtins.input", return_value="r"):
            decision, plan, architecture = qz_agent.prompt_plan_approval("plan", "tree")
        self.assertEqual(decision, "rejected")

    def test_best_judgment(self):
        with patch("builtins.input", return_value="b"):
            decision, plan, architecture = qz_agent.prompt_plan_approval("plan", "tree")
        self.assertEqual(decision, "proceed_best_judgment")

    def test_edit_collects_replacement_plan_until_end_sentinel(self):
        inputs = iter(["e", "new line one", "new line two", "END"])
        with patch("builtins.input", side_effect=lambda *a: next(inputs)):
            decision, plan, architecture = qz_agent.prompt_plan_approval("old plan", "tree")
        self.assertEqual(decision, "edited")
        self.assertEqual(plan, "new line one\nnew line two")

    def test_invalid_choice_is_reprompted(self):
        inputs = iter(["nonsense", "a"])
        with patch("builtins.input", side_effect=lambda *a: next(inputs)):
            decision, plan, architecture = qz_agent.prompt_plan_approval("plan", "tree")
        self.assertEqual(decision, "approved")


class RunTaskModeIntegrationTests(unittest.TestCase):
    """Confirms run_task wires classification -> resolve_plan -> executor correctly."""

    def _common_patches(self):
        return [
            patch("qz_agent.prepare_environment"),
            patch("qz_agent.ensure_git_repository", return_value="clean"),
            patch("qz_agent.capture_pre_existing_test_failures", return_value=None),
            patch("qz_agent.load_or_build_index", return_value=({"files": []}, False)),
            patch("qz_agent.format_index_summary", return_value="idx"),
        ]

    def test_rejected_plan_never_reaches_executor(self):
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("qz_agent.classify_task_mode", return_value="complex"), \
             patch("qz_agent.resolve_plan", return_value={"status": "rejected", "task": "t",
                                                           "plan": "p", "architecture": "a", "decision": "rejected"}), \
             patch("qz_agent.run_executor") as executor:
            result = qz_agent.run_task("Refactor auth and migrate schema")
            executor.assert_not_called()
            self.assertIn("rejected", result.lower())

    def test_approved_plan_reaches_executor_with_resolved_task(self):
        patches = self._common_patches()
        outcome = {
            "status": "ready",
            "task": "Add a delete-note endpoint\n\nClarifications...: Soft-delete them",
            "plan": "1. Add route",
            "architecture": "tree",
            "decision": "approved",
        }
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("qz_agent.classify_task_mode", return_value="complex"), \
             patch("qz_agent.resolve_plan", return_value=outcome), \
             patch("qz_agent.run_executor", return_value="done") as executor:
            result = qz_agent.run_task("Add a delete-note endpoint")
            executor.assert_called_once()
            args, kwargs = executor.call_args
            self.assertEqual(args, (outcome["task"], outcome["plan"], outcome["architecture"]))
            self.assertIsNone(kwargs.get("test_baseline"))
            self.assertTrue(kwargs.get("task_id"))
            self.assertEqual(result, "done")


if __name__ == "__main__":
    unittest.main()
