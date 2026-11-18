"""Exercise recovery on real journal and harness entry points without a game server."""

import copy
import json
import tempfile
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
