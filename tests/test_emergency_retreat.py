"""Emergency observations use no production scans, inventory or saved routes."""

import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from agent.survival_planner import SurvivalPlanner
from core.client import APIError
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from core.survival_state import ObservationCollector
from skills import SkillResult


class Client:
    base_url = "http://fake/api/v1"
    username = "EmergencyTest"
    password = token = ""

    def __init__(self):
        self.status = {"ready": True, "alive": True, "busy": None, "health": 20, "food": 20,
                       "dimension": "overworld", "gamemode": "survival", "pos": [.5, 64, .5],
                       "on_ground": True, "is_day": True, "thundering": False, "time_of_day": 3000,
                       "nearest_hostile": {"id": 1, "name": "zombie", "dist": 3}}
        self.entities = [{"id": 1, "name": "zombie", "type": "hostile", "pos": [3.5, 64, .5]}]
        self.calls = []
        self.on_entities = None

    def get_status(self, **kwargs):
        self.calls.append(("status", kwargs))
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def get_entities(self, **kwargs):
        self.calls.append(("entities", kwargs))
        if self.on_entities:
            self.on_entities()
        return copy.deepcopy(self.entities)

    def stop(self, **kwargs):
        self.calls.append(("stop", kwargs))
        return {"ok": True, "result": {"stopped": True}}


class Collector(ObservationCollector):
    def __init__(self, client):
        super().__init__(client)
        self.full_calls = 0
        self.allow_full = False

    def collect(self, cancel_event=None):
        self.full_calls += 1
        if not self.allow_full:
            raise AssertionError("Emergency must not wait for a production scan")
        return {"status": copy.deepcopy(self.client.status), "entities": copy.deepcopy(self.client.entities),
                "counts": {"bread": 2}, "inventory": [{"name": "bread", "count": 2}], "blocks": [], "fresh": True}


class NoModelPlanner(SurvivalPlanner):
    uses_llm = True

    def choose(self, *_args, **_kwargs):
        raise AssertionError("Emergency must never request a model decision")


class EmergencyRetreatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = Client()
        self.collector = Collector(self.client)
        self.settings = {**copy.deepcopy(DEFAULTS["survival"]), "objective": "daily", "state_dir": self.temp.name}
        self.skills = Mock()
        self.skills.execute.return_value = SkillResult(False, "No verified route", {"executed": False}, "unsafe")

    def harness(self):
        harness = SurvivalHarness(self.client, self.settings, DEFAULTS["sleep"],
            planner=NoModelPlanner(self.settings), skills=self.skills, collector=self.collector)
        self.addCleanup(harness.close)
        return harness

    def finish(self, harness):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = harness.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.001)
        self.fail("Worker did not finish")

    def test_near_hostile_bypasses_all_production_and_post_action_scans(self):
        harness = self.harness()
        harness.request_step()
        result = self.finish(harness)
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(self.skills.execute.call_args.args[0], "retreat_from")
        self.assertEqual(self.collector.full_calls, 0)
        self.assertEqual(harness.last_observation["observation_scope"], "safety")
        self.assertFalse(harness.last_observation["inventory_fresh"])
        self.assertFalse(harness.paused)

    def test_partial_snapshot_never_overwrites_memory_counts_or_milestones(self):
        harness = self.harness()
        harness.context["milestones"] = {"wooden_pickaxe": True}
        harness.memory.observe = Mock()
        harness.memory.record_result = Mock()
        harness.request_step()
        self.finish(harness)
        self.assertEqual(harness.context["milestones"], {"wooden_pickaxe": True})
        harness.memory.observe.assert_not_called()
        harness.memory.record_result.assert_not_called()

    def test_partial_board_is_explicitly_stale_until_a_full_observation(self):
        harness = self.harness()
        harness.daily_board = {"fresh": True, "tasks": [{"id": "tools", "status": "satisfied"}]}
        harness.context["daily_tasks"] = {"fresh": True, "tasks": [{"id": "tools", "status": "satisfied"}]}
        harness.request_step()
        self.finish(harness)
        self.assertFalse(harness.daily_board["fresh"])
        self.assertEqual(harness.daily_board["observation_scope"], "safety")
        self.assertEqual(harness.daily_board["tasks"][0]["status"], "satisfied")
        self.assertFalse(harness.context["daily_tasks"]["fresh"])
        self.collector.allow_full = True
        full = self.collector.collect()
        harness._prepare_context(full)
        self.assertTrue(harness.daily_board["fresh"])
        self.assertEqual(harness.daily_board["observation_scope"], "full")
        self.assertTrue(harness.context["daily_tasks"]["fresh"])

    def test_dead_or_not_ready_null_position_is_normal_wait(self):
        for change in ({"ready": False, "pos": None}, {"alive": False, "ready": False, "pos": None}):
            self.client.status = {**Client().status, **change}
            harness = self.harness()
            decision, result, obs = harness._cycle(None)
            with self.subTest(change=change):
                self.assertEqual(decision.name, "wait")
                self.assertTrue(result.success, result)
                self.assertIsNone(obs["status"]["pos"])
                self.skills.execute.assert_not_called()
                self.assertFalse(any(name == "entities" for name, _ in self.client.calls))
                self.assertEqual(self.collector.full_calls, 0)

    def test_relay_has_zero_poll_delay_and_next_leg_is_fresh_not_saved_path(self):
        harness = self.harness()
        self.settings["poll_interval_seconds"] = 30
        def execute(name, args, **kwargs):
            self.assertEqual(name, "retreat_from")
            self.assertNotIn("planned_path", kwargs["recovery_context"])
            self.client.status["pos"] = [-.5, 64, .5]
            self.client.status["nearest_hostile"] = None
            self.client.entities = []
            return SkillResult(True, "Reached one verified relay", {"executed": True, "target": [-.5, 64, .5],
                "retreat_complete": False, "evacuation_stage": "relay", "planned_path": [[99, 64, 99]]})
        self.skills.execute.side_effect = execute
        harness.request_step()
        self.assertTrue(self.finish(harness).success)
        self.assertLessEqual(harness._next_attempt, time.monotonic())
        self.assertEqual(self.collector.full_calls, 0)
        self.assertTrue(harness.context["retreat_recovery"]["evacuating"])
        self.skills.execute.side_effect = None
        self.skills.execute.return_value = SkillResult(True, "Ground", {"retreat_complete": True, "evacuation_stage": "ground"})
        reads = len(self.client.calls)
        harness.tick()
        self.assertTrue(self.finish(harness).success)
        self.assertGreater(len(self.client.calls), reads)
        self.assertEqual(self.skills.execute.call_args.kwargs["recovery_context"]["evacuation_origin"], [.5, 64, .5])
        self.assertNotIn("retreat_recovery", harness.context)

    def test_short_observation_cannot_authorize_manual_production(self):
        from agent.survival_planner import Decision
        harness = self.harness()
        decision, _, _ = harness._cycle(Decision("craft_item", {"item": "stick", "count": 4}, "manual", "stick"))
        self.assertEqual(decision.name, "retreat_from")
        self.assertEqual(self.skills.execute.call_args.args[0], "retreat_from")

    def test_missing_hostile_position_waits_without_model_or_production(self):
        self.client.entities = []
        harness = self.harness()
        decision, result, _ = harness._cycle(None)
        self.assertEqual(decision.name, "wait")
        self.assertTrue(result.success)
        self.skills.execute.assert_not_called()
        self.assertEqual(self.collector.full_calls, 0)

    def test_environment_unstable_dead_busy_and_wrong_dimension_never_move(self):
        for change in ({"in_lava": True}, {"on_ground": False}, {"alive": False, "ready": False},
                       {"busy": "dig"}, {"dimension": "the_nether"}, {"gamemode": "creative"}):
            self.client.status.update(change)
            harness = self.harness()
            decision, _, _ = harness._cycle(None)
            with self.subTest(change=change):
                self.assertEqual(decision.name, "wait")
                self.skills.execute.assert_not_called()
                self.assertEqual(self.collector.full_calls, 0)
            self.client.status = Client().status

    def test_low_health_without_close_threat_gets_real_inventory_for_food(self):
        self.client.status.update(health=6, food=10, nearest_hostile=None)
        self.client.entities = []
        self.collector.allow_full = True
        self.skills.execute.return_value = SkillResult(True, "Existing food consumed")
        harness = self.harness()
        harness.context["retreat_recovery"] = {"dimension": "overworld", "evacuating": True,
            "evacuation_origin": [.5, 64, .5], "threat_pos": [12, 64, 0], "excluded_until": {}}
        decision, _, _ = harness._cycle(None)
        self.assertEqual(decision.name, "eat_food")
        self.assertEqual(decision.args["item"], "bread")
        self.assertGreater(self.collector.full_calls, 0)

    def test_near_enemy_retreat_keeps_existing_priority_over_low_health_food(self):
        self.client.status.update(health=6, food=10)
        harness = self.harness()
        decision, _, _ = harness._cycle(None)
        self.assertEqual(decision.name, "retreat_from")
        self.assertEqual(self.collector.full_calls, 0)

    def test_unknown_retreat_preserves_pending_and_never_replays(self):
        self.skills.execute.return_value = SkillResult(False, "Unknown", {"executed": True, "action_state": "unknown"}, "request_timeout")
        harness = self.harness()
        harness.request_step()
        self.finish(harness)
        self.assertTrue(harness.paused)
        self.assertFalse(harness.journal.state["pending"]["before_counts_fresh"])
        self.assertEqual(self.collector.full_calls, 0)
        for _ in range(3):
            harness.tick()
        self.assertEqual(self.skills.execute.call_count, 1)
        self.assertIsNotNone(harness.journal.state["pending"])

    def test_changed_position_life_or_busy_during_short_scan_refuses_action(self):
        for change in ({"pos": [1.1, 64, .5]}, {"pos": [.5, 64.5, .5]}, {"last_death": {"t": 9}},
                       {"dimension": "the_nether"}, {"alive": False}, {"ready": False}, {"busy": "move_to"}):
            self.client.status = Client().status
            self.client.on_entities = lambda change=change: self.client.status.update(change)
            with self.subTest(change=change):
                with self.assertRaises(APIError) as caught:
                    self.collector.collect_safety()
                self.assertEqual(caught.exception.code, "observation_changed")

    def test_short_scan_cancellation_has_no_final_read_or_action(self):
        event = threading.Event()
        self.client.on_entities = event.set
        with self.assertRaises(APIError) as caught:
            self.collector.collect_safety(event)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual([name for name, _ in self.client.calls], ["status", "entities"])

    def test_short_scan_timeouts_share_two_second_total_budget(self):
        with patch("core.survival_state.time.monotonic", side_effect=[0, 0, .5, 1.5, 2.1]):
            with self.assertRaises(APIError) as caught:
                self.collector.collect_safety()
        self.assertEqual(caught.exception.code, "observation_changed")
        self.assertEqual([options["timeout_seconds"] for _, options in self.client.calls], [2, 1.5, .5])

    def test_failed_or_malformed_entity_read_cannot_reuse_old_threats(self):
        for value in (None, [{"type": "hostile", "pos": [float("nan"), 64, 0]}]):
            self.client.entities = value
            with self.subTest(value=value):
                with self.assertRaises(APIError) as caught:
                    self.collector.collect_safety()
                self.assertEqual(caught.exception.code, "invalid_response")


if __name__ == "__main__":
    unittest.main()
