"""Complete hurt acknowledgements reconcile through reads, never another write."""

import copy
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.survival import SurvivalSkills
from test_canopy_escape import terrain
from test_survival_skills import World, envelope


class InterruptedRetreatTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        for name, effect in (("monotonic", lambda: self.now), ("sleep", self.advance)):
            timer = patch("skills.survival.time." + name, side_effect=effect)
            timer.start()
            self.addCleanup(timer.stop)
        self.world = World()
        self.world.status.update(gamemode="survival", last_death={"t": 1}, speed=0,
                                 velocity=[0, 0, 0], jumping=False)
        self.frames = iter([])
        self.read_timeouts = []
        self.after_write = False
        self.original_status = self.world.get_status
        self.world.get_status = self.status
        self.reply = APIError("interrupted", "stopped because of hurt", 200, {"cause": "hurt"})
        self.world.move_effect = self.move

    def advance(self, seconds):
        self.now += seconds

    def status(self, **kwargs):
        if self.after_write:
            self.read_timeouts.append(kwargs.get("timeout_seconds"))
            frame = next(self.frames, {})
            if isinstance(frame, Exception):
                raise frame
            self.world.status.update(frame)
        return self.original_status(**kwargs)

    def move(self, pos, options):
        self.after_write = True
        self.world.status.update(pos=[2.5, 64, .5], health=16)
        if isinstance(self.reply, Exception):
            raise self.reply
        return copy.deepcopy(self.reply)

    def run_retreat(self, cancel=None):
        return SurvivalSkills(self.world).execute("retreat_from", {"pos": [-3, 64, .5]}, cancel)

    def assert_one_write(self):
        self.assertEqual([row[0] for row in self.world.writes()], ["move_to"])

    def assert_unknown(self, result):
        self.assertFalse(result.success, result)
        self.assertEqual(result.data.get("action_state"), "unknown", result)
        self.assert_one_write()

    def test_complete_error_acknowledgement_verifies_actual_position_without_success(self):
        result = self.run_retreat()
        self.assertFalse(result.success, result)
        self.assertEqual(result.error_code, "retreat_interrupted")
        self.assertEqual(result.data["action_state"], "terminated")
        self.assertTrue(result.data["candidate_failed"])
        self.assertTrue(result.data["backend_terminated"])
        self.assertFalse(result.data["retreat_complete"])
        self.assertNotEqual(result.data["target"], result.data["pos"])
        self.assertEqual(result.data["pos"], [2.5, 64, .5])
        self.assertEqual(result.data["actual_footing"], {"pos": [2, 63, 0], "name": "stone"})
        self.assertEqual(result.data["interruption"]["cause"], "hurt")
        self.assertEqual(result.data["interruption"]["http_status"], 200)
        self.assertEqual(result.data["interruption_observations"], 2)
        self.assertAlmostEqual(self.now - 100, .1)
        self.assertTrue(all(0 < timeout <= 1.5 for timeout in self.read_timeouts))
        self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)
        self.assert_one_write()

    def test_validated_success_envelope_with_hurt_is_also_failed_terminated(self):
        self.reply = envelope({"pos": [99, 70, 99], "interrupted_by": "hurt"})
        result = self.run_retreat()
        self.assertEqual(result.error_code, "retreat_interrupted", result)
        self.assertEqual(result.data["pos"], [2.5, 64, .5])
        self.assertEqual(result.data["interruption"]["response"], "success")
        self.assert_one_write()

    def test_non_authoritative_failures_never_start_the_reconciliation_reads(self):
        replies = [APIError("interrupted", "hurt", status, {"cause": "hurt"}) for status in (None, 503)]
        replies += [APIError("interrupted", "hurt", 200, details) for details in
                    ({}, {"cause": "stop"}, {"cause": "death"}, {"cause": "hurt", "action_state": "unknown"})]
        replies += [APIError(code, "hurt", 200, {"cause": "hurt"}) for code in
                    ("network_error", "request_timeout", "dead", "invalid_response")]
        replies += [{"ok": False, "error": {"code": "interrupted", "cause": "hurt", "message": "hurt"}}]
        for reply in replies:
            with self.subTest(reply=str(reply)):
                self.world.calls.clear()
                self.world.status.update(pos=[.5, 64, .5], health=20)
                self.after_write = False
                self.read_timeouts.clear()
                self.reply = reply
                self.assert_unknown(self.run_retreat())
                self.assertEqual(self.read_timeouts, [])

    def test_lifecycle_hazards_and_nonidle_status_remain_unknown(self):
        for update in ({"alive": False}, {"ready": False}, {"busy": "move_to"}, {"busy": False},
                       {"last_death": {"t": 2}}, {"dimension": "the_nether"}, {"on_fire": True},
                       {"held_controls": {"forward": True}}, {"in_water": True}):
            with self.subTest(update=update):
                original = copy.deepcopy(self.world.status)
                self.world.calls.clear()
                self.after_write = False
                self.frames = iter([update])
                self.assert_unknown(self.run_retreat())
                self.world.status = original

    def test_missing_busy_is_not_evidence_of_an_idle_backend(self):
        original = self.world.move_effect
        def move(pos, options):
            try:
                return original(pos, options)
            finally:
                self.world.status.pop("busy", None)
        self.world.move_effect = move
        self.assert_unknown(self.run_retreat())

    def test_missing_or_nonfinite_motion_evidence_is_never_assumed_stationary(self):
        for key, value in (("speed", None), ("speed", float("nan")),
                           ("velocity", None), ("velocity", [float("nan"), 0, 0])):
            with self.subTest(key=key, value=value):
                self.world.calls.clear()
                self.world.status.update(pos=[.5, 64, .5], health=20, speed=0, velocity=[0, 0, 0])
                self.after_write = False
                self.frames = iter([{key: value}])
                self.assert_unknown(self.run_retreat())

    def test_hurt_error_before_this_move_is_sent_has_no_reconciliation_authority(self):
        original = self.world.get_status
        def status(**kwargs):
            if len([row for row in self.world.calls if row[0] == "status"]) == 3:
                raise APIError("interrupted", "Unrelated status failure", 200, {"cause": "hurt"})
            return original(**kwargs)
        self.world.get_status = status
        result = self.run_retreat()
        self.assertFalse(result.success)
        self.assertNotEqual(result.error_code, "retreat_interrupted")
        self.assertFalse(result.data["executed"])
        self.assertNotIn("actual_footing_verified", result.data)
        self.assertFalse(self.world.writes())

    def test_damage_read_failure_or_life_change_after_geometry_remains_unknown(self):
        for update in ({"health": 15}, {"last_death": {"t": 2}},
                       APIError("network_error", "Lost proof")):
            with self.subTest(update=update):
                self.world.calls.clear()
                self.world.status.update(pos=[.5, 64, .5], health=20, last_death={"t": 1})
                self.after_write = False
                self.frames = iter([{}, update])
                self.assert_unknown(self.run_retreat())

    def test_actual_footing_and_full_body_footprint_must_be_known_and_safe(self):
        for pos, name, properties in (([2, 63, 0], "air", {}), ([2, 63, 0], "water", {}),
                                      ([2, 63, 0], "oak_leaves", {"waterlogged": True}),
                                      ([2, 64, 0], "stone", {})):
            with self.subTest(pos=pos, name=name):
                self.world.calls.clear()
                self.world.status.update(pos=[.5, 64, .5], health=20)
                self.after_write = False
                self.world.set_block(pos, name, properties=properties)
                self.assert_unknown(self.run_retreat())
                self.world.blocks.clear()
        self.world.calls.clear()
        self.world.status.update(pos=[.5, 64, .5], health=20)
        self.after_write = False
        self.frames = iter([{"pos": [2.9, 64, .5]}])
        self.world.set_block([3, 64, 0], "stone")
        self.assert_unknown(self.run_retreat())

    def test_unloaded_actual_footing_stays_unknown(self):
        original = self.world.get_block
        def read(pos, **kwargs):
            if self.after_write:
                raise APIError("not_loaded", "Proof outside observed terrain")
            return original(pos, **kwargs)
        self.world.get_block = read
        self.assert_unknown(self.run_retreat())

    def test_motion_that_never_settles_exhausts_only_the_bounded_read_budget(self):
        self.frames = iter([{"on_ground": False, "speed": 1, "velocity": [1, 0, 0]}])
        self.assert_unknown(self.run_retreat())
        self.assertLessEqual(self.now - 100, 1.500001)
        self.assertLessEqual(len(self.read_timeouts), 16)

    def test_cancel_after_geometry_cannot_become_a_known_interruption(self):
        cancel = threading.Event()
        def wait(seconds):
            self.advance(seconds)
            cancel.set()
        cancel.wait = wait
        self.assert_unknown(self.run_retreat(cancel))

    def test_canopy_escape_does_not_gain_hurt_reconciliation_authority(self):
        world = terrain([(0, 70, 0, "oak_leaves"), (1, 69, 0, "dirt")])
        def move(pos, options):
            raise APIError("interrupted", "hurt", 200, {"cause": "hurt"})
        world.move_effect = move
        result = SurvivalSkills(world).execute("canopy_escape", {})
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual([row[0] for row in world.writes()], ["move_to"])


if __name__ == "__main__":
    unittest.main()
