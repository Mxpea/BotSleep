"""Single natural terrain steps over fake loaded blocks; never contact a server."""

import copy
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.positioning import PositioningSkills
from test_daily_skills import DailyWorld, envelope


class PositioningStepTests(unittest.TestCase):
    def world(self, direction=(1, 0), dy=1):
        world = DailyWorld()
        world.status.update(speed=0, velocity=[0, 0, 0], jumping=False, last_death={"t": 1})
        x, z = direction
        world.block([x, 64 if dy > 0 else 63, z], "stone" if dy > 0 else "air")
        return world, [x + .5, 64 + dy, z + .5]

    def run_step(self, world, target, cancel=None):
        return PositioningSkills(world).execute("position_for_task", {"pos": target}, cancel)

    def writes(self, world):
        return [row for row in world.calls if row[0] in {"move", "stop", "dig", "place", "control"}]

    def test_four_cardinal_directions_up_and_down_are_single_verified_moves(self):
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for dy in (1, -1):
                with self.subTest(direction=direction, dy=dy):
                    world, target = self.world(direction, dy)
                    result = self.run_step(world, target)
                    self.assertTrue(result.success, result)
                    self.assertEqual(self.writes(world), [("move", target, {"range": 0, "timeout_ms": 8000})])
                    self.assertTrue(result.data["landing_stable"])
                    self.assertFalse(result.data["actual_path_verified"])
                    self.assertEqual(result.data["terrain_step"]["height_change"], dy)
                    self.assertLessEqual(result.data["read_calls"], 96)

    def test_offset_source_requires_every_real_footprint_and_terrace_support(self):
        world, target = self.world()
        world.status["pos"] = [.3, 64, .3]
        world.block([1, 64, -1], "stone")
        result = self.run_step(world, target)
        self.assertTrue(result.success, result)
        self.assertLessEqual(result.data["read_calls"], 96)
        self.assertGreaterEqual(world.calls.count(("get_block", (-1, 63, -1))), 2)
        world, target = self.world()
        world.status["pos"] = [.3, 64, .3]
        world.block([-1, 63, -1], "air")
        self.assertFalse(self.run_step(world, target).success)
        self.assertFalse(self.writes(world))

    def test_diagonal_distant_two_high_and_noninteger_source_are_rejected(self):
        for target in ([1.5, 65, 1.5], [2.5, 65, .5], [1.5, 66, .5], [.5, 65, .5]):
            world, _ = self.world()
            result = self.run_step(world, target)
            self.assertEqual(result.error_code, "out_of_range", result)
            self.assertFalse(self.writes(world))
        world, target = self.world()
        world.status["pos"][1] = 64.5
        self.assertFalse(self.run_step(world, target).success)
        self.assertFalse(self.writes(world))

    def test_full_support_and_jump_head_envelope_are_mandatory(self):
        for pos, name, props in (([0, 63, 0], "oak_log", {}), ([1, 64, 0], "oak_leaves", {"waterlogged": False}),
                                 ([1, 64, 0], "stone_slab", {}), ([0, 67, 0], "stone", {}),
                                 ([1, 67, 0], "stone", {}), ([1, 65, 0], "water", {}),
                                 ([1, 65, 0], "short_grass", {"waterlogged": True})):
            with self.subTest(pos=pos, name=name):
                world, target = self.world()
                world.block(pos, name, properties=props)
                self.assertFalse(self.run_step(world, target).success)
                self.assertFalse(self.writes(world))

    def test_descent_requires_the_full_upper_and_lower_target_column(self):
        for y in (63, 64, 65):
            world, target = self.world(dy=-1)
            world.block([1, y, 0], "stone")
            self.assertFalse(self.run_step(world, target).success)
            self.assertFalse(self.writes(world))

    def test_unknown_or_changed_prewrite_geometry_never_starts_a_move(self):
        for changed in (False, True):
            world, target = self.world()
            original = world.get_block
            hits = 0
            def block(pos, **kwargs):
                nonlocal hits
                if pos == [1, 66, 0]:
                    hits += 1
                    if hits >= (2 if changed else 1):
                        if changed:
                            world.block(pos, "stone")
                        else:
                            raise APIError("not_loaded", "Unobserved headroom")
                return original(pos, **kwargs)
            world.get_block = block
            self.assertFalse(self.run_step(world, target).success)
            self.assertFalse(self.writes(world))

    def test_shooter_sixteen_radius_and_jump_apex_are_checked(self):
        for pos in ([17, 65, .5], [1.5, 81.1, .5]):
            world, target = self.world()
            world.entities = [{"id": 2, "type": "hostile", "name": "skeleton", "pos": pos}]
            self.assertEqual(self.run_step(world, target).error_code, "unsafe")
            self.assertFalse(self.writes(world))

    def test_new_threat_bad_rows_and_saturated_scans_refuse_the_write(self):
        for rows in ([{}], [{"id": 2, "type": "hostile", "name": "zombie", "pos": [1, 64, 1]}],
                     [{"id": i, "type": "hostile", "name": "zombie", "pos": [30, 64, 30]} for i in range(40)]):
            world, target = self.world()
            original = world.get_entities
            scans = 0
            def entities(**kwargs):
                nonlocal scans
                scans += 1
                original(**kwargs)
                return copy.deepcopy(rows) if scans == 2 else []
            world.get_entities = entities
            self.assertFalse(self.run_step(world, target).success)
            self.assertFalse(self.writes(world))

    def test_unstable_missing_velocity_or_source_push_prevents_write(self):
        for changes in ({"speed": .2}, {"velocity": None}, {"jumping": True}, {"held_controls": {"keys": ["forward"]}}):
            world, target = self.world()
            world.status.update(changes)
            self.assertFalse(self.run_step(world, target).success)
            self.assertFalse(self.writes(world))
        world, target = self.world()
        original = world.get_entities
        def entities(**kwargs):
            result = original(**kwargs)
            if len([r for r in world.calls if r[0] == "entities"]) == 2:
                world.status["pos"] = [.8, 64, .5]
            return result
        world.get_entities = entities
        self.assertFalse(self.run_step(world, target).success)
        self.assertFalse(self.writes(world))

    def test_lost_or_invalid_move_response_never_adds_stop_or_replay(self):
        for code in ("network_error", "request_timeout", "not_connected", "interrupted", "invalid_response"):
            world, target = self.world()
            def fail():
                raise APIError(code, "Injected uncertain move")
            world.effects["move"] = fail
            result = self.run_step(world, target)
            self.assertFalse(result.success)
            self.assertEqual(result.data["action_state"], "unknown", result)
            self.assertEqual([r[0] for r in self.writes(world)], ["move"])

    def test_hurt_death_life_change_cancel_and_wrong_landing_remain_unknown(self):
        for cause in ("hurt", "death", "life", "cancel", "wrong", "water"):
            world, target = self.world()
            cancel = threading.Event()
            def move():
                if cause == "hurt": world.status["health"] = 19
                if cause == "death": world.status.update(alive=False, health=0)
                if cause == "life": world.status["last_death"] = {"t": 2}
                if cause == "cancel": cancel.set()
                if cause == "wrong": world.status["pos"] = [.5, 64, .5]
                if cause == "water": world.status["in_water"] = True
                return envelope({"pos": target})
            world.effects["move"] = move
            result = self.run_step(world, target, cancel)
            self.assertFalse(result.success, cause)
            self.assertEqual(result.data["action_state"], "unknown", result)
            self.assertEqual([r[0] for r in self.writes(world)], ["move"])

    def test_missing_landing_support_is_unknown_without_cleanup(self):
        world, target = self.world()
        def move():
            world.block([1, 64, 0], "air")
            return envelope({"pos": target})
        world.effects["move"] = move
        result = self.run_step(world, target)
        self.assertEqual(result.data["action_state"], "unknown", result)
        self.assertEqual([r[0] for r in self.writes(world)], ["move"])

    def test_final_landing_footprint_cannot_enter_an_unread_column(self):
        world, target = self.world()
        original = world.get_status
        reads_after_move = 0
        def status(**kwargs):
            nonlocal reads_after_move
            if self.writes(world):
                reads_after_move += 1
                world.status["pos"] = [1.62 if reads_after_move < 3 else 1.66, 65, .5]
            return original(**kwargs)
        world.get_status = status
        result = self.run_step(world, target)
        self.assertEqual(result.data["action_state"], "unknown", result)
        self.assertEqual([r[0] for r in self.writes(world)], ["move"])

    def test_postwrite_read_deadline_and_read_budget_are_bounded(self):
        world, target = self.world()
        now = [100.]
        original = world.get_status
        def status(**kwargs):
            if self.writes(world):
                self.assertLessEqual(kwargs["timeout_seconds"], 1.5)
                now[0] += 2
            return original(**kwargs)
        world.get_status = status
        with patch("skills.positioning.time.monotonic", side_effect=lambda: now[0]):
            result = self.run_step(world, target)
        self.assertEqual(result.data["action_state"], "unknown", result)
        self.assertEqual([r[0] for r in self.writes(world)], ["move"])
        world, target = self.world()
        with patch.object(PositioningSkills, "MAX_READS", 8):
            result = self.run_step(world, target)
        self.assertFalse(result.success)
        self.assertFalse(self.writes(world))

    def test_last_status_must_stay_in_target_cell_tolerance_and_all_enemy_circles(self):
        for kind in ("height", "range", "shooter"):
            world, target = self.world()
            if kind == "range":
                world.block([2, 64, 0], "stone")
            original = world.get_status
            reads_after_move = 0
            def status(**kwargs):
                nonlocal reads_after_move
                if self.writes(world):
                    reads_after_move += 1
                    final = reads_after_move >= 3
                    if kind == "height": world.status["pos"] = [1.5, 64.99 if final else 65.01, .5]
                    if kind == "range": world.status["pos"] = [1.83 if final else 1.79, 65, .5]
                    if kind == "shooter": world.status["pos"] = [1.54 if final else 1.5, 65, .5]
                return original(**kwargs)
            world.get_status = status
            old_entities = world.get_entities
            def entities(**kwargs):
                rows = old_entities(**kwargs)
                return [{"id": 3, "name": "skeleton", "type": "hostile", "pos": [17.52, 65, .5]}] if self.writes(world) else rows
            if kind == "shooter": world.get_entities = entities
            result = self.run_step(world, target)
            self.assertFalse(result.success, kind)
            self.assertEqual(result.data["action_state"], "unknown", result)
            self.assertEqual([r[0] for r in self.writes(world)], ["move"])


if __name__ == "__main__":
    unittest.main()
