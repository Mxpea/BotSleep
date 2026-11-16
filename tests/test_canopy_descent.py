"""Regression for body-clear canopy stalls: no model or real game calls."""

import copy
import math
import unittest

from core.config import DEFAULTS
from core.local_execution import LocalExecutor
from test_llm_planner import observation
from core.canopy_geometry import plan_canopy_step
from test_daily_runtime import RuntimeDailyWorld
import test_local_first_runtime as local_runtime


class CanopyDescentRegressionTests(unittest.TestCase):
    def test_body_clear_canopy_can_cut_a_checked_side_step_before_model_takeover(self):
        settings = copy.deepcopy(DEFAULTS["survival"])
        settings["objective"] = "daily"
        obs = observation()
        obs["status"].update(pos=[.5, 92, .5], is_day=True, thundering=False,
                             health=20, food=20, on_ground=True, time_of_day=5000, nearest_hostile={"name": "spider", "dist": 15.9})
        obs["blocks"] = []  # All body-height leaves already removed, as in the captured run.
        obs["canopy_step"] = {"origin": [.5, 92, .5], "pos": [1.5, 91, .5],
            "clear_pos": [1, 91, 0], "expected_name": "birch_leaves", "support": "birch_leaves",
            "drop": 1, "route_end": [1.5, 91, .5], "fresh": True, "verified": True, "dimension": "overworld"}
        context = {"retreat_recovery": {"canopy_blocked": True, "evacuating": True}}
        decision = LocalExecutor(settings).choose(obs, context)
        self.assertIsNotNone(decision, "Body-height clearance exhaustion must not force a model wait when a verified lower step exists")
        self.assertEqual(decision.name, "descend_canopy")
        self.assertEqual(decision.args["clear_pos"], [1, 91, 0])

    def test_internal_descent_is_not_a_model_tool(self):
        from agent.tools import tools_for_objective
        for objective in ("starter", "daily", "competition"):
            names = {tool["function"]["name"] for tool in tools_for_objective(objective)}
            self.assertNotIn("descend_canopy", names)



class CanopyColumnsWorld(RuntimeDailyWorld):
    """Normalised captured symptom: clear body, four-block ground gap."""

    def __init__(self):
        super().__init__()
        self.status.update(pos=[.5, 92, .5], time_of_day=5000, health=20, food=20,
                           on_ground=True, nearest_hostile={"name": "spider", "dist": 15.9})
        self.blocks.clear()
        for x in range(-6, 7):
            for z in range(-4, 5):
                self.block([x, 87, z], "grass_block")
        for pos in ([0, 91, 0], [-1, 91, 0], [-1, 90, 0], [-2, 90, 0], [-2, 89, 0]):
            self.block(pos, "birch_leaves", properties={"waterlogged": False}, can_see=True, diggable=True)

    def dig(self, pos, collect=True):
        self.calls.append(("dig", list(pos), collect))
        assert not collect
        assert self.blocks[tuple(pos)]["name"] == "birch_leaves"
        self.block(pos, "air")
        return {"ok": True, "result": {"dug": True}}


class CanopyGeometryTests(unittest.TestCase):
    def setUp(self):
        self.world = CanopyColumnsWorld()
        self.reads = []

    def read(self, pos):
        self.reads.append(tuple(pos))
        return self.world.get_block(list(pos))["result"]

    def test_side_leaf_has_a_lower_foothold_without_any_body_height_obstacle(self):
        plan = plan_canopy_step(self.world.status, self.read)
        self.assertEqual(plan["pos"], [-.5, 91, .5])
        self.assertEqual(plan["clear_pos"], [-1, 91, 0])
        self.assertEqual(plan["drop"], 1)
        self.assertNotEqual(plan["clear_pos"], [0, 91, 0])
        self.assertEqual(len(self.reads), len(set(self.reads)))
        self.assertLessEqual(len(self.reads), 96)

    def test_nearer_historical_threat_does_not_prohibit_quiet_descent(self):
        self.world.status["nearest_hostile"] = None
        plan = plan_canopy_step(self.world.status, self.read)
        old_threat = [-10.5, 88, .5]
        self.assertLess(math.dist(plan["pos"], old_threat), math.dist(self.world.status["pos"], old_threat))
        self.assertEqual(plan["drop"], 1)

    def test_search_has_a_hard_read_budget_when_flat_canopy_has_no_exit(self):
        calls = []
        def read(pos):
            calls.append(pos)
            return {"name": "birch_leaves" if pos[1] == 91 else "air",
                    "properties": {"waterlogged": False}, "diggable": True}
        self.assertIsNone(plan_canopy_step(self.world.status, read))
        self.assertLessEqual(len(calls), 96)
        self.assertEqual(len(calls), len(set(calls)))

    def test_an_excluded_first_step_is_not_reselected(self):
        plan = plan_canopy_step(self.world.status, self.read,
                               target=[-.5, 91, .5], excluded=["-1,91,0"])
        self.assertIsNone(plan)

    def test_four_block_drop_without_lower_leaf_support_is_not_a_route(self):
        for pos in ([-1, 90, 0], [-2, 90, 0], [-2, 89, 0]):
            self.world.block(pos, "air")
        self.assertIsNone(plan_canopy_step(self.world.status, self.read))

    def test_unknown_wet_or_hazardous_support_does_not_authorize_cutting(self):
        for value in (None, {"name": "lava"}, {"name": "water"},
                      {"name": "birch_leaves", "properties": {"waterlogged": True}}):
            def read(pos):
                return value if tuple(pos) == (-1, 90, 0) else self.read(pos)
            with self.subTest(value=value):
                self.assertIsNone(plan_canopy_step(self.world.status, read, target=[-.5, 91, .5]))

    def test_edge_stance_never_digs_a_support_the_body_may_touch(self):
        self.world.status["pos"][0] = .69
        self.assertIsNone(plan_canopy_step(self.world.status, self.read))
        self.assertEqual(self.reads, [])

    def test_flat_relay_is_allowed_only_towards_a_checked_lower_step(self):
        self.world.blocks.clear()
        for x in (0, 1, 2):
            self.world.block([x, 91, 0], "birch_leaves", properties={"waterlogged": False})
        self.world.block([3, 90, 0], "grass_block")
        plan = plan_canopy_step(self.world.status, self.read)
        self.assertEqual((plan["pos"], plan["drop"], plan["clear_pos"]), ([1.5, 92, .5], 0, None))
        self.assertEqual(plan["route_end"], [3.5, 91, .5])
        self.world.block([3, 90, 0], "air")
        self.assertIsNone(plan_canopy_step(self.world.status, self.read))

    def test_lava_next_to_the_landing_floor_is_rejected(self):
        self.world.block([-1, 90, 1], "lava")
        self.assertIsNone(plan_canopy_step(self.world.status, self.read, target=[-.5, 91, .5]))


class CanopyDescentRuntimeTests(unittest.TestCase):
    def runner(self, *, objective="daily", world=None):
        helper = local_runtime.LocalFirstRuntimeTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        world = world or CanopyColumnsWorld()
        if objective == "competition":
            from core.competition_rules import scoring_catalog
            rows = [{"id": row["id"], "title": row["title"], "frame": "task", "status": "not_started",
                "completed_at": None, "progress": {"criteria_done": 0, "criteria_total": 1}} for row in scoring_catalog()]
            world.advancements = lambda **kwargs: {"ok": True, "result": {"ready": True,
                "updated_at": 1794838624878, "packets_received": 1,
                "summary": {"total": len(rows), "completed": 0, "in_progress": 0, "not_started": len(rows)},
                "advancements": rows}}
        runner, brain = helper.harness(objective=objective, world=world)
        runner.context["retreat_recovery"] = {"dimension": "overworld", "evacuating": True,
            "canopy_blocked": True, "threat_pos": [-10.5, 88, .5], "evacuation_origin": [.5, 92, .5],
            "excluded_until": {}, "retry_after": 0, "blocked_cells": []}
        return helper, runner, brain, world

    def test_competition_selection_allows_local_canopy_descent_before_scoring_tasks(self):
        helper, runner, brain, world = self.runner(objective="competition")
        result = helper.step(runner)
        self.assertTrue(result.success, result)
        self.assertEqual(world.status["pos"], [-.5, 91, .5])
        self.assertIsNone(runner.competition.snapshot()["active_goal_id"])
        self.assertEqual(runner.competition.snapshot()["score"]["raw"], 0)
        self.assertEqual(brain.requests, [])

    def test_unknown_side_leaf_write_preserves_pending_and_does_not_move(self):
        from core.client import APIError
        world = CanopyColumnsWorld()
        def dig(*args, **kwargs):
            world.calls.append(("dig",))
            raise APIError("request_timeout", "Unknown leaf write")
        world.dig = dig
        helper, runner, brain, world = self.runner(world=world)
        result = helper.step(runner)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertTrue(runner.paused)
        self.assertIsNotNone(runner.journal.state["pending"])
        for _ in range(3): runner.tick(automatic=True)
        self.assertEqual(len([row for row in world.calls if row[0] == "dig"]), 1)
        self.assertFalse([row for row in world.calls if row[0] == "move"])
        self.assertEqual(brain.requests, [])

    def test_leaf_support_alone_marks_canopy_recovery_even_when_first_rejections_are_air(self):
        from agent.survival_planner import Decision
        from skills import SkillResult
        helper, runner, _, _ = self.runner()
        runner.context.pop("retreat_recovery")
        obs = runner.collector.collect()
        decision = Decision("retreat_from", {"pos": [-10.5, 88, .5]}, "Old retreat", "safety")
        runner._retreat_state(obs, decision.args["pos"])
        result = SkillResult(False, "No complete route", {"origin_support": {"center_name": "birch_leaves"},
            "blocked_cells": [{"pos": [0, 90, -1], "name": "air", "reason": "unsupported_ground"}]}, "unsafe")
        self.assertTrue(runner._retreat_result(decision, result, obs))
        self.assertTrue(runner.context["retreat_recovery"]["canopy_blocked"])

    def test_harness_cuts_side_steps_and_reaches_real_ground_without_model(self):
        helper = local_runtime.LocalFirstRuntimeTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        world = CanopyColumnsWorld()
        runner, brain = helper.harness(objective="daily", world=world)
        runner.context["retreat_recovery"] = {"dimension": "overworld", "evacuating": True,
            "canopy_blocked": True, "threat_pos": [-10.5, 88, .5], "evacuation_origin": [.5, 92, .5],
            "excluded_until": {}, "retry_after": 0, "blocked_cells": []}
        for height in (91, 90, 88):
            result = helper.step(runner)
            self.assertTrue(result.success, result)
            self.assertEqual(world.status["pos"][1], height)
            self.assertIsNone(runner.journal.state["pending"])
        self.assertNotIn("retreat_recovery", runner.context)
        self.assertTrue(result.data["retreat_complete"])
        self.assertEqual(brain.requests, [])
        self.assertEqual(len([row for row in world.calls if row[0] == "dig"]), 2)
        self.assertEqual(len([row for row in world.calls if row[0] == "move"]), 3)
        self.assertEqual(world.blocks[(0, 91, 0)]["name"], "birch_leaves")


if __name__ == "__main__":
    unittest.main()
