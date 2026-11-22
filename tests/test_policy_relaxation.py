"""Original model intent can pass a soft hold, not a hard safety check."""

import unittest
from unittest.mock import patch

from core.client import APIError
from core.policy_relaxation import LOW_HEALTH, NIGHT
import test_task_harness as fixtures


class PolicyRelaxationTests(unittest.TestCase):
    harness = fixtures.TaskHarnessTests.harness
    cycle = fixtures.TaskHarnessTests.cycle
    until = fixtures.TaskHarnessTests.until
    feedback = staticmethod(fixtures.TaskHarnessTests.feedback)

    def setUp(self):
        fixtures.TaskHarnessTests.setUp(self)
        self.settings["sleep_policy"] = "strict"

    def dusk(self):
        self.world.status.update(is_day=False, time_of_day=13000)

    def gather_goal(self, count=1):
        self.world.block([1, 64, 0], "oak_log")
        return self.harness(fixtures.response("run_survival_task", {"task": "gather", "resource": "logs", "count": count}))

    def test_dusk_during_model_call_keeps_original_goal_for_multiple_stationary_children(self):
        self.world.block([1, 64, 0], "oak_log")
        self.world.block([-1, 64, 0], "birch_log")

        def reply(_messages):
            self.dusk()
            return fixtures.response("run_survival_task", {"task": "gather", "resource": "logs", "count": 2}, "original")

        runner, brain = self.harness(reply)
        self.assertTrue(self.cycle(runner).data["accepted"])
        results = self.until(runner, lambda: runner.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual([result.data["relaxed_policy"] for result in results], [NIGHT, NIGHT])
        self.assertEqual(self.world.counts, {"oak_log": 1, "birch_log": 1})
        self.assertEqual(self.world.dig_options, [([1, 64, 0], False), ([-1, 64, 0], False)])
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual([row["tool_call_id"] for row in self.feedback(runner.planner)], ["original"])
        self.assertIsNone(runner.journal.state["pending"])

    def test_existing_craft_goal_continues_when_low_health_food_recovery_has_no_option(self):
        self.world.status["pos"] = [.5, 90, .5]
        self.world.block([0, 89, 0], "stone")
        self.world.counts["oak_log"] = 1
        runner, brain = self.harness(fixtures.response("run_survival_task", {
            "task": "craft", "item": "oak_planks", "count": 4}))
        self.assertTrue(self.cycle(runner).data["accepted"])
        # Also qualifies for cautious bootstrap. It must continue the in-flight
        # task instead of opening a new model recovery turn.
        self.world.status.update(health=6, food=15)
        result = self.cycle(runner)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["relaxed_policy"], LOW_HEALTH)
        self.assertEqual(self.world.counts["oak_planks"], 4)
        self.assertEqual(runner.snapshot()["task_execution"]["status"], "completed")
        self.assertEqual(len(brain.requests), 1)
        self.assertFalse(any(call[0] in {"move", "stop"} for call in self.world.calls))

    def test_atomic_model_action_keeps_its_call_id_after_soft_recheck(self):
        self.settings["autonomy"]["enabled"] = False
        self.world.block([1, 64, 0], "oak_log")

        def reply(_messages):
            self.dusk()
            return fixtures.response("gather_block", {"pos": [1, 64, 0], "expected_name": "oak_log"}, "atomic")

        runner, brain = self.harness(reply)
        result = self.cycle(runner)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["relaxed_policy"], NIGHT)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual([row["tool_call_id"] for row in self.feedback(runner.planner)], ["atomic"])

    def test_far_target_is_not_approached_in_relaxed_work(self):
        self.world.block([5, 64, 0], "oak_log")
        runner, brain = self.harness(fixtures.response("run_survival_task", {"task": "gather", "resource": "logs", "count": 1}))
        self.assertTrue(self.cycle(runner).data["accepted"])
        self.dusk()
        self.assertTrue(self.cycle(runner).data["waiting"])
        self.assertEqual(runner.snapshot()["task_execution"]["status"], "suspended")
        self.assertEqual(len(brain.requests), 1)
        self.assertFalse(any(call[0] in {"move", "dig"} for call in self.world.calls))

    def test_relaxed_dig_accepts_delayed_inventory_gain_without_moving(self):
        runner, _ = self.gather_goal()
        self.assertTrue(self.cycle(runner).data["accepted"])
        self.dusk()
        self.world.natural_pickup = False
        inventory = self.world.inventory

        def inventory_after_pickup_delay(**kwargs):
            if "timeout_seconds" in kwargs and self.world.dig_options:
                self.world.counts["oak_log"] = 1
            return inventory(**kwargs)

        with patch.object(self.world, "inventory", side_effect=inventory_after_pickup_delay):
            result = self.cycle(runner)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"oak_log": 1})
        self.assertEqual(runner.snapshot()["task_execution"]["status"], "completed")
        self.assertEqual(self.world.dig_options, [([1, 64, 0], False)])
        self.assertFalse(any(call[0] == "move" for call in self.world.calls))

    def test_new_hostile_detected_by_skill_cancels_relaxed_write(self):
        runner, _ = self.gather_goal()
        self.assertTrue(self.cycle(runner).data["accepted"])
        self.dusk()
        original = self.world.get_entities

        def scan(*args, **kwargs):
            if kwargs.get("radius") == 19 and kwargs.get("timeout_seconds") == 2:
                return [{"id": 4, "type": "hostile", "name": "skeleton", "pos": [.5, 64, 10.5]}]
            return original(*args, **kwargs)

        with patch.object(self.world, "get_entities", side_effect=scan):
            result = self.cycle(runner)
        self.assertEqual(result.error_code, "near_hostile", result)
        self.assertFalse(result.data["executed"])
        self.assertEqual(runner.snapshot()["task_execution"]["status"], "needs_ai")
        self.assertFalse(self.world.dig_options)
        self.assertIsNone(runner.journal.state["pending"])

    def test_unknown_relaxed_write_still_freezes_and_cannot_replay(self):
        runner, brain = self.gather_goal()
        self.assertTrue(self.cycle(runner).data["accepted"])
        self.dusk()
        with patch.object(self.world, "dig", side_effect=APIError("request_timeout", "lost acknowledgement")) as dig:
            result = self.cycle(runner)
            self.assertFalse(result.success)
            self.assertEqual(result.data["relaxed_policy"], NIGHT)
            self.assertTrue(runner.paused)
            self.assertEqual(runner.snapshot()["task_execution"]["status"], "unknown")
            self.assertIsNotNone(runner.journal.state["pending"])
            self.assertIsNone(runner.tick())
        dig.assert_called_once()
        self.assertEqual(len(brain.requests), 1)

    def test_cancel_after_relaxed_write_does_not_clear_pending(self):
        runner, _ = self.gather_goal()
        self.assertTrue(self.cycle(runner).data["accepted"])
        self.dusk()
        original = self.world.dig

        def cancelled(*args, **kwargs):
            reply = original(*args, **kwargs)
            runner._cancel.set()
            return reply

        with patch.object(self.world, "dig", side_effect=cancelled) as dig:
            result = self.cycle(runner)
            self.assertEqual(result.error_code, "action_unconfirmed", result)
            self.assertTrue(runner.paused)
            self.assertIsNotNone(runner.journal.state["pending"])
            dig.assert_called_once()
