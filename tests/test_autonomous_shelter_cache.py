"""Scoped, expiring negative search hints; never construction permission."""

import json
import unittest
from unittest.mock import patch

from core.autonomous_tasks import AutonomousTaskController
import test_autonomous_shelter as shelter_fixtures
from test_autonomous_tasks import apply_atomic


class ShelterSearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.scope = {"endpoint": "http://offline.invalid", "username": "fixture", "world_id": "world-a"}
        self.context = {"life_generation": 1}
        self.obs = shelter_fixtures.AutonomousShelterTests.survey_observation()
        self.controller = AutonomousTaskController(scope=self.scope)
        self.controller.start({"task": "shelter"}, self.obs, self.context)

    def reject(self, **changes):
        decision = self.controller.next_action(self.obs, self.context)
        result = shelter_fixtures.AutonomousShelterTests.survey_rejection(decision, **changes)
        self.controller.record_result(decision, result, self.obs, self.context)
        return decision

    def restart_goal(self):
        self.controller.stop("bounded_search_finished")
        self.controller.start({"task": "shelter"}, self.obs, self.context)

    def test_new_goal_keeps_negative_sites_but_resets_all_task_budgets(self):
        first = self.reject()
        self.restart_goal()
        state = self.controller.state()
        for key in ("steps", "survey_steps", "survey_since_move", "explore_steps", "local_failures", "no_progress"):
            self.assertEqual(state[key], 0, key)
        self.assertEqual(state["rejected_sites"], [])  # Current task evidence stays separate.
        next_decision = self.controller.next_action(self.obs, self.context)
        self.assertNotEqual(next_decision.args["pos"], first.args["pos"])
        report = self.controller.snapshot()["shelter_search"]
        self.assertEqual(report["recent_rejected_centers"], [first.args["pos"]])
        self.assertFalse(report["safety_verified"])
        self.assertNotIn("base_site", self.obs["daily_evidence"])

    def test_expiry_allows_fresh_resurvey_without_guessing_site_changed(self):
        first = self.reject()
        expiry = max(row["expires_at"] for row in self.controller.state()["shelter_search_cache"]["rejected"].values())
        with patch("core.autonomous_tasks.time.time", return_value=expiry + 1):
            self.restart_goal()
            self.assertEqual(self.controller.snapshot()["shelter_search"]["rejected_center_count"], 0)
            self.assertEqual(self.controller.next_action(self.obs, self.context).args["pos"], first.args["pos"])

    def test_dimension_death_and_life_generation_each_invalidate_cache(self):
        for change in ("dimension", "death", "generation"):
            with self.subTest(change=change):
                self.setUp()
                self.reject()
                if change == "dimension": self.obs["status"]["dimension"] = "minecraft:overworld"
                elif change == "death": self.obs["status"]["last_death"] = {"t": 123}
                else: self.context["life_generation"] += 1
                self.restart_goal()
                self.assertEqual(self.controller.snapshot()["shelter_search"]["rejected_center_count"], 0)

    def test_checkpoint_retains_timed_cache_only_in_matching_world_scope(self):
        first = self.reject()
        self.controller.stop("search_finished")
        saved = self.controller.state()
        for same_scope in (True, False):
            with self.subTest(same_scope=same_scope):
                scope = self.scope if same_scope else {**self.scope, "world_id": "world-b"}
                restored = AutonomousTaskController(saved=saved, scope=scope)
                restored.start({"task": "shelter"}, self.obs, self.context)
                found = restored.next_action(self.obs, self.context)
                self.assertEqual(found.args["pos"] == first.args["pos"], not same_scope)

    def test_old_checkpoint_without_timed_cache_does_not_invent_expiry(self):
        self.reject()
        self.controller.stop("search_finished")
        saved = self.controller.state()
        saved.pop("shelter_search_cache")
        restored = AutonomousTaskController(saved=saved, scope=self.scope)
        self.assertTrue(restored.state()["rejected_sites"])
        self.assertEqual(restored.snapshot()["shelter_search"]["rejected_center_count"], 0)
        restored.start({"task": "shelter"}, self.obs, self.context)
        self.assertEqual(restored.snapshot()["shelter_search"]["rejected_center_count"], 0)

    def test_unknown_and_incomplete_rejection_do_not_enter_cache_or_unfreeze_task(self):
        for change in ({"fresh": False}, {"executed": True}, {"geometry": {}}, {"orientation": None},
                       {"action_state": "unknown"}):
            with self.subTest(change=change):
                self.setUp()
                self.reject(**change)
                self.assertEqual(self.controller.snapshot()["shelter_search"]["rejected_center_count"], 0)
                if change.get("action_state") == "unknown":
                    with self.assertRaises(ValueError): self.restart_goal()
                    self.assertEqual(self.controller.snapshot()["status"], "unknown")

    def test_verified_exploration_stances_carry_over_but_not_explore_budget(self):
        for _ in range(4): self.reject()
        decision = self.controller.next_action(self.obs, self.context)
        self.assertEqual(decision.goal, "explore")
        result = apply_atomic(self.obs, decision)
        shelter_fixtures.AutonomousShelterTests.refresh_survey_stands(self.obs)
        self.controller.record_result(decision, result, self.obs, self.context)
        landed = list(self.obs["status"]["pos"])
        self.restart_goal()
        self.assertEqual(self.controller.state()["explore_steps"], 0)
        self.assertIn(landed, self.controller.snapshot()["shelter_search"]["visited_stances"])
        self.obs["status"]["pos"] = [landed[0] - 2, landed[1], landed[2]]
        self.obs["local_geometry_origin"] = list(self.obs["status"]["pos"])
        self.obs["safe_stand_candidates"] = [{"pos": landed, "support": "grass_block"}]
        self.assertIsNone(self.controller._explore(self.obs, self.controller._context(self.context, self.obs)))

    def test_cache_is_bounded_and_invalid_timestamp_does_not_erase_unknown_state(self):
        for i in range(160):
            self.controller._remember_shelter_search("rejected", f"{i},64,0|north",
                pos=[i,64,0], orientation="north", error_code="unsafe_site")
            self.controller._remember_shelter_search("visited", f"{i},64,0", pos=[i,64,0])
        self.controller.stop("unresolved_write", unknown=True)
        saved = self.controller.state()
        self.assertEqual(len(saved["shelter_search_cache"]["rejected"]), 128)
        self.assertEqual(len(saved["shelter_search_cache"]["visited"]), 32)
        self.assertLess(len(json.dumps(saved).encode()), 65536)
        next(iter(saved["shelter_search_cache"]["rejected"].values())).pop("observed_at")
        restored = AutonomousTaskController(saved=saved, scope=self.scope)
        self.assertEqual(restored.snapshot()["status"], "unknown")
        self.assertEqual(restored.snapshot()["shelter_search"]["rejected_center_count"], 0)


if __name__ == "__main__":
    unittest.main()
