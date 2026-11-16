"""LocalExecutor state-machine contracts, isolated from planning and the harness."""

import copy
import json
import unittest
from unittest.mock import patch

from agent.survival_planner import Decision
from core.local_execution import LocalExecutor
from skills import SkillResult


class FakeClock:
    """Explicit elapsed time without sleeps or a dependency on wall time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def observation():
    return {"fresh": True, "status": {"dimension": "overworld", "last_death": None,
            "pos": [0.5, 64, 0.5]}, "counts": {},
            "blocks": [{"pos": [2, 64, 0], "name": "oak_log", "can_see": True}]}


class LocalExecutionTests(unittest.TestCase):
    def setUp(self):
        planner_patch = patch("core.local_execution.LocalTaskPlanner", autospec=True)
        self.planner_type = planner_patch.start()
        self.addCleanup(planner_patch.stop)
        self.planner = self.planner_type.return_value
        self.intent = Decision("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"},
                               "One supplied task step", "wood")
        self.planner.choose.return_value = self.intent
        recovery_patch = patch("core.local_execution.canopy_clearances", return_value=[])
        self.clearances = recovery_patch.start()
        self.addCleanup(recovery_patch.stop)
        self.clock = FakeClock()
        self.executor = self.make_executor()
        self.obs = observation()
        self.context = {}

    def make_executor(self, **limits):
        return LocalExecutor({"objective": "daily", "local_execution": {
            "enabled": True, "max_steps": 24, "max_seconds": 90, **limits}}, clock=self.clock)

    def choose(self, **kwargs):
        return self.executor.choose(self.obs, self.context, **kwargs)

    def record_progress(self, decision=None, **kwargs):
        self.obs["counts"]["oak_log"] = self.obs["counts"].get("oak_log", 0) + 1
        self.executor.record(self.intent if decision is None else decision, SkillResult(True),
                             self.obs, self.context, **kwargs)

    def processing_job(self):
        self.planner.choose.return_value = None
        self.obs["furnaces"] = [{"pos": [2, 64, 0], "dimension": "overworld", "fresh": True,
            "verified": True, "burning": True, "input": {"name": "raw_iron", "count": 1},
            "output": {"count": 0}}]
        self.context["smelting_jobs"] = [{"pos": [2, 64, 0], "dimension": "overworld",
                                         "expected_output": "iron_ingot"}]

    def test_known_processing_wait_is_local_and_still_time_bounded(self):
        self.processing_job()
        first = self.choose()
        self.assertEqual((first.name, first.goal, first.args), ("wait", "local_processing", {"seconds": 5}))
        self.executor.record(first, SkillResult(True), self.obs, self.context)
        self.assertEqual(self.choose(), first)
        self.clock.advance(90)
        self.assertIsNone(self.choose())
        self.assertEqual(self.executor.summary(self.context)["reason"], "local_budget_exhausted")

    def test_processing_wait_requires_matching_fresh_burning_job(self):
        for field, value in (("fresh", False), ("burning", False), ("dimension", "the_nether"),
                             ("input", {"name": "raw_iron", "count": 0}),
                             ("output", {"name": "iron_ingot", "count": 1})):
            with self.subTest(field=field):
                self.context = {}
                self.processing_job()
                self.obs["furnaces"][0][field] = value
                self.assertIsNone(self.choose())
        self.context = {}
        self.processing_job()
        self.assertIsNone(self.choose(competition={"phase": "selection"}))

    def test_verified_furnace_slot_change_counts_as_progress(self):
        intent = Decision("inspect_container", {"pos": [2, 64, 0]}, "Inspect", "iron")
        self.planner.choose.return_value = intent
        self.choose()
        self.obs["furnaces"] = [{"pos": [2, 64, 0], "fresh": True, "verified": True,
            "input": {"name": "raw_iron", "count": 1}, "burning": True}]
        self.executor.record(intent, SkillResult(True), self.obs, self.context)
        self.assertTrue(self.executor.summary(self.context)["recent"][-1]["progress"])
        self.assertEqual(self.choose(), intent)

    def test_disabled_executor_does_not_plan_or_create_context_state(self):
        self.executor = self.make_executor(enabled=False)
        before = copy.deepcopy(self.context)
        self.assertIsNone(self.choose())
        self.planner.choose.assert_not_called()
        self.clearances.assert_not_called()
        self.assertEqual(self.context, before)

    def test_choose_forwards_supplied_boards_without_recording_a_step(self):
        competition = {"phase": "active", "active_goal_id": "goal-a"}
        daily = {"active_task_id": "starter_tools", "tasks": []}
        before = copy.deepcopy((self.obs, competition, daily))
        self.assertEqual(self.choose(competition=competition, daily_board=daily), self.intent)
        self.planner.choose.assert_called_once_with(self.obs, self.context,
                                                   competition=competition, daily_board=daily)
        self.assertEqual((self.obs, competition, daily), before)
        self.assertEqual(self.executor.summary(self.context)["steps"], 0)
        self.assertEqual(self.choose(competition=competition, daily_board=daily), self.intent)
        self.assertEqual(self.executor.summary(self.context)["steps"], 0)

    def test_record_consumes_step_budget_only_for_recorded_attempts(self):
        self.executor = self.make_executor(max_steps=2)
        self.assertEqual(self.choose(), self.intent)
        self.record_progress()
        self.assertEqual(self.choose(), self.intent)
        self.record_progress()
        calls = self.planner.choose.call_count
        self.assertIsNone(self.choose())
        self.assertEqual(self.planner.choose.call_count, calls)
        summary = self.executor.summary(self.context)
        self.assertEqual((summary["phase"], summary["reason"], summary["steps"]),
                         ("model_takeover", "local_budget_exhausted", 2))

    def test_zero_step_or_time_budget_hands_over_before_invoking_planner(self):
        for limits in ({"max_steps": 0}, {"max_seconds": 0}):
            with self.subTest(limits=limits):
                executor = self.make_executor(**limits)
                context = {}
                self.assertIsNone(executor.choose(self.obs, context))
                self.assertEqual(executor.summary(context)["reason"], "local_budget_exhausted")
        self.planner.choose.assert_not_called()

    def test_fake_clock_enforces_exact_time_boundary_without_sleep(self):
        self.executor = self.make_executor(max_seconds=10)
        self.assertEqual(self.choose(), self.intent)
        self.clock.advance(9.5)
        self.assertEqual(self.choose(), self.intent)
        self.clock.advance(.5)
        calls = self.planner.choose.call_count
        self.assertIsNone(self.choose())
        self.assertEqual(self.executor.summary(self.context)["reason"], "local_budget_exhausted")
        self.clock.advance(100)
        self.assertIsNone(self.choose())
        self.assertEqual(self.planner.choose.call_count, calls)

    def test_progress_during_running_phase_does_not_extend_time_budget(self):
        self.executor = self.make_executor(max_seconds=10)
        self.choose()
        self.clock.advance(9)
        self.record_progress()
        self.assertEqual(self.choose(), self.intent)
        self.clock.advance(1)
        self.assertIsNone(self.choose())
        self.assertEqual(self.executor.summary(self.context)["reason"], "local_budget_exhausted")

    def test_changed_reality_after_takeover_restarts_elapsed_budget(self):
        self.executor = self.make_executor(max_seconds=10)
        self.choose()
        self.clock.advance(10)
        self.assertIsNone(self.choose())
        self.clock.advance(200)
        self.obs["counts"]["oak_log"] = 1
        self.assertEqual(self.choose(), self.intent)
        self.assertEqual(self.executor.summary(self.context)["steps"], 0)
        self.clock.advance(9.5)
        self.assertEqual(self.choose(), self.intent)
        self.clock.advance(.5)
        self.assertIsNone(self.choose())

    def test_failed_signature_is_deduplicated_even_if_reason_and_goal_change(self):
        self.choose()
        self.executor.record(self.intent, SkillResult(False, error_code="no_path"), self.obs, self.context)
        self.planner.choose.return_value = Decision("gather_block", {
            "expected_name": "oak_log", "pos": [2, 64, 0]}, "Different prose", "different_goal")
        self.assertIsNone(self.choose())
        summary = self.executor.summary(self.context)
        self.assertEqual(summary["reason"], "no_verified_local_step")
        self.assertEqual(summary["recent"], [{"action": "gather_block", "success": False,
                                             "error_code": "no_path", "progress": False}])
        calls = self.planner.choose.call_count
        for _ in range(3):
            self.assertIsNone(self.choose())
        self.assertEqual(self.planner.choose.call_count, calls)

    def test_nominal_success_without_real_change_is_not_retried(self):
        self.choose()
        self.executor.record(self.intent, SkillResult(True, "Reported success"), self.obs, self.context)
        self.assertFalse(self.executor.summary(self.context)["recent"][0]["progress"])
        self.assertIsNone(self.choose())

    def test_missing_or_explicitly_unfresh_post_observation_proves_no_progress(self):
        for post in (None, {**observation(), "fresh": False, "counts": {"oak_log": 1}}):
            with self.subTest(post=post):
                context = {}
                self.executor.choose(self.obs, context)
                self.executor.record(self.intent, SkillResult(True), post, context)
                self.assertFalse(self.executor.summary(context)["recent"][0]["progress"])
                self.assertIsNone(self.executor.choose(self.obs, context))

    def test_only_fresh_nonstale_post_observation_can_prove_progress(self):
        for freshness in ({"fresh": None}, {"stale": True}, {"missing_fresh": True}):
            with self.subTest(freshness=freshness):
                context = {}
                self.executor.choose(self.obs, context)
                post = copy.deepcopy(self.obs)
                post["counts"]["oak_log"] = 1
                if freshness.get("missing_fresh"):
                    del post["fresh"]
                else:
                    post.update(freshness)
                self.executor.record(self.intent, SkillResult(True), post, context)
                self.assertFalse(self.executor.summary(context)["recent"][0]["progress"],
                                 "Missing/unknown freshness or stale data must not establish progress")

    def test_changed_evidence_reopens_a_failed_signature_after_takeover(self):
        for change in ("counts", "position", "block", "visibility"):
            with self.subTest(change=change):
                obs, context = observation(), {}
                self.executor.choose(obs, context)
                self.executor.record(self.intent, SkillResult(False, error_code="no_path"), obs, context)
                self.assertIsNone(self.executor.choose(obs, context))
                if change == "counts":
                    obs["counts"]["oak_log"] = 1
                elif change == "position":
                    obs["status"]["pos"][0] += 1
                elif change == "block":
                    obs["blocks"][0]["name"] = "air"
                else:
                    obs["blocks"][0]["can_see"] = False
                self.assertEqual(self.executor.choose(obs, context), self.intent)
                summary = self.executor.summary(context)
                self.assertEqual((summary["phase"], summary["reason"], summary["steps"]), ("running", None, 0))

    def test_timestamp_or_dictionary_order_changes_do_not_reopen_takeover(self):
        self.choose()
        self.executor.record(self.intent, SkillResult(False, error_code="no_path"), self.obs, self.context)
        self.assertIsNone(self.choose())
        calls = self.planner.choose.call_count
        self.obs["observed_at"] = 99999
        self.obs["status"]["timestamp"] = 99999
        self.obs["status"] = dict(reversed(list(self.obs["status"].items())))
        self.context["monitor_heartbeat"] = 99999
        self.clock.advance(300)
        self.assertIsNone(self.choose())
        self.assertEqual(self.planner.choose.call_count, calls)

    def test_no_plan_takeover_is_sticky_until_real_evidence_changes(self):
        self.planner.choose.return_value = None
        self.assertIsNone(self.choose())
        self.assertEqual(self.executor.summary(self.context)["reason"], "no_verified_local_step")
        self.planner.choose.return_value = self.intent
        self.assertIsNone(self.choose())
        self.planner.choose.assert_called_once()
        self.obs["counts"]["stick"] = 2
        self.assertEqual(self.choose(), self.intent)

    def test_known_scope_changes_reset_spent_budget_and_recent_history(self):
        self.executor = self.make_executor(max_steps=1)
        leaf = Decision("clear_foliage", {"pos": [1, 64, 0], "expected_name": "oak_leaves"}, "Stub clearance", "safety")
        self.clearances.return_value = [leaf]
        for change in ("dimension", "last_death", "phase", "goal", "recovery_mode"):
            with self.subTest(change=change):
                obs, context = observation(), {}
                board = {"phase": "active", "active_goal_id": "goal-a"}
                decision = self.executor.choose(obs, context, competition=board)
                obs["counts"]["oak_log"] = 1
                self.executor.record(decision, SkillResult(True), obs, context, competition=board)
                self.assertIsNone(self.executor.choose(obs, context, competition=board))
                if change == "dimension":
                    obs["status"]["dimension"] = "the_nether"
                elif change == "last_death":
                    obs["status"]["last_death"] = {"id": "new-life"}
                elif change == "phase":
                    board["phase"] = "selection"
                elif change == "goal":
                    board["active_goal_id"] = "goal-b"
                else:
                    context["retreat_recovery"] = {"canopy_blocked": True}
                self.assertIsNotNone(self.executor.choose(obs, context, competition=board))
                summary = self.executor.summary(context)
                self.assertEqual((summary["phase"], summary["steps"], summary["recent"]), ("running", 0, []))

    def test_recovery_candidates_skip_failed_intent_without_testing_geometry(self):
        first = Decision("clear_foliage", {"pos": [1, 64, 0], "expected_name": "oak_leaves"}, "First", "safety")
        second = Decision("clear_foliage", {"pos": [1, 65, 0], "expected_name": "oak_leaves"}, "Second", "safety")
        self.clearances.return_value = [first, second]
        self.context["retreat_recovery"] = {"canopy_blocked": True}
        decision = self.choose()
        self.assertEqual(decision.args["pos"], first.args["pos"])
        self.executor.record(decision, SkillResult(False, error_code="blocked"), self.obs, self.context)
        decision = self.choose()
        self.assertEqual(decision.args["pos"], second.args["pos"])
        self.executor.record(decision, SkillResult(False, error_code="blocked"), self.obs, self.context)
        self.assertIsNone(self.choose())
        self.assertEqual(self.executor.summary(self.context)["reason"], "no_verified_local_recovery")
        self.planner.choose.assert_not_called()

    def test_unknown_results_stay_blocked_despite_clock_or_same_scope_evidence_change(self):
        results = [SkillResult(False, error_code=code) for code in
                   ("request_timeout", "network_error", "invalid_json", "invalid_response",
                    "dig_unconfirmed", "action_unconfirmed", "internal", "internal_error")]
        results.append(SkillResult(True, data={"action_state": "unknown"}))
        for result in results:
            with self.subTest(code=result.error_code, data=result.data):
                obs, context = observation(), {}
                self.executor.choose(obs, context)
                self.executor.record(self.intent, result, None, context)
                summary = self.executor.summary(context)
                self.assertEqual((summary["phase"], summary["reason"]), ("blocked_unknown", "uncertain_action"))
                calls = self.planner.choose.call_count
                obs["counts"]["oak_log"] = 1
                obs["status"]["pos"][0] += 3
                self.clock.advance(1000)
                self.assertIsNone(self.executor.choose(obs, context))
                self.assertEqual(self.planner.choose.call_count, calls)

    def test_unknown_result_remains_locked_when_scope_changes(self):
        for change in ("dimension", "last_death", "phase", "goal", "recovery_mode"):
            with self.subTest(change=change):
                obs, context = observation(), {}
                board = {"phase": "active", "active_goal_id": "goal-a"}
                self.executor.choose(obs, context, competition=board)
                self.executor.record(self.intent, SkillResult(False, error_code="action_unconfirmed"),
                                     None, context, competition=board)
                if change == "dimension":
                    obs["status"]["dimension"] = "the_nether"
                elif change == "last_death":
                    obs["status"]["last_death"] = {"id": "new-life"}
                elif change == "phase":
                    board["phase"] = "selection"
                elif change == "goal":
                    board["active_goal_id"] = "goal-b"
                else:
                    context["retreat_recovery"] = {"canopy_blocked": True}
                calls = self.planner.choose.call_count
                self.assertIsNone(self.executor.choose(obs, context, competition=board),
                                  "A scope change must not clear an unresolved action lock")
                self.assertEqual(self.executor.summary(context)["phase"], "blocked_unknown")
                self.assertEqual(self.planner.choose.call_count, calls)

    def test_unknown_lock_survives_executor_recreation_and_context_serialization(self):
        self.choose()
        self.executor.record(self.intent, SkillResult(False, data={"action_state": "unknown"}), None, self.context)
        restored = json.loads(json.dumps(self.context))
        self.executor = self.make_executor()
        calls = self.planner.choose.call_count
        self.assertIsNone(self.executor.choose(self.obs, restored))
        self.assertEqual(self.executor.summary(restored)["phase"], "blocked_unknown")
        self.assertEqual(self.planner.choose.call_count, calls)

    def test_reject_hands_over_without_spending_step_or_repeating_same_basis(self):
        self.choose()
        self.executor.reject(self.context, "local_safety_veto")
        summary = self.executor.summary(self.context)
        self.assertEqual((summary["phase"], summary["reason"], summary["steps"], summary["recent"]),
                         ("model_takeover", "local_safety_veto", 0, []))
        calls = self.planner.choose.call_count
        self.assertIsNone(self.choose())
        self.assertEqual(self.planner.choose.call_count, calls)
        self.obs["counts"]["oak_log"] = 1
        self.assertEqual(self.choose(), self.intent)

    def test_reject_cannot_downgrade_unknown_result_to_model_takeover(self):
        self.choose()
        self.executor.record(self.intent, SkillResult(False, error_code="request_timeout"), None, self.context)
        self.executor.reject(self.context, "local_safety_veto")
        self.assertEqual(self.executor.summary(self.context)["phase"], "blocked_unknown",
                         "Rejection must not clear an unresolved action lock")
        self.obs["counts"]["oak_log"] = 1
        self.assertIsNone(self.choose())

    def test_record_and_reject_without_started_state_are_noops(self):
        self.executor.record(self.intent, SkillResult(False), None, self.context)
        self.executor.reject(self.context, "no_intent")
        self.assertEqual(self.context, {})
        self.assertEqual(self.executor.summary(self.context), {
            "phase": None, "reason": None, "steps": None, "recent": None})

    def test_summary_contains_only_six_recent_result_records_without_payloads(self):
        for index in range(10):
            intent = Decision("craft_item", {"item": "private-item-" + str(index), "count": 1},
                              "private-reason-" + str(index), "private-goal")
            self.planner.choose.return_value = intent
            self.assertEqual(self.choose(), intent)
            self.obs["counts"]["oak_log"] = index + 1
            result = SkillResult(True, "private-message", {"raw_response": "private-payload"}, "code_" + str(index))
            self.executor.record(intent, result, self.obs, self.context)
        self.context["unrelated_secret"] = "private-config"
        summary = self.executor.summary(self.context)
        self.assertEqual(set(summary), {"phase", "reason", "steps", "recent"})
        self.assertEqual(summary["steps"], 10)
        self.assertEqual(len(summary["recent"]), 6)
        self.assertEqual([row["error_code"] for row in summary["recent"]], ["code_" + str(index) for index in range(4, 10)])
        for row in summary["recent"]:
            self.assertEqual(set(row), {"action", "success", "error_code", "progress"})
            self.assertEqual((row["action"], row["success"], row["progress"]), ("craft_item", True, True))
        self.assertNotIn("private-", json.dumps(summary))
        self.assertLess(len(json.dumps(summary)), 1000)

    def test_summary_is_a_detached_copy_and_names_inner_recovery_action(self):
        intent = Decision("force_attempt", {"action": "clear_foliage", "pos": [1, 64, 0]}, "Stub", "safety")
        self.planner.choose.return_value = intent
        self.assertEqual(self.choose(), intent)
        self.executor.record(intent, SkillResult(False, error_code="blocked"), self.obs, self.context)
        summary = self.executor.summary(self.context)
        self.assertEqual(summary["recent"][0]["action"], "clear_foliage")
        original = copy.deepcopy(summary)
        summary["phase"] = "modified"
        summary["recent"][0]["action"] = "modified"
        summary["recent"].append({"action": "modified"})
        self.assertEqual(self.executor.summary(self.context), original)


if __name__ == "__main__":
    unittest.main()
