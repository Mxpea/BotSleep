"""Whole-house material demand stays local, fresh and bounded; no live I/O."""

import unittest
from unittest.mock import patch

from core.autonomous_tasks import AutonomousTaskController
from skills import SkillResult
from test_autonomous_tasks import apply_atomic, verified, world


class ShelterStockpileTests(unittest.TestCase):
    def setup_goal(self, counts=None, missing=43, **limits):
        obs = world({"oak_planks": 7} if counts is None else counts)
        obs["daily_evidence"]["base_site"] = verified(pos=[0, 64, 0], orientation="north")
        obs["shelter"] = {
            "fresh": True, "dimension": "overworld", "pos": [0, 64, 0], "orientation": "north",
            "geometry": {"floor_verified": True, "exit_verified": True},
            "hazards": [], "occupied_blueprint_cells": [], "missing_blocks": missing,
            "needs_position": True, "stand_candidates": [[-.5, 64, -.5]],
            "door": {"frame_verified": False}, "needs_door": {"pos": [0, 64, -2], "reason": "door_missing"},
        }
        obs["blocks"] = [{"name": "oak_log", "pos": [3 + i % 4, 64, i // 4], "can_see": True}
                         for i in range(20)]
        controller = AutonomousTaskController({"autonomy": limits})
        controller.start({"task": "shelter"}, obs)
        return controller, obs

    def test_live_remaining_43_blocks_collects_12_logs_before_any_return(self):
        controller, obs = self.setup_goal()
        for count in range(12):
            decision = controller.next_action(obs)
            self.assertEqual(decision.name, "gather_block", (count, decision))
            result = apply_atomic(obs, decision)
            controller.record_result(decision, result, obs)
            self.assertEqual(obs["counts"]["oak_log"], count + 1)
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "position_for_task")
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)
        self.assertEqual(controller.snapshot()["steps"], 12)

    def test_inventory_and_newly_completed_wall_reduce_deficit_immediately(self):
        controller, obs = self.setup_goal({"oak_log": 12, "oak_planks": 7})
        self.assertEqual(controller.next_action(obs).name, "position_for_task")
        controller, obs = self.setup_goal({"oak_log": 1, "oak_planks": 7})
        obs["shelter"]["missing_blocks"] = 1  # Another builder's fresh verified progress.
        self.assertEqual(controller.next_action(obs).name, "position_for_task")

    def test_mixed_wall_materials_and_held_or_existing_door_do_not_overgather(self):
        for door in ("held", "existing"):
            with self.subTest(door=door):
                controller, obs = self.setup_goal({"dirt": 10, "cobblestone": 10, "oak_planks": 23})
                if door == "held":
                    obs["counts"]["oak_door"] = 1
                else:
                    obs["shelter"]["door"] = {"frame_verified": True, "open": True}
                self.assertEqual(controller.next_action(obs).name, "position_for_task")

    def test_held_or_fresh_nearby_table_removes_its_four_plank_reserve(self):
        for table in ("held", "world"):
            with self.subTest(table=table):
                controller, obs = self.setup_goal({"oak_planks": 49})
                if table == "held":
                    obs["counts"]["crafting_table"] = 1
                else:
                    obs["blocks"].append({"name": "crafting_table", "pos": [1, 64, 0]})
                self.assertEqual(controller.next_action(obs).name, "position_for_task")
        controller, obs = self.setup_goal({"oak_planks": 49})
        self.assertEqual(controller.next_action(obs).name, "gather_block")

    def test_door_six_planks_are_reserved_in_one_species(self):
        controller, obs = self.setup_goal({"cobblestone": 43, "oak_planks": 4, "birch_planks": 5, "crafting_table": 1})
        obs["blocks"].append({"name": "birch_log", "pos": [3, 64, -1], "can_see": True})
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["expected_name"]), ("gather_block", "birch_log"))
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertEqual(controller.next_action(obs).name, "position_for_task")

    def test_unavailable_or_blocked_door_species_can_switch_to_observed_wood(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                controller, obs = self.setup_goal({"cobblestone": 43, "birch_planks": 4, "crafting_table": 1})
                context = {}
                if blocked:
                    obs["blocks"].append({"name": "birch_log", "pos": [2, 64, 0], "can_see": True})
                    context["blocked_targets"] = [controller.rules._key([2, 64, 0])]
                for _ in range(2):
                    decision = controller.next_action(obs, context)
                    self.assertEqual(decision.args.get("expected_name"), "oak_log")
                    controller.record_result(decision, apply_atomic(obs, decision), obs, context)
                self.assertEqual(controller.next_action(obs, context).name, "position_for_task")

    def test_nonbuilding_planks_can_fund_door_but_never_count_as_wall_material(self):
        controller, obs = self.setup_goal({"pale_oak_planks": 64})
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["expected_name"]), ("gather_block", "oak_log"))
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        obs["counts"]["oak_log"] = 11
        self.assertEqual(controller.next_action(obs).name, "position_for_task")

    def test_changed_or_incomplete_site_cannot_create_stockpile_authority(self):
        cases = ({"fresh": False}, {"pos": [10, 64, 0]}, {"dimension": "the_nether"},
                 {"orientation": "east"}, {"hazards": ["lava"]}, {"occupied_blueprint_cells": [[1, 64, 1]]},
                 {"missing_blocks": True}, {"missing_blocks": 56}, {"missing_blocks": -1},
                 {"geometry": []}, {"door": "unknown"},
                 {"geometry": {"floor_verified": True, "exit_verified": False}})
        for change in cases:
            with self.subTest(change=change):
                controller, obs = self.setup_goal()
                obs["shelter"].update(change)
                self.assertEqual(controller._shelter_stockpile(obs, {}), (None, False))

    def test_missing_resource_uses_no_return_or_unobserved_gather(self):
        controller, obs = self.setup_goal()
        obs["blocks"] = []
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertEqual(controller.snapshot()["steps"], 0)

    def test_known_failure_changes_target_and_unknown_never_replays(self):
        controller, obs = self.setup_goal()
        first = controller.next_action(obs)
        controller.record_result(first, SkillResult(False, "Blocked", {"executed": False}, "stalled"), obs)
        second = controller.next_action(obs)
        self.assertNotEqual(second.args["pos"], first.args["pos"])
        controller.record_result(second, SkillResult(False, "Unknown", {"action_state": "unknown"}, "action_unconfirmed"), obs)
        self.assertIsNone(controller.next_action(obs))
        restored = AutonomousTaskController(saved=controller.state())
        self.assertIsNone(restored.next_action(obs))
        self.assertEqual(restored.snapshot()["status"], "unknown")

    def test_no_fresh_inventory_life_dimension_danger_or_budget_bypass(self):
        for change in ({"on_ground": False}, {"health": 2}, {"food": 2}, {"is_day": False},
                       {"nearest_hostile": {"name": "skeleton", "dist": 2}},
                       {"last_death": {"t": 999}}, {"dimension": "the_nether"}):
            with self.subTest(change=change):
                controller, obs = self.setup_goal()
                obs["status"].update(change)
                self.assertIsNone(controller.next_action(obs))
                self.assertEqual(controller.snapshot()["steps"], 0)
        for key in ("fresh", "inventory_fresh"):
            controller, obs = self.setup_goal()
            obs[key] = False
            self.assertIsNone(controller.next_action(obs))
        for context in ({"pending": {"anything": True}}, {"life_generation": 1}):
            controller, obs = self.setup_goal()
            self.assertIsNone(controller.next_action(obs, context))
        controller, obs = self.setup_goal(max_steps=1)
        decision = controller.next_action(obs)
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["reason"], "task_budget_exhausted")
        controller, obs = self.setup_goal()
        with patch("core.autonomous_tasks.time.time", return_value=controller.state()["started_at"] + 601):
            self.assertIsNone(controller.next_action(obs))

    def test_restart_recomputes_demand_from_new_site_and_actual_inventory(self):
        controller, obs = self.setup_goal()
        decision = controller.next_action(obs)
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        saved = controller.state()
        self.assertNotIn("shelter_stockpile", saved)  # No stale action or quantity queue.
        restored = AutonomousTaskController(saved=saved)
        obs["counts"]["oak_log"] = 12
        self.assertEqual(restored.next_action(obs).name, "position_for_task")


if __name__ == "__main__":
    unittest.main()
