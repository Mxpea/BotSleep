"""Offline behavioral tests: no HTTP requests or game processes are started."""

import copy
import unittest
from unittest.mock import patch

from skills.sleep import AutoSleepSkill


def state(**changes):
    return {"ready": True, "alive": True, "health": 20, "dimension": "overworld",
            "pos": [0, 64, 0], "is_day": False, "thundering": False,
            "is_sleeping": False, **changes}


def bed(pos=(3, 64, 0), *, occupied=False, part="head", facing="east"):
    return {"name": "red_bed", "pos": list(pos),
            "properties": {"occupied": occupied, "part": part, "facing": facing}}


def ok(result):
    return {"ok": True, "result": result}


class FakeFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Cancel:
    def __init__(self, clock, *, cancelled=False, on_wait=False):
        self.clock = clock
        self.cancelled = cancelled
        self.on_wait = on_wait

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.clock.sleep(seconds)
        if self.on_wait:
            self.cancelled = True
        return self.cancelled


class Client:
    def __init__(self):
        self.current = state()
        self.blocks = [bed()]
        self.calls = []
        self.move_errors = {}
        self.block_overrides = {}
        self.entities = []
        self.after_move = None
        self.interaction = {"slept": True}
        self.after_interact = [state(is_sleeping=True), state(is_sleeping=True), state(is_day=True)]
        self.pending_statuses = []
        self.status_error = None

    def get_status(self):
        self.calls.append(("status",))
        if self.status_error:
            raise FakeFailure(self.status_error)
        if self.pending_statuses:
            self.current = self.pending_statuses.pop(0)
        return ok(copy.deepcopy(self.current))

    def get_blocks(self, **kwargs):
        self.calls.append(("blocks", kwargs))
        return copy.deepcopy(self.blocks)

    def get_entities(self, **kwargs):
        self.calls.append(("entities", kwargs))
        return copy.deepcopy(self.entities)

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("move", [x, y, z], kwargs))
        if (x, y, z) in self.move_errors:
            raise FakeFailure(self.move_errors[(x, y, z)])
        self.current["pos"] = [x - 1, y, z]
        if self.after_move:
            self.after_move(self)
        return ok({"pos": self.current["pos"]})

    def get_block(self, pos):
        self.calls.append(("block", pos))
        return ok(copy.deepcopy(self.block_overrides.get(tuple(pos), bed(pos))))

    def interact(self, **kwargs):
        self.calls.append(("interact", kwargs))
        if isinstance(self.interaction, Exception):
            raise self.interaction
        self.pending_statuses = copy.deepcopy(self.after_interact)
        return ok(self.interaction)

    def stop(self):
        self.calls.append(("stop",))
        return ok({"stopped": True})


class AutoSleepTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = Client()
        self.skill = AutoSleepSkill(self.client)
        self.patches = [patch("skills.sleep.time.monotonic", self.clock.monotonic),
                        patch("skills.sleep.time.sleep", self.clock.sleep)]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def run_skill(self, **kwargs):
        return self.skill.execute(**{"timeout_seconds": 10, "confirm_timeout_seconds": 1,
                                     "poll_interval_seconds": 0.25, **kwargs})

    def calls(self, name):
        return [call for call in self.client.calls if call[0] == name]

    def assert_no_actions(self):
        self.assertFalse(self.calls("move"))
        self.assertFalse(self.calls("interact"))

    def test_observed_sleep_then_multiplayer_wait_and_wake(self):
        result = self.run_skill()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["verification"], "observed_sleep_then_wake")
        self.assertEqual(result.data["bed_pos"], [3, 64, 0])
        self.assertGreaterEqual(self.clock.now, 0.5)
        self.assertEqual(len(self.calls("interact")), 1)
        self.assertEqual(self.calls("move")[0][2]["range"], 2)
        self.assertEqual(set(self.calls("blocks")[0][1]["types"]), set(AutoSleepSkill.BED_TYPES))
        self.assertEqual(len(AutoSleepSkill.BED_TYPES), 16)

    def test_successful_http_click_does_not_prove_sleep(self):
        self.client.interaction = {"interacted": True, "slept": False}
        self.client.after_interact = []
        result = self.run_skill()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "sleep_unverified")

    def test_reported_sleep_without_observation_or_transition_is_unverified(self):
        self.client.after_interact = []
        result = self.run_skill()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "sleep_unverified")

    def test_single_player_instant_night_skip_has_two_pieces_of_evidence(self):
        self.client.after_interact = [state(is_day=True)]
        result = self.run_skill()
        self.assertTrue(result.success)
        self.assertEqual(result.data["verification"], "reported_sleep_and_time_transition")
        self.assertEqual([e["kind"] for e in result.data["evidence"]], ["interaction", "instant_sleep_transition"])

    def test_natural_dawn_without_slept_ack_is_not_success(self):
        self.client.interaction = {"slept": False}
        self.client.after_interact = [state(is_day=True)]
        result = self.run_skill()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "sleep_unverified")

    def test_daytime_storm_can_be_slept_through(self):
        self.client.current = state(is_day=True, thundering=True)
        self.client.after_interact = [state(is_day=True)]
        self.assertTrue(self.run_skill().success)

    def test_no_bed_reports_failure_without_actions(self):
        self.client.blocks = []
        result = self.run_skill()
        self.assertEqual(result.error_code, "no_bed_nearby")
        self.assert_no_actions()

    def test_daytime_reports_failure_without_scanning_or_actions(self):
        self.client.current = state(is_day=True)
        self.assertEqual(self.run_skill().error_code, "daytime")
        self.assertFalse(self.calls("blocks"))
        self.assert_no_actions()

    def test_dangerous_dimensions_never_interact_with_beds(self):
        for dimension in ("the_nether", "the_end", "minecraft:the_nether", "unknown"):
            with self.subTest(dimension=dimension):
                self.client.current = state(dimension=dimension)
                self.assertEqual(self.run_skill().error_code, "unsafe_dimension")
        self.assert_no_actions()

    def test_dimension_is_rechecked_after_walking(self):
        self.client.after_move = lambda c: c.current.update(dimension="the_nether")
        result = self.run_skill()
        self.assertEqual(result.error_code, "unsafe_dimension")
        self.assertFalse(self.calls("interact"))
        self.assertTrue(self.calls("stop"))

    def test_death_after_walking_is_not_a_sleep_failure_or_success(self):
        self.client.after_move = lambda c: c.current.update(alive=False, health=0)
        self.assertEqual(self.run_skill().error_code, "dead")
        self.assertFalse(self.calls("interact"))

    def test_not_ready_and_disconnected_remain_visible(self):
        self.client.current["ready"] = False
        self.assertEqual(self.run_skill().error_code, "not_ready")
        self.client.status_error = "not_connected"
        self.assertEqual(self.run_skill().error_code, "disconnected")
        self.assert_no_actions()

    def test_nearby_monster_refuses_sleep_without_pretending_to_retreat(self):
        self.client.entities = [{"id": 1, "type": "hostile", "name": "zombie", "pos": [1, 64, 1]}]
        result = self.run_skill()
        self.assertEqual(result.error_code, "monsters_nearby")
        self.assertEqual(result.data["threats"][0]["name"], "zombie")
        self.assert_no_actions()

    def test_monsters_are_checked_again_after_walking(self):
        self.client.after_move = lambda c: c.entities.append({"type": "hostile", "pos": [3, 64, 1]})
        self.assertEqual(self.run_skill().error_code, "monsters_nearby")
        self.assertEqual(len(self.calls("move")), 1)
        self.assertFalse(self.calls("interact"))

    def test_threat_check_can_be_disabled(self):
        self.client.entities = [{"type": "hostile", "pos": [1, 64, 1]}]
        self.assertTrue(self.run_skill(threat_check=False).success)
        self.assertFalse(self.calls("entities"))

    def test_bed_halves_are_deduplicated_and_occupied_bed_is_skipped(self):
        self.client.blocks = [bed((2, 64, 0), part="foot"), bed((3, 64, 0), occupied=True), bed((8, 64, 0))]
        result = self.run_skill()
        self.assertTrue(result.success)
        self.assertEqual(len(result.data["attempts"]), 2)
        self.assertEqual(result.data["attempts"][0]["error_code"], "bed_occupied")
        self.assertEqual([c[1] for c in self.calls("move")], [[8, 64, 0]])

    def test_all_occupied_beds_fail_without_actions(self):
        self.client.blocks = [bed(occupied=True)]
        self.assertEqual(self.run_skill().error_code, "bed_occupied")
        self.assert_no_actions()

    def test_unreachable_nearest_bed_tries_next_bed(self):
        self.client.blocks = [bed((8, 64, 0)), bed((3, 64, 0))]
        self.client.move_errors[(3, 64, 0)] = "path_not_found"
        result = self.run_skill()
        self.assertTrue(result.success)
        self.assertEqual([c[1] for c in self.calls("move")], [[3, 64, 0], [8, 64, 0]])
        self.assertEqual(result.data["attempts"][0]["error_code"], "path_unreachable")

    def test_all_unreachable_beds_fail(self):
        self.client.move_errors[(3, 64, 0)] = "stalled"
        self.assertEqual(self.run_skill().error_code, "path_unreachable")
        self.assertFalse(self.calls("interact"))

    def test_partial_movement_interrupted_by_hurt_does_not_continue(self):
        self.client.move_to = lambda *args, **kwargs: ok({"pos": [2, 64, 0], "interrupted_by": "hurt"})
        self.assertEqual(self.run_skill().error_code, "interrupted")
        self.assertFalse(self.calls("interact"))
        self.assertTrue(self.calls("stop"))

    def test_movement_http_timeout_does_not_retry_unknown_action(self):
        self.client.blocks.append(bed((8, 64, 0)))
        self.client.move_errors[(3, 64, 0)] = "request_timeout"
        result = self.run_skill()
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual(len(self.calls("move")), 1)
        self.assertFalse(self.calls("interact"))
        self.assertTrue(self.calls("stop"))

    def test_still_far_from_bed_does_not_start_another_hidden_path(self):
        self.client.after_move = lambda c: c.current.update(pos=[100, 64, 0])
        self.assertEqual(self.run_skill().error_code, "path_unreachable")
        self.assertFalse(self.calls("interact"))

    def test_interaction_timeout_does_not_retry_bed_click(self):
        self.client.blocks.append(bed((8, 64, 0)))
        self.client.interaction = FakeFailure("request_timeout")
        self.assertEqual(self.run_skill().error_code, "request_timeout")
        self.assertEqual(len(self.calls("interact")), 1)
        self.assertTrue(self.calls("stop"))

    def test_changed_bed_is_skipped_before_click(self):
        self.client.blocks.append(bed((8, 64, 0)))
        self.client.block_overrides[(3, 64, 0)] = {"name": "stone"}
        result = self.run_skill()
        self.assertTrue(result.success)
        self.assertEqual(self.calls("interact")[0][1]["pos"], [8, 64, 0])

    def test_cancellation_before_start_makes_no_requests(self):
        event = Cancel(self.clock, cancelled=True)
        self.assertEqual(self.run_skill(cancel_event=event).error_code, "cancelled")
        self.assertEqual(self.client.calls, [])

    def test_cancellation_during_sleep_does_not_click_bed_again(self):
        result = self.run_skill(cancel_event=Cancel(self.clock, on_wait=True))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual(len(self.calls("interact")), 1)
        self.assertTrue(self.calls("stop"))

    def test_sleep_wait_has_overall_timeout_and_conservative_cleanup(self):
        self.client.after_interact = [state(is_sleeping=True)]
        result = self.run_skill(timeout_seconds=1)
        self.assertEqual(result.error_code, "timeout")
        self.assertEqual(self.clock.now, 1)
        self.assertEqual(len(self.calls("interact")), 1)
        self.assertTrue(self.calls("stop"))

    def test_waking_during_night_is_interruption(self):
        self.client.after_interact = [state(is_sleeping=True), state()]
        self.assertEqual(self.run_skill().error_code, "sleep_interrupted")
        self.assertEqual(self.clock.now, 1.25)
        self.assertEqual(len(self.calls("interact")), 1)

    def test_wake_packet_before_time_packet_waits_for_dawn_confirmation(self):
        self.client.after_interact = [state(is_sleeping=True), state(), state(), state(is_day=True)]
        result = self.run_skill()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["verification"], "observed_sleep_then_wake")
        self.assertEqual(self.clock.now, 0.75)
        self.assertIn("wake_pending_time_confirmation", [e["kind"] for e in result.data["evidence"]])
        self.assertEqual(len(self.calls("interact")), 1)

    def test_wake_packet_before_weather_packet_waits_for_storm_end(self):
        self.client.current = state(is_day=True, thundering=True)
        self.client.after_interact = [state(is_day=True, thundering=True, is_sleeping=True),
                                     state(is_day=True, thundering=True), state(is_day=True)]
        self.assertTrue(self.run_skill().success)
        self.assertEqual(len(self.calls("interact")), 1)

    def test_sleeping_again_resets_wake_confirmation_without_another_click(self):
        self.client.after_interact = [state(is_sleeping=True), state(), state(is_sleeping=True),
                                     state(is_sleeping=True), state(), state(is_day=True)]
        result = self.run_skill()
        self.assertTrue(result.success, result)
        self.assertIn("sleep_resumed", [e["kind"] for e in result.data["evidence"]])
        self.assertEqual(len(self.calls("interact")), 1)

    def test_wake_confirmation_does_not_extend_overall_deadline(self):
        self.client.after_interact = [state(is_sleeping=True), state()]
        result = self.run_skill(timeout_seconds=0.5, confirm_timeout_seconds=5)
        self.assertEqual(result.error_code, "timeout")
        self.assertEqual(self.clock.now, 0.5)

    def test_death_and_dimension_changes_are_checked_during_wake_confirmation(self):
        for changed, expected in ((state(alive=False, health=0), "dead"),
                                  (state(dimension="the_nether"), "unsafe_dimension")):
            with self.subTest(error=expected):
                self.client.current = state()
                self.client.after_interact = [state(is_sleeping=True), state(), changed]
                self.assertEqual(self.run_skill().error_code, expected)

    def test_dawn_after_wake_confirmation_deadline_still_fails(self):
        self.client.after_interact = [state(is_sleeping=True), *[state() for _ in range(8)], state(is_day=True)]
        result = self.run_skill()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "sleep_interrupted")
        self.assertEqual(self.clock.now, 1.25)

    def test_already_sleeping_waits_without_another_bed_click(self):
        self.client.pending_statuses = [state(is_sleeping=True), state(is_day=True)]
        result = self.run_skill()
        self.assertTrue(result.success)
        self.assertIsNone(result.data["bed_pos"])
        self.assert_no_actions()

    def test_api_error_is_not_converted_to_empty_bed_result(self):
        self.client.get_blocks = lambda **kwargs: (_ for _ in ()).throw(FakeFailure("unknown_api"))
        self.assertEqual(self.run_skill().error_code, "unknown_api")

    def test_invalid_inputs_do_not_call_api(self):
        for kwargs in ({"radius": 0}, {"radius": True}, {"timeout_seconds": float("nan")},
                       {"poll_interval_seconds": 0}, {"threat_check": "false"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.run_skill(**kwargs).error_code, "bad_args")
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
