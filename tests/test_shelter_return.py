"""Fresh bounded return proposals, finite detours and real positioning guards."""

import copy
import math
import unittest

from core.autonomous_tasks import AutonomousTaskController
from core.shelter_return import MAX_SIDE_STEPS, choose_shelter_return
from skills.positioning import PositioningSkills
from test_autonomous_tasks import world
import test_positioning_steps as positioning_fixture


class ShelterReturnTests(unittest.TestCase):
    ANCHOR = {"pos": [0, 64, 0], "dimension": "overworld"}

    def observe(self, origin, positions):
        obs = world(pos=list(origin))
        obs["blocks"] = []
        obs["local_geometry_origin"] = list(origin)
        obs["safe_stand_candidates"] = [{"pos": list(pos), "support": "grass_block"} for pos in positions]
        return obs

    def choose(self, obs, *, visited=(), context=None, anchor=None, allow_step=None):
        return choose_shelter_return(obs, anchor or self.ANCHOR, context or {}, visited=visited,
                                     allow_step=allow_step or (lambda *args: False))

    def test_far_anchor_selects_fresh_waypoint_not_anchor_or_blind_pathfinder(self):
        obs = self.observe([27.5, 64, .5], [[26.5, 64, .5], [25.5, 64, .5], [28.5, 64, .5]])
        action = self.choose(obs)
        self.assertEqual((action.name, action.args["pos"], action.goal),
                         ("position_for_task", [25.5, 64, .5], "shelter_return"))
        self.assertLessEqual(math.dist(obs["status"]["pos"], action.args["pos"]), 3)

    def test_27_block_return_can_take_more_than_eight_fresh_steps_without_global_search(self):
        origin, visited, steps = [27.5, 64, .5], [], 0
        while math.dist(origin, self.ANCHOR["pos"]) > 1:
            obs = self.observe(origin, [[origin[0] - 1, 64, .5], [origin[0] - 2, 64, .5]])
            decision = self.choose(obs, visited=visited)
            self.assertIsNotNone(decision)
            visited.append(origin)
            origin = decision.args["pos"]
            steps += 1
            self.assertLessEqual(steps, 32)
        self.assertGreater(steps, 8)
        self.assertEqual(steps, 14)

    def test_direct_progress_precedes_sideways_and_visited_or_failed_targets_are_excluded(self):
        obs = self.observe([5.5, 64, .5], [[4.5, 64, .5], [5.5, 64, 1.5], [5.5, 64, -.5]])
        self.assertEqual(self.choose(obs).args["pos"], [4.5, 64, .5])
        result = self.choose(obs, visited=[[4.5, 64, .5]], context={"blocked_targets": ["5,64,-1"]})
        self.assertEqual(result.args["pos"], [5.5, 64, 1.5])

    def test_finite_lateral_detour_goes_around_obstruction_then_resumes_progress(self):
        route = [([5.5, 64, .5], [[5.5, 64, 1.5]], [5.5, 64, 1.5]),
                 ([5.5, 64, 1.5], [[5.5, 64, .5], [5.5, 64, 2.5]], [5.5, 64, 2.5]),
                 ([5.5, 64, 2.5], [[5.5, 64, 1.5], [4.5, 64, 2.5]], [4.5, 64, 2.5]),
                 ([4.5, 64, 2.5], [[3.5, 64, 2.5]], [3.5, 64, 2.5])]
        visited = []
        for origin, candidates, expected in route:
            action = self.choose(self.observe(origin, candidates), visited=visited)
            self.assertEqual(action.args["pos"], expected)
            visited.append(origin)

    def test_side_detour_cannot_exceed_count_or_distance_from_best_progress(self):
        # Nine same-radius moves cannot renew the lateral allowance indefinitely.
        visited = [[20.5, 64, z + .5] for z in range(-4, 5)]
        obs = self.observe(visited[-1], [[20.5, 64, 5.5]])
        self.assertEqual(MAX_SIDE_STEPS, 8)
        self.assertIsNone(self.choose(obs, visited=visited))
        obs = self.observe([10.5, 64, .5], [[11.5, 64, .5]])
        self.assertIsNone(self.choose(obs, visited=[[7.5, 64, .5]]))

    def test_short_neighbor_allowed_but_same_cell_far_uncentred_or_unproven_step_rejected(self):
        obs = self.observe([.6, 64, .5], [[1.5, 64, .5]])
        far_anchor = {"pos": [20, 64, 0], "dimension": "overworld"}
        self.assertEqual(self.choose(obs, anchor=far_anchor).args["pos"], [1.5, 64, .5])
        for target in ([.5, 64, .5], [1.4, 64, .5], [1.5, 64.2, .5], [4.5, 64, .5], [1.5, 65, .5]):
            with self.subTest(target=target):
                self.assertIsNone(self.choose(self.observe([.6, 64, .5], [target]), anchor=far_anchor))

    def test_legal_single_step_uses_existing_filter_and_real_positioning_verification(self):
        fixture = positioning_fixture.PositioningStepTests()
        for dy in (-1, 1):
            client, target = fixture.world(dy=dy)
            obs = self.observe(client.status["pos"], [target])
            obs["status"].update(client.status)
            controller = AutonomousTaskController()
            controller.start({"task": "shelter"}, obs)
            anchor = {"pos": [10, 64 + dy, 0], "dimension": "overworld"}
            action = self.choose(obs, anchor=anchor, allow_step=controller._local_shelter_step)
            self.assertIsNotNone(action)
            result = PositioningSkills(client).execute(action.name, action.args)
            self.assertTrue(result.success, result)
            self.assertEqual([entry[0] for entry in client.calls if entry[0] in {"move", "stop", "dig", "place"}], ["move"])

    def test_no_stale_missing_hostile_unknown_or_non_survival_observation(self):
        original = self.observe([5.5, 64, .5], [[4.5, 64, .5]])
        for change in ({"fresh": False}, {"inventory_fresh": False}, {"observation_scope": "safety"},
                       {"local_geometry_origin": [9, 64, .5]}, {"entities": None}, {"counts": None},
                       {"entities": [{"type": "passive", "name": "cow"}] * 40}):
            obs = copy.deepcopy(original)
            obs.update(change)
            self.assertIsNone(self.choose(obs))
        for change in ({"alive": False}, {"ready": False}, {"gamemode": "creative"}, {"busy": "pathfinding"},
                       {"on_ground": False}, {"is_day": False}, {"food": 2}, {"health": 0}, {"on_fire": True},
                       {"nearest_hostile": {"name": "skeleton", "dist": 12}},
                       {"nearest_hostile": {"name": "skeleton", "dist": 17}},
                       {"nearest_hostile": {"name": "zombie"}}):
            obs = copy.deepcopy(original)
            obs["status"].update(change)
            self.assertIsNone(self.choose(obs))
        for context in ({"pending": {"move": True}}, {"action_state": "unknown"},
                        {"last_result": {"error_code": "action_unconfirmed"}},
                        {"last_result": {"data": {"action_state": "unknown"}}}):
            self.assertIsNone(self.choose(original, context=context))

    def test_all_hostiles_and_nearby_hazards_filter_even_otherwise_closer_stance(self):
        obs = self.observe([20.5, 64, .5], [[18.5, 64, .5], [20.5, 64, 1.5]])
        obs["entities"] = [{"id": 1, "type": "hostile", "name": "skeleton", "pos": [3, 64, .5]}]
        action = self.choose(obs)
        self.assertEqual(action.args["pos"], [20.5, 64, 1.5])
        obs["blocks"] = [{"name": "lava", "pos": [20, 64, 2]}]
        self.assertIsNone(self.choose(obs))
        obs["entities"] = [{"type": "hostile", "name": "skeleton"}]
        self.assertIsNone(self.choose(obs))

    def test_foreign_anchor_unknown_support_and_invalid_history_never_supply_permission(self):
        obs = self.observe([5.5, 64, .5], [[4.5, 64, .5]])
        self.assertIsNone(self.choose(obs, anchor={"pos": [0, 64, 0], "dimension": "the_nether"}))
        obs["safe_stand_candidates"][0]["support"] = "oak_leaves"
        self.assertIsNone(self.choose(obs))
        obs["safe_stand_candidates"][0]["support"] = "grass_block"
        for history in ([[None, 0, 0]], [[float("nan"), 0, 0]], [[0, 64, 0]] * 65):
            self.assertIsNone(self.choose(obs, visited=history))


if __name__ == "__main__":
    unittest.main()
