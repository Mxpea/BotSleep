"""Offline integration of fresh footing, local recovery and the real harness.

Only the game transport and model reply source are fakes. Geometry, skills,
collectors, policy, persistence and uncertainty handling execute normally.
"""

import copy
import json
import math
import time
import unittest
from unittest.mock import patch

from core.client import APIError
from agent.survival_planner import Decision
from core.daily_observation import DailyObservationCollector
from skills.daily_survival import DailySurvivalSkills
import test_task_harness as fixtures


class RecoveryWorld(fixtures.TaskWorld):
    def __init__(self):
        super().__init__()
        self.status.update(health=6, food=17)
        self.move_mode = None

    def move_to(self, x, y, z, **kwargs):
        target = [x, y, z]
        if self.move_mode:
            self.calls.append(("move", target, kwargs))
            if self.move_mode == "stalled":
                raise APIError("stalled", "The simulated path is known to have failed")
            if self.move_mode == "unknown":
                self.status["pos"] = target
                raise APIError("request_timeout", "The move response was lost after dispatch")
            if self.move_mode == "offset":
                self.status["pos"] = [x + .1, y, z]
                return fixtures.envelope({"pos": target})
            raise AssertionError("Unsupported fake movement mode")
        return super().move_to(x, y, z, **kwargs)


class LocalRecoveryRuntimeTests(unittest.TestCase):
    # Reuse scheduling helpers without inheriting and rediscovering their tests.
    make_harness = fixtures.TaskHarnessTests.harness
    cycle = fixtures.TaskHarnessTests.cycle
    events = staticmethod(fixtures.TaskHarnessTests.events)

    def setUp(self):
        fixtures.TaskHarnessTests.setUp(self)
        self.world = RecoveryWorld()

    def canopy(self, *, route=True):
        self.world.status["pos"] = [.5, 70, .5]
        nodes = [(0, 70, 0, "oak_leaves")]
        if route:
            nodes += [(1, 70, 0, "birch_leaves"), (2, 69, 0, "oak_leaves"), (3, 67, 0, "dirt")]
        for x, y, z, name in nodes:
            self.world.block([x, y - 1, z], name, properties={"waterlogged": False})

    def last_decision(self, harness):
        return next(row["data"]["decision"] for row in reversed(self.events(harness)) if row["event"] == "STEP_RESULT")

    def test_full_collector_has_real_footing_but_safety_snapshot_cannot_authorize_it(self):
        self.canopy()
        collector = DailyObservationCollector(self.world)
        full = collector.collect()
        self.assertEqual(full["footing_origin"], [.5, 70, .5])
        self.assertEqual(full["footing"], {"pos": [0, 69, 0], "name": "oak_leaves",
            "properties": {"waterlogged": False}, "fresh": True, "verified": True, "source": "api_verified"})
        before = len(self.world.calls)
        short = collector.collect_safety(force=True)
        self.assertEqual(short["observation_scope"], "safety")
        self.assertFalse(short["inventory_fresh"])
        self.assertFalse(short["terrain_fresh"])
        self.assertNotIn("footing", short)
        self.assertFalse(any(call[0] == "get_block" for call in self.world.calls[before:]))
        harness, brain = self.make_harness()
        self.assertEqual(harness.daily_policy.constraints(full)["scenario"], "canopy_escape")
        self.assertNotEqual(harness.daily_policy.constraints(short)["scenario"], "canopy_escape")
        self.assertEqual(brain.requests, [])

    def test_internal_canopy_dispatch_uses_daily_skill_and_persists_observed_relay(self):
        self.canopy()
        self.world.move_mode = "offset"
        harness, brain = self.make_harness()
        self.assertIsInstance(harness.skills, DailySurvivalSkills)
        self.assertNotIn("canopy_escape", harness.planner.tool_schemas)
        with patch.object(harness.skills, "execute", wraps=harness.skills.execute) as dispatch:
            result = self.cycle(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["evacuation_stage"], "relay")
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(dispatch.call_args.args[:2], ("canopy_escape", {}))
        options = dispatch.call_args.kwargs["recovery_context"]
        self.assertEqual(options["purpose"], "canopy_escape")
        self.assertEqual(options["evacuation_origin"], [.5, 70, .5])
        self.assertIn("cancel_event", dispatch.call_args.kwargs)
        self.assertNotEqual(result.data["pos"], result.data["target"])
        self.assertEqual(harness.last_observation["status"]["pos"], result.data["pos"])
        state = harness.context["canopy_recovery"]
        self.assertEqual(state["steps"], 1)
        self.assertTrue(state["evacuating"])
        self.assertEqual(state["visited"], [[math.floor(v + .0001) for v in result.data["pos"]]])
        saved = json.loads(harness.journal.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["context"]["canopy_recovery"], state)
        self.assertIsNone(saved["pending"])
        self.assertEqual(brain.requests, [])

    def test_canopy_relays_reach_verified_ground_then_clear_only_completed_recovery(self):
        self.canopy()
        harness, brain = self.make_harness()
        for i, expected in enumerate(([1.5, 70, .5], [2.5, 69, .5], [3.5, 67, .5])):
            result = self.cycle(harness)
            self.assertTrue(result.success, result)
            self.assertEqual(self.world.status["pos"], expected)
            self.assertEqual(result.data["evacuation_stage"], "ground" if i == 2 else "relay")
        self.assertNotIn("canopy_recovery", harness.context)
        self.assertTrue(any(row["event"] == "CANOPY_ESCAPE_COMPLETED" for row in self.events(harness)))
        self.assertEqual(len([call for call in self.world.calls if call[0] == "move"]), 3)
        self.assertEqual(brain.requests, [])

    def test_restarted_canopy_recovery_reobserves_next_step_instead_of_replaying_arrival(self):
        self.canopy()
        harness, brain = self.make_harness()
        self.assertTrue(self.cycle(harness).success)
        saved = copy.deepcopy(harness.context["canopy_recovery"])
        harness.close()
        restarted, next_brain = self.make_harness()
        self.assertEqual(restarted.context["canopy_recovery"], saved)
        self.assertTrue(self.cycle(restarted).success)
        self.assertEqual([call[1] for call in self.world.calls if call[0] == "move"],
                         [[1.5, 70, .5], [2.5, 69, .5]])
        self.assertEqual(restarted.context["canopy_recovery"]["steps"], 2)
        self.assertEqual(brain.requests + next_brain.requests, [])

    def test_failed_canopy_search_cools_down_without_repeated_actions_or_model(self):
        self.canopy(route=False)
        harness, brain = self.make_harness()
        first = self.cycle(harness)
        self.assertEqual(first.error_code, "unsafe")
        state = copy.deepcopy(harness.context["canopy_recovery"])
        self.assertEqual(state["steps"], 1)
        self.assertGreater(state["retry_after"], time.time())
        reads = len([call for call in self.world.calls if call[0] == "get_block"])
        second = self.cycle(harness)
        self.assertTrue(second.success)
        self.assertEqual(self.last_decision(harness)["name"], "wait")
        self.assertEqual(harness.context["canopy_recovery"]["steps"], 1)
        # A fresh collection is allowed; the failed 256-read skill is not rerun.
        self.assertLess(len([call for call in self.world.calls if call[0] == "get_block"]) - reads, 200)
        self.assertFalse(any(call[0] == "move" for call in self.world.calls))
        self.assertEqual(brain.requests, [])

    def test_unknown_canopy_move_preserves_pending_with_zero_cleanup_and_no_replay(self):
        self.canopy()
        self.world.move_mode = "unknown"
        harness, brain = self.make_harness()
        result = self.cycle(harness)
        self.assertFalse(result.success)
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["cleanup_skipped"], "navigation_unknown")
        self.assertTrue(harness.paused)
        pending = copy.deepcopy(harness.journal.state["pending"])
        self.assertIsNotNone(pending)
        self.assertTrue(harness.context["canopy_recovery"]["unknown"])
        for _ in range(3):
            self.assertIsNone(harness.tick())
        self.assertEqual(harness.journal.state["pending"], pending)
        self.assertEqual(len([call for call in self.world.calls if call[0] == "move"]), 1)
        self.assertFalse(any(call[0] == "stop" for call in self.world.calls))
        harness.close()
        self.assertEqual(harness.journal.state["pending"], pending)
        self.assertFalse(any(call[0] == "stop" for call in self.world.calls))
        self.assertEqual(brain.requests, [])
        restarted, next_brain = self.make_harness()
        self.assertTrue(restarted.paused)
        self.assertIsNone(restarted.tick())
        self.assertEqual(restarted.journal.state["pending"], pending)
        self.assertEqual(len([call for call in self.world.calls if call[0] == "move"]), 1)
        self.assertEqual(next_brain.requests, [])

    def test_eight_known_failed_food_positions_are_bounded_before_one_model_goal(self):
        self.world.move_mode = "stalled"
        harness, brain = self.make_harness(fixtures.response("run_survival_task",
            {"task": "craft", "item": "wooden_pickaxe", "count": 1}, "after_food_search"))
        for expected in range(1, 9):
            result = self.cycle(harness)
            self.assertEqual(result.error_code, "stalled", result)
            self.assertEqual(harness.context["food_recovery"]["steps"], expected)
            self.assertFalse(harness.paused)
            self.assertEqual(brain.requests, [])
        recovery = harness.context["food_recovery"]
        self.assertTrue(recovery["needs_ai"])
        self.assertEqual(len(recovery["excluded_until"]), 8)
        result = self.cycle(harness)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["accepted"])
        self.assertEqual(self.last_decision(harness)["name"], "run_survival_task")
        self.assertEqual(harness.context["survival_constraints"]["scenario"], "cautious_bootstrap")
        self.assertTrue(harness.context["cautious_bootstrap"])
        self.assertEqual(len(brain.requests), 1)

    def test_eight_successful_food_search_steps_use_observed_positions_then_model(self):
        harness, brain = self.make_harness(fixtures.response("wait", {"seconds": 1}, "review_empty_food"))
        visited = [[math.floor(v + .0001) for v in self.world.status["pos"]]]
        for expected in range(1, 9):
            result = self.cycle(harness)
            self.assertTrue(result.success, result)
            self.assertEqual(self.last_decision(harness)["name"], "position_for_task")
            cell = [math.floor(v + .0001) for v in self.world.status["pos"]]
            self.assertNotIn(cell, visited)
            visited.append(cell)
            self.assertEqual(harness.context["food_recovery"]["steps"], expected)
            self.assertEqual(harness.context["food_recovery"]["visited"], visited)
            self.assertEqual(brain.requests, [])
        self.assertTrue(harness.context["food_recovery"]["needs_ai"])
        self.assertTrue(self.cycle(harness).success)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual(self.world.status["health"], 6)
        self.assertEqual(self.world.status["food"], 17)

    def test_skeleton_twelve_blocks_interrupts_food_search_before_more_production(self):
        harness, brain = self.make_harness()
        self.assertTrue(self.cycle(harness).success)
        searched = harness.context["food_recovery"]["steps"]
        origin = list(self.world.status["pos"])
        self.world.entities = [{"id": 7, "type": "hostile", "name": "skeleton",
                                "pos": [origin[0], origin[1], origin[2] + 12]}]
        result = self.cycle(harness)
        self.assertIn(self.last_decision(harness)["name"], {"retreat_from", "wait"})
        self.assertEqual(harness.last_observation["observation_scope"], "safety")
        self.assertEqual(harness.context["food_recovery"]["steps"], searched)
        self.assertEqual(brain.requests, [])
        self.assertFalse(any(call[0] in {"dig", "craft", "place"} for call in self.world.calls))
        self.assertNotEqual(result.error_code, "internal")

    def test_night_defers_food_search_without_exhausting_its_daytime_budget(self):
        harness, brain = self.make_harness()
        self.world.status.update(is_day=False, time_of_day=16000)
        for _ in range(2):
            result = self.cycle(harness)
            self.assertTrue(result.success, result)
            self.assertEqual(self.last_decision(harness)["name"], "wait")
        recovery = harness.context["food_recovery"]
        self.assertEqual(recovery["steps"], 0)
        self.assertFalse(recovery.get("needs_ai"))
        self.assertEqual(recovery["reason"], "food_search_deferred_until_daylight")
        self.world.status.update(is_day=True, time_of_day=1000)
        result = self.cycle(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(self.last_decision(harness)["name"], "position_for_task")
        self.assertEqual(recovery["steps"], 1)
        self.assertEqual(brain.requests, [])

    def test_destination_and_idle_wakeup_use_ranged_enemy_distance(self):
        self.world.status.update(health=20, food=20)
        harness, brain = self.make_harness()
        origin = self.world.status["pos"]
        self.world.entities = [{"id": 7, "type": "hostile", "name": "skeleton",
                                "pos": [origin[0] + 18, origin[1], origin[2]]}]
        observation = harness.collector.collect()
        decision = Decision("position_for_task", {"pos": [origin[0] + 3, origin[1], origin[2]]}, "test", "explore")
        self.assertIsNotNone(harness._unsafe_destination(decision, observation))
        observation["blocks"] = [{"name": "oak_log", "pos": list(decision.args["pos"])}]
        self.assertEqual(harness.daily_goals._safe_rows(observation, {"oak_log"}), [])
        observation["entities"][0]["name"] = "zombie"
        self.assertIsNone(harness._unsafe_destination(decision, observation))
        self.assertEqual(len(harness.daily_goals._safe_rows(observation, {"oak_log"})), 1)
        harness._next_attempt = time.monotonic() + 30
        self.world.status["nearest_hostile"] = {"id": 7, "name": "skeleton", "dist": 12}
        harness._monitor_idle()
        self.assertLessEqual(harness._next_attempt, time.monotonic())
        self.assertEqual(brain.requests, [])

    def test_legacy_night_block_does_not_skip_fresh_daytime_food_search(self):
        harness, brain = self.make_harness()
        harness.context["food_recovery"] = {"dimension": "overworld", "life": None,
            "origin": list(self.world.status["pos"]), "steps": 0, "visited": [],
            "excluded_until": {}, "needs_ai": True, "reason": "Food recovery is blocked"}
        result = self.cycle(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(self.last_decision(harness)["name"], "position_for_task")
        self.assertEqual(harness.context["food_recovery"]["steps"], 1)
        self.assertFalse(harness.context["food_recovery"].get("needs_ai"))
        self.assertEqual(brain.requests, [])

    def test_death_without_api_marker_discards_local_recovery_but_keeps_pending(self):
        self.canopy()
        harness, brain = self.make_harness()
        self.assertTrue(self.cycle(harness).success)
        prior = copy.deepcopy(harness.context["canopy_recovery"])
        pending = {"decision": {"name": "canopy_escape", "args": {}}, "unknown": True}
        harness.journal.update(pending=pending)
        self.world.status.update(alive=False, health=0, last_death=None)
        harness._update_lifecycle(self.world.status)
        self.assertNotIn("canopy_recovery", harness.context)
        self.assertNotIn("food_recovery", harness.context)
        self.assertEqual(harness.context["life_generation"], prior["life_generation"] + 1)
        self.assertEqual(harness.journal.state["pending"], pending)
        self.world.status.update(alive=True, health=20, food=20, last_death=None, pos=[20.5, 70, .5])
        harness._update_lifecycle(self.world.status)
        renewed = harness._recovery_state("canopy_recovery", {"status": self.world.status})
        self.assertEqual(renewed["origin"], [20.5, 70, .5])
        self.assertEqual(renewed["steps"], 0)
        self.assertEqual(renewed["life_generation"], 1)
        self.assertEqual(harness.journal.state["pending"], pending)
        self.assertEqual(brain.requests, [])

    def test_restart_while_still_dead_does_not_increment_the_same_life_twice(self):
        harness, brain = self.make_harness()
        harness._update_lifecycle(self.world.status)
        self.world.status.update(alive=False, health=0, last_death=None)
        harness._update_lifecycle(self.world.status)
        self.assertEqual(harness.context["life_generation"], 1)
        harness._checkpoint(context=harness.context)
        harness.close()
        restarted, next_brain = self.make_harness()
        restarted._update_lifecycle(self.world.status)
        self.assertEqual(restarted.context["life_generation"], 1)
        self.assertEqual(brain.requests + next_brain.requests, [])

    def test_quiet_enemy_retreat_hands_off_to_fresh_canopy_escape_without_model(self):
        self.canopy()
        harness, brain = self.make_harness()
        harness.context["retreat_recovery"] = {"dimension": "overworld", "evacuating": True,
            "evacuation_origin": [.5, 70, .5], "threat_pos": [10, 70, 0], "excluded_until": {}}
        result = self.cycle(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(self.last_decision(harness)["name"], "canopy_escape")
        self.assertNotIn("retreat_recovery", harness.context)
        self.assertTrue(harness.context["canopy_recovery"]["evacuating"])
        self.assertTrue(any(row["event"] == "RETREAT_THREAT_CLEARED" for row in self.events(harness)))
        for _ in range(2):
            self.assertTrue(self.cycle(harness).success)
        self.assertNotIn("canopy_recovery", harness.context)
        self.assertEqual(self.world.status["pos"], [3.5, 67, .5])
        self.assertEqual(brain.requests, [])

    def saved_evacuation(self, harness):
        harness.context["retreat_recovery"] = {"dimension": "overworld", "evacuating": True,
            "evacuation_origin": list(self.world.status["pos"]), "threat_pos": [10, 64, 0],
            "excluded_until": {}}
        return copy.deepcopy(harness.context["retreat_recovery"])

    def test_quiet_healthy_retreat_reads_full_footing_before_solid_ground_handoff(self):
        self.world.status.update(health=20, food=20)
        self.world.block([0, 63, 0], "stone")
        harness, brain = self.make_harness()
        self.saved_evacuation(harness)
        harness.context["combat_retreat"] = {"entity_id": 7, "name": "skeleton"}
        with patch.object(harness.collector, "collect", wraps=harness.collector.collect) as full:
            observation = harness._observe()
        full.assert_called_once()
        self.assertEqual(observation["footing"]["name"], "stone")
        self.assertNotEqual(observation.get("observation_scope"), "safety")
        harness._prepare_context(observation)
        self.assertNotIn("retreat_recovery", harness.context)
        self.assertNotIn("combat_retreat", harness.context)
        handoff = [row for row in self.events(harness) if row["event"] == "RETREAT_THREAT_CLEARED"]
        self.assertEqual(handoff[-1]["data"]["mode"], "solid_footing")
        self.assertEqual(handoff[-1]["data"]["pos"], observation["status"]["pos"])
        self.assertEqual(brain.requests, [])
        self.assertFalse(any(row[0] in {"move", "dig", "craft", "place", "stop"} for row in self.world.calls))

    def test_quiet_handoff_rejects_stale_partial_unstable_and_hazard_observations(self):
        harness, brain = self.make_harness()
        base = harness.collector.collect()
        cases = [("fresh", False), ("stale", True), ("partial", True),
                 ("inventory_fresh", False), ("terrain_fresh", False), ("observation_scope", "safety")]
        status_cases = [("on_ground", False), ("ready", False), ("alive", False), ("busy", "moving"),
                        ("in_water", True), ("in_lava", True), ("on_fire", True),
                        ("submerged", True), ("in_vehicle", True), ("is_sleeping", True)]
        for status_change, changes in ((False, cases), (True, status_cases)):
            for key, value in changes:
                with self.subTest(status=status_change, key=key):
                    expected = self.saved_evacuation(harness)
                    observation = copy.deepcopy(base)
                    (observation["status"] if status_change else observation)[key] = value
                    harness._prepare_local_recovery(observation)
                    self.assertEqual(harness.context["retreat_recovery"], expected)
        self.assertFalse(any(row["event"] == "RETREAT_THREAT_CLEARED" for row in self.events(harness)))
        self.assertEqual(brain.requests, [])

    def test_new_enemy_or_saturated_entity_observation_keeps_retreat(self):
        harness, brain = self.make_harness()
        base = harness.collector.collect()
        origin = base["status"]["pos"]
        cases = [
            {"entities": [{"id": 7, "type": "hostile", "name": "skeleton",
                            "pos": [origin[0] + 12, origin[1], origin[2]]}]},
            {"nearest_hostile": {"id": 8, "name": "skeleton", "dist": 12}},
            {"entities": [{"id": i, "type": "animal", "name": "cow", "pos": [20, 64, 20]} for i in range(40)]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                expected = self.saved_evacuation(harness)
                observation = copy.deepcopy(base)
                observation["entities"] = changes.get("entities", [])
                observation["status"]["nearest_hostile"] = changes.get("nearest_hostile")
                harness._prepare_local_recovery(observation)
                self.assertEqual(harness.context["retreat_recovery"], expected)
        self.assertEqual(brain.requests, [])

    def test_new_near_enemy_keeps_short_scan_instead_of_delaying_for_handoff(self):
        harness, brain = self.make_harness()
        expected = self.saved_evacuation(harness)
        origin = self.world.status["pos"]
        self.world.entities = [{"id": 7, "type": "hostile", "name": "skeleton",
                                "pos": [origin[0] + 12, origin[1], origin[2]]}]
        with patch.object(harness.collector, "collect", side_effect=AssertionError("No full scan near an enemy")):
            observation = harness._observe()
        self.assertEqual(observation["observation_scope"], "safety")
        harness._prepare_local_recovery(observation)
        self.assertEqual(harness.context["retreat_recovery"], expected)
        self.assertEqual(brain.requests, [])

    def test_forced_safety_observation_cannot_finish_quiet_evacuation(self):
        harness, brain = self.make_harness()
        expected = self.saved_evacuation(harness)
        with patch.object(harness.collector, "collect", side_effect=AssertionError("Safety-only must remain short")):
            observation = harness._observe(safety_only=True)
        harness._prepare_local_recovery(observation)
        self.assertEqual(harness.context["retreat_recovery"], expected)
        self.assertNotIn("footing", observation)
        self.assertEqual(brain.requests, [])

    def test_full_handoff_observation_rejects_position_or_life_change_during_scan(self):
        harness, brain = self.make_harness()
        original = copy.deepcopy(self.world.status)
        read_final = harness.collector._status
        for changes in ({"pos": [original["pos"][0] + .3, *original["pos"][1:]]},
                        {"last_death": {"dimension": "overworld", "pos": [0, 64, 0], "timestamp": "new-life"}}):
            with self.subTest(changes=changes):
                self.world.status = copy.deepcopy(original)
                expected = self.saved_evacuation(harness)

                def changed_status(*args, **kwargs):
                    self.world.status.update(copy.deepcopy(changes))
                    return read_final(*args, **kwargs)

                with patch.object(harness.collector, "_status", side_effect=changed_status):
                    with self.assertRaises(APIError) as caught:
                        harness._observe()
                self.assertEqual(caught.exception.code, "observation_changed")
                self.assertEqual(harness.context["retreat_recovery"], expected)
        self.assertEqual(brain.requests, [])

    def test_quiet_ground_does_not_resume_or_replay_unknown_pending(self):
        harness, brain = self.make_harness()
        expected = self.saved_evacuation(harness)
        pending = {"decision": {"name": "retreat_from", "args": {"pos": [10, 64, 0]}},
                   "action_state": "unknown"}
        harness.journal.update(pending=copy.deepcopy(pending))
        harness._paused = True
        harness.pause_reason = "action_unconfirmed"
        for _ in range(3):
            self.assertIsNone(harness.tick())
        self.assertTrue(harness.paused)
        self.assertEqual(harness.pause_reason, "action_unconfirmed")
        self.assertEqual(harness.journal.state["pending"], pending)
        self.assertEqual(harness.context["retreat_recovery"], expected)
        self.assertEqual(brain.requests, [])
        self.assertFalse(any(row[0] in {"move", "dig", "craft", "place", "stop"} for row in self.world.calls))

    def test_revisited_canopy_cell_refreshes_tail_of_visited_history_to_prevent_pingpong(self):
        from skills import SkillResult
        harness, _ = self.make_harness()
        obs = {
            "fresh": True,
            "status": {"pos": [1.5, 70.0, 0.5], "health": 20, "food": 20, "dimension": "overworld"},
        }
        state = harness._recovery_state("canopy_recovery", obs)
        state["visited"] = [[1, 70, 0]] + [[i, 70, 0] for i in range(10, 22)]
        res = SkillResult(True, "Moved", {"pos": [1.5, 70.0, 0.5], "evacuation_stage": "relay"})
        harness._local_recovery_result(Decision("canopy_escape", {}, "Step", "safety"), res, obs)
        opts = harness._canopy_options(obs)
        self.assertEqual(opts["recent_targets"][-1], [1, 70, 0])
        self.assertIn([1, 70, 0], opts["excluded_targets"])
        self.assertEqual(len(opts["recent_targets"]), 8)

    def test_water_recovery_tracks_progress_and_clears_when_dry(self):
        from skills import SkillResult
        harness, _ = self.make_harness()
        wet_obs = {
            "fresh": True,
            "status": {"pos": [1.5, 62.0, 0.5], "health": 20, "food": 20, "dimension": "overworld",
                       "in_water": True, "submerged": False, "on_ground": False, "ready": True, "alive": True},
        }
        prog_res = SkillResult(False, "Swimming", {"progress_made": True}, "still_in_water")
        replan = harness._local_recovery_result(Decision("water_escape", {}, "Swim", "safety"), prog_res, wet_obs)
        self.assertTrue(replan)
        self.assertEqual(harness.context["water_recovery"]["steps"], 1)
        self.assertEqual(harness.context["water_recovery"]["stalled_steps"], 0)
        dry_obs = {
            "fresh": True,
            "inventory_fresh": True,
            "terrain_fresh": True,
            "status": {"pos": [2.5, 63.0, 0.5], "health": 20, "food": 20, "dimension": "overworld",
                       "in_water": False, "submerged": False, "on_ground": True, "ready": True, "alive": True, "busy": None},
        }
        harness._prepare_local_recovery(dry_obs)
        self.assertNotIn("water_recovery", harness.context)


if __name__ == "__main__":
    unittest.main()
