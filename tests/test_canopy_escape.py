"""Verified canopy relays: local geometry only, no server/model connection."""

import copy
import json
import math
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.client import APIError
from skills.daily_survival import DailySurvivalSkills
from skills.survival import SurvivalSkills
from test_survival_skills import World, envelope


def terrain(nodes):
    world = World()
    world.ground = "air"
    world.status.update(pos=[nodes[0][0] + .5, nodes[0][1], nodes[0][2] + .5], health=6, food=17,
                        gamemode="survival", last_death=None)
    for x, y, z, name in nodes:
        world.set_block([x, y - 1, z], name, properties={"waterlogged": False})
    return world


class CanopyEscapeTests(unittest.TestCase):
    def world(self):
        return terrain([(0, 70, 0, "oak_leaves"), (1, 70, 0, "birch_leaves"),
                        (2, 69, 0, "oak_leaves"), (3, 67, 0, "dirt")])

    def execute(self, world, context=None, cancel=None, cls=SurvivalSkills):
        return cls(world).execute("canopy_escape", {}, cancel, recovery_context=context)

    def test_low_health_exit_has_verified_steps_and_only_ground_is_complete(self):
        world = self.world()
        anchor = list(world.status["pos"])
        recent = []
        for i, expected in enumerate(([1.5, 70, .5], [2.5, 69, .5], [3.5, 67, .5])):
            before = list(world.status["pos"])
            result = self.execute(world, {"evacuation_origin": anchor, "recent_targets": recent,
                                          "evacuating": i > 0}, cls=DailySurvivalSkills)
            self.assertTrue(result.success, result)
            self.assertEqual(result.data["target"], expected)
            self.assertIsNone(result.data["threat_pos"])
            self.assertEqual(result.data["purpose"], "canopy_escape")
            self.assertEqual(result.data["retreat_complete"], i == 2)
            self.assertEqual(result.data["evacuation_stage"], "ground" if i == 2 else "relay")
            self.assertEqual(world.writes()[-1][2]["range"], 0)
            self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)
            recent.append([math.floor(v) for v in before])
        self.assertEqual([row[0] for row in world.writes()], ["move_to"] * 3)
        self.assertNotIn("oak_leaves", SurvivalSkills.SUPPORT)

    def test_leaf_only_route_is_progress_not_fabricated_ground(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 70, 0, "oak_leaves"), (2, 69, 0, "oak_leaves")])
        result = self.execute(world)
        self.assertTrue(result.success, result)
        self.assertFalse(result.data["retreat_complete"])
        self.assertFalse(result.data["ground_route_verified"])
        self.assertIsNone(result.data["final_target"])
        self.assertEqual(result.data["relay_goal"], [2.5, 69, .5])
        self.assertEqual(result.data["canopy_progress"]["observed_descent"], 1)

    def test_solid_origin_is_not_authority_for_generic_exploration(self):
        world = self.world()
        world.set_block([0, 69, 0], "stone")
        result = self.execute(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["terrain_search"]["termination"], "not_on_canopy")
        self.assertFalse(world.writes())

    def test_support_must_be_known_dry_leaf_and_body_clear(self):
        for name, props in (("oak_leaves", {}), ("oak_leaves", {"waterlogged": True}),
                            ("unknown_leaves", {"waterlogged": False}), ("water", {})):
            with self.subTest(name=name, props=props):
                world = self.world()
                world.set_block([0, 69, 0], name, properties=props)
                self.assertEqual(self.execute(world).error_code, "unsafe")
                self.assertFalse(world.writes())
        world = self.world()
        world.set_block([1, 71, 0], "oak_leaves", properties={"waterlogged": False})
        self.assertEqual(self.execute(world).error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_three_block_drop_and_unloaded_terrain_are_never_used(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 67, 0, "dirt")])
        self.assertEqual(self.execute(world).error_code, "unsafe")
        original = world.get_block
        def unknown(pos, **kwargs):
            if pos[0] != 0:
                raise APIError("not_loaded", "No observation")
            return original(pos, **kwargs)
        world.get_block = unknown
        self.assertEqual(self.execute(world).error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_recent_and_excluded_targets_cannot_be_replayed(self):
        for key in ("recent_targets", "excluded_targets"):
            with self.subTest(key=key):
                world = self.world()
                result = self.execute(world, {key: [[1, 70, 0]]})
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_ranged_hostile_uses_sixteen_blocks_and_other_mobs_use_eight(self):
        for name, distance in (("skeleton", 12), ("stray", 15), ("bogged", 15), ("pillager", 15), ("spider", 7)):
            with self.subTest(name=name):
                world = self.world()
                world.entities = [{"name": name, "type": "hostile", "pos": [.5, 70, .5 + distance]}]
                self.assertEqual(self.execute(world).error_code, "near_hostile")
                self.assertFalse(world.writes())
        world = self.world()
        world.entities = [{"name": "spider", "type": "hostile", "pos": [.5, 70, 14.5]}]
        self.assertTrue(self.execute(world).success)

    def test_configured_larger_threat_radius_and_target_clearance_are_respected(self):
        world = self.world()
        world.entities = [{"name": "spider", "type": "hostile", "pos": [.5, 70, 10.5]}]
        self.assertEqual(self.execute(world, {"threat_radius": 12}).error_code, "near_hostile")
        self.assertFalse(world.writes())
        world.entities[0]["pos"] = [9, 70, .5]  # Origin 8.5, first landing 7.5.
        self.assertEqual(self.execute(world).error_code, "near_hostile")
        self.assertFalse(world.writes())

    def test_new_threat_during_geometry_read_preempts_move(self):
        world = self.world()
        original = world.get_entities
        reads = 0
        def entities(**kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                world.entities = [{"name": "skeleton", "type": "hostile", "pos": [12, 70, 0]}]
            return original(**kwargs)
        world.get_entities = entities
        self.assertEqual(self.execute(world).error_code, "near_hostile")
        self.assertFalse(world.writes())

    def test_last_status_checks_health_life_position_and_ground(self):
        for change in ({"health": 5}, {"last_death": {"t": 2}}, {"pos": [1.1, 70, .5]}, {"on_ground": False}):
            with self.subTest(change=change):
                world = self.world()
                original = world.get_status
                reads = 0
                def status(**kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 4:
                        world.status.update(change)
                    return original(**kwargs)
                world.get_status = status
                result = self.execute(world)
                self.assertFalse(result.success)
                self.assertFalse(result.data["executed"])
                self.assertFalse(world.writes())

    def test_last_status_hostile_preempts_after_the_final_entity_scan(self):
        for name, distance in (("skeleton", 15), ("stray", 12), ("spider", 7)):
            with self.subTest(name=name):
                world = self.world()
                original = world.get_status
                reads = 0
                def status(**kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 4:
                        world.status["nearest_hostile"] = {"name": name, "dist": distance}
                    return original(**kwargs)
                world.get_status = status
                result = self.execute(world)
                self.assertFalse(result.success)
                self.assertIn(result.error_code, {"near_hostile", "safety_changed"})
                self.assertFalse(result.data["executed"])
                self.assertFalse(world.writes())
                self.assertEqual(len([call for call in world.calls if call[0] == "get_entities"]), 2)

    def test_unknown_move_sends_no_cleanup_or_second_move(self):
        for code in ("request_timeout", "network_error", "invalid_json"):
            with self.subTest(code=code):
                world = self.world()
                def failed(*_):
                    raise APIError(code, "No confirmed move response")
                world.move_effect = failed
                result = self.execute(world)
                self.assertFalse(result.success)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
                self.assertTrue(result.data["executed"])
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_post_move_read_failure_and_cancel_remain_unknown_without_stop(self):
        for cancellation in (False, True):
            with self.subTest(cancellation=cancellation):
                world = self.world()
                cancel = threading.Event()
                original = world.get_status
                def status(**kwargs):
                    if any(row[0] == "move_to" for row in world.calls):
                        raise APIError("invalid_response", "No final status")
                    return original(**kwargs)
                def move(pos, kwargs):
                    world.status["pos"] = pos
                    cancel.set()
                    return envelope({"pos": pos})
                if cancellation:
                    world.move_effect = move
                else:
                    world.get_status = status
                result = self.execute(world, cancel=cancel, cls=DailySurvivalSkills)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertTrue(result.data["executed"])
                self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_backend_success_without_arrival_is_not_success(self):
        world = self.world()
        world.move_effect = lambda pos, kwargs: envelope({"pos": pos})
        result = self.execute(world)
        self.assertEqual(result.error_code, "retreat_unverified")
        self.assertTrue(result.data["candidate_failed"])

    def test_interrupted_response_or_busy_postcheck_does_not_clear_unknown(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                world = self.world()
                def move(pos, kwargs):
                    world.status["pos"] = pos
                    if interrupted:
                        return envelope({"interrupted_by": "other_action"})
                    world.status["busy"] = "move_to"
                    return envelope({"pos": pos})
                world.move_effect = move
                result = self.execute(world)
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_internal_context_and_arguments_are_bounded(self):
        for context in ({"recent_targets": [[1, 70, 0]] * 9}, {"recent_targets": [[1.1, 70, 0]]},
                        {"purpose": "skip_safety"}, {"threat_radius": float("nan")}):
            with self.subTest(context=context):
                world = self.world()
                self.assertEqual(self.execute(world, context).error_code, "bad_args")
                self.assertFalse(world.writes())
        self.assertEqual(SurvivalSkills(self.world()).execute("canopy_escape", {"pos": [1, 70, 0]}).error_code, "bad_args")


class ObservedCanopyEscapeTests(unittest.TestCase):
    def test_actual_tree_can_descend_in_fresh_relays_without_inventing_ground(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "canopy_progressive_exit.json").read_text(encoding="utf-8"))
        world = World()
        world.status.update(pos=fixture["origin"], health=6, food=17, gamemode="survival")
        world.entities = fixture["entities"]
        reads = []
        def read(pos, **kwargs):
            key = ",".join(str(math.floor(v)) for v in pos)
            reads.append(key)
            block = fixture["blocks"].get(key)
            if block is None:
                raise APIError("not_loaded", "Outside the captured terrain")
            return envelope({"pos": pos, **block})
        world.get_block = read
        recent = []
        for i, waypoint in enumerate(fixture["expected_waypoints"]):
            before = list(world.status["pos"])
            count = len(reads)
            result = SurvivalSkills(world).execute("canopy_escape", {}, recovery_context={
                "evacuation_origin": fixture["origin"], "recent_targets": recent[-8:], "evacuating": i > 0})
            self.assertTrue(result.success, (i, result))
            self.assertEqual(result.data["target"], waypoint)
            self.assertFalse(result.data["retreat_complete"])
            self.assertFalse(result.data["ground_route_verified"])
            self.assertEqual(result.data["evacuation_stage"], "relay")
            self.assertLessEqual(len(reads) - count, 256)
            self.assertTrue(-2 <= waypoint[1] - before[1] <= 1)
            self.assertEqual(world.writes()[-1][2]["range"], 0)
            recent.append([math.floor(v + .0001) for v in before])
        self.assertEqual(world.status["pos"], [2.5, 122, 7.5])
        self.assertEqual([row[0] for row in world.writes()], ["move_to"] * 11)
        self.assertEqual(fixture["blocks"]["2,121,7"]["name"], "oak_leaves")

    def test_clear_canopy_descent_allows_leaf_distance_property_update(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "oak_leaves")])
        world.set_block([0, 69, 0], "oak_leaves", properties={"waterlogged": False, "distance": 3})
        world.set_block([1, 68, 0], "oak_leaves", properties={"waterlogged": False, "distance": 4})
        world.set_block([1, 69, 0], "oak_leaves", properties={"waterlogged": False, "distance": 3},
                        diggable=True, harvest_tool_ok=True, can_see=True)
        orig_dig = world.dig

        def dig_with_distance_update(*args, **kwargs):
            res = orig_dig(*args, **kwargs)
            world.set_block([0, 69, 0], "oak_leaves", properties={"waterlogged": False, "distance": 5})
            world.set_block([1, 68, 0], "oak_leaves", properties={"waterlogged": False, "distance": 6})
            return res

        world.dig = dig_with_distance_update
        result = DailySurvivalSkills(world).execute("canopy_escape", {})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data.get("operation"), "clear_foliage")

    def test_pale_garden_moss_floor_and_hanging_moss_complete_canopy_escape(self):
        world = terrain([(0, 70, 0, "pale_oak_leaves"), (1, 69, 0, "pale_moss_block")])
        world.set_block([0, 69, 0], "pale_oak_leaves", properties={"waterlogged": False})
        world.set_block([1, 68, 0], "pale_moss_block")
        world.set_block([1, 70, 0], "pale_hanging_moss")
        result = DailySurvivalSkills(world).execute("canopy_escape", {})
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "ground")


if __name__ == "__main__":
    unittest.main()
