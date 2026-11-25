"""Known route refusal -> one checked leaf -> fresh route, with no live I/O."""

import copy
import unittest

from agent.daily_survival import DailySurvivalGoals
from agent.survival_planner import Decision
from core.autonomous_tasks import AutonomousTaskController
from core.client import APIError
from core.route_foliage_recovery import choose_route_foliage, position_foliage_hint
from skills import SkillResult
from skills.daily_survival import DailySurvivalSkills
from skills.positioning import PositioningSkills
from test_autonomous_tasks import world, verified
from test_daily_skills import DailyWorld, envelope


class RouteFoliageRecoveryTests(unittest.TestCase):
    def scene(self):
        client = DailyWorld()
        client.status.update(pos=[.6, 64, .5], speed=0, velocity=[0, 0, 0],
            is_day=True, time_of_day=1000, thundering=False, jumping=False)
        client.counts = {"oak_planks": 64, "oak_door": 1, "crafting_table": 1}
        target, blocker = [-.5, 63, .5], [-1, 65, 0]
        client.block([-1, 63, 0], "air")
        client.block(blocker, "oak_leaves", properties={"waterlogged": False})
        def dig():
            client.block(blocker, "air")
            return envelope({"removed": True})
        client.effects["dig"] = dig
        return client, target, blocker

    def observe(self, client, target, blocker):
        obs = world(client.counts, **client.status)
        row = client.get_block(blocker)["result"]
        obs["blocks"] = [{**row, "fresh": True, "can_see": True}]
        obs["local_geometry_origin"] = list(client.status["pos"])
        obs["safe_stand_candidates"] = [{"pos": target, "support": "stone"}]
        obs["daily_evidence"] = {"base_site": verified(pos=[-2, 63, 0], orientation="north")}
        obs["shelter"] = {"fresh": True, "dimension": "overworld", "pos": [-2, 63, 0],
            "orientation": "north", "missing_blocks": 43, "inside": False,
            "geometry": {"floor_verified": True, "exit_verified": True},
            "hazards": [], "occupied_blueprint_cells": [], "needs_position": True,
            "stand_candidates": [target], "next_gap": {"pos": [-4, 63, -2], "stage": "wall"},
            "door": {"frame_verified": False}}
        return obs

    def failed_route(self, **settings):
        client, target, blocker = self.scene()
        obs = self.observe(client, target, blocker)
        controller = AutonomousTaskController(settings)
        controller.start({"task": "shelter"}, obs)
        move = controller.next_action(obs)
        self.assertEqual(move.name, "position_for_task", move)
        self.assertEqual(move.args["pos"], target)
        failed = PositioningSkills(client).execute(move.name, move.args)
        self.assertEqual(failed.error_code, "unsafe_route", failed)
        self.assertIs(failed.data["executed"], False)
        self.assertEqual(failed.data["blocked_cell"], blocker)
        controller.record_result(move, failed, obs)
        return controller, client, target, blocker, move, failed

    @staticmethod
    def writes(client):
        return [row for row in client.calls if row[0] in {"move", "dig", "stop", "place", "control"}]

    def test_real_skills_recover_one_leaf_then_fresh_downstep_without_ai(self):
        controller, client, target, blocker, _, _ = self.failed_route()
        self.assertEqual(self.writes(client), [])
        key = controller.rules._key(target)
        self.assertIn(key, controller.state()["blocked"])
        clear = controller.next_action(self.observe(client, target, blocker))
        self.assertEqual(clear.name, "clear_foliage", clear)
        self.assertEqual(clear.args["pos"], blocker)
        result = DailySurvivalSkills(client).execute(clear.name, clear.args)
        self.assertTrue(result.success, result)
        controller.record_result(clear, result, self.observe(client, target, blocker))
        self.assertNotIn(key, controller.state()["blocked"])
        move = controller.next_action(self.observe(client, target, blocker))
        self.assertEqual(move.name, "position_for_task", move)
        self.assertEqual(move.args["pos"], target)
        moved = PositioningSkills(client).execute(move.name, move.args)
        self.assertTrue(moved.success, moved)
        controller.record_result(move, moved, self.observe(client, target, blocker))
        self.assertEqual([row[0] for row in self.writes(client)], ["dig", "move"])
        self.assertEqual(client.status["pos"], target)
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)

    def test_unknown_clear_keeps_route_cooldown_and_freezes_without_extra_write(self):
        controller, client, target, blocker, _, _ = self.failed_route()
        clear = controller.next_action(self.observe(client, target, blocker))
        def lost():
            client.block(blocker, "air")
            raise APIError("request_timeout", "Clearing acknowledgement was lost")
        client.effects["dig"] = lost
        result = DailySurvivalSkills(client).execute(clear.name, clear.args)
        self.assertFalse(result.success)
        controller.record_result(clear, result, self.observe(client, target, blocker))
        self.assertEqual(controller.snapshot()["status"], "unknown")
        self.assertIn(controller.rules._key(target), controller.state()["blocked"])
        for _ in range(3):
            self.assertIsNone(controller.next_action(self.observe(client, target, blocker)))
        self.assertEqual([row[0] for row in self.writes(client)], ["dig"])

    def test_route_hint_needs_exact_nonexecuted_adjacent_foliage_failure(self):
        _, client, target, blocker, move, failed = self.failed_route()
        obs = self.observe(client, target, blocker)
        variants = [{"executed": True}, {"executed": None}, {"action_state": "unknown"},
            {"blocked_cell": [-1, 63, 0]}, {"blocked_cell": [-3, 65, 0]},
            {"blocked_cell": [0, 65, 0]}, {"block": "oak_log"}, {"origin": None}]
        for changes in variants:
            with self.subTest(changes=changes):
                result = SkillResult(False, "refused", {**failed.data, **changes}, "unsafe_route")
                self.assertIsNone(position_foliage_hint(move, result, obs))
        self.assertIsNone(position_foliage_hint(move, SkillResult(False, "unknown", failed.data, "action_unconfirmed"), obs))

    def test_stale_changed_hidden_wet_or_unsafe_current_evidence_never_selects_clear(self):
        controller, client, target, blocker, _, _ = self.failed_route()
        base = self.observe(client, target, blocker)
        recent = controller.state()["recent_results"]
        daily = DailySurvivalGoals()
        context = {"capabilities": ["clear_foliage"]}
        variants = [lambda o: o.update(fresh=False), lambda o: o.update(observation_scope="safety"),
            lambda o: o["status"].update(pos=[.5, 64, 1.5]),
            lambda o: o["status"].update(last_death={"t": 1}),
            lambda o: o["status"].update(time_of_day=11000),
            lambda o: o["status"].update(health=11), lambda o: o["status"].update(food=7),
            lambda o: o["status"].update(nearest_hostile={"name": "skeleton", "dist": 15}),
            lambda o: o["blocks"][0].update(fresh=False), lambda o: o["blocks"][0].update(can_see=False),
            lambda o: o["blocks"][0].update(name="oak_log"), lambda o: o["blocks"][0].update(diggable=False),
            lambda o: o["blocks"][0].update(properties={}),
            lambda o: o["blocks"][0].update(properties={"waterlogged": True}),
            lambda o: o["blocks"][0].update(properties={"waterlogged": 0})]
        for mutate in variants:
            current = copy.deepcopy(base)
            mutate(current)
            self.assertIsNone(choose_route_foliage(recent, current, context, daily, "shelter"), current)
        self.assertIsNone(choose_route_foliage(recent, base, {**context, "pending": {"unknown": True}}, daily, "shelter"))
        self.assertIsNone(choose_route_foliage(recent, base, {**context, "blocked_targets": [controller.rules._key(blocker)]}, daily, "shelter"))

    def test_recovery_keeps_global_local_and_life_budgets(self):
        for settings in ({"autonomy": {"max_steps": 1}}, {"autonomy": {"max_local_failures": 1}}):
            with self.subTest(settings=settings):
                controller, client, target, blocker, _, _ = self.failed_route(**settings)
                self.assertIsNone(controller.next_action(self.observe(client, target, blocker)))
                self.assertEqual(controller.snapshot()["status"], "needs_ai")
                self.assertEqual(self.writes(client), [])
        controller, client, target, blocker, _, _ = self.failed_route()
        self.assertIsNone(controller.next_action(self.observe(client, target, blocker), {"life_generation": 1}))
        self.assertEqual(self.writes(client), [])

    def test_unverified_clear_ack_never_releases_route_or_repeats_old_hint(self):
        controller, client, target, blocker, _, _ = self.failed_route()
        obs = self.observe(client, target, blocker)
        clear = controller.next_action(obs)
        controller.record_result(clear, SkillResult(True, "ack only", {"executed": True}), obs)
        self.assertIn(controller.rules._key(target), controller.state()["blocked"])
        next_action = controller.next_action(obs)
        self.assertTrue(next_action is None or next_action.name != "clear_foliage")
        self.assertEqual(self.writes(client), [])

    def test_life_generation_change_after_clear_retains_old_failure_evidence(self):
        controller, client, target, blocker, _, _ = self.failed_route()
        clear = controller.next_action(self.observe(client, target, blocker))
        result = DailySurvivalSkills(client).execute(clear.name, clear.args)
        self.assertTrue(result.success, result)
        controller.record_result(clear, result, self.observe(client, target, blocker), context={"life_generation": 1})
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertIn(controller.rules._key(target), controller.state()["blocked"])
        self.assertEqual([row[0] for row in self.writes(client)], ["dig"])


if __name__ == "__main__":
    unittest.main()
