"""Offline tool dispatch, emergency combat and progression integration."""

import copy
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from agent.brain import LLMError
from agent.daily_survival import DAILY_SKILLS, DailySurvivalGoals
from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision
from agent.tools import tools_for_objective
from core.config import DEFAULTS
from core.daily_observation import DailyObservationCollector
from core.daily_policy import DailySafetyPolicy
from core.survival_harness import SurvivalHarness
from skills import SkillResult
from test_daily_observation import DailyClient
from test_daily_survival import ready_world
from test_llm_planner import Brain, response


def world(*, enemy=False, distance=2, shield=True, health=20, ranged=False):
    obs = ready_world()
    obs["status"].update(time_of_day=1000, health=health, nearest_hostile=None)
    obs["counts"].pop("shield", None)
    if shield:
        obs["counts"]["shield"] = 1
    if ranged:
        obs["counts"].update(bow=1, arrow=8)
    obs["inventory"] = [{"name": name, "count": count} for name, count in obs["counts"].items()]
    obs["inventory_fresh"] = True
    if enemy:
        obs["entities"] = [{"id": 44, "name": "skeleton", "type": "hostile", "pos": [.5 + distance, 64, .5]}]
        obs["status"]["nearest_hostile"] = {"id": 44, "name": "skeleton", "dist": distance}
    return obs


class ExtensionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = DailySafetyPolicy(DEFAULTS["survival"])
        self.retreat = Decision("retreat_from", {"pos": [2.5, 64, .5], "distance": 8}, "Threat", "safety")

    def test_tools_are_bound_only_in_daily_and_competition(self):
        added = {"fight_hostile", "collect_liquid", "hunt_with_lava"}
        for objective in ("starter", "daily", "competition"):
            names = {tool["function"]["name"] for tool in tools_for_objective(objective)}
            self.assertEqual(added <= names, objective != "starter")
        self.assertTrue(added <= DAILY_SKILLS)

    def test_local_combat_works_with_shield_and_without_waiting_for_model(self):
        for shield, health, expected in ((True, 14, "fight_hostile"), (False, 18, "retreat_from")):
            with self.subTest(shield=shield):
                obs = world(enemy=True, shield=shield, health=health)
                choice = self.policy.choose(obs, self.retreat)
                self.assertEqual(choice.name, expected)
                self.assertIsNone(self.policy.validate(choice, obs))
                self.assertFalse(self.policy.constraints(obs)["model_allowed"])

    def test_low_health_multiple_enemies_and_unknown_inventory_still_withdraw(self):
        for change in ("health", "multiple", "inventory", "weapon"):
            obs = world(enemy=True)
            if change == "health":
                obs["status"]["health"] = 8
            elif change == "multiple":
                obs["entities"].append({"id": 45, "name": "zombie", "type": "hostile", "pos": [3, 64, 0]})
            elif change == "inventory":
                obs["inventory_fresh"] = False
            else:
                obs["inventory"] = []
                obs["counts"] = {}
            with self.subTest(change=change):
                self.assertEqual(self.policy.choose(obs, self.retreat).name, "retreat_from")

    def test_short_combat_scan_adds_inventory_without_production_scans(self):
        obs = world(enemy=True)
        client = DailyClient()
        client.status = copy.deepcopy(obs["status"])
        client.items = copy.deepcopy(obs["inventory"])
        client.get_entities = Mock(return_value=obs["entities"])
        short = DailyObservationCollector(client).collect_safety()
        self.assertEqual(short["observation_scope"], "safety")
        self.assertTrue(short["inventory_fresh"])
        self.assertFalse(short["terrain_fresh"])
        self.assertTrue(short["combat"]["allowed"])
        self.assertEqual(short["counts"]["shield"], 1)
        self.assertEqual(sum(call[0] == "inventory" for call in client.calls), 1)
        self.assertFalse(any(call[0] in {"find", "block", "window"} for call in client.calls))
        self.assertIsNotNone(self.policy.validate(Decision("craft_item", {"item": "stick"}, "Craft", "wood"), short))

    def test_short_inventory_lifecycle_change_is_discarded(self):
        obs = world(enemy=True)
        client = DailyClient()
        client.status = copy.deepcopy(obs["status"])
        client.items = obs["inventory"]
        client.get_entities = Mock(return_value=obs["entities"])
        original = client.inventory
        def inventory(**kwargs):
            result = original(**kwargs)
            client.status["last_death"] = {"t": 10}
            return result
        client.inventory = inventory
        from core.client import APIError
        with self.assertRaises(APIError) as caught:
            DailyObservationCollector(client).collect_safety()
        self.assertEqual(caught.exception.code, "observation_changed")

    def test_shield_then_bucket_are_real_iron_upgrade_prerequisites(self):
        goals = DailySurvivalGoals()
        obs = world(shield=False)
        obs["counts"].update(iron_ingot=4, oak_planks=6)
        obs["counts"].pop("bucket")
        obs["blocks"].append({"name": "crafting_table", "pos": [2, 64, 1]})
        for item in ("shield", "bucket"):
            task = next(row for row in goals.build(obs)["tasks"] if row["id"] == "iron_tools")
            self.assertEqual(task["next_action"]["name"], "craft_item")
            self.assertEqual(task["next_action"]["args"]["item"], item)
            self.assertIn(item, task["missing"])
            obs["counts"][item] = 1

    def test_far_passive_animal_gets_an_approach_step_before_stationary_hunt(self):
        obs = world()
        obs["counts"] = {"stone_sword": 1}
        obs["blocks"] = []
        obs["containers"] = []
        obs["entities"] = [{"id": 51, "name": "pig", "type": "passive", "pos": [6, 64, .5]}]
        obs["local_geometry_origin"] = obs["status"]["pos"]
        obs["safe_stand_candidates"] = [{"pos": [2.5, 64, .5], "support": "grass_block"}]
        goals = DailySurvivalGoals()
        task = next(row for row in goals.build(obs)["tasks"] if row["id"] == "food_reserve")
        self.assertEqual(task["next_action"]["name"], "position_for_task")
        self.assertEqual(task["next_action"]["args"], {"pos": [2.5, 64, .5]})
        obs["entities"][0]["pos"] = [2.5, 64, .5]
        task = next(row for row in goals.build(obs)["tasks"] if row["id"] == "food_reserve")
        self.assertEqual(task["next_action"]["name"], "hunt_animal")

    def test_current_offhand_shield_is_not_crafted_again(self):
        obs = world(shield=False)
        obs["status"]["offhand"] = {"name": "shield", "count": 1}
        task = next(row for row in DailySurvivalGoals().build(obs)["tasks"] if row["id"] == "iron_tools")
        self.assertEqual(task["status"], "satisfied")
        self.assertNotIn("shield", task["missing"])


class ExtensionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = copy.deepcopy(DEFAULTS["survival"])
        self.settings.update(objective="daily", state_dir=self.temp.name, poll_interval_seconds=.001)
        self.settings["autonomy"]["enabled"] = False  # Explicit native combat/bucket calls are the subject here.
        self.obs = world()
        self.client = Mock(username="OfflineExtensionBot", base_url="http://offline.invalid/api/v1", password="", token="")
        self.client.get_status.side_effect = lambda **kwargs: {"ok": True, "result": copy.deepcopy(self.obs["status"])}
        self.collector = Mock()
        self.collector.collect.side_effect = lambda *args: copy.deepcopy(self.obs)

    def runner(self, *responses, rules=False):
        brain = Brain(*responses)
        planner = None if rules else LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, self.settings, brain=brain)
        runner = SurvivalHarness(self.client, self.settings, DEFAULTS["sleep"], planner=planner, collector=self.collector)
        self.addCleanup(runner.close)
        runner.combat_skills = Mock()
        runner.combat_skills.execute.return_value = SkillResult(True, "One hit verified", {"executed": True, "completed": False, "killed": False})
        runner.bucket_skills = Mock()
        runner.bucket_skills.execute.return_value = SkillResult(True, "Bucket inventory verified", {"executed": True, "source_recovered": True})
        return runner, brain

    def finish(self, runner, *, start=True):
        if start:
            self.assertTrue(runner.request_step())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = runner.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.002)
        self.fail("Offline extension worker did not finish")

    def test_local_combat_dispatch_has_no_model_request(self):
        self.obs = world(enemy=True)
        self.obs.update(observation_scope="safety", terrain_fresh=False)
        runner, brain = self.runner()
        runner.memory = Mock(wraps=runner.memory)
        result = self.finish(runner)
        self.assertTrue(result.success, result)
        self.assertFalse(result.data["killed"])
        self.assertEqual(brain.requests, [])
        self.assertEqual(runner.combat_skills.execute.call_args.args, ("fight_hostile", {"entity_id": 44, "expected_name": "skeleton"}))
        self.assertIsNone(runner.journal.state["pending"])
        runner.memory.record_result.assert_called_once()

    def test_known_failed_combat_replans_to_retreat_instead_of_repeating(self):
        self.obs = world(enemy=True)
        runner, _ = self.runner()
        runner.combat_skills.execute.return_value = SkillResult(False, "No safe strike", {"executed": False}, "retreat_required")
        self.assertFalse(self.finish(runner).success)
        self.assertEqual(runner._mandatory_safety(self.obs).name, "retreat_from")

    def test_rules_cycle_honors_successful_combat_withdrawal_requirement(self):
        self.obs = world(enemy=True)
        runner, brain = self.runner(rules=True)
        runner.combat_skills.execute.return_value = SkillResult(True, "Withdrawal still required", {
            "executed": True, "killed": False, "retreat_required": True})
        runner.skills = Mock()
        runner.skills.execute.return_value = SkillResult(True, "Verified safe ground", {
            "retreat_complete": True, "evacuation_stage": "ground"})
        self.assertTrue(self.finish(runner).success)
        self.assertIn("combat_retreat", runner.context)
        self.assertTrue(self.finish(runner).success)
        runner.combat_skills.execute.assert_called_once()
        self.assertEqual(runner.skills.execute.call_args.args[0], "retreat_from")
        self.assertFalse(runner.uses_llm)
        self.assertEqual(brain.requests, [])

    def test_rules_cycle_honors_failed_combat_target_cooldown(self):
        self.obs = world(enemy=True)
        runner, _ = self.runner(rules=True)
        runner.combat_skills.execute.return_value = SkillResult(False, "No verified damage", {
            "executed": True}, "combat_no_progress")
        runner.skills = Mock()
        runner.skills.execute.return_value = SkillResult(True, "Verified safe ground", {
            "retreat_complete": True, "evacuation_stage": "ground"})
        self.assertFalse(self.finish(runner).success)
        self.assertNotIn("combat_retreat", runner.context)
        self.assertGreater(runner.context["combat_blocked_until"]["44"], time.time())
        self.assertTrue(self.finish(runner).success)
        runner.combat_skills.execute.assert_called_once()
        self.assertEqual(runner.skills.execute.call_args.args[0], "retreat_from")

    def test_known_local_combat_failure_immediately_reobserves_to_retreat_without_model(self):
        self.obs = world(enemy=True, distance=2.2)
        self.obs["entities"][0]["name"] = "zombie"
        self.obs["status"]["nearest_hostile"]["name"] = "zombie"
        runner, brain = self.runner(response("fight_hostile", {"entity_id": 44, "expected_name": "zombie"}))
        runner.recorder = Mock()
        runner.recorder.redact.side_effect = copy.deepcopy
        def failed_combat(*args, **kwargs):
            self.obs = world(enemy=True)
            return SkillResult(False, "No verified damage; withdraw", {
                "executed": True, "retreat_required": True}, "combat_no_progress")
        runner.combat_skills.execute.side_effect = failed_combat
        runner.skills = Mock()
        runner.skills.execute.return_value = SkillResult(True, "Verified safe ground", {
            "retreat_complete": True, "evacuation_stage": "ground"})
        self.assertTrue(runner.resume())
        runner.failures = self.settings["max_consecutive_failures"] - 1
        runner.tick()
        self.assertFalse(self.finish(runner, start=False).success)
        self.assertFalse(runner.paused)
        self.assertEqual(runner.failures, self.settings["max_consecutive_failures"] - 1)
        self.assertLessEqual(runner._next_attempt, time.monotonic())
        self.assertEqual(runner.journal.state["last_result"]["error_code"], "combat_no_progress")
        self.assertTrue(any(call.args[2] == "COMBAT_RECOVERY_SCHEDULED" for call in runner.recorder.record.call_args_list))
        runner.tick()
        self.assertTrue(runner.busy)
        self.assertTrue(self.finish(runner, start=False).success)
        runner.combat_skills.execute.assert_called_once()
        self.assertEqual(runner.skills.execute.call_args.args[0], "retreat_from")
        self.assertEqual(len(brain.requests), 0)

    def test_fatal_combat_failure_is_not_recovery_even_with_retreat_flag(self):
        self.obs = world(enemy=True)
        runner, _ = self.runner()
        runner.combat_skills.execute.return_value = SkillResult(False, "Session unauthorized", {
            "executed": False, "retreat_required": True}, "unauthorized")
        self.assertFalse(self.finish(runner).success)
        self.assertTrue(runner.paused)
        self.assertEqual(runner.pause_reason, "unauthorized")
        runner.tick()
        runner.combat_skills.execute.assert_called_once()

    def test_successful_defensive_step_can_require_retreat_until_threat_leaves(self):
        self.obs = world(enemy=True)
        runner, _ = self.runner()
        runner.combat_skills.execute.return_value = SkillResult(True, "Short withdrawal", {
            "executed": True, "completed": False, "killed": False, "retreat_required": True})
        self.assertTrue(self.finish(runner).success)
        self.assertIn("combat_retreat", runner.context)
        self.assertEqual(runner._mandatory_safety(self.obs).name, "retreat_from")
        self.obs["entities"] = []
        self.obs["status"]["nearest_hostile"] = None
        runner._mandatory_safety(self.obs)
        self.assertNotIn("combat_retreat", runner.context)

    def test_life_change_invalidates_combat_cooldown(self):
        runner, _ = self.runner()
        runner._update_lifecycle(self.obs["status"])
        runner.context.update(combat_retreat={"entity_id": 44}, combat_blocked_until={"44": time.time() + 30})
        runner._update_lifecycle({**self.obs["status"], "last_death": {"t": 10}})
        self.assertNotIn("combat_retreat", runner.context)
        self.assertNotIn("combat_blocked_until", runner.context)

    def test_speedrun_intent_is_explicit_and_only_advice(self):
        self.settings["intent"] = "speedrun"
        runner, brain = self.runner(response("wait"))
        self.assertTrue(self.finish(runner).success)
        self.assertEqual(runner.playbook.goal, "speedrun")
        self.assertEqual(len(brain.requests), 1)
        self.assertIn("playbook", runner.snapshot())

    def test_safety_snapshot_marks_old_playbook_advice_stale(self):
        runner, _ = self.runner()
        runner._prepare_context(self.obs)
        before = runner.snapshot()["playbook"]
        self.assertTrue(before["fresh"])
        short = world(enemy=True)
        short.update(observation_scope="safety", inventory_fresh=False)
        runner._prepare_context(short)
        after = runner.snapshot()["playbook"]
        self.assertFalse(after["fresh"])
        self.assertEqual(after["observation_scope"], "safety")
        self.assertEqual(after["stage"], before["stage"])

    def test_unknown_combat_retains_pending_without_cleanup_or_replay(self):
        self.obs = world(enemy=True)
        runner, brain = self.runner()
        runner.combat_skills.execute.return_value = SkillResult(False, "Unknown hit", {
            "executed": True, "action_state": "unknown", "cleanup_skipped": "combat_unknown", "retreat_required": True}, "action_unconfirmed")
        self.assertFalse(self.finish(runner).success)
        runner.tick()
        self.assertTrue(runner.paused)
        self.assertEqual(runner.journal.state["pending"]["decision"]["name"], "fight_hostile")
        self.assertEqual(runner.combat_skills.execute.call_count, 1)
        self.client.stop.assert_not_called()
        self.assertEqual(brain.requests, [])

    def test_shield_release_is_a_separate_journaled_step_without_model(self):
        runner, brain = self.runner()
        runner.combat_skills.defense_active = True
        def release(**kwargs):
            self.assertEqual(runner.journal.state["pending"]["decision"]["name"], "release_defense")
            return SkillResult(True, "Released own shield", {"executed": True, "released": True})
        runner.combat_skills.release_defense.side_effect = release
        self.assertTrue(self.finish(runner).success)
        self.assertEqual(brain.requests, [])
        self.assertIsNone(runner.journal.state["pending"])

    def test_unknown_shield_release_is_not_cleared_or_stopped_again(self):
        runner, brain = self.runner()
        runner.combat_skills.defense_active = True
        runner.combat_skills.release_defense.return_value = SkillResult(False, "Release unknown", {
            "executed": True, "action_state": "unknown", "cleanup_skipped": "combat_unknown"}, "action_unconfirmed")
        self.assertFalse(self.finish(runner).success)
        self.assertTrue(runner.paused)
        self.assertEqual(runner.journal.state["pending"]["decision"]["name"], "release_defense")
        runner.tick()
        runner.combat_skills.release_defense.assert_called_once()
        self.client.stop.assert_not_called()
        self.assertEqual(brain.requests, [])

    def test_existing_pending_or_near_threat_forbids_automatic_shield_release(self):
        runner, _ = self.runner()
        runner.combat_skills.defense_active = True
        pending = {"decision": {"name": "fight_hostile", "args": {"entity_id": 44}}}
        runner._checkpoint(pending=pending)
        self.assertIsNone(runner._release_defense(self.obs))
        self.assertEqual(runner.journal.state["pending"], pending)
        runner._checkpoint(pending=None)
        self.assertIsNone(runner._release_defense(world(enemy=True)))
        runner.combat_skills.release_defense.assert_not_called()

    def test_release_exception_does_not_become_an_observe_error_and_clear_pending(self):
        runner, _ = self.runner()
        runner.combat_skills.defense_active = True
        runner.combat_skills.release_defense.side_effect = RuntimeError("Interrupted transport")
        result = self.finish(runner)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertTrue(runner.paused)
        self.assertEqual(runner.journal.state["pending"]["decision"]["name"], "release_defense")
        self.client.stop.assert_not_called()

    def test_bucket_dispatch_and_paired_result_use_current_inventory(self):
        self.obs["blocks"].append({"name": "water", "pos": [2, 64, 1]})
        runner, brain = self.runner(response("collect_liquid", {"pos": [2, 64, 1], "expected_name": "water"}), response("wait", call_id="after_bucket"))
        self.assertTrue(self.finish(runner).success)
        self.assertEqual(runner.bucket_skills.execute.call_args.args[0], "collect_liquid")
        self.assertTrue(self.finish(runner).success)
        feedback = [row for row in brain.requests[-1]["messages"] if row["role"] == "tool"]
        self.assertEqual(feedback[-1]["tool_call_id"], "call_1")
        self.assertTrue(json.loads(feedback[-1]["content"])["result"]["success"])
        prompt = json.loads([row["content"] for row in brain.requests[-1]["messages"] if row["role"] == "user"][-1])
        self.assertEqual(prompt["context"]["playbook"]["profile"], "java_1_21_11")

    def test_unknown_bucket_retains_source_evidence_without_stop(self):
        self.obs["blocks"].append({"name": "lava", "pos": [2, 64, 1]})
        runner, _ = self.runner(response("collect_liquid", {"pos": [2, 64, 1], "expected_name": "lava"}))
        runner.bucket_skills.execute.return_value = SkillResult(False, "Source uncertain", {
            "executed": True, "action_state": "unknown", "cleanup_skipped": "unknown_bucket",
            "source_pos": [2, 64, 1], "lava_outstanding": True}, "action_unconfirmed")
        self.assertFalse(self.finish(runner).success)
        self.assertTrue(runner.paused)
        self.assertIsNotNone(runner.journal.state["pending"])
        self.assertTrue(runner.journal.state["last_result"]["data"]["lava_outstanding"])
        self.client.stop.assert_not_called()

    def test_model_combat_target_is_rejected_even_when_observed(self):
        self.obs = world(enemy=True, distance=9, ranged=True)
        self.obs["entities"][0]["name"] = "zombie"
        self.obs["status"]["nearest_hostile"]["name"] = "zombie"
        runner, brain = self.runner(response("fight_hostile", {"entity_id": 44, "expected_name": "zombie"}))
        result = self.finish(runner)
        self.assertEqual(result.error_code, "llm_invalid_arguments")
        runner.combat_skills.execute.assert_not_called()
        self.assertEqual(len(brain.requests), 1)
        self.assertIsNone(runner.planner._pending)

    def test_combat_monitor_ignores_expected_enemy_but_preempts_health_loss(self):
        self.obs = world(enemy=True)
        runner, _ = self.runner()
        runner._active_decision = Decision("fight_hostile", {"entity_id": 44, "expected_name": "skeleton"}, "Defend", "safety")
        runner._safety_baseline = copy.deepcopy(self.obs["status"])
        runner._monitor()
        self.assertFalse(runner._cancel.is_set())
        self.obs["status"]["health"] = 19
        runner._next_monitor = 0
        runner._monitor()
        self.assertTrue(runner._cancel.is_set())
        self.client.stop.assert_called_once()

    def test_model_wait_is_preempted_without_consuming_late_action(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(messages):
            entered.set()
            release.wait(2)
            return response("craft_item", {"item": "stick"})
        runner, brain = self.runner(slow)
        self.assertTrue(runner.request_step())
        self.assertTrue(entered.wait(1))
        self.obs = world(enemy=True)
        runner._next_monitor = 0
        runner._monitor()
        result = self.finish(runner, start=False)
        self.assertTrue(result.data.get("model_pending"))
        self.assertTrue(self.finish(runner).success)
        runner.combat_skills.execute.assert_called_once()
        self.assertEqual(len(brain.requests), 1)
        release.set()


if __name__ == "__main__":
    unittest.main()
