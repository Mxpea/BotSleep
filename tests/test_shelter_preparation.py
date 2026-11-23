"""Bounded natural-site preparation; synthetic loaded terrain, no live API."""

import copy
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.shelter_preparation import ShelterPreparationSkills
from test_daily_skills import DailyWorld, envelope


class PreparationWorld(DailyWorld):
    def __init__(self):
        super().__init__()
        self.status.update(is_day=True, time_of_day=4000, thundering=False, speed=0,
                           velocity=[0, 0, 0], jumping=False, held_controls=None, last_death={"t": 1})
        self.block([1, 64, 0], "dirt")

    def dig(self, pos, collect=True):
        self.calls.append(("dig", list(pos), {"collect": collect}))
        if "dig" in self.effects:
            return self.effects["dig"]()
        self.block(pos, "air")
        return envelope({"dug": True})


class ShelterPreparationTests(unittest.TestCase):
    def run_skill(self, world, name="prepare_shelter_step", cancel=None, args=None):
        return ShelterPreparationSkills(world).execute(name, args or {"pos": [0, 64, 0]}, cancel)

    @staticmethod
    def writes(world):
        return [row for row in world.calls if row[0] in {"move", "stop", "dig", "place", "control", "equip"}]

    def test_read_only_plan_never_authorizes_a_base_or_mutates(self):
        world = PreparationWorld()
        result = self.run_skill(world, "plan_shelter_preparation")
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["preparation_plan_verified"])
        self.assertFalse(result.data["shelter_verified"])
        self.assertFalse(result.data["executed"])
        self.assertEqual(result.data["remaining_obstacles"], [{"pos": [1, 64, 0], "name": "dirt"}])
        self.assertFalse(self.writes(world))

    def test_known_read_only_rejection_can_try_another_orientation_without_write(self):
        world = PreparationWorld()
        world.block([2,63,2], "air")
        result = self.run_skill(world, "plan_shelter_preparation", args={"pos": [0,64,0], "orientation": "west"})
        self.assertEqual(result.error_code, "unsafe_site", result)
        self.assertTrue(result.data["preparation_rejected"])
        self.assertTrue(result.data["fresh"])
        self.assertFalse(result.data["preparation_plan_verified"])
        self.assertEqual(result.data["orientation"], "west")
        self.assertFalse(self.writes(world))

    def test_uncaptured_geometry_or_changed_safety_never_becomes_known_rejection(self):
        for cause in ("unknown", "not_loaded", "network_error", "request_timeout", "hurt"):
            with self.subTest(cause=cause):
                world = PreparationWorld()
                original = world.get_block
                def block(pos, **kwargs):
                    if pos == [-2,63,-2]:
                        if cause in {"not_loaded", "network_error", "request_timeout"}:
                            raise APIError(cause, "Not captured")
                        if cause == "unknown": return envelope({"name": "unknown"})
                        world.status["health"] = 18
                        return envelope({"name": "air"})
                    return original(pos, **kwargs)
                world.get_block = block
                result = self.run_skill(world, "plan_shelter_preparation")
                self.assertFalse(result.success, result)
                self.assertFalse(result.data.get("preparation_rejected"), result)
                self.assertFalse(self.writes(world))

    def test_one_dig_rechecks_full_plot_then_verifies_air_and_actual_stance(self):
        world = PreparationWorld()
        result = self.run_skill(world)
        self.assertTrue(result.success, result)
        self.assertEqual(self.writes(world), [("dig", [1, 64, 0], {"collect": False})])
        self.assertEqual(result.data["action_state"], "verified")
        self.assertEqual(result.data["pos"], [0, 64, 0])
        self.assertEqual(result.data["cleared_pos"], [1, 64, 0])
        self.assertTrue(result.data["requires_shelter_replan"])
        self.assertTrue(result.data["preparation_complete"])
        self.assertFalse(result.data["shelter_verified"])
        self.assertGreaterEqual(world.calls.count(("get_block", (-2, 63, -2))), 2)
        self.assertGreaterEqual(world.calls.count(("get_block", (2, 66, 2))), 2)
        self.assertLessEqual(result.data["read_calls"], 320)

    def test_eight_obstacles_are_fully_counted_but_only_one_is_dug(self):
        world = PreparationWorld()
        cells = [(1,64,0),(2,64,0),(-1,64,0),(-2,64,0),(0,64,1),(0,64,2),(0,64,-1),(0,64,-2)]
        for cell in cells:
            world.block(list(cell), "grass_block")
        plan = self.run_skill(world, "plan_shelter_preparation")
        self.assertTrue(plan.success, plan)
        self.assertEqual(plan.data["clear_remaining"], 8)
        result = self.run_skill(world)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["remaining"], 7)
        self.assertEqual(len(self.writes(world)), 1)
        world = PreparationWorld()
        for cell in cells + [(1,64,1)]:
            world.block(list(cell), "dirt")
        self.assertEqual(self.run_skill(world).error_code, "unsafe_site")
        self.assertFalse(self.writes(world))

    def test_floor_hole_liquid_building_tree_leaf_or_gravity_are_never_prepared(self):
        changes = (([2,63,2], "air"), ([2,63,2], "water"), ([1,64,0], "oak_planks"),
                   ([1,64,0], "oak_log"), ([1,64,0], "oak_leaves"), ([1,65,0], "gravel"),
                   ([2,64,0], "water"), ([1,64,0], "chest"))
        for pos, name in changes:
            with self.subTest(pos=pos, name=name):
                world = PreparationWorld()
                world.block(pos, name)
                result = self.run_skill(world)
                self.assertFalse(result.success, result)
                self.assertFalse(self.writes(world))

    def test_missing_neighbor_is_unknown_geometry_and_never_assumed_air(self):
        world = PreparationWorld()
        world.block([1,64,0], "air")
        world.block([2,64,0], "dirt")
        original = world.get_block
        def block(pos, **kwargs):
            if pos == [3,64,0]:
                raise APIError("not_loaded", "Uncaptured neighbor")
            return original(pos, **kwargs)
        world.get_block = block
        result = self.run_skill(world)
        self.assertFalse(result.success, result)
        self.assertFalse(self.writes(world))

    def test_workflow_only_replaceable_shapes_do_not_authorize_a_useless_dig(self):
        for name in ("snow", "tall_grass", "poppy"):
            with self.subTest(name=name):
                world = PreparationWorld()
                world.block([2,64,2], name, properties={"layers": 8} if name == "snow" else {})
                result = self.run_skill(world)
                self.assertEqual(result.error_code, "unsafe_site", result)
                self.assertFalse(self.writes(world))

    def test_own_head_column_or_distant_obstacle_needs_position_without_moving(self):
        for target in ([0,66,0], [2,66,2]):
            with self.subTest(target=target):
                world = PreparationWorld()
                world.block([1,64,0], "air")
                world.block(target, "dirt")
                if target == [2,66,2]:
                    world.status["pos"] = [-1.5,64,-1.5]
                result = self.run_skill(world)
                self.assertEqual(result.error_code, "needs_position", result)
                self.assertTrue(result.data["preparation_plan_verified"])
                self.assertTrue(result.data["needs_position"])
                self.assertFalse(self.writes(world))

    def test_offset_player_cannot_dig_any_actual_support_column(self):
        world = PreparationWorld()
        world.status["pos"] = [.9,65,.5]
        world.block([0,64,0], "dirt")
        result = self.run_skill(world)
        self.assertEqual(result.error_code, "needs_position", result)
        self.assertFalse(self.writes(world))
        self.assertEqual(world.blocks[(0,64,0)]["name"], "dirt")
        self.assertEqual(world.blocks[(1,64,0)]["name"], "dirt")

    def test_final_source_shift_cannot_put_target_under_actual_footprint(self):
        world = PreparationWorld()
        world.status["pos"] = [.62,64,.5]
        original = world.get_entities
        scans = 0
        def entities(**kwargs):
            nonlocal scans
            scans += 1
            rows = original(**kwargs)
            if scans == 2:
                world.status["pos"] = [.66,64,.5]
            return rows
        world.get_entities = entities
        result = self.run_skill(world)
        self.assertFalse(result.success, result)
        self.assertFalse(self.writes(world))

    def test_fresh_whole_plot_change_prevents_the_write(self):
        world = PreparationWorld()
        original = world.get_block
        hits = 0
        def block(pos, **kwargs):
            nonlocal hits
            if pos == [-2,63,-2]:
                hits += 1
                if hits == 2:
                    world.block(pos, "air")
            return original(pos, **kwargs)
        world.get_block = block
        result = self.run_skill(world)
        self.assertFalse(result.success, result)
        self.assertFalse(self.writes(world))

    def test_new_secondary_shooter_prevents_write_even_with_safe_nearest(self):
        world = PreparationWorld()
        world.status["nearest_hostile"] = {"id": 1, "name": "zombie", "dist": 9}
        original = world.get_entities
        scans = 0
        def entities(**kwargs):
            nonlocal scans
            scans += 1
            rows = original(**kwargs)
            if scans == 2:
                rows.append({"id": 2, "name": "skeleton", "type": "hostile", "pos": [15,64,.5]})
            return rows
        world.get_entities = entities
        self.assertFalse(self.run_skill(world).success)
        self.assertFalse(self.writes(world))

    def test_bad_hostile_rows_motion_and_daylight_fail_before_write(self):
        for updates, entities in (({"speed": None}, []), ({"velocity": [float("nan"),0,0]}, []),
                                  ({"jumping": True}, []), ({"is_day": False}, []),
                                  ({"time_of_day": 11000}, []), ({"health": 11}, []),
                                  ({}, [{}])):
            with self.subTest(updates=updates, entities=entities):
                world = PreparationWorld()
                world.status.update(updates)
                world.entities = entities
                self.assertFalse(self.run_skill(world).success)
                self.assertFalse(self.writes(world))

    def test_write_transport_interruption_and_dig_unconfirmed_are_sticky_unknown(self):
        for code in ("network_error", "request_timeout", "not_connected", "interrupted", "dig_unconfirmed"):
            with self.subTest(code=code):
                world = PreparationWorld()
                def fail():
                    raise APIError(code, "Injected uncertain dig")
                world.effects["dig"] = fail
                result = self.run_skill(world)
                self.assertFalse(result.success, result)
                self.assertEqual(result.data["action_state"], "unknown", result)
                self.assertEqual(len(self.writes(world)), 1)
                self.assertEqual(self.writes(world)[0][0], "dig")

    def test_after_write_cancel_life_dimension_hurt_wrong_result_or_footing_remain_unknown(self):
        for cause in ("cancel", "life", "dimension", "hurt", "unchanged", "footing", "motion", "invalid_ack"):
            with self.subTest(cause=cause):
                world = PreparationWorld()
                cancel = threading.Event()
                def effect():
                    if cause != "unchanged": world.block([1,64,0], "air")
                    if cause == "cancel": cancel.set()
                    if cause == "life": world.status["last_death"] = {"t": 2}
                    if cause == "dimension": world.status["dimension"] = "the_nether"
                    if cause == "hurt": world.status["health"] = 18
                    if cause == "footing": world.block([0,63,0], "air")
                    if cause == "motion": world.status["speed"] = .2
                    return {} if cause == "invalid_ack" else envelope({"dug": True})
                world.effects["dig"] = effect
                result = self.run_skill(world, cancel=cancel)
                self.assertFalse(result.success, result)
                self.assertEqual(result.data["action_state"], "unknown", result)
                self.assertEqual([row[0] for row in self.writes(world)], ["dig"])

    def test_final_stance_tick_or_second_shooter_cannot_be_ignored(self):
        for cause in ("shift", "shooter", "health"):
            world = PreparationWorld()
            original_status = world.get_status
            original_entities = world.get_entities
            reads = 0
            def status(**kwargs):
                nonlocal reads
                if self.writes(world):
                    reads += 1
                    if reads == 2:
                        if cause == "shift": world.status["pos"] = [.65,64,.5]
                        if cause == "health": world.status["health"] = 19
                return original_status(**kwargs)
            def entities(**kwargs):
                rows = original_entities(**kwargs)
                if self.writes(world) and cause == "shooter":
                    rows.append({"id": 2, "type": "hostile", "name": "skeleton", "pos": [15,64,.5]})
                return rows
            world.get_status, world.get_entities = status, entities
            result = self.run_skill(world)
            self.assertEqual(result.data["action_state"], "unknown", result)
            self.assertEqual([row[0] for row in self.writes(world)], ["dig"])

    def test_prewrite_budget_and_postwrite_settle_deadline_are_hard_limits(self):
        world = PreparationWorld()
        with patch.object(ShelterPreparationSkills, "MAX_READS", 20):
            self.assertEqual(self.run_skill(world).error_code, "preparation_budget_exhausted")
        self.assertFalse(self.writes(world))
        world = PreparationWorld()
        now = [100.]
        original = world.get_status
        def status(**kwargs):
            if self.writes(world):
                self.assertLessEqual(kwargs["timeout_seconds"], 1.5)
                now[0] += 2
            return original(**kwargs)
        world.get_status = status
        with patch("skills.shelter_preparation.time.monotonic", side_effect=lambda: now[0]):
            result = self.run_skill(world)
        self.assertEqual(result.data["action_state"], "unknown", result)
        self.assertEqual([row[0] for row in self.writes(world)], ["dig"])

    def test_last_prewrite_check_reserves_the_whole_landing_read_allowance(self):
        # This simple fixture uses 250 reads before the dig. Even though its
        # centered landing only needs seven, the full 20-read reserve is firm.
        for budget, can_dig in ((269, False), (270, True)):
            with self.subTest(budget=budget):
                world = PreparationWorld()
                with patch.object(ShelterPreparationSkills, "MAX_READS", budget):
                    result = self.run_skill(world)
                if can_dig:
                    self.assertTrue(result.success, result)
                    self.assertEqual([row[0] for row in self.writes(world)], ["dig"])
                else:
                    self.assertEqual(result.error_code, "preparation_budget_exhausted", result)
                    self.assertFalse(self.writes(world))


if __name__ == "__main__":
    unittest.main()
