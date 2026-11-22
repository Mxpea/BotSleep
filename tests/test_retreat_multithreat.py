"""Fresh multi-hostile retreat choices; no game or model connection."""

import copy
import math
import unittest

from core.client import APIError
from skills.survival import SurvivalSkills
from test_canopy_escape import terrain
from test_survival_skills import World


def hostile(name, pos, entity_id):
    return {"id": entity_id, "name": name, "type": "hostile", "pos": pos}


class MultiThreatRetreatTests(unittest.TestCase):
    def retreat(self, world, primary=None):
        return SurvivalSkills(world).execute("retreat_from", {"pos": primary or [-3, 64, .5]})

    def assert_single_move(self, world, result):
        self.assertTrue(result.success, result)
        self.assertEqual([row[0] for row in world.writes()], ["move_to"])
        self.assertEqual(world.writes()[0][2]["range"], 0)
        self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)
        self.assertLessEqual(result.data["terrain_search"]["nodes"], 64)

    def test_primary_inside_danger_circle_can_still_retreat(self):
        world = World()
        world.entities = [hostile("skeleton", [-3, 64, .5], 1)]
        world.status["nearest_hostile"] = {"id": 1, "name": "skeleton", "dist": 3.5}
        result = self.retreat(world)
        self.assert_single_move(world, result)
        self.assertEqual(result.data["target"], [1.5, 64, .5])
        self.assertTrue(result.data["threat_assessment"]["all_threats_evaluated"])
        self.assertEqual(len([row for row in world.calls if row[0] == "get_entities"]), 2)

    def test_opposed_creeper_and_skeleton_take_perpendicular_route(self):
        world = World()
        world.entities = [hostile("creeper", [-3, 64, .5], 1), hostile("skeleton", [6.5, 64, .5], 2)]
        original = list(world.status["pos"])
        result = self.retreat(world)
        self.assert_single_move(world, result)
        self.assertEqual(result.data["target"][0], original[0])
        self.assertNotEqual(result.data["target"][2], original[2])
        for point in result.data["planned_path"]:
            for row in world.entities:
                self.assertGreaterEqual(math.dist(point, row["pos"]) + .1, math.dist(original, row["pos"]))
        self.assertGreater(result.data["terrain_search"]["multi_threat_rejections"], 0)

    def test_shooter_margin_ranks_actual_first_step(self):
        world = World()
        world.entities = [hostile("creeper", [-3, 64, .5], 1), hostile("skeleton", [.5, 64, 8.5], 2)]
        result = self.retreat(world)
        self.assert_single_move(world, result)
        self.assertEqual(result.data["target"], [.5, 64, -.5])

    def test_secondary_outside_circle_cannot_be_entered(self):
        world = World()
        world.entities = [hostile("creeper", [-3, 64, .5], 1), hostile("skeleton", [17, 64, .5], 2)]
        result = self.retreat(world)
        self.assert_single_move(world, result)
        self.assertTrue(all(math.dist(point, world.entities[1]["pos"]) > 16 for point in result.data["planned_path"]))

    def test_only_corridor_toward_secondary_is_rejected_without_write(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 70, 0, "oak_leaves"), (2, 70, 0, "dirt")])
        world.entities = [hostile("creeper", [-3, 70, .5], 1), hostile("skeleton", [6.5, 70, .5], 2)]
        result = self.retreat(world, [-3, 70, .5])
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_saturated_entity_scan_refuses_claim_of_complete_threats(self):
        world = World()
        world.entities = [hostile("zombie", [-3, 64, .5], index) for index in range(40)]
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertIn("limit", result.message)
        self.assertFalse(world.writes())

    def test_new_secondary_at_final_scan_rejects_planned_route(self):
        world = World()
        original = world.get_entities
        scans = 0
        def scan(*args, **kwargs):
            nonlocal scans
            scans += 1
            if scans == 2:
                world.entities = [hostile("skeleton", [4.5, 64, .5], 2)]
            return original(*args, **kwargs)
        world.get_entities = scan
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(scans, 2)
        self.assertFalse(world.writes())

    def test_final_status_new_hostile_is_not_confused_with_known_primary(self):
        world = World()
        world.entities = [hostile("skeleton", [-3, 64, .5], 1)]
        original = world.get_status
        def status(**kwargs):
            if len([row for row in world.calls if row[0] == "get_entities"]) == 2:
                world.status["nearest_hostile"] = {"id": 2, "name": "skeleton", "dist": 3}
            return original(**kwargs)
        world.get_status = status
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_latest_position_rechecks_secondary_margin(self):
        world = World()
        world.entities = [hostile("creeper", [-3, 64, .5], 1), hostile("skeleton", [6.5, 64, .5], 2)]
        original = world.get_status
        def status(**kwargs):
            if len([row for row in world.calls if row[0] == "get_entities"]) == 2:
                world.status["pos"] = [.1, 64, .5]
            return original(**kwargs)
        world.get_status = status
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_transport_unknown_does_not_stop_or_try_another_candidate(self):
        world = World()
        def lost(pos, options):
            raise APIError("request_timeout", "Lost reply", details={"action_state": "unknown"})
        world.move_effect = lost
        result = self.retreat(world)
        self.assertFalse(result.success)
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_sub_one_and_a_half_progress_can_be_a_verified_leaf_relay(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 71, 0, "oak_leaves")])
        result = self.retreat(world, [-3, 70, -3])
        self.assert_single_move(world, result)
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["target"], [1.5, 71, .5])
        self.assertLess(math.hypot(4.5, 3.5) - math.hypot(3.5, 3.5), 1.5)

    def test_distant_secondary_skeleton_beyond_7_5_blocks_does_not_paralyze_escape_from_close_spider(self):
        world = World()
        # Close spider to the west, wall to the east/south, distant skeleton 9m NW leaving north corridor >= 8m from skeleton.
        world.status["pos"] = [0.5, 64, 0.5]
        world.set_block([1, 64, 0], "stone")
        world.set_block([1, 65, 0], "stone")
        world.set_block([0, 64, 1], "stone")
        world.set_block([0, 65, 1], "stone")
        world.entities = [hostile("spider", [-1.0, 64, 0.5], 1), hostile("skeleton", [-6.0, 64, -7.0], 2)]
        result = self.retreat(world, [-1.0, 64, 0.5])
        self.assert_single_move(world, result)
        self.assertGreater(math.hypot(result.data["target"][0] + 1.0, result.data["target"][2] - 0.5), 1.5)


class UpwardCanopyRelayTests(unittest.TestCase):
    def escape(self, world, recovery=None):
        return SurvivalSkills(world).execute("canopy_escape", {}, recovery_context=recovery)

    def test_single_verified_upward_exit_is_relay_not_ground(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 71, 0, "oak_leaves")])
        result = self.escape(world)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["target"], [1.5, 71, .5])
        self.assertFalse(result.data["ground_route_verified"])
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "relay")
        self.assertEqual(world.writes(), [("move_to", [1.5, 71, .5], {"range": 0, "timeout_ms": 10000})])

    def test_flat_relay_is_preferred_over_upward_dead_end(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 71, 0, "oak_leaves"), (0, 70, 1, "oak_leaves")])
        result = self.escape(world)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["target"], [.5, 70, 1.5])

    def test_upward_exit_still_checks_ceiling_and_recent_targets(self):
        for blocked in (True, False):
            world = terrain([(0, 70, 0, "oak_leaves"), (1, 71, 0, "oak_leaves")])
            recovery = {"recent_targets": [[1, 71, 0]]} if not blocked else {}
            if blocked:
                world.set_block([0, 72, 0], "stone")
            result = self.escape(world, recovery)
            self.assertEqual(result.error_code, "unsafe")
            self.assertFalse(world.writes())


if __name__ == "__main__":
    unittest.main()
