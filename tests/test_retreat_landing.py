"""Deterministic landing fixtures; no live API, RCON or model calls."""

import copy
import math
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.survival import SurvivalSkills


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class LandingWorld:
    """A checked canopy route with scripted status frames after one move."""

    def __init__(self, frames):
        self.status = {"ready": True, "alive": True, "health": 20, "food": 20,
                       "dimension": "overworld", "pos": [0.5, 82, 0.5],
                       "busy": None, "on_ground": True}
        self.blocks = {(0, 81, 0): {"name": "birch_leaves", "properties": {"waterlogged": False}},
                       (1, 80, 0): {"name": "oak_leaves", "properties": {"waterlogged": False}},
                       (2, 79, 0): {"name": "dirt"}}
        self.frames = frames
        self.calls = []
        self.landing_reads = 0
        self.moved = False
        self.on_frame = None
        self.on_move = None

    @staticmethod
    def envelope(result):
        return {"ok": True, "result": copy.deepcopy(result)}

    def get_status(self, *, timeout_seconds=None):
        self.calls.append(("status", timeout_seconds))
        if self.moved:
            self.status.update(copy.deepcopy(self.frames[min(self.landing_reads, len(self.frames) - 1)]))
            self.landing_reads += 1
            if self.on_frame:
                self.on_frame(self)
        return self.envelope(self.status)

    def get_block(self, pos, *, timeout_seconds=None):
        self.calls.append(("block", tuple(pos), timeout_seconds))
        return self.envelope(self.blocks.get(tuple(pos), {"name": "air", "properties": {}}))

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("move_to", [x, y, z], kwargs))
        self.moved = True
        if self.on_move:
            self.on_move()
        return self.envelope({"pos": [x, y, z]})

    def stop(self):
        self.calls.append(("stop",))
        return self.envelope({"stopped": True})


AIRBORNE = {"pos": [1.5, 81.9, 0.5], "on_ground": False}
LANDED = {"pos": [1.5, 81, 0.5], "on_ground": True}


class RetreatLandingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for target, replacement in (("monotonic", self.clock.monotonic), ("sleep", self.clock.sleep)):
            mock = patch("skills.survival.time." + target, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def execute(self, world, cancel=None):
        result = SurvivalSkills(world).execute("retreat_from", {"pos": [-3, 82, 0.5]}, cancel)
        self.assertEqual(len([c for c in world.calls if c[0] == "move_to"]), 1)
        self.assertEqual(result.data["target"], [1.5, 81, 0.5])
        self.assertEqual(result.data["planned_path"], [[0.5, 82, 0.5], [1.5, 81, 0.5], [2.5, 80, 0.5]])
        return result

    def assert_stopped(self, world, result, reason):
        self.assertFalse(result.success)
        self.assertEqual(result.data["landing_reason"], reason)
        self.assertEqual(len([c for c in world.calls if c[0] == "stop"]), 1)
        self.assertAlmostEqual(result.data["moved"], round(math.dist([0.5, 82, 0.5], result.data["pos"]), 3))

    def test_airborne_then_landed_at_checked_step(self):
        world = LandingWorld([AIRBORNE, {"pos": [1.5, 81.3, 0.5], "on_ground": False}, LANDED])
        result = self.execute(world)
        self.assertTrue(result.success, result)
        self.assertEqual(world.landing_reads, 3)
        self.assertEqual(result.data["landing_reason"], "confirmed")
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "relay")
        self.assertFalse(any(c[0] == "stop" for c in world.calls))
        move_index = next(i for i, c in enumerate(world.calls) if c[0] == "move_to")
        self.assertEqual([c[1] for c in world.calls[move_index + 1:] if c[0] == "block"],
                         [(1, 80, 0), (1, 81, 0), (1, 82, 0)])

    def test_proximity_does_not_expand_exact_target_cell(self):
        world = LandingWorld([{"pos": [0.99, 81, 0.5], "on_ground": True}])
        self.assert_stopped(world, self.execute(world), "wrong_cell")
        self.assertEqual(world.landing_reads, 1)

    def test_not_near_target_is_rejected_without_wait(self):
        world = LandingWorld([{"pos": [0.6, 82, 0.5], "on_ground": False}])
        result = self.execute(world)
        self.assert_stopped(world, result, "wrong_cell")
        self.assertEqual(self.clock.now, 0)
        self.assertGreater(result.data["moved"], 0)

    def test_floating_in_target_cell_never_counts_as_success(self):
        world = LandingWorld([AIRBORNE])
        result = self.execute(world)
        self.assert_stopped(world, result, "not_grounded")
        self.assertEqual(result.error_code, "retreat_unverified")
        self.assertEqual(world.landing_reads, 6)
        self.assertGreaterEqual(self.clock.now, 1.0)
        self.assertLessEqual(self.clock.now, 1.25)
        self.assertEqual(result.data["landing_termination"], "read_limit")

    def test_height_outside_step_transition_is_not_waited_out(self):
        for height in (80.9, 82.6):
            with self.subTest(height=height):
                world = LandingWorld([{"pos": [1.5, height, 0.5], "on_ground": False}])
                self.assert_stopped(world, self.execute(world), "height_band")
                self.assertEqual(world.landing_reads, 1)

    def test_drift_away_from_target_stops_polling(self):
        world = LandingWorld([AIRBORNE, {"pos": [1.2, 81.5, 0.5], "on_ground": False}, LANDED])
        self.assert_stopped(world, self.execute(world), "distance_regression")
        self.assertEqual(world.landing_reads, 2)

    def test_damage_stops_even_on_the_landing_frame(self):
        world = LandingWorld([AIRBORNE, {**LANDED, "health": 19}])
        self.assert_stopped(world, self.execute(world), "health_changed")
        self.assertEqual(world.landing_reads, 2)

    def test_respawn_marker_cannot_confirm_the_previous_retreat(self):
        world = LandingWorld([AIRBORNE, {**LANDED, "last_death": {"t": 1, "pos": [0, 82, 0]}}])
        result = self.execute(world)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len([call for call in world.calls if call[0] == "move_to"]), 1)

    def test_regeneration_does_not_turn_a_checked_landing_into_failure(self):
        world = LandingWorld([AIRBORNE, {**LANDED, "health": 21}])
        result = self.execute(world)
        self.assertTrue(result.success, result)
        self.assertEqual(len([call for call in world.calls if call[0] == "move_to"]), 1)

    def test_cancel_during_status_read_records_observed_displacement(self):
        cancel = threading.Event()
        world = LandingWorld([AIRBORNE])
        world.on_frame = lambda _world: cancel.set()
        result = self.execute(world, cancel)
        self.assert_stopped(world, result, "cancelled")
        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual(world.landing_reads, 1)

    def test_dimension_and_life_changes_keep_last_observed_position(self):
        for update, reason in (({"dimension": "the_nether"}, "dimension_changed"),
                               ({"alive": False}, "dead"), ({"busy": "pathfinding"}, "busy"),
                               ({"in_vehicle": True}, "unsafe_state")):
            with self.subTest(update=update):
                world = LandingWorld([AIRBORNE, {**LANDED, **update}])
                result = self.execute(world)
                self.assertFalse(result.success)
                self.assertEqual(result.data["landing_reason"], reason)
                self.assertGreater(result.data["moved"], 0)
                self.assertEqual(world.landing_reads, 2)

    def test_new_environment_hazards_stop_waiting(self):
        for flag in ("in_water", "in_lava", "on_fire"):
            with self.subTest(flag=flag):
                world = LandingWorld([AIRBORNE, {**LANDED, flag: True}])
                self.assert_stopped(world, self.execute(world), "hazard")

    def test_closer_or_new_hostile_stops_waiting(self):
        for initial in (None, {"id": 1, "name": "zombie", "dist": 6}):
            with self.subTest(initial=initial):
                world = LandingWorld([AIRBORNE, {**LANDED, "nearest_hostile": {"id": 1, "name": "zombie", "dist": 4}}])
                world.status["nearest_hostile"] = initial
                self.assert_stopped(world, self.execute(world), "hostile_closer" if initial else "new_hostile")

    def test_slow_reads_obey_deadline_including_late_landing(self):
        world = LandingWorld([AIRBORNE, LANDED])
        world.on_frame = lambda _world: self.clock.sleep(0.65)
        result = self.execute(world)
        self.assert_stopped(world, result, "not_grounded")
        self.assertEqual(result.data["landing_termination"], "timeout")
        self.assertEqual(world.landing_reads, 2)
        budgets = [c[1] for c in world.calls if c[0] == "status" and c[1] is not None]
        self.assertEqual(len(budgets), 2)
        self.assertLess(budgets[1], budgets[0])
        self.assertLessEqual(budgets[0], 1.25)

    def test_support_is_rechecked_after_waiting(self):
        world = LandingWorld([AIRBORNE, LANDED])
        def lose_support(client):
            if client.landing_reads == 2:
                client.blocks[(1, 80, 0)] = {"name": "lava"}
        world.on_frame = lose_support
        result = self.execute(world)
        self.assert_stopped(world, result, "unsafe_support")
        self.assertIn({"pos": [1, 80, 0], "name": "lava", "reason": "hazard"}, result.data["blocked_cells"])

    def test_unknown_move_outcome_is_never_replayed_or_polled(self):
        world = LandingWorld([LANDED])
        def unknown():
            raise APIError("timeout", "Move outcome unknown")
        world.on_move = unknown
        result = self.execute(world)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "timeout")
        self.assertEqual(world.landing_reads, 0)
        self.assertEqual(len([c for c in world.calls if c[0] == "stop"]), 1)


if __name__ == "__main__":
    unittest.main()
