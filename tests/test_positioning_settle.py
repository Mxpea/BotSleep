"""A completed step may need a few read-only ticks to stop moving."""

import copy
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.positioning import PositioningSkills
import test_positioning_steps


class TerrainLandingSettleTests(unittest.TestCase):
    def world(self, dy=-1):
        return test_positioning_steps.PositioningStepTests().world(dy=dy)

    @staticmethod
    def writes(world):
        return [row for row in world.calls if row[0] in {"move", "stop", "dig", "place", "control"}]

    def run_samples(self, world, target, samples, *, read_limit=96, entity_effect=None):
        now = [100.]
        sleeps = []
        status_reads = [0]
        original = world.get_status
        def status(**kwargs):
            if self.writes(world):
                self.assertLessEqual(kwargs["timeout_seconds"], 1.5)
                update = samples[min(status_reads[0], len(samples) - 1)]
                status_reads[0] += 1
                world.status.update(copy.deepcopy(update))
                if entity_effect is not None:
                    entity_effect(world, status_reads[0])
            return original(**kwargs)
        world.get_status = status
        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, .1)
            sleeps.append(seconds)
            now[0] += seconds
        with patch("skills.positioning.time.monotonic", side_effect=lambda: now[0]), \
             patch("skills.positioning.time.sleep", side_effect=sleep), \
             patch.object(PositioningSkills, "MAX_READS", read_limit):
            result = PositioningSkills(world).execute("position_for_task", {"pos": target})
        self.assertEqual([row[0] for row in self.writes(world)], ["move"])
        return result, sleeps, status_reads[0]

    def test_downhill_residual_velocity_waits_then_proves_stable_actual_footing(self):
        world, target = self.world()
        result, sleeps, reads = self.run_samples(world, target, [
            {"speed": 1.2, "velocity": [-1.2,0,0]},
            {"speed": 0, "velocity": [0,0,0]},
        ])
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["landing_stable"])
        self.assertEqual(result.data["pos"], target)
        self.assertGreaterEqual(reads, 4)
        self.assertGreaterEqual(sum(sleeps), .2)
        evidence = result.data["landing_observation"]
        self.assertEqual(evidence["first"]["waiting_for"], ["speed", "velocity"])
        self.assertEqual(evidence["last"]["waiting_for"], [])
        self.assertEqual(evidence["termination"], "verified")

    def test_grounded_height_error_and_airborne_sample_can_settle_without_another_write(self):
        for first in ({"pos": [1.5,63.12,.5]},
                      {"on_ground": False, "speed": .8, "velocity": [0,-.8,0]}):
            with self.subTest(first=first):
                world, target = self.world()
                result, sleeps, _ = self.run_samples(world, target, [first,
                    {"pos": target, "on_ground": True, "speed": 0, "velocity": [0,0,0]}])
                self.assertTrue(result.success, result)
                self.assertGreaterEqual(sum(sleeps), .2)
                self.assertEqual(result.data["landing_observation"]["motion_wait_samples"], 1)

    def test_last_status_residual_motion_restarts_the_stable_pair(self):
        world, target = self.world(dy=1)
        result, sleeps, reads = self.run_samples(world, target, [
            {}, {}, {"speed": .2, "velocity": [.2,0,0]},
            {"speed": 0, "velocity": [0,0,0]},
        ])
        self.assertTrue(result.success, result)
        self.assertGreaterEqual(reads, 6)
        self.assertGreaterEqual(sum(sleeps), .3)
        self.assertEqual(result.data["landing_observation"]["motion_wait_samples"], 1)

    def test_persistent_motion_uses_bounded_waits_and_expires_unknown(self):
        for motion in ({"speed": .2, "velocity": [.2,0,0]}, {"pos": [1.5,63.12,.5]}):
            with self.subTest(motion=motion):
                world, target = self.world()
                result, sleeps, reads = self.run_samples(world, target, [motion])
                self.assertFalse(result.success, result)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["landing_observation"]["termination"], "deadline")
                self.assertGreaterEqual(sum(sleeps), 1.49)
                self.assertLessEqual(sum(sleeps), 1.501)
                self.assertGreaterEqual(reads, 10)
                self.assertLessEqual(reads, 16)
                self.assertLessEqual(result.data["read_calls"], 96)

    def test_hurt_life_dimension_controls_or_hazard_during_motion_is_immediately_unknown(self):
        for unsafe in ({"health": 19}, {"last_death": {"t": 2}}, {"dimension": "the_nether"},
                       {"held_controls": {"keys": ["forward"]}}, {"jumping": True}, {"in_water": True},
                       {"busy": "other_action"}, {"on_ground": None}, {"velocity": None}):
            with self.subTest(unsafe=unsafe):
                world, target = self.world()
                result, sleeps, reads = self.run_samples(world, target, [
                    {"speed": .3, "velocity": [.3,0,0]}, unsafe])
                self.assertFalse(result.success, result)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertLessEqual(reads, 2)
                self.assertLessEqual(sum(sleeps), .101)
                self.assertNotEqual(result.data["landing_observation"]["termination"], "deadline")

    def test_fresh_second_shooter_interrupts_motion_wait_even_with_safe_nearest(self):
        world, target = self.world()
        world.status["nearest_hostile"] = {"id": 1, "name": "zombie", "dist": 9}
        def enemies(world, samples):
            if samples >= 2:
                world.entities = [{"id": 2, "name": "skeleton", "type": "hostile", "pos": [17,63,.5]}]
        result, sleeps, reads = self.run_samples(world, target,
            [{"speed": .3, "velocity": [.3,0,0]}], entity_effect=enemies)
        self.assertEqual(result.data["action_state"], "unknown", result)
        self.assertEqual(result.data["landing_observation"]["termination"], "unsafe")
        self.assertEqual(reads, 2)
        self.assertLessEqual(sum(sleeps), .101)

    def test_read_budget_still_limits_motion_wait_before_the_time_deadline(self):
        world, target = self.world()
        result, sleeps, _ = self.run_samples(world, target,
            [{"speed": .3, "velocity": [.3,0,0]}], read_limit=30)
        self.assertEqual(result.data["action_state"], "unknown", result)
        self.assertEqual(result.data["landing_observation"]["termination"], "read_budget")
        self.assertEqual(result.data["read_calls"], 30)
        self.assertLess(sum(sleeps), 1.5)

    def test_unavailable_fresh_threat_scan_never_becomes_permission_to_keep_waiting(self):
        world, target = self.world()
        original = world.get_entities
        def entities(**kwargs):
            if self.writes(world):
                raise APIError("network_error", "Fresh enemies unavailable")
            return original(**kwargs)
        world.get_entities = entities
        result, sleeps, reads = self.run_samples(world, target,
            [{"speed": .3, "velocity": [.3,0,0]}])
        self.assertEqual(result.data["action_state"], "unknown", result)
        self.assertEqual(result.data["landing_observation"]["termination"], "network_error")
        self.assertEqual(reads, 1)
        self.assertFalse(sleeps)


if __name__ == "__main__":
    unittest.main()
