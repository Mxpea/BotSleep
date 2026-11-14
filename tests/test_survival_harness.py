"""Harness recovery, cancellation and persistence contracts; no game server."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from agent.survival_planner import Decision
from core.client import APIError
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from core.survival_state import ObservationCollector, SurvivalJournal
from skills import SkillResult


def observation(**status_changes):
    return {"status": {"ready": True, "alive": True, "busy": None, "health": 20,
                       "food": 20, "is_day": True, "dimension": "overworld", "gamemode": "survival",
                       "pos": [0, 64, 0], **status_changes},
            "counts": {}, "inventory": [], "blocks": [], "entities": [], "fresh": True}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = {**copy.deepcopy(DEFAULTS["survival"]), "state_dir": self.temp.name}
        self.client = Mock(username="HarnessBot", base_url="http://local/api/v1")
        self.client.get_status.return_value = {"ok": True, "result": observation()["status"]}
        self.collector = Mock()
        self.collector.collect.side_effect = lambda *_: copy.deepcopy(observation())
        self.planner = Mock()
        self.planner.progress.return_value = {}
        self.planner.choose.return_value = Decision("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "Get wood", "wood")
        self.skills = Mock()
        self.skills.execute.return_value = SkillResult(True, "Wood gathered")

    def harness(self):
        result = SurvivalHarness(self.client, self.settings, DEFAULTS["sleep"],
                                 planner=self.planner, skills=self.skills, collector=self.collector)
        self.addCleanup(result.close)
        return result

    def finish(self, harness):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = harness.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(0.002)
        self.fail("Worker did not finish")

    def test_intent_is_durable_before_action_and_completion_clears_it(self):
        harness = self.harness()
        def execute(*args, **kwargs):
            state = json.loads(harness.journal.path.read_text())
            self.assertEqual(state["pending"]["decision"]["name"], "gather_block")
            return SkillResult(True, "done", {"gained": {"oak_log": 1}})
        self.skills.execute.side_effect = execute
        harness.request_step()
        self.assertTrue(self.finish(harness).success)
        state = json.loads(harness.journal.path.read_text())
        self.assertIsNone(state["pending"])
        self.assertTrue(state["last_result"]["success"])
        events = [json.loads(line)["event"] for line in harness.journal.events_path.read_text().splitlines()]
        self.assertIn("DECISION", events)
        self.assertIn("STEP_RESULT", events)

    def test_pause_serializes_and_requires_resume(self):
        entered = threading.Event()
        def execute(*args, cancel_event=None):
            entered.set()
            cancel_event.wait(2)
            return SkillResult(False, "cancelled", error_code="cancelled")
        self.skills.execute.side_effect = execute
        harness = self.harness()
        self.assertTrue(harness.request_step())
        self.assertTrue(entered.wait(1))
        self.assertFalse(harness.request_step())
        harness.pause()
        self.assertEqual(self.finish(harness).error_code, "cancelled")
        harness._next_attempt = 0
        harness.tick()
        self.assertFalse(harness.busy)
        self.assertEqual(self.skills.execute.call_count, 1)
        self.client.stop.assert_called_once()

    def test_unknown_action_outcome_pauses_and_is_not_replayed_on_restart(self):
        harness = self.harness()
        self.skills.execute.return_value = SkillResult(False, "timeout", error_code="request_timeout")
        harness.request_step()
        self.finish(harness)
        self.assertTrue(harness.paused)
        self.assertIsNotNone(harness.journal.state["pending"])
        harness.close()
        restarted = self.harness()
        restarted.tick()
        self.assertTrue(restarted.paused)
        self.assertFalse(restarted.busy)
        self.assertEqual(self.skills.execute.call_count, 1)
        # Explicit resume observes and replans, rather than using the journal's action.
        self.planner.choose.return_value = Decision("wait", {"seconds": 1}, "new state", "safety")
        restarted.request_step()
        self.finish(restarted)
        self.assertEqual(self.skills.execute.call_count, 1)

    def test_failed_observation_does_not_plan_using_previous_snapshot(self):
        harness = self.harness()
        harness.last_observation = observation()
        self.collector.collect.side_effect = APIError("network_error", "unreachable")
        harness.request_step()
        self.assertEqual(self.finish(harness).error_code, "network_error")
        self.planner.choose.assert_not_called()
        self.skills.execute.assert_not_called()

    def test_unconfirmed_dig_keeps_intention_even_if_stop_fails(self):
        self.collector.collect.side_effect = lambda *_: {**observation(), "counts": {"stick": 2}}
        self.skills.execute.return_value = SkillResult(False, "Not confirmed", {
            "action_state": "unknown", "api_details": {"eye_dist": 4.8}}, "dig_unconfirmed")
        self.client.stop.side_effect = APIError("network_error", "stop unavailable")
        harness = self.harness()
        harness.request_step()
        self.assertEqual(self.finish(harness).error_code, "dig_unconfirmed")
        self.assertTrue(harness.paused)
        self.assertEqual(harness.pause_reason, "dig_unconfirmed")
        state = json.loads(harness.journal.path.read_text())
        self.assertEqual(state["pending"]["decision"]["args"]["pos"], [2, 64, 0])
        self.assertEqual(state["pending"]["before_counts"], {"stick": 2})
        self.assertEqual(state["last_result"]["error_code"], "dig_unconfirmed")
        self.assertEqual(state["last_result"]["data"]["api_details"], {"eye_dist": 4.8})
        events = [json.loads(line)["event"] for line in harness.journal.events_path.read_text().splitlines()]
        self.assertIn("STOP_FAILED", events)
        harness._next_attempt = 0
        harness.tick()
        self.assertFalse(harness.busy)
        harness.close()
        restarted = self.harness()
        restarted.tick()
        self.assertTrue(restarted.paused)
        self.assertEqual(self.skills.execute.call_count, 1)
        self.planner.choose.return_value = Decision("wait", {"seconds": 1}, "New observation", "observe")
        reads = self.collector.collect.call_count
        restarted.request_step()
        self.assertTrue(self.finish(restarted).success)
        self.assertGreater(self.collector.collect.call_count, reads)
        self.assertIsNone(restarted.journal.state["pending"])
        self.assertEqual(self.skills.execute.call_count, 1)

    def test_unconfirmed_dig_feedback_is_paired_before_pause_and_replanning(self):
        from agent.llm_planner import LLMSurvivalPlanner

        brain = Mock()
        def response(name, args, call_id):
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
        brain.chat_completion.side_effect = [
            response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "dig_once"),
            response("wait", {"seconds": 1, "reason": "Observe before another action"}, "wait_after_resume"),
        ]
        obs = observation(on_ground=True, thundering=False)
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0], "diggable": True}]
        self.collector.collect.side_effect = lambda *_: copy.deepcopy(obs)
        self.planner = LLMSurvivalPlanner({"enabled": True, "model": "fake", "api_key": "fake"},
                                         self.settings, brain=brain)
        self.skills.execute.return_value = SkillResult(False, "Not confirmed", {
            "action_state": "unknown"}, "dig_unconfirmed")
        harness = self.harness()
        harness.request_step()
        self.assertEqual(self.finish(harness).error_code, "dig_unconfirmed")
        self.assertTrue(harness.paused)
        self.assertIsNotNone(harness.journal.state["pending"])
        self.assertIsNone(self.planner._pending)
        feedback = [message for message in self.planner.history if message.get("role") == "tool"]
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["tool_call_id"], "dig_once")
        result = json.loads(feedback[0]["content"])["result"]
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "dig_unconfirmed")
        harness._next_attempt = 0
        harness.tick()
        self.assertEqual(brain.chat_completion.call_count, 1)
        harness.request_step()  # Explicit recovery, never automatic replay.
        self.assertTrue(self.finish(harness).success)
        messages = brain.chat_completion.call_args.args[0]
        paired = [message for message in messages if message.get("role") == "tool"]
        self.assertEqual(paired[0]["tool_call_id"], "dig_once")
        self.assertEqual(self.skills.execute.call_count, 1)
        self.assertIsNone(harness.journal.state["pending"])

    def test_reconciled_dig_evidence_clears_pending_without_pause(self):
        self.skills.execute.return_value = SkillResult(True, "Verified after delayed acknowledgement", {
            "recovered_from": "dig_unconfirmed", "block_removed": True, "gained": {"oak_log": 1}})
        harness = self.harness()
        harness.request_step()
        self.assertTrue(self.finish(harness).success)
        self.assertFalse(harness.paused)
        self.assertIsNone(harness.journal.state["pending"])

    def test_real_skill_recovery_does_not_stop_twice_in_harness(self):
        from skills.survival import SurvivalSkills
        from test_survival_skills import World

        for stop_fails in (False, True):
            with self.subTest(stop_fails=stop_fails):
                world = World()
                world.base_url, world.username = "http://local/api/v1", "HarnessBot"
                world.set_block([2, 64, 0], "oak_log", can_see=True, harvest_tool_ok=True)
                def dig(pos):
                    raise APIError("dig_unconfirmed", "Server acknowledgement missing")
                world.dig_effect = dig
                if stop_fails:
                    def failed_stop(**kwargs):
                        world.calls.append(("stop",))
                        raise APIError("network_error", "Stop unavailable")
                    world.stop = failed_stop
                self.client, self.skills = world, SurvivalSkills(world)
                self.settings["state_dir"] = str(Path(self.temp.name) / str(stop_fails))
                harness = self.harness()
                harness.request_step()
                result = self.finish(harness)
                self.assertEqual(result.error_code, "dig_unconfirmed")
                self.assertTrue(harness.paused)
                self.assertIsNotNone(harness.journal.state["pending"])
                self.assertEqual([call[0] for call in world.writes()], ["dig", "stop"])

    def test_local_failure_after_action_preserves_uncertain_intention(self):
        harness = self.harness()
        self.skills.execute.return_value = SkillResult(False, "verification crashed", error_code="internal_error")
        harness.request_step()
        self.finish(harness)
        self.assertTrue(harness.paused)
        self.assertIsNotNone(harness.journal.state["pending"])

    def test_reconnect_uses_existing_session_not_login(self):
        harness = self.harness()
        self.collector.collect.side_effect = APIError("not_connected", "disconnected")
        harness.request_step()
        self.assertEqual(self.finish(harness).error_code, "reconnecting")
        self.client.connect.assert_called_once()
        self.client.login.assert_not_called()
        self.skills.execute.assert_not_called()

    def test_repeated_failed_target_cools_down(self):
        harness = self.harness()
        self.skills.execute.return_value = SkillResult(False, "unreachable", error_code="no_path")
        for _ in range(2):
            harness.request_step()
            self.finish(harness)
        self.assertIn("2,64,0", harness.context["blocked_until"])
        harness.request_step()
        self.finish(harness)
        self.assertIn("2,64,0", self.planner.choose.call_args.args[1]["blocked_targets"])

    def test_safety_interrupt_cancels_worker_before_new_task(self):
        entered = threading.Event()
        def execute(*args, cancel_event=None):
            entered.set()
            cancel_event.wait(2)
            return SkillResult(False, "cancelled", error_code="cancelled")
        self.skills.execute.side_effect = execute
        harness = self.harness()
        harness.request_step()
        self.assertTrue(entered.wait(1))
        self.client.get_status.return_value = {"ok": True, "result": observation(on_fire=True)["status"]}
        harness.tick(automatic=False)
        self.finish(harness)
        self.client.stop.assert_called_once()
        self.assertEqual(self.skills.execute.call_count, 1)

    def test_goal_and_step_budgets_have_distinct_outcomes(self):
        harness = self.harness()
        self.planner.choose.return_value = Decision("goal_complete", {}, "Starter tools ready", "starter")
        harness.request_step()
        self.assertTrue(self.finish(harness).success)
        self.assertTrue(harness.completed)
        self.assertTrue(harness.finished)
        self.skills.execute.assert_not_called()
        other = self.harness()
        other.steps = self.settings["max_steps"]
        other.tick()
        self.assertTrue(other.finished)
        self.assertFalse(other.completed)

    def test_runtime_budget_applies_while_paused(self):
        harness = self.harness()
        harness.pause()
        harness._started = time.monotonic() - self.settings["max_runtime_seconds"] - 1
        harness.tick()
        self.assertTrue(harness.finished)
        self.assertEqual(harness.pause_reason, "budget_exhausted")

    def test_manual_action_cannot_overrun_step_budget(self):
        harness = self.harness()
        harness.steps = self.settings["max_steps"]
        self.assertFalse(harness.request_step())
        self.assertFalse(harness.request_sleep())
        self.assertTrue(harness.finished)
        self.skills.execute.assert_not_called()

    def test_runtime_expiration_requests_stop_and_joins_inflight_action(self):
        entered = threading.Event()
        def execute(*args, cancel_event=None):
            entered.set()
            cancel_event.wait(2)
            return SkillResult(False, "cancelled", error_code="cancelled")
        self.skills.execute.side_effect = execute
        harness = self.harness()
        harness.request_step()
        self.assertTrue(entered.wait(1))
        harness._started = time.monotonic() - self.settings["max_runtime_seconds"] - 1
        harness.tick(automatic=False)
        self.finish(harness)
        self.assertTrue(harness.finished)
        self.assertFalse(harness.completed)
        self.client.stop.assert_called_once()

    def test_manual_sleep_cannot_replace_unsettled_pending_action(self):
        harness = self.harness()
        pending = {"decision": {"name": "craft_item"}}
        harness.journal.update(pending=pending)
        self.collector.collect.side_effect = lambda *_: observation(busy="craft")
        harness.request_sleep()
        self.assertFalse(self.finish(harness).success)
        self.assertEqual(harness.journal.state["pending"], pending)
        self.skills.execute.assert_not_called()

    def test_death_resets_target_memory_and_exploration(self):
        harness = self.harness()
        harness.context.update(explore_index=8, was_alive=True, spawn=[99, 64, 99],
                               blocked_until={"1,2,3": time.time() + 100})
        self.collector.collect.side_effect = lambda *_: observation(alive=False, ready=False, last_death={"t": 100})
        self.planner.choose.return_value = Decision("wait", {"seconds": 1}, "Respawning", "safety")
        harness.request_step()
        self.finish(harness)
        self.assertEqual(harness.context["death_count"], 1)
        self.assertEqual(harness.context["explore_index"], 0)
        self.assertEqual(harness.context["blocked_until"], {})
        self.assertIsNone(harness.context["spawn"])

    def test_delayed_death_marker_counts_once_and_login_history_is_not_a_new_death(self):
        harness = self.harness()
        harness._prepare_context(observation(last_death={"t": 10}))
        self.assertEqual(harness.context["death_count"], 0)
        harness._prepare_context(observation(alive=False, ready=False, last_death={"t": 10}))
        harness._prepare_context(observation(alive=False, ready=False, last_death={"t": 20}))
        harness._prepare_context(observation(last_death={"t": 20}))
        self.assertEqual(harness.context["death_count"], 1)
        harness._prepare_context(observation(last_death={"t": 30}))
        self.assertEqual(harness.context["death_count"], 2)

    def retreat_fixture(self):
        current = observation(thundering=False, on_ground=True)
        current["entities"] = [{"type": "hostile", "name": "zombie", "pos": [2, 64, 0]}]
        current["status"]["nearest_hostile"] = {"dist": 2}
        self.collector.collect.side_effect = lambda *_: copy.deepcopy(current)
        self.client.get_status.side_effect = lambda **_: {"ok": True, "result": copy.deepcopy(current["status"])}
        self.planner.uses_llm = True
        return current

    def step_without_resume(self, harness):
        self.assertTrue(harness._start())
        return self.finish(harness)

    def test_retreat_no_route_waits_and_reobserves_without_six_global_failures(self):
        self.retreat_fixture()
        self.skills.execute.return_value = SkillResult(False, "No safe route", {"executed": False}, "unsafe")
        harness = self.harness()
        harness.failures = self.settings["max_consecutive_failures"] - 1
        self.assertFalse(self.step_without_resume(harness).success)
        self.assertFalse(harness.paused)
        self.assertEqual(harness.failures, self.settings["max_consecutive_failures"] - 1)
        for _ in range(6):
            self.assertTrue(self.step_without_resume(harness).data["waiting"])
        self.assertEqual(self.skills.execute.call_count, 1)
        self.assertEqual(self.collector.collect.call_count, 7)
        self.planner.choose.assert_not_called()
        self.assertIn("retreat_recovery", harness.context)
        self.assertNotIn("2,64,0", harness.context["blocked_until"])
        harness.context["retreat_recovery"]["retry_after"] = time.time() - 1
        self.assertFalse(self.step_without_resume(harness).success)
        self.assertEqual(self.skills.execute.call_count, 2)

    def test_retreat_wait_still_consumes_the_existing_step_budget(self):
        self.retreat_fixture()
        self.settings["max_steps"] = 3
        self.skills.execute.return_value = SkillResult(False, "No safe route", error_code="unsafe")
        harness = self.harness()
        for _ in range(3):
            self.step_without_resume(harness)
        self.assertFalse(harness._start())
        self.assertTrue(harness.finished)
        self.assertFalse(harness.completed)
        self.assertEqual(self.skills.execute.call_count, 1)

    def test_observed_obstruction_change_allows_early_retreat_search(self):
        current = self.retreat_fixture()
        current["terrain"] = [{"pos": [-1, 64, 0], "name": "stone"}]
        self.skills.execute.return_value = SkillResult(False, "Blocked", {
            "blocked_cells": [{"pos": [-1, 64, 0], "name": "stone"}]}, "unsafe")
        harness = self.harness()
        self.step_without_resume(harness)
        self.step_without_resume(harness)
        self.assertEqual(self.skills.execute.call_count, 1)
        current["terrain"][0]["name"] = "air"
        self.step_without_resume(harness)
        self.assertEqual(self.skills.execute.call_count, 2)

    def test_failed_retreat_excludes_actual_target_and_selects_another_candidate(self):
        self.retreat_fixture()
        self.skills.execute.side_effect = [
            SkillResult(False, "No path", {"target": [-4.5, 64, 0.5], "executed": True}, "path_not_found"),
            SkillResult(True, "On safe ground", {"target": [-4, 64, 2], "retreat_complete": True, "evacuation_stage": "ground"}),
        ]
        harness = self.harness()
        self.step_without_resume(harness)
        self.assertEqual(harness.context["failures"], {})
        self.step_without_resume(harness)
        options = self.skills.execute.call_args.kwargs["recovery_context"]
        self.assertEqual(options["excluded_targets"], [[-5, 64, 0]])
        self.assertFalse(options["evacuating"])
        self.assertNotIn("retreat_recovery", harness.context)
        self.assertIsNone(harness.journal.state["pending"])

    def test_retreat_target_cooldowns_expire_and_are_capped_at_eight(self):
        self.retreat_fixture()
        self.skills.execute.side_effect = lambda *_args, **_kwargs: SkillResult(False, "Unverified", {
            "target": [-4, 64, self.skills.execute.call_count], "executed": True}, "retreat_unverified")
        harness = self.harness()
        for _ in range(10):
            self.step_without_resume(harness)
        recovery = harness.context["retreat_recovery"]
        self.assertEqual(len(recovery["excluded_until"]), 8)
        self.assertEqual(len(self.skills.execute.call_args.kwargs["recovery_context"]["excluded_targets"]), 8)
        recovery["excluded_until"] = {key: time.time() - 1 for key in recovery["excluded_until"]}
        self.step_without_resume(harness)
        self.assertEqual(self.skills.execute.call_args.kwargs["recovery_context"]["excluded_targets"], [])
        self.assertFalse(harness.paused)

    def test_relay_continues_from_fresh_observation_after_hostile_leaves_radius(self):
        current = self.retreat_fixture()
        def execute(name, args, **kwargs):
            options = kwargs["recovery_context"]
            self.assertEqual(name, "retreat_from")
            self.assertEqual(options["evacuation_origin"], [0, 64, 0])
            if self.skills.execute.call_count == 1:
                self.assertFalse(options["evacuating"])
                current["status"].update(pos=[-8, 65, 0], nearest_hostile={"dist": 10})
                return SkillResult(True, "Reached canopy relay", {"target": [-8, 65, 0],
                    "retreat_complete": False, "evacuation_stage": "relay", "planned_path": [[-8, 65, 0]]})
            self.assertTrue(options["evacuating"])
            self.assertEqual(harness.latest_status["pos"], [-8, 65, 0])
            return SkillResult(True, "Reached ground", {"target": [-10, 64, 0],
                "retreat_complete": True, "evacuation_stage": "ground"})
        self.skills.execute.side_effect = execute
        harness = self.harness()
        self.assertTrue(self.step_without_resume(harness).success)
        self.assertTrue(harness.context["retreat_recovery"]["evacuating"])
        self.assertNotIn("planned_path", harness.context["retreat_recovery"])
        self.assertTrue(self.step_without_resume(harness).success)
        self.assertNotIn("retreat_recovery", harness.context)
        self.assertEqual(self.collector.collect.call_count, 2)
        self.planner.choose.assert_not_called()

    def test_relay_does_not_override_lethal_hazard_or_unstable_footing(self):
        current = self.retreat_fixture()
        self.skills.execute.return_value = SkillResult(True, "relay", {
            "retreat_complete": False, "evacuation_stage": "relay"})
        harness = self.harness()
        self.step_without_resume(harness)
        for changes in ({"in_lava": True}, {"in_lava": False, "on_ground": False}):
            current["status"].update(changes)
            self.assertTrue(self.step_without_resume(harness).data["waiting"])
        self.assertEqual(self.skills.execute.call_count, 1)
        self.assertTrue(harness.context["retreat_recovery"]["evacuating"])

    def test_relay_persists_across_restart_and_rules_planning_never_replays_a_path(self):
        current = self.retreat_fixture()
        self.skills.execute.return_value = SkillResult(True, "relay", {
            "retreat_complete": False, "evacuation_stage": "relay", "planned_path": [[-2, 65, 0]]})
        original = self.harness()
        self.step_without_resume(original)
        original.close()
        current["status"].update(pos=[-8, 65, 0], nearest_hostile={"dist": 10})
        self.planner.uses_llm = False
        restored = self.harness()
        self.assertFalse(restored.paused)
        self.step_without_resume(restored)
        call = self.skills.execute.call_args
        self.assertEqual(call.args[0], "retreat_from")
        self.assertEqual(call.kwargs["recovery_context"]["evacuation_origin"], [0, 64, 0])
        self.assertTrue(call.kwargs["recovery_context"]["evacuating"])
        self.assertNotIn("planned_path", call.kwargs["recovery_context"])
        self.assertEqual(restored.latest_status["pos"], [-8, 65, 0])

    def test_position_change_breaks_no_route_cooldown_and_refreshes_first_leg_origin(self):
        current = self.retreat_fixture()
        self.skills.execute.return_value = SkillResult(False, "No route", error_code="unsafe")
        harness = self.harness()
        self.step_without_resume(harness)
        current["status"]["pos"] = [-1, 64, 0]
        self.step_without_resume(harness)
        self.assertEqual(self.skills.execute.call_count, 2)
        self.assertEqual(self.skills.execute.call_args.kwargs["recovery_context"]["evacuation_origin"], [-1, 64, 0])

    def test_unknown_retreat_pauses_with_pending_and_never_excludes_or_retries(self):
        self.retreat_fixture()
        self.skills.execute.return_value = SkillResult(False, "Unknown move", {
            "target": [-4, 64, 0], "candidate_failed": True, "executed": True}, "request_timeout")
        harness = self.harness()
        self.step_without_resume(harness)
        self.assertTrue(harness.paused)
        self.assertEqual(harness.pause_reason, "request_timeout")
        self.assertEqual(harness.context["retreat_recovery"]["excluded_until"], {})
        pending = copy.deepcopy(harness.journal.state["pending"])
        self.assertEqual(pending["decision"]["name"], "retreat_from")
        for _ in range(3):
            harness.tick()
        self.assertEqual(self.skills.execute.call_count, 1)
        self.assertEqual(harness.journal.state["pending"], pending)
        self.assertTrue(harness.paused)

    def test_paused_lifecycle_reads_count_delayed_death_without_erasing_inventory_evidence(self):
        current = self.retreat_fixture()
        harness = self.harness()
        harness._prepare_context(current)
        harness.context["milestones"] = {"wood": True}
        harness.context["retreat_recovery"] = {"evacuating": True, "dimension": "overworld"}
        pending = {"decision": {"name": "retreat_from"}}
        harness.journal.update(pending=pending)
        harness.pause("user_requested")
        current["status"].update(alive=False, ready=False)
        harness.tick()
        self.assertEqual(harness.context["death_count"], 1)
        self.assertNotIn("retreat_recovery", harness.context)
        self.assertEqual(harness.context["milestones"], {"wood": True})
        harness.tick()
        self.assertEqual(self.client.get_status.call_count, 1)
        self.assertEqual(self.client.get_status.call_args.kwargs["timeout_seconds"], 2)
        harness._next_idle_monitor = 0
        current["status"].update(alive=True, ready=True, last_death={"t": 22}, pos=[20, 70, 20])
        harness.tick()
        self.assertEqual(harness.context["death_count"], 1)
        self.assertEqual(harness.context["spawn"], [20, 70, 20])
        self.assertEqual(harness.context["milestones"], {"wood": True})
        self.assertTrue(harness.paused)
        self.assertEqual(harness.pause_reason, "user_requested")
        self.assertEqual(harness.journal.state["pending"], pending)
        self.skills.execute.assert_not_called()

    def test_idle_dimension_change_clears_retreat_but_not_milestones_or_pause(self):
        current = self.retreat_fixture()
        harness = self.harness()
        harness._prepare_context(current)
        harness.context.update(retreat_recovery={"evacuating": True}, milestones={"stone_pickaxe": True})
        harness.pause("request_timeout")
        current["status"]["dimension"] = "the_nether"
        harness.tick()
        self.assertNotIn("retreat_recovery", harness.context)
        self.assertEqual(harness.context["milestones"], {"stone_pickaxe": True})
        self.assertEqual(harness.pause_reason, "request_timeout")
        self.skills.execute.assert_not_called()

    def test_idle_read_failure_keeps_unknown_pause_and_pending(self):
        harness = self.harness()
        harness.pause("request_timeout")
        pending = {"decision": {"name": "move_to"}}
        harness.journal.update(pending=pending)
        self.client.get_status.side_effect = APIError("network_error", "Offline")
        harness.tick()
        self.assertTrue(harness.paused)
        self.assertEqual(harness.pause_reason, "request_timeout")
        self.assertEqual(harness.journal.state["pending"], pending)
        self.client.connect.assert_not_called()
        self.skills.execute.assert_not_called()


class StateTests(unittest.TestCase):
    def test_world_and_identity_have_separate_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            one = SurvivalJournal(directory, endpoint="http://local", username="One", world_id="world1")
            two = SurvivalJournal(directory, endpoint="http://local", username="One", world_id="world2")
            three = SurvivalJournal(directory, endpoint="http://local", username="Two", world_id="world1")
            one.update(pending={"decision": "do not replay"})
            self.assertEqual(len({one.directory, two.directory, three.directory}), 3)
            self.assertIsNone(two.state["pending"])

    def test_corrupt_checkpoint_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SurvivalJournal(directory, endpoint="http://local", username="One", world_id="world1")
            journal.path.write_text("{broken")
            with self.assertRaises(ValueError):
                SurvivalJournal(directory, endpoint="http://local", username="One", world_id="world1")
            self.assertEqual(journal.path.read_text(), "{broken")

    def test_observer_reads_final_status_and_counts_all_stacks(self):
        client = Mock()
        client.get_status.side_effect = [{"result": observation()["status"]},
                                         {"result": observation(health=7)["status"]}]
        client.inventory.return_value = {"result": {"items": [{"name": "oak_log", "count": 2}, {"name": "oak_log", "count": 3}]}}
        client.get_blocks.return_value = []
        client.get_entities.return_value = []
        result = ObservationCollector(client).collect()
        self.assertEqual(result["counts"]["oak_log"], 5)
        self.assertEqual(result["status"]["health"], 7)
        self.assertTrue(result["fresh"])

    def test_observer_does_not_scan_when_dead_or_busy(self):
        client = Mock()
        client.get_status.return_value = {"result": observation(busy="dig")["status"]}
        self.assertEqual(ObservationCollector(client).collect()["blocks"], [])
        client.inventory.assert_not_called()

    def test_observer_discards_inventory_if_respawn_occurs_during_scan(self):
        client = Mock()
        client.get_status.side_effect = [{"result": observation()["status"]},
                                         {"result": observation(last_death={"t": 123})["status"]}]
        client.inventory.return_value = {"result": {"items": [{"name": "stone_pickaxe", "count": 1}]}}
        client.get_blocks.return_value = []
        client.get_entities.return_value = []
        with self.assertRaises(APIError) as caught:
            ObservationCollector(client).collect()
        self.assertEqual(caught.exception.code, "observation_changed")


class ObservationTests(unittest.TestCase):
    """Local geometry from the tree-spawn failure; no game or model requests."""

    @staticmethod
    def client(pos=None, blocks=None, ground_y=108):
        client = Mock()
        status = observation(pos=pos or [4.3, 109, -5.5])["status"]
        client.get_status.side_effect = lambda: {"result": copy.deepcopy(status)}
        client.inventory.return_value = {"result": {"items": []}}
        client.get_blocks.return_value = []
        client.get_entities.return_value = []
        blocks = blocks or {}

        def get_block(pos):
            name = "grass_block" if pos[1] == ground_y else "air"
            return {"result": {"name": name, "properties": {}, "pos": list(pos),
                               **blocks.get(tuple(pos), {})}}

        client.get_block.side_effect = get_block
        return client

    def test_flat_ground_candidates_are_small_centered_and_exclude_current_column(self):
        client = self.client()
        result = ObservationCollector(client, include_terrain=True).collect()
        self.assertEqual(result["local_geometry_origin"], [4.3, 109, -5.5])
        self.assertEqual(len(result["safe_stand_candidates"]), 8)
        self.assertEqual(len(result["placement_candidates"]), 8)
        for row in result["safe_stand_candidates"]:
            self.assertEqual(row["pos"][0] % 1, 0.5)
            self.assertEqual(row["pos"][2] % 1, 0.5)
            self.assertNotEqual((int(row["pos"][0] // 1), int(row["pos"][2] // 1)), (4, -6))
            self.assertEqual(row["support"], "grass_block")
        for row in result["placement_candidates"]:
            self.assertTrue(all(type(value) is int for value in row["pos"]))
            self.assertNotEqual((row["pos"][0], row["pos"][2]), (4, -6))
        reads = [tuple(call.args[0]) for call in client.get_block.call_args_list]
        self.assertEqual(len(reads), 24)
        self.assertEqual(len(reads), len(set(reads)))

    def test_rule_mode_adds_no_geometry_requests(self):
        client = self.client()
        result = ObservationCollector(client).collect()
        client.get_block.assert_not_called()
        self.assertEqual(client.get_blocks.call_count, 2)
        self.assertNotIn("safe_stand_candidates", result)
        self.assertNotIn("placement_candidates", result)

    def test_geometry_rejects_leaves_liquid_waterlogged_support_and_blocked_head(self):
        client = self.client(blocks={
            (5, 110, -6): {"name": "stone"},
            (4, 108, -5): {"name": "oak_leaves"},
            (4, 108, -7): {"name": "lava"},
            (5, 108, -5): {"name": "oak_log", "properties": {"waterlogged": "true"}},
            (3, 109, -5): {"name": "water"},
        })
        result = ObservationCollector(client, include_terrain=True).collect()
        stands = {tuple(row["pos"]) for row in result["safe_stand_candidates"]}
        places = {tuple(row["pos"]) for row in result["placement_candidates"]}
        self.assertNotIn((5.5, 109, -5.5), stands)
        for x, z in ((4, -5), (4, -7), (5, -5), (3, -5)):
            self.assertNotIn((x + 0.5, 109, z + 0.5), stands)
            self.assertNotIn((x, 109, z), places)

    def test_passable_litter_and_grass_are_standing_but_not_placement_cells(self):
        client = self.client(blocks={
            (5, 109, -6): {"name": "short_grass"},
            (3, 109, -6): {"name": "leaf_litter"},
        })
        result = ObservationCollector(client, include_terrain=True).collect()
        stands = {tuple(row["pos"]) for row in result["safe_stand_candidates"]}
        places = {tuple(row["pos"]) for row in result["placement_candidates"]}
        for x in (3, 5):
            self.assertIn((x + 0.5, 109, -5.5), stands)
            self.assertNotIn((x, 109, -6), places)

    def test_candidate_scan_checks_one_level_above_and_below_with_bounded_reads(self):
        client = self.client(ground_y=None, blocks={
            (5, 109, -6): {"name": "stone"},
            (3, 107, -6): {"name": "dirt"},
        })
        result = ObservationCollector(client, include_terrain=True).collect()
        stands = {tuple(row["pos"]) for row in result["safe_stand_candidates"]}
        places = {tuple(row["pos"]) for row in result["placement_candidates"]}
        self.assertEqual(stands, {(5.5, 110, -5.5), (3.5, 108, -5.5)})
        self.assertEqual(places, {(5, 110, -6), (3, 108, -6)})
        reads = [tuple(call.args[0]) for call in client.get_block.call_args_list]
        self.assertLessEqual(len(reads), 60)
        self.assertEqual(len(reads), len(set(reads)))

    def test_unloaded_cells_are_not_candidates_and_other_read_errors_propagate(self):
        client = self.client()
        client.get_block.side_effect = APIError("not_loaded", "offline unloaded chunk")
        result = ObservationCollector(client, include_terrain=True).collect()
        self.assertEqual(result["safe_stand_candidates"], [])
        self.assertEqual(result["placement_candidates"], [])
        self.assertLessEqual(client.get_block.call_count, 12)
        client.get_block.side_effect = APIError("network_error", "offline read failure")
        with self.assertRaises(APIError) as caught:
            ObservationCollector(client, include_terrain=True).collect()
        self.assertEqual(caught.exception.code, "network_error")

    def test_geometry_is_reread_each_observation_instead_of_cached_across_turns(self):
        client = self.client()
        collector = ObservationCollector(client, include_terrain=True)
        self.assertTrue(collector.collect()["safe_stand_candidates"])
        client.get_block.side_effect = lambda pos: {"result": {"name": "air", "properties": {}}}
        self.assertEqual(collector.collect()["safe_stand_candidates"], [])

    def test_cross_cell_movement_and_same_cell_fall_discard_mixed_snapshot(self):
        scenarios = (([4.3, 112, -5.5], [4.3, 109, -5.5]),
                     ([4.9, 109, -5.5], [5.1, 109, -5.5]),
                     ([4.3, 109.9, -5.5], [4.3, 109.2, -5.5]))
        for start, end in scenarios:
            with self.subTest(start=start, end=end):
                client = self.client(pos=start)
                client.get_status.side_effect = [{"result": observation(pos=start)["status"]},
                                                 {"result": observation(pos=end)["status"]}]
                with self.assertRaises(APIError) as caught:
                    ObservationCollector(client, include_terrain=True).collect()
                self.assertEqual(caught.exception.code, "observation_changed")

    def test_small_same_cell_motion_preserves_fresh_snapshot(self):
        client = self.client()
        client.get_status.side_effect = [{"result": observation(pos=[4.3, 109, -5.5])["status"]},
                                         {"result": observation(pos=[4.35, 109, -5.4])["status"]}]
        result = ObservationCollector(client, include_terrain=True).collect()
        self.assertEqual(result["status"]["pos"], [4.35, 109, -5.4])
        self.assertTrue(result["fresh"])

    def test_cancel_during_block_read_stops_scanning_and_never_returns_partial_fresh_data(self):
        client = self.client()
        cancel = threading.Event()

        def read_and_cancel(pos):
            cancel.set()
            return {"result": {"name": "grass_block", "properties": {}}}

        client.get_block.side_effect = read_and_cancel
        with self.assertRaises(APIError) as caught:
            ObservationCollector(client, include_terrain=True).collect(cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(client.get_block.call_count, 1)
        client.get_entities.assert_not_called()
        self.assertEqual(client.get_status.call_count, 1)

    def test_pre_cancelled_collection_does_not_make_requests(self):
        client = self.client()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(APIError) as caught:
            ObservationCollector(client, include_terrain=True).collect(cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        client.get_status.assert_not_called()

    def test_invalid_position_is_rejected_before_resource_scans(self):
        client = self.client(pos=[4, float("nan"), -6])
        with self.assertRaises(APIError) as caught:
            ObservationCollector(client, include_terrain=True).collect()
        self.assertEqual(caught.exception.code, "invalid_response")
        client.get_blocks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
