"""Daily controller, real stair skills and crafting inventory share one goal."""

import copy
import unittest

from core.autonomous_tasks import AutonomousTaskController
from core.skill_registry import resolve_skill_runner, skills_for_objective
from skills import SkillResult
from skills.starter_staircase import StarterStaircaseSkills
from skills.survival import SurvivalSkills
from test_autonomous_tasks import world
from test_daily_skills import envelope
from test_starter_staircase import StairWorld


class CraftWorld(StairWorld):
    def __init__(self):
        super().__init__()
        self.counts["stick"] = 2
        self.block([2, 64, 2], "crafting_table")

    def observe(self):
        obs = world(self.counts, **copy.deepcopy(self.status))
        obs.update(local_geometry_origin=list(self.status["pos"]), blocks=[
            {"name": "crafting_table", "pos": [2, 64, 2]},
            {"name": "stone", "pos": [3, 60, 4], "dist": 6.1}])
        return obs

    def craft(self, item, count=1):
        assert item == "stone_pickaxe" and count == 1
        assert self.counts["cobblestone"] >= 3 and self.counts["stick"] >= 2
        assert self.status["pos"][1] == 64
        self.counts["cobblestone"] -= 3
        self.counts["stick"] -= 2
        self.counts[item] = self.counts.get(item, 0) + 1
        return envelope({"crafted": item})


class StarterMiningTaskTests(unittest.TestCase):
    def test_buried_stone_to_surface_crafting_through_each_daily_goal(self):
        for args in ({"task": "craft", "item": "stone_pickaxe"}, {"task": "starter_tools"},
                     {"task": "gather", "resource": "cobblestone", "count": 3}):
            with self.subTest(args=args):
                server = CraftWorld()
                ctl = AutonomousTaskController({"objective": "daily"})
                ctl.start(args, server.observe())
                mine = StarterStaircaseSkills(server)
                crafted = False
                returned = False
                ops = []
                sources = type("Runners", (), {"daily": True, "starter_staircase_skills": mine,
                                               "skills": SurvivalSkills(server)})()
                for _ in range(60):
                    obs = server.observe()
                    step = ctl.next_action(obs)
                    if step is None:
                        break
                    runner = resolve_skill_runner(sources, step.name)
                    result = runner.execute(step.name, step.args)
                    self.assertTrue(result.success, (step, result))
                    ctl.record_result(step, result, server.observe())
                    ops.append(result.data.get("operation", step.name))
                    if not returned and result.data.get("depth") == 2:
                        ctl = AutonomousTaskController({"objective": "daily"}, saved=ctl.state())
                        returned = True
                    if step.name == "craft_item":
                        crafted = True
                        break
                self.assertIn("dig", ops)
                self.assertIn("ascend", ops)
                self.assertTrue(returned)
                self.assertEqual(server.status["pos"][1], 64)
                self.assertEqual(ctl.snapshot()["ai_escalations"], 0)
                if args["task"] == "gather":
                    self.assertEqual(ctl.snapshot()["status"], "completed")
                    self.assertGreaterEqual(server.counts["cobblestone"], 3)
                else:
                    self.assertTrue(crafted)
                    self.assertEqual(server.counts["stone_pickaxe"], 1)

    def test_visible_stone_precedes_survey_and_internal_names_stay_private(self):
        server = CraftWorld()
        obs = server.observe()
        obs["blocks"].append({"name": "stone", "pos": [2, 64, 0], "can_see": True, "can_dig": True})
        ctl = AutonomousTaskController({"objective": "daily"})
        ctl.start({"task": "craft", "item": "stone_pickaxe"}, obs)
        self.assertEqual(ctl.next_action(obs).name, "gather_block")
        for mode in ("starter", "daily", "competition"):
            self.assertNotIn("step_starter_staircase", skills_for_objective(mode))

    def test_protect_home_exit_and_move_before_resurveying_a_rejected_stance(self):
        w = CraftWorld()
        obs = w.observe()
        ctl = AutonomousTaskController({"objective": "daily"})
        ctl.start({"task": "gather", "resource": "cobblestone", "count": 3}, obs)
        context = {"base_project": {"pos": [0, 64, 0], "dimension": "overworld"}}
        plan = ctl.next_action(obs, context)
        self.assertEqual(plan.args["protected_pos"], [0, 64, 0])
        result = StarterStaircaseSkills(w).execute(plan.name, plan.args)
        self.assertFalse(result.success)
        self.assertFalse(result.data["executed"])
        ctl.record_result(plan, result, obs)
        obs["safe_stand_candidates"] = [{"pos": [1.5, 64, .5], "support": "grass_block"}]
        self.assertEqual(ctl.next_action(obs, context).name, "position_for_task")

    def test_unknown_dig_cannot_be_replaced_by_movement_or_a_new_site(self):
        w = CraftWorld()
        obs = w.observe()
        ctl = AutonomousTaskController({"objective": "daily"})
        ctl.start({"task": "craft", "item": "stone_pickaxe"}, obs)
        plan = ctl.next_action(obs)
        ctl.record_result(plan, StarterStaircaseSkills(w).execute(plan.name, plan.args), obs)
        cut = ctl.next_action(obs)
        ctl.record_result(cut, SkillResult(False, "server outcome unresolved",
            {"executed": True, "action_state": "unknown"}, "action_unconfirmed"), obs)
        self.assertIsNone(ctl.next_action(obs))
        self.assertEqual(ctl.snapshot()["status"], "unknown")
        self.assertIsNone(AutonomousTaskController(saved=ctl.state()).next_action(obs))

    def test_night_or_enemy_preempts_mining_before_any_write(self):
        for unsafe in ({"is_day": False}, {"nearest_hostile": {"name": "zombie", "dist": 3}}):
            with self.subTest(state=unsafe):
                w = CraftWorld()
                obs = w.observe()
                ctl = AutonomousTaskController({"objective": "daily"})
                ctl.start({"task": "craft", "item": "stone_pickaxe"}, obs)
                obs["status"].update(unsafe)
                self.assertIsNone(ctl.next_action(obs))
                self.assertEqual(ctl.snapshot()["status"], "suspended")
