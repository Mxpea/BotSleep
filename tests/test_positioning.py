"""Exact task stances against a fake world; no model or Minecraft connection."""

import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.positioning import PositioningSkills
from test_daily_skills import DailyWorld, envelope


class PositioningTests(unittest.TestCase):
    def setUp(self):
        self.world = DailyWorld()
        self.skills = PositioningSkills(self.world)

    def move(self, pos=None, cancel=None):
        return self.skills.execute("position_for_task", {"pos": pos or [1.5, 64, .5]}, cancel)

    def moves(self):
        return [call for call in self.world.calls if call[0] == "move"]

    def test_adjacent_task_cell_is_reached_with_range_zero_not_loose_noop(self):
        result = self.move()
        self.assertTrue(result.success, result)
        self.assertEqual(self.moves()[0][2], {"range": 0, "timeout_ms": 8000})
        self.assertEqual(result.data["pos"], [1.5, 64, .5])
        self.assertTrue(result.data["executed"])
        self.assertFalse(result.data["actual_path_verified"])
        self.assertLessEqual(result.data["read_calls"], 96)

    def test_already_centered_is_verified_without_movement(self):
        result = self.move([.5, 64, .5])
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["already_positioned"])
        self.assertFalse(result.data["executed"])
        self.assertFalse(self.moves())

    def test_partial_shapes_leaf_support_water_and_open_doors_are_not_passable(self):
        for block, where, props in (("water", [1, 64, 0], {}), ("oak_door", [1, 64, 0], {"open": True}),
                                    ("oak_leaves", [1, 63, 0], {"waterlogged": False}),
                                    ("stone_slab", [1, 63, 0], {"type": "bottom"}),
                                    ("short_grass", [1, 64, 0], {"waterlogged": True})):
            self.world = DailyWorld()
            self.skills = PositioningSkills(self.world)
            self.world.block(where, block, properties=props)
            with self.subTest(block=block):
                self.assertEqual(self.move().error_code, "unsafe_route")
                self.assertFalse(self.moves())

    def test_full_swept_width_rejects_diagonal_corner_not_only_center_line(self):
        self.world.block([1, 64, 0], "stone")
        result = self.move([2.5, 64, 2.5])
        self.assertEqual(result.error_code, "unsafe_route")
        self.assertFalse(self.moves())

    def test_start_body_edge_and_every_swept_floor_cell_are_checked(self):
        self.world.status["pos"] = [.95, 64, .5]
        self.world.block([1, 63, 0], "air")
        self.assertEqual(self.move().error_code, "unsafe_route")
        self.assertFalse(self.moves())

    def test_unknown_loaded_cell_stops_without_write(self):
        original = self.world.get_block
        def block(pos, **kwargs):
            if pos == [1, 65, 0]:
                raise APIError("not_loaded", "Unknown body block")
            return original(pos, **kwargs)
        self.world.get_block = block
        result = self.move()
        self.assertEqual(result.error_code, "not_loaded")
        self.assertFalse(self.moves())

    def test_unknown_shapes_are_rejected_but_pass_through_plants_are_allowed(self):
        for name in ("short_grass", "leaf_litter", "poppy"):
            self.world.block([1, 64, 0], name)
            result = self.move()
            self.assertTrue(result.success, result)
            self.world.status["pos"] = [.5, 64, .5]

    def test_height_distance_and_noncenter_targets_are_rejected(self):
        for pos in ([1.5, 65, .5], [4.5, 64, .5], [1.2, 64, .5], [1.5, 64.5, .5]):
            with self.subTest(pos=pos):
                self.assertFalse(self.move(pos).success)
                self.assertFalse(self.moves())

    def test_target_near_hostile_is_rejected_even_if_bot_is_farther(self):
        self.world.entities = [{"type": "hostile", "name": "zombie", "pos": [10, 64, .5]}]
        result = self.move([3.5, 64, .5])
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(self.moves())

    def test_route_is_reloaded_immediately_before_single_move(self):
        original = self.world.get_block
        hits = 0
        def block(pos, **kwargs):
            nonlocal hits
            if pos == [1, 64, 0]:
                hits += 1
                if hits == 2:
                    self.world.block(pos, "stone")
            return original(pos, **kwargs)
        self.world.get_block = block
        self.assertEqual(self.move().error_code, "unsafe_route")
        self.assertFalse(self.moves())

    def test_origin_push_during_scan_prevents_old_move(self):
        original = self.world.get_block
        def block(pos, **kwargs):
            value = original(pos, **kwargs)
            if pos == [1, 63, 0]:
                self.world.status["pos"] = [.5, 64, 1.5]
            return value
        self.world.get_block = block
        self.assertEqual(self.move().error_code, "safety_changed")
        self.assertFalse(self.moves())

    def test_backend_ack_and_same_cell_are_insufficient_without_center_distance(self):
        def incomplete():
            self.world.status["pos"] = [1.01, 64, .5]
            return envelope({"pos": [1.5, 64, .5]})
        self.world.effects["move"] = incomplete
        result = self.move()
        self.assertEqual(result.error_code, "position_unverified")
        self.assertEqual(len(self.moves()), 1)

    def test_backend_half_block_early_return_is_not_claimed_as_success(self):
        self.world.status["pos"] = [1.05, 64, .5]
        def unchanged():
            self.world.status["pos"] = [1.05, 64, .5]
            return envelope({"pos": self.world.status["pos"]})
        self.world.effects["move"] = unchanged
        self.assertEqual(self.move().error_code, "position_unverified")
        self.assertEqual(len(self.moves()), 1)

    def test_transport_timeout_is_unknown_and_never_retried(self):
        def fail():
            raise APIError("request_timeout", "Unknown position")
        self.world.effects["move"] = fail
        result = self.move()
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.moves()), 1)

    def test_cancel_or_death_after_send_keeps_unknown(self):
        for cause in ("cancel", "death"):
            self.world = DailyWorld()
            self.skills = PositioningSkills(self.world)
            event = threading.Event()
            def after_send():
                if cause == "cancel":
                    event.set()
                else:
                    self.world.status.update(alive=False, health=0)
                return envelope({"pos": [1.5, 64, .5]})
            self.world.effects["move"] = after_send
            result = self.move(cancel=event)
            with self.subTest(cause=cause):
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(len(self.moves()), 1)

    def test_read_or_time_budget_exhaustion_does_not_start_movement(self):
        for attr in ("MAX_READS", "READ_SECONDS"):
            with self.subTest(attr=attr), patch.object(PositioningSkills, attr, 0):
                result = self.move()
                self.assertEqual(result.error_code, "position_budget_exhausted")
                self.assertFalse(self.moves())


if __name__ == "__main__":
    unittest.main()
