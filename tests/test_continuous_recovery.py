"""Exercise recovery on real journal and harness entry points without a game server."""

import copy
import json
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from unittest.mock import Mock, patch

from agent.survival_planner import Decision
from core.client import APIError
from core.config import DEFAULTS
from core.model_gate import ModelGateError
from core.survival_harness import SurvivalHarness
from core.survival_state import SurvivalJournal
from test_task_harness import TaskWorld


class ContinuousRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = TaskWorld()
        self.settings = {**copy.deepcopy(DEFAULTS["survival"]), "continuous": True, "objective": "daily",
                         "state_dir": self.temp.name, "poll_interval_seconds": .001, "retry_seconds": .001}
        self.journal = SurvivalJournal(self.temp.name, endpoint=self.world.base_url, username=self.world.username,
                                      world_id=self.settings["world_id"])
        self.old = Decision("move_to", {"pos": [2, 64, 0], "range": 1}, "Old interrupted move", "explore")
        self.pending = {"decision": asdict(self.old), "before_counts": {}, "task_id": None}

    def harness(self):
        h = SurvivalHarness(self.world, self.settings, DEFAULTS["sleep"], journal=self.journal)
        self.addCleanup(h.close)
        return h

    def cycle(self, h):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            result = h.tick()
            if result is not None:
                return result
            time.sleep(.002)
        self.fail(h.snapshot())

    def test_interrupted_move_first_observes_without_game_writes_then_excludes_same_intent(self):
        self.journal.update(pending=self.pending, last_result={"error_code": "action_unconfirmed"})
        h = self.harness()
        self.assertFalse(h.paused)
        with patch.object(self.world, "move_to", side_effect=AssertionError("replayed move")), \
             patch.object(self.world, "window_view", side_effect=AssertionError("opened a container")):
            result = self.cycle(h)
        self.assertTrue(result.success)
        self.assertFalse(result.data["executed"])
        self.assertFalse(result.data["original_action_confirmed"])
        self.assertIsNone(h.journal.state["pending"])
        self.assertTrue(h._matches_abandoned_action(self.old, h.last_observation))
        alternative = Decision("move_to", {"pos": [0, 64, 2], "range": 1}, "Newly planned step", "explore")
        self.assertFalse(h._matches_abandoned_action(alternative, h.last_observation))
        events = [json.loads(line) for line in self.journal.events_path.read_text(encoding="utf8").splitlines()]
        evidence = next(r["data"] for r in events if r["event"] == "CONTINUOUS_INTENT_ABANDONED")
        self.assertEqual(evidence["prior_result"]["error_code"], "action_unconfirmed")

    def test_explicit_daily_resume_reads_back_the_recorded_night_step_without_replaying_it(self):
        self.settings["continuous"] = False
        pos = [1.5, 64, .5]
        old = Decision("position_for_task", {"pos": pos}, "Old checked night step", "explore")
        self.journal.update(pending={**self.pending, "decision": asdict(old)}, last_result={
            "error_code": "action_unconfirmed", "data": {"skill": "position_for_task", "target": pos,
            "night_home_step": True, "action_state": "unknown"}})
        h = self.harness()
        self.assertTrue(h.paused)
        self.assertTrue(h.resume())
        with patch.object(self.world, "move_to", side_effect=AssertionError("replayed move")), \
             patch.object(self.world, "window_view", side_effect=AssertionError("opened a container")):
            result = self.cycle(h)
        self.assertTrue(result.data["reobserved"])
        self.assertFalse(result.data["original_action_confirmed"])
        self.assertIsNone(h.journal.state["pending"])

    def test_headroom_readback_cannot_clear_intent_from_partial_evidence(self):
        self.settings["continuous"] = False
        old = Decision("clear_food_headroom", {"pos": [1, 65, 0], "expected_name": "dirt"},
                       "Clear one head block", "food_recovery")
        pending = {**self.pending, "decision": asdict(old)}
        self.journal.update(pending=pending, context={"food_recovery": {"unknown": True, "steps": 1}})
        h = self.harness()
        partial = {"status": self.world.status, "fresh": True, "counts": {}, "observation_scope": "safety"}
        with patch("core.continuous_recovery.ObservationCollector.collect", return_value=partial):
            self.assertTrue(h.resume())
            self.assertFalse(self.cycle(h).success)
        self.assertEqual(h.journal.state["pending"], pending)
        self.assertTrue(h.context["food_recovery"]["unknown"])
        self.assertTrue(h.resume())
        self.assertTrue(self.cycle(h).data["reobserved"])
        self.assertIsNone(h.journal.state["pending"])
        self.assertNotIn("unknown", h.context["food_recovery"])
        self.assertFalse(any(call[0] in {"move", "dig", "stop", "window"} for call in self.world.calls))

    def test_manual_pause_and_time_budget_cancel_pending_read_without_stop(self):
        self.settings["continuous"] = False
        for trigger in ("pause", "time"):
            with self.subTest(trigger=trigger):
                self.journal.update(pending=self.pending)
                h = self.harness()
                entered, release = threading.Event(), threading.Event()

                def read(cancel):
                    entered.set()
                    self.assertTrue(release.wait(2))
                    raise APIError("cancelled", "Readback interrupted")

                with patch("core.continuous_recovery.ObservationCollector.collect", side_effect=read), \
                     patch.object(self.world, "stop", wraps=self.world.stop) as stop:
                    self.assertTrue(h.request_step())
                    try:
                        self.assertTrue(entered.wait(1))
                        if trigger == "pause":
                            h.pause()
                        else:
                            h._started -= self.settings["max_runtime_seconds"] + 1
                            h.tick()
                        stop.assert_not_called()
                    finally:
                        release.set()
                    self.assertFalse(self.cycle(h).success)
                    self.assertEqual(h.journal.state["pending"], self.pending)
                h.close()

    def test_incomplete_readback_keeps_intent_and_rechecks_automatically_after_backoff(self):
        self.journal.update(pending=self.pending)
        h = self.harness()
        self.world.status["busy"] = "pathfinding"
        result = self.cycle(h)
        self.assertFalse(result.success)
        self.assertFalse(h.paused)
        self.assertEqual(h.journal.state["pending"], self.pending)
        self.assertGreater(h._continuous_retry_at, time.monotonic())
        h._next_attempt = 0
        self.assertIsNone(h.tick())
        self.assertFalse(h.busy)
        self.world.status["busy"] = None
        h._next_attempt = h._continuous_retry_at = 0
        self.assertTrue(self.cycle(h).success)
        self.assertIsNone(h.journal.state["pending"])

    def test_connection_cooldown_keeps_original_session_and_pending(self):
        self.journal.update(pending=self.pending)
        h = self.harness()
        busy = APIError("death_loop_cooldown", "bot is in death loop cooldown, 145s remaining")
        with patch.object(self.world, "get_status", side_effect=APIError("not_connected", "disconnected")), \
             patch.object(self.world, "connect", side_effect=busy, create=True) as connect:
            result = self.cycle(h)
        self.assertEqual(result.error_code, "death_loop_cooldown")
        self.assertGreater(h._continuous_retry_at, time.monotonic() + 144)
        self.assertEqual(h.journal.state["pending"], self.pending)
        self.assertFalse(h.paused)
        connect.assert_called_once_with()

    def test_manual_pause_survives_restart_until_operator_resumes(self):
        self.journal.update(pending=self.pending)
        h = self.harness()
        h.pause()
        h.close()
        restarted = self.harness()
        self.assertTrue(restarted.paused)
        for _ in range(3):
            self.assertIsNone(restarted.tick())
        self.assertEqual(self.journal.state["pending"], self.pending)
        self.assertTrue(restarted.resume())
        self.assertTrue(self.cycle(restarted).success)
        self.assertIsNone(self.journal.state["pending"])

    def test_exhausted_canopy_and_pending_resume_at_observation_barrier(self):
        self.journal.update(pending=self.pending, context={"canopy_recovery": {"steps": 64, "exhausted": True}})
        h = self.harness()
        with patch.object(h, "_request_canopy_resume", return_value=False) as canopy:
            self.assertTrue(h.resume())
            self.assertTrue(self.cycle(h).data["reobserved"])
        canopy.assert_not_called()
        self.assertIsNone(h.journal.state["pending"])

    def test_unreadable_model_gate_never_unlocks_or_crashes_game_recovery(self):
        h = self.harness()
        gate = Mock()
        gate.snapshot.side_effect = ModelGateError("llm_gate_corrupt", "Unreadable ledger")
        h.planner.brain = Mock(request_gate=gate)
        h._model_blocked = "llm_gate_corrupt"
        self.assertTrue(h.resume())
        self.assertEqual(h._model_blocked, "llm_gate_corrupt")
        gate.snapshot.assert_called_once_with()

    def test_repeat_read_failures_back_off_without_pausing_or_sending_model_request(self):
        self.settings["max_consecutive_failures"] = 1
        h = self.harness()
        with patch.object(self.world, "get_status", side_effect=APIError("network_error", "unreachable")):
            result = self.cycle(h)
        self.assertEqual(result.error_code, "network_error")
        self.assertFalse(h.paused)
        self.assertFalse(h.finished)
        self.assertGreater(h._continuous_retry_at, time.monotonic())

    def test_interrupted_water_escape_and_obs_error_recover_without_permanent_pause(self):
        self.journal.update(context={"water_recovery": {"steps": 1}})
        h = self.harness()
        for name, rec_err in (("water_escape", "interrupted"), ("retreat_from", "interruption_observation_error")):
            with self.subTest(name=name, rec_err=rec_err):
                d = Decision(name, {} if name == "water_escape" else {"pos": [4, 64, 0], "distance": 8}, "Recover", "safety")
                fail = __import__("skills").SkillResult(
                    False,
                    "Interrupted",
                    {"executed": True, "action_state": "unknown", "cleanup_skipped": "navigation_unknown", "recovery_error": rec_err},
                    "action_unconfirmed",
                )
                obs = {"status": dict(self.world.status), "fresh": True, "counts": {}, "inventory": [], "blocks": [], "entities": []}
                with patch.object(h, "_cycle", return_value=(d, fail, obs)):
                    res = self.cycle(h)
                self.assertEqual(res.error_code, "action_unconfirmed")
                self.assertFalse(h.paused)
                self.assertEqual(res.data.get("action_state"), "verified_idle")
                self.assertNotIn("water_recovery", h.context)

    def test_action_watchdog_cancels_stalled_command_and_hands_to_llm_without_pause(self):
        settings = {**self.settings, "intent": "speedrun", "continuous": False, "llm_override_on_block": True,
                    "action_watchdog_seconds": 0.05}
        h = SurvivalHarness(self.world, settings, DEFAULTS["sleep"], journal=self.journal)
        self.addCleanup(h.close)
        entered = threading.Event()
        release = threading.Event()
        d = Decision("move_to", {"pos": [6, 64, 0], "range": 1}, "Stalled move", "explore")

        def slow_cycle(_manual):
            now = time.monotonic()
            h._watchdog_started_at = now
            h._watchdog_last_progress_at = now
            h._watchdog_decision_id = id(d)
            h._active_decision = d
            entered.set()
            release.wait(2.0)
            return d, __import__("skills").SkillResult(False, "Cancelled", {"executed": True}, "cancelled"), {
                "status": dict(self.world.status), "fresh": True, "counts": {}, "inventory": []}

        with patch.object(h, "_cycle", side_effect=slow_cycle), \
             patch.object(self.world, "stop", wraps=self.world.stop) as stop:
            self.assertTrue(h._start())
            self.assertTrue(entered.wait(1.0))
            time.sleep(0.08)
            h.tick()
            release.set()
            res = self.cycle(h)
        stop.assert_called()
        self.assertEqual(res.error_code, "watchdog_timeout")
        self.assertFalse(h.paused)
        self.assertIn("WATCHDOG_TIMEOUT", h.context.get("watchdog_warning", ""))

    def test_idle_auto_recovers_non_operator_game_pause_after_respawn(self):
        settings = {**self.settings, "intent": "speedrun", "continuous": False, "llm_override_on_block": True}
        h = SurvivalHarness(self.world, settings, DEFAULTS["sleep"], journal=self.journal)
        self.addCleanup(h.close)
        h._paused = True
        h.pause_reason = "action_unconfirmed"
        h._saw_observation = True
        h.context["last_death"] = "old"
        self.world.status["last_death"] = {"t": 123, "cause": "skeleton"}
        h._next_idle_monitor = 0.0
        h._monitor_idle()
        self.assertFalse(h.paused)
        self.assertIsNone(h.pause_reason)
        h._paused = True
        h._operator_pause = "user_requested"
        h.pause_reason = "user_requested"
        h._next_idle_monitor = 0.0
        h._monitor_idle()
        self.assertTrue(h.paused)
