"""Landing and single-leaf exit recovery, with no game or model connections."""
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
from test_canopy_escape import terrain
from test_survival_skills import World, envelope


class LandingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "oak_leaves"), (2, 68, 0, "dirt")])
        self.now = 100.
        for name, effect in (("monotonic", lambda: self.now), ("sleep", self.advance)):
            patcher = patch("skills.survival.time." + name, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.timeout_values = []

    def advance(self, seconds):
        self.now += seconds

    def falling(self, updates):
        original = self.world.get_status
        self.frames = iter(updates)
        def status(**kwargs):
            if any(call[0] == "move_to" for call in self.world.calls):
                self.timeout_values.append(kwargs.get("timeout_seconds"))
                update = next(self.frames, {})
                if isinstance(update, Exception):
                    raise update
                self.world.status.update(update)
            return original(**kwargs)
        def move(pos, kwargs):
            self.world.status.update(pos=[pos[0], pos[1] + .9, pos[2]], on_ground=False)
            return envelope({"pos": pos})
        self.world.get_status = status
        self.world.move_effect = move

    def run_escape(self, cancel=None):
        return DailySurvivalSkills(self.world).execute("canopy_escape", {}, cancel_event=cancel)

    def test_transient_point_nine_fall_settles_without_second_move_or_stop(self):
        self.falling([{}, {"pos": [1.5, 69.4, .5]}, {"pos": [1.5, 69, .5], "on_ground": True}])
        result = self.run_escape()
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["landing_settled"])
        self.assertEqual(result.data["landing_observations"], 3)
        self.assertEqual(result.data["pos"], [1.5, 69, .5])
        self.assertLessEqual(self.now - 100., 1.5)
        self.assertTrue(all(0 < value <= 1.5 for value in self.timeout_values))
        self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def test_unsettled_deadline_is_unknown_and_never_sends_cleanup(self):
        self.falling([{}] * 30)
        result = self.run_escape()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
        self.assertLessEqual(self.now - 100., 1.500001)
        self.assertLessEqual(len(self.timeout_values), 16)
        self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def test_life_health_or_read_failure_during_fall_is_unknown(self):
        for update in ({"health": 5}, {"last_death": {"t": 1}}, APIError("network_error", "Lost status")):
            with self.subTest(update=update):
                self.world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "oak_leaves"), (2, 68, 0, "dirt")])
                self.falling([{}, update])
                result = self.run_escape()
                self.assertFalse(result.success)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
                self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def test_cancel_after_move_remains_unknown_with_no_stop(self):
        event = threading.Event()
        def move(pos, kwargs):
            self.world.status.update(pos=[pos[0], pos[1] + .9, pos[2]], on_ground=False)
            event.set()
            return envelope({"pos": pos})
        self.world.move_effect = move
        result = self.run_escape(event)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
        self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def test_hurt_interruption_replans_only_from_verified_settled_position(self):
        for cause, changes, expected in (
            ("hurt", {}, "retreat_interrupted"),
            ("hurt", {"on_ground": False}, "action_unconfirmed"),
            ("hurt", {"last_death": {"t": 1}}, "action_unconfirmed"),
            ("hurt", {"busy": {"name": "move_to"}}, "action_unconfirmed"),
            (None, {}, "action_unconfirmed"),
        ):
            with self.subTest(cause=cause, changes=changes):
                self.world = World()
                self.world.status.update(speed=0, velocity=[0, 0, 0])

                def move(pos, kwargs):
                    self.world.status.update(health=19, **changes)
                    raise APIError("interrupted", "Move did not finish", 200, {"cause": cause})

                self.world.move_effect = move
                result = SurvivalSkills(self.world).execute("retreat_from", {"pos": [-3, 64, .5]})
                self.assertFalse(result.success, result)
                self.assertEqual(result.error_code, expected)
                self.assertEqual(result.data["api_details"]["cause"], cause)
                self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])
                if expected == "retreat_interrupted":
                    self.assertTrue(result.data["candidate_failed"])
                    self.assertTrue(result.data["landing_settled"])
                    self.assertEqual(result.data["pos"], self.world.status["pos"])
                else:
                    self.assertEqual(result.data["action_state"], "unknown")

    def test_injured_verified_landing_is_known_and_requests_fresh_safety(self):
        for skill, args in (("canopy_escape", {}), ("retreat_from", {"pos": [-3, 70, .5]})):
            with self.subTest(skill=skill):
                self.world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "oak_leaves"), (2, 68, 0, "dirt")])
                self.falling([{"pos": [1.5, 69, .5], "on_ground": True, "jumping": False, "health": 5}])
                result = DailySurvivalSkills(self.world).execute(skill, args)
                self.assertTrue(result.success, result)
                self.assertTrue(result.data["landing_settled"])
                self.assertEqual(result.data["health_lost"], 1)
                self.assertEqual(result.data["safety_changed"], "health_lost")
                self.assertEqual((result.data["health_before"], result.data["health_after"]), (6, 5))
                self.assertNotEqual(result.data.get("action_state"), "unknown")
                self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def test_injured_arrival_without_safe_footing_remains_unknown(self):
        for support in ("air", "water"):
            with self.subTest(support=support):
                self.world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "oak_leaves"), (2, 68, 0, "dirt")])
                def move(pos, kwargs):
                    self.world.status.update(pos=list(pos), on_ground=True, health=5)
                    self.world.set_block([1, 68, 0], support)
                    return envelope({"pos": pos})
                self.world.move_effect = move
                result = self.run_escape()
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
                self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def test_injured_target_position_is_not_known_while_jumping_or_after_death(self):
        for change in ({"jumping": True}, {"last_death": {"t": 1}}, {"alive": False, "health": 0}):
            with self.subTest(change=change):
                self.world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "oak_leaves"), (2, 68, 0, "dirt")])
                self.falling([{"pos": [1.5, 69, .5], "on_ground": True, "health": 5, **change}])
                result = self.run_escape()
                self.assertFalse(result.success)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
                self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def test_injured_arrival_support_read_failure_remains_unknown(self):
        self.falling([{"pos": [1.5, 69, .5], "on_ground": True, "health": 5}])
        original = self.world.get_block
        def block(pos, **kwargs):
            if any(row[0] == "move_to" for row in self.world.calls):
                raise APIError("network_error", "Landing support unavailable")
            return original(pos, **kwargs)
        self.world.get_block = block
        result = self.run_escape()
        self.assertFalse(result.success)
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
        self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])


class CanopyClearingTests(unittest.TestCase):
    def world(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "oak_leaves")])
        world.set_block([1, 69, 0], "oak_leaves", properties={"waterlogged": False},
                        diggable=True, harvest_tool_ok=True, can_see=True)
        return world

    def test_one_adjacent_leaf_cleared_then_next_fresh_cycle_descends(self):
        world = self.world()
        source = copy.deepcopy(world.blocks[(0, 69, 0)])
        landing_support = copy.deepcopy(world.blocks[(1, 68, 0)])
        result = DailySurvivalSkills(world).execute("canopy_escape", {})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["operation"], "clear_foliage")
        self.assertEqual(result.data["cleared_pos"], [1, 69, 0])
        self.assertEqual(result.data["moved"], 0)
        self.assertFalse(result.data["retreat_complete"])
        self.assertIsNone(result.data["target"])
        self.assertEqual(world.writes(), [("dig", [1, 69, 0], False)])
        self.assertEqual(world.blocks[(0, 69, 0)], source)
        self.assertEqual(world.blocks[(1, 68, 0)], landing_support)
        second = DailySurvivalSkills(world).execute("canopy_escape", {})
        self.assertTrue(second.success, second)
        self.assertEqual(second.data["target"], [1.5, 69, .5])
        self.assertFalse(second.data["retreat_complete"])
        self.assertEqual([row[0] for row in world.writes()], ["dig", "move_to"])

    def test_multiple_obstructions_never_clear_more_than_one_leaf_per_cycle(self):
        world = self.world()
        world.set_block([1, 70, 0], "oak_leaves", properties={"waterlogged": False},
                        diggable=True, harvest_tool_ok=True, can_see=True)
        world.set_block([1, 71, 0], "oak_leaves", properties={"waterlogged": False},
                        diggable=True, harvest_tool_ok=True, can_see=True)
        result = SurvivalSkills(world).execute("canopy_escape", {})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["cleared_pos"], [1, 71, 0])
        self.assertEqual(result.data["clearance_remaining"], 1)
        self.assertEqual(world.blocks[(1, 69, 0)]["name"], "oak_leaves")
        self.assertEqual(world.writes(), [("dig", [1, 71, 0], False)])

    def test_hazards_unknown_support_and_nonleaf_body_never_authorize_dig(self):
        for pos, name, props in (([1, 68, 0], "air", {}), ([1, 68, 0], "water", {}),
                                 ([1, 68, 0], "oak_leaves", {"waterlogged": True}),
                                 ([1, 70, 0], "stone", {}), ([2, 69, 0], "lava", {})):
            with self.subTest(pos=pos, name=name):
                world = self.world()
                world.set_block(pos, name, properties=props)
                SurvivalSkills(world).execute("canopy_escape", {})
                self.assertFalse(any(row[0] == "dig" for row in world.calls))

    def test_visibility_reach_and_current_footprint_support_are_required(self):
        for change in ("occluded", "unreachable", "contact"):
            with self.subTest(change=change):
                world = self.world()
                if change == "occluded":
                    world.blocks[(1, 69, 0)]["can_see"] = False
                elif change == "unreachable":
                    world.blocks[(1, 69, 0)]["harvest_tool_ok"] = False
                else:
                    world.status["pos"] = [.8, 70, .5]
                SurvivalSkills(world).execute("canopy_escape", {})
                self.assertFalse(any(row[0] == "dig" for row in world.calls))

    def test_last_stance_drift_cannot_turn_target_leaf_into_player_support(self):
        world = self.world()
        original = world.get_status
        reads = 0
        def status(**kwargs):
            nonlocal reads
            reads += 1
            if reads == 3:  # Final _action status, after the clearing proof.
                world.status["pos"] = [.69, 70, .5]
            return original(**kwargs)
        world.get_status = status
        result = SurvivalSkills(world).execute("canopy_escape", {})
        self.assertEqual(result.error_code, "safety_changed")
        self.assertIn("footprint", result.message)
        self.assertFalse(result.data["executed"])
        self.assertFalse(world.writes())

    def test_unknown_dig_or_confirmation_read_does_not_stop_or_replay(self):
        for code in ("dig_unconfirmed", "request_timeout", "read_failed"):
            with self.subTest(code=code):
                world = self.world()
                original = world.get_block
                def dig(pos):
                    if code != "read_failed":
                        raise APIError(code, "No confirmed write")
                    world.set_block(pos, "air")
                    return envelope({"dug": "oak_leaves"})
                def read(pos, **kwargs):
                    if code == "read_failed" and any(row[0] == "dig" for row in world.calls):
                        raise APIError("network_error", "Lost confirmation")
                    return original(pos, **kwargs)
                world.dig_effect = dig
                world.get_block = read
                result = DailySurvivalSkills(world).execute("canopy_escape", {})
                self.assertFalse(result.success)
                self.assertTrue(result.data["executed"])
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
                self.assertEqual(world.writes(), [("dig", [1, 69, 0], False)])

    def test_raw_log_is_only_a_canopy_relay_not_a_ground_destination(self):
        for name in ("oak_log", "birch_wood"):
            with self.subTest(name=name):
                world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, name)])
                result = SurvivalSkills(world).execute("canopy_escape", {})
                self.assertTrue(result.success, result)
                self.assertFalse(result.data["retreat_complete"])
                self.assertFalse(result.data["ground_route_verified"])
                self.assertEqual(result.data["evacuation_stage"], "relay")

    def test_unvisited_level_step_can_be_sideways_or_towards_escape_origin(self):
        for destination in ((2, 70, 0), (3, 70, 1)):
            with self.subTest(destination=destination):
                world = terrain([(3, 70, 0, "oak_leaves"), (*destination, "oak_leaves")])
                result = SurvivalSkills(world).execute("canopy_escape", {}, recovery_context={
                    "evacuating": True, "evacuation_origin": [.5, 70, .5], "recent_targets": [[3, 70, 0]]})
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["target"], [destination[0] + .5, destination[1], destination[2] + .5])
                self.assertFalse(result.data["retreat_complete"])
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_threat_retreat_can_take_one_checked_upward_leaf_step_without_clearing(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 71, 0, "oak_leaves"), (2, 71, 0, "oak_leaves")])
        result = SurvivalSkills(world).execute("retreat_from", {"pos": [-3, 70, .5], "distance": 8})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["target"], [1.5, 71, .5])
        self.assertEqual(result.data["planned_path"][-1], [2.5, 71, .5])
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "relay")
        self.assertEqual([row[0] for row in world.writes()], ["move_to"])


class ObservedClearingTests(unittest.TestCase):
    def install(self, fixture_name, origin=None):
        fixture = json.loads((Path(__file__).parent / "fixtures" / fixture_name).read_text(encoding="utf-8"))
        world = World()
        world.status.update(pos=origin or fixture["origin"], health=6, food=17, gamemode="survival")
        world.entities = fixture["entities"]
        def read(pos, **kwargs):
            key = ",".join(str(math.floor(v)) for v in pos)
            block = fixture["blocks"].get(key)
            if block is None:
                raise APIError("not_loaded", "Outside the observed fixture")
            return envelope({"pos": pos, "diggable": True,
                             "harvest_tool_ok": math.dist(world.status["pos"], pos) <= 3, **block})
        def dig(pos):
            fixture["blocks"][",".join(map(str, pos))] = {"name": "air", "properties": {}}
            return envelope({"dug": "oak_leaves"})
        world.get_block = read
        world.dig_effect = dig
        return world, fixture

    def test_observed_one_leaf_exit_clears_then_descends_without_touching_supports(self):
        world, fixture = self.install("canopy_progressive_exit.json", [2.5, 122, 7.5])
        source, support = copy.deepcopy(fixture["blocks"]["2,121,7"]), copy.deepcopy(fixture["blocks"]["2,120,6"])
        skills = DailySurvivalSkills(world)
        first = skills.execute("canopy_escape", {}, recovery_context={"evacuation_origin": fixture["origin"]})
        self.assertTrue(first.success, first)
        self.assertEqual(first.data["cleared_pos"], [2, 121, 6])
        self.assertEqual(world.writes(), [("dig", [2, 121, 6], False)])
        self.assertEqual(fixture["blocks"]["2,121,7"], source)
        self.assertEqual(fixture["blocks"]["2,120,6"], support)
        second = skills.execute("canopy_escape", {}, recovery_context={"evacuation_origin": fixture["origin"]})
        self.assertTrue(second.success, second)
        self.assertEqual(second.data["target"], [2.5, 121, 6.5])
        self.assertFalse(second.data["retreat_complete"])
        self.assertTrue(all(result.data["terrain_search"]["block_reads"] <= 256 for result in (first, second)))

    def test_latest_real_trunk_stance_is_not_mistaken_for_finished_ground(self):
        world, fixture = self.install("canopy_trunk_exit.json")
        result = DailySurvivalSkills(world).execute("canopy_escape", {}, recovery_context={"evacuating": True})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["origin_support"]["center_name"], "oak_log")
        # With no recent-target exclusions the verified +1 leaf surface is a
        # non-destructive escape option. It is still a relay, not solid land.
        self.assertEqual(result.data["target"], [5.5, 122, 6.5])
        self.assertEqual([row[0] for row in world.writes()], ["move_to"])
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "relay")

    def test_real_recent_waypoints_allow_one_level_opening_then_fresh_movement(self):
        world, fixture = self.install("canopy_trunk_exit.json")
        context = fixture["recovery_context"]
        source = copy.deepcopy(fixture["blocks"]["4,120,6"])
        support = copy.deepcopy(fixture["blocks"]["5,120,6"])
        skills = DailySurvivalSkills(world)
        first = skills.execute("canopy_escape", {}, recovery_context=context)
        self.assertTrue(first.success, first)
        self.assertEqual(first.data["cleared_pos"], fixture["expected_cleared_pos"])
        self.assertEqual(first.data["canopy_progress"]["observed_descent"], 0)
        self.assertFalse(first.data["retreat_complete"])
        self.assertEqual(first.data["moved"], 0)
        self.assertEqual(world.writes(), [("dig", [5, 121, 6], False)])
        self.assertEqual(fixture["blocks"]["4,120,6"], source)
        self.assertEqual(fixture["blocks"]["5,120,6"], support)
        second = skills.execute("canopy_escape", {}, recovery_context=context)
        self.assertTrue(second.success, second)
        self.assertEqual(second.data["target"], fixture["expected_level_target"])
        self.assertEqual(second.data["evacuation_stage"], "relay")
        self.assertTrue(second.data["landing_settled"])
        self.assertEqual([row[0] for row in world.writes()], ["dig", "move_to"])
        self.assertTrue(all(result.data["terrain_search"]["block_reads"] <= 256 for result in (first, second)))

    def test_healthy_tall_tree_allows_verified_three_block_drop_onto_solid_ground(self):
        world = terrain([(0, 74, 0, "oak_leaves"), (1, 71, 0, "dirt")])
        world.status["health"] = 20
        result = SurvivalSkills(world).execute("canopy_escape", {})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["target"], [1.5, 71, 0.5])
        self.assertTrue(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "ground")


if __name__ == "__main__":
    unittest.main()
