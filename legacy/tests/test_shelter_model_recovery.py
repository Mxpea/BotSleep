"""A goal's model repair cannot reinterpret a wall cell as a new house centre."""

import copy
import json
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from test_autonomous_tasks import world
from test_llm_planner import Brain, response
import test_shelter_project as fixtures


class ShelterModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ShelterProjectTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.anchor = self.fixture.anchor()

    def harness(self, *replies):
        settings = {**copy.deepcopy(DEFAULTS["survival"]), "objective": "daily",
                    "world_id": self.fixture.SCOPE["world_id"], "state_dir": self.fixture.temp.name,
                    "poll_interval_seconds": .001, "retry_seconds": .001}
        brain = Brain(*replies)
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, settings, brain=brain)
        harness = SurvivalHarness(self.fixture.client, settings, DEFAULTS["sleep"], planner=planner)
        self.addCleanup(harness.close)
        harness.context["shelter_project"] = copy.deepcopy(self.anchor)
        harness.context["base_project"] = {key: self.anchor[key] for key in ("pos", "orientation", "material", "dimension")}
        harness.task_controller.start({"task": "shelter"}, self.observation(), harness.context)
        harness.task_controller.stop("local_navigation_budget")
        harness._save_task("TASK_NEEDS_AI")
        return harness, brain

    def observation(self):
        return world(self.fixture.client.items, **self.fixture.client.status)

    def cycle_patches(self, harness):
        stack = ExitStack()
        stack.enter_context(patch.object(harness, "_observe", side_effect=lambda **_: self.observation()))
        stack.enter_context(patch.object(harness, "_mandatory_safety", return_value=None))
        stack.enter_context(patch.object(harness, "_local_task_food", return_value=None))
        stack.enter_context(patch.object(harness, "_unsafe_destination", return_value=None))
        stack.enter_context(patch.object(harness.daily_policy, "validate", return_value=None))
        return stack

    def step(self, harness):
        harness._next_attempt = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = harness.tick(automatic=True)
            if result is not None:
                return result
            self.assertFalse(harness.paused, harness.pause_reason)
            time.sleep(.001)
        self.fail("Offline harness did not finish")

    def test_model_repair_rejects_shifted_centre_and_rotated_site_before_any_write(self):
        harness, _ = self.harness()
        before = copy.deepcopy(harness.context["shelter_project"])
        for name in ("plan_shelter", "plan_shelter_preparation", "prepare_shelter_step",
                     "build_shelter_step", "build_shelter_batch"):
            for changes in ({"pos": [-1, 64, 1]}, {"orientation": "north"}):
                with self.subTest(name=name, changes=changes):
                    decision = Decision(name, {**self.fixture.args, **changes}, "Repair placement", "shelter")
                    with self.cycle_patches(harness), patch.object(harness, "_model_decision", return_value=decision):
                        _, result, _ = harness._cycle(None)
                    self.assertEqual(result.error_code, "shelter_project_active")
                    self.assertFalse(result.data["executed"])
                    self.assertEqual(result.data["project"]["pos"], before["pos"])
                    self.assertIsNone(harness.journal.state["pending"])
                    self.assertFalse(harness._task_child)
        self.assertEqual(harness.context["shelter_project"], before)
        self.assertEqual(len(self.fixture.client.actions), 1)  # Only the real initial wall.

    def test_rules_after_escalation_obey_same_site_lock(self):
        harness, _ = self.harness()
        harness.uses_llm = False
        decision = Decision("build_shelter_step", {**self.fixture.args, "pos": [-1, 64, 1]}, "Repair", "shelter")
        with self.cycle_patches(harness), patch.object(harness.daily_goals, "choose", return_value=decision):
            _, result, _ = harness._cycle(None)
        self.assertEqual(result.error_code, "shelter_project_active")
        self.assertEqual(len(self.fixture.client.actions), 1)

    def test_native_rejection_feedback_then_original_centre_builds_and_keeps_anchor(self):
        inspected = self.fixture.skills.execute("inspect_shelter", {
            key: self.fixture.args[key] for key in ("pos", "orientation")})
        self.fixture.client.status["pos"] = list(inspected.data["stand_candidates"][0])
        wrong = {**self.fixture.args, "pos": [-1, 64, 1]}
        harness, brain = self.harness(response("build_shelter_step", wrong, "wrong_site"),
                                      response("build_shelter_step", self.fixture.args, "original_site"))
        with self.cycle_patches(harness):
            rejected = self.step(harness)
            self.assertEqual(rejected.error_code, "shelter_project_active", rejected)
            self.assertIsNone(harness.journal.state["pending"])
            self.assertEqual(len(self.fixture.client.actions), 1)
            result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["used"], 1)
        self.assertEqual(len(self.fixture.client.actions), 2)
        self.assertEqual(harness.context["shelter_project"]["pos"], self.anchor["pos"])
        self.assertEqual(harness.context["shelter_project"]["orientation"], "east")
        self.assertIsNone(harness.journal.state["pending"])
        messages = brain.requests[1]["messages"]
        feedback = next(row for row in messages if row.get("tool_call_id") == "wrong_site")
        actual = json.loads(feedback["content"])["result"]
        self.assertEqual(actual["error_code"], "shelter_project_active")
        self.assertEqual(actual["data"]["project"]["pos"], self.anchor["pos"])
        prompt = json.loads(next(row["content"] for row in reversed(messages) if row["role"] == "user"))
        constraint = prompt["context"]["task_execution"]["shelter_recovery_constraint"]
        self.assertEqual(constraint["pos"], self.anchor["pos"])
        self.assertTrue(constraint["requires_revalidation"])

    def test_result_persistence_does_not_replace_anchor_after_model_feedback_is_consumed(self):
        harness, _ = self.harness()
        harness._automatic_cycle = True
        harness._model_feedback = False
        harness._task_child = False
        decision = self.new_site()
        decision = Decision("build_shelter_step", decision.args, decision.reason, decision.goal)
        for _ in range(3):
            result = self.fixture.skills.execute(decision.name, decision.args)
            if result.error_code == "needs_position":
                self.fixture.client.status["pos"] = list(result.data["stand_candidates"][0])
                continue
            break
        self.assertTrue(result.success, result)
        harness._daily_result(decision, result, self.observation())
        self.assertEqual(harness.context["shelter_project"]["pos"], self.anchor["pos"])
        self.assertEqual(harness.context["base_project"]["pos"], self.anchor["pos"])

    def test_restarted_needs_ai_goal_retains_model_recovery_lock(self):
        previous, _ = self.harness()
        previous.close()
        restored = SurvivalHarness(self.fixture.client, previous.settings, DEFAULTS["sleep"])
        self.addCleanup(restored.close)
        self.assertEqual(restored._task_snapshot()["status"], "needs_ai")
        decision = Decision("build_shelter_step", {**self.fixture.args, "pos": [-1, 64, 1]}, "Repair", "shelter")
        with self.cycle_patches(restored), patch.object(restored.daily_goals, "choose", return_value=decision):
            _, result, _ = restored._cycle(None)
        self.assertEqual(result.error_code, "shelter_project_active")
        self.assertIsNone(restored.journal.state["pending"])

    def new_site(self):
        client = self.fixture.client
        client.status["pos"] = [-9.5, 64, .5]
        for x in range(-13, -6):
            for z in range(-4, 4):
                client.blocks[x, 63, z] = {"name": "grass_block", "properties": {}}
        return Decision("plan_shelter", {**self.fixture.args, "pos": [-10, 64, 0]},
                        "Explicitly choose a new construction project", "shelter")

    def test_explicit_manual_new_site_is_still_verified_and_persisted(self):
        harness, _ = self.harness()
        decision = self.new_site()
        with self.cycle_patches(harness):
            _, result, obs = harness._cycle(decision)
        self.assertTrue(result.success, result)
        harness._daily_result(decision, result, obs)
        self.assertEqual(harness.context["base_project"]["pos"], decision.args["pos"])
        self.assertNotIn("shelter_project", harness.context)

    def test_completed_project_allows_new_automatic_site(self):
        harness, _ = self.harness()
        harness.context["shelter_project"]["complete"] = True
        decision = self.new_site()
        with self.cycle_patches(harness), patch.object(harness, "_model_decision", return_value=decision):
            _, result, obs = harness._cycle(None)
        self.assertTrue(result.success, result)
        harness._daily_result(decision, result, obs)
        self.assertEqual(harness.context["base_project"]["pos"], decision.args["pos"])
        self.assertNotIn("shelter_project", harness.context)

    def test_other_goal_manual_competition_dimension_and_scope_keep_existing_boundaries(self):
        harness, _ = self.harness()
        harness._automatic_cycle = True
        self.assertIsNotNone(harness._shelter_project_lock("overworld"))
        self.assertIsNone(harness._shelter_project_lock("the_nether"))
        harness.context["shelter_project"]["scope"]["world_id"] = "other-world"
        self.assertIsNone(harness._shelter_project_lock("overworld"))
        harness.context["shelter_project"] = copy.deepcopy(self.anchor)
        harness.competition_mode = True
        self.assertIsNone(harness._shelter_project_lock("overworld"))
        harness.competition_mode = False
        harness.task_controller.stop("Operator cancelled", cancelled=True)
        self.assertIsNone(harness._shelter_project_lock("overworld"))
        harness.task_controller.start({"task": "gather", "resource": "logs", "count": 4}, self.observation(), harness.context)
        self.assertIsNone(harness._shelter_project_lock("overworld"))


if __name__ == "__main__":
    unittest.main()
