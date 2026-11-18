"""Autonomous workspaces require observed cells and leave shelter access clear."""

import unittest
from unittest.mock import patch

from agent.survival_planner import Decision
from core.autonomous_tasks import AutonomousTaskController
from skills import SkillResult
from test_autonomous_tasks import apply_atomic, verified, world


class AutonomousWorkstationTests(unittest.TestCase):
    def setup_goal(self, item="wooden_pickaxe", **limits):
        obs = world({"crafting_table": 1, "oak_planks": 6, "stick": 2})
        obs["blocks"] = []
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [1.5, 64, .5], "support": "grass_block"}]
        obs["placement_candidates"] = [{"pos": [0, 64, 1], "support": "grass_block"}]
        controller = AutonomousTaskController({"autonomy": limits})
        controller.start({"task": "craft", "item": item, "count": 1}, obs)
        return controller, obs

    def test_observed_workspace_then_pickaxe_and_door_reuse_same_table(self):
        controller, obs = self.setup_goal()
        selected = []
        for _ in range(3):
            decision = controller.next_action(obs)
            if decision is None:
                break
            selected.append(decision)
            controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertEqual([(d.name, d.args.get("item")) for d in selected],
                         [("place_item", "crafting_table"), ("craft_item", "wooden_pickaxe")])
        self.assertEqual(selected[0].args["pos"], [0, 64, 1])
        self.assertEqual(controller.snapshot()["status"], "completed")
        obs["counts"]["oak_planks"] = 6
        controller.start({"task": "craft", "item": "oak_door", "count": 1}, obs)
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["item"]), ("craft_item", "oak_door"))
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "completed")

    def test_missing_candidate_repositions_once_then_places_newly_observed_cell(self):
        controller, obs = self.setup_goal(max_explore_steps=1)
        obs["placement_candidates"] = []
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "position_for_task")
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertEqual(controller.state()["explore_steps"], 1)
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["placement_candidates"] = [{"pos": [2, 64, 1], "support": "grass_block"}]
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["pos"]), ("place_item", [2, 64, 1]))
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)

    def test_unknown_placement_never_retries_or_replaces_after_restart(self):
        for code in ("request_timeout", "action_unconfirmed", "invalid_response"):
            with self.subTest(code=code):
                controller, obs = self.setup_goal()
                decision = controller.next_action(obs)
                controller.record_result(decision, SkillResult(False, "Unknown placement", {}, code), obs)
                obs["placement_candidates"] = [{"pos": [-1, 64, 0], "support": "grass_block"}]
                self.assertIsNone(controller.next_action(obs))
                restored = AutonomousTaskController(saved=controller.state())
                self.assertIsNone(restored.next_action(obs))
                self.assertEqual(restored.snapshot()["status"], "unknown")

    def test_stale_geometry_or_unknown_support_never_authorizes_table(self):
        for change in ({"local_geometry_origin": [9, 64, 9]}, {"local_geometry_origin": None},
                       {"placement_candidates": []},
                       {"placement_candidates": [{"pos": [0, 64, 1]}]},
                       {"placement_candidates": [{"pos": [0, 64, 1], "support": "leaf_litter"}]},
                       {"placement_candidates": [{"pos": [0, 64, 1], "support": "air"}]}):
            with self.subTest(change=change):
                controller, obs = self.setup_goal()
                obs.update(change)
                decision = controller.next_action(obs)
                self.assertTrue(decision is None or decision.name == "position_for_task", decision)
                self.assertEqual(controller.state()["local_failures"], 0)

    def test_unsafe_cells_filtered_before_selecting_observed_table_cell(self):
        controller, obs = self.setup_goal()
        obs["placement_candidates"] = [
            {"pos": pos, "support": "grass_block"} for pos in
            ([0, 64, 0], [0, 65, 0], [.5, 64, 1], [8, 64, 0], [-1, 64, 0], [0, 64, 1])]
        context = {"blocked_targets": [controller.rules._key([-1, 64, 0])]}
        decision = controller.next_action(obs, context)
        self.assertEqual(decision.args["pos"], [0, 64, 1])

    def test_generic_workspace_wait_cannot_hide_an_observed_legal_cell(self):
        controller, obs = self.setup_goal()
        with patch.object(controller.rules, "_workspace", return_value=Decision(
                "wait", {"seconds": 2}, "Old offset list has no candidate", "crafting_table")):
            decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["pos"]), ("place_item", [0, 64, 1]))

    def test_workstation_reserves_rotated_blueprint_and_door_corridor(self):
        for orientation, direction in (("north", (0, -1)), ("south", (0, 1)),
                                       ("east", (1, 0)), ("west", (-1, 0))):
            with self.subTest(orientation=orientation):
                controller, obs = self.setup_goal("oak_door")
                obs["daily_evidence"]["base_site"] = verified(pos=[0, 64, 0], orientation=orientation)
                dx, dz = direction
                corridor = [[dx * n, 64, dz * n] for n in (1, 2, 3)]
                bed_side = [[dz + dx * n, 64, -dx + dz * n] for n in (-1, 0, 1)]
                side = [-dz, 64, dx]
                obs["placement_candidates"] = [{"pos": p, "support": "grass_block"}
                                               for p in corridor + bed_side + [side]]
                decision = controller.next_action(obs)
                self.assertEqual((decision.name, decision.args["pos"]), ("place_item", side))

    def test_missing_workspace_is_bounded_without_blind_place_failures(self):
        controller, obs = self.setup_goal(max_explore_steps=1)
        obs["placement_candidates"] = []
        decision = controller.next_action(obs)
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [2.5, 64, .5], "support": "grass_block"}]
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertEqual(controller.state()["steps"], 1)
        self.assertEqual(controller.state()["local_failures"], 0)


if __name__ == "__main__":
    unittest.main()
