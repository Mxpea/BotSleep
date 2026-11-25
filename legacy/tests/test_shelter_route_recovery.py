"""Known refused sweeps cool down per origin, while global risk blocks remain."""

import unittest

from core.autonomous_tasks import AutonomousTaskController
from skills.positioning import PositioningSkills
import test_shelter_approach as approach_fixture


class ShelterRouteRecoveryTests(unittest.TestCase):
    def scene(self, change_result=None):
        fixture = approach_fixture.ShelterApproachTests()
        client, collector, context = fixture.complete_shell()
        client.status["pos"] = [4.4, 64, 2.6]
        client.blocks[(3, 64, 1)] = {"name": "grass_block", "properties": {}}
        observation = fixture.observe(client, collector)
        controller = AutonomousTaskController()
        controller.start({"task": "shelter"}, observation, context)
        decision = controller.next_action(observation, context)
        self.assertEqual(decision.args["pos"], [3.5, 64, .5])
        result = PositioningSkills(client).execute(decision.name, decision.args)
        self.assertEqual(result.error_code, "unsafe_route", result)
        self.assertFalse(result.data["executed"])
        self.assertFalse(client.actions)
        if change_result:
            change_result(result)
        controller.record_result(decision, result, observation, context)
        return fixture, client, collector, context, controller, decision

    def test_fresh_new_cell_can_recheck_same_previously_visited_door_target(self):
        fixture, client, collector, context, controller, first = self.scene()
        key = controller.rules._key(first.args["pos"])
        same = fixture.observe(client, collector)
        self.assertIn(key, controller._context(context, same)["blocked_targets"])
        controller._data["shelter_return_visited"] = [first.args["pos"]]
        # The world reports a new real stance after its intervening local moves.
        client.status["pos"] = [3.4, 64, -.5]
        fresh = fixture.observe(client, collector)
        self.assertNotIn(key, controller._context(context, fresh)["blocked_targets"])
        self.assertIn(key, controller.state()["blocked"])  # Original sweep still cools down.
        action = controller.next_action(fresh, context)
        self.assertEqual(action.args["pos"], first.args["pos"])
        result = PositioningSkills(client).execute(action.name, action.args)
        self.assertTrue(result.success, result)
        self.assertEqual(client.actions, [("move", first.args["pos"])])
        controller.record_result(action, result, fixture.observe(client, collector), context)
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)

    def test_returning_to_original_origin_keeps_its_refused_sweep_blocked(self):
        fixture, client, collector, context, controller, first = self.scene()
        key = controller.rules._key(first.args["pos"])
        client.status["pos"] = [3.4, 64, -.5]
        self.assertNotIn(key, controller._context(context, fixture.observe(client, collector))["blocked_targets"])
        client.status["pos"] = [4.4, 64, 2.6]
        self.assertIn(key, controller._context(context, fixture.observe(client, collector))["blocked_targets"])

    def test_same_cell_small_jitter_stale_site_life_and_global_blocks_are_retained(self):
        for change in ("same_cell", "tiny_cell_change", "stale", "wrong_site", "life", "generation", "pending", "global"):
            with self.subTest(change=change):
                fixture, client, collector, context, controller, first = self.scene()
                key = controller.rules._key(first.args["pos"])
                client.status["pos"] = [3.4, 64, -.5]
                fresh = fixture.observe(client, collector)
                if change == "same_cell":
                    fresh["status"]["pos"] = [4.8, 64, 2.7]
                elif change == "tiny_cell_change":
                    origin = controller._data["recent_results"][-1]["shelter_route_refusal"]["origin"]
                    origin[:] = [4.01, 64, 2.6]
                    fresh["status"]["pos"] = [3.99, 64, 2.6]
                elif change == "stale":
                    fresh["fresh"] = False
                elif change == "wrong_site":
                    fresh["daily_evidence"]["base_site"]["orientation"] = "north"
                elif change == "life":
                    fresh["status"]["last_death"] = {"t": 12}
                elif change == "generation":
                    context["life_generation"] = 1
                elif change == "pending":
                    context["pending"] = {"decision": "unresolved"}
                else:
                    context["blocked_targets"] = [key]
                self.assertIn(key, controller._context(context, fresh)["blocked_targets"])

    def test_executed_or_hazardous_refusal_does_not_gain_route_exception(self):
        for field, value in (("executed", True), ("block", "lava"), ("block", "unknown"), ("origin", None)):
            with self.subTest(field=field, value=value):
                fixture, client, collector, context, controller, first = self.scene(
                    lambda result: result.data.update({field: value}))
                key = controller.rules._key(first.args["pos"])
                client.status["pos"] = [3.4, 64, -.5]
                self.assertNotIn("shelter_route_refusal", controller.state()["recent_results"][-1])
                self.assertIn(key, controller._context(context, fixture.observe(client, collector))["blocked_targets"])

    def test_latest_real_failure_and_other_task_prevent_old_hint_from_unlocking(self):
        fixture, client, collector, context, controller, first = self.scene()
        key = controller.rules._key(first.args["pos"])
        client.status["pos"] = [3.4, 64, -.5]
        fresh = fixture.observe(client, collector)
        controller._data["recent_results"].append({"name": "position_for_task", "success": False,
            "error_code": "unsafe_route", "executed": True, "target": first.args["pos"]})
        self.assertIn(key, controller._context(context, fresh)["blocked_targets"])
        controller._data["recent_results"].pop()
        controller._data["task"] = "starter_tools"
        self.assertIn(key, controller._context(context, fresh)["blocked_targets"])

    def test_unknown_action_remains_frozen_even_after_world_position_changes(self):
        fixture, client, collector, context, controller, _ = self.scene(
            lambda result: result.data.update(action_state="unknown"))
        client.status["pos"] = [3.4, 64, -.5]
        self.assertIsNone(controller.next_action(fixture.observe(client, collector), context))
        self.assertEqual(controller.snapshot()["status"], "unknown")
        self.assertFalse(client.actions)


if __name__ == "__main__":
    unittest.main()
