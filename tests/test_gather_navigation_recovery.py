"""Known failed far approaches trigger bounded fresh local recovery, no live I/O."""

import unittest
from unittest.mock import patch

from core.autonomous_tasks import AutonomousTaskController
from skills import SkillResult
from test_autonomous_tasks import apply_atomic, world


class GatherNavigationRecoveryTests(unittest.TestCase):
    def setup_goal(self, **limits):
        obs = world(pos=[.6, 68, 8.5])
        obs["blocks"] = [{"name": name, "pos": pos, "can_see": True, "can_dig": True}
            for name, pos in (("oak_log", [5, 71, 0]), ("oak_log", [5, 72, 0]),
                              ("birch_log", [14, 70, 8]), ("birch_log", [14, 71, 8]))]
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [1.5, 68, 8.5], "support": "grass_block"}]
        controller = AutonomousTaskController({"autonomy": limits})
        controller.start({"task": "gather", "resource": "logs", "count": 12}, obs)
        return controller, obs

    def fail_far(self, controller, obs, *, code="path_not_found", **data):
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "gather_block")
        result = SkillResult(False, "No path to the goal", {
            "pos_before": list(obs["status"]["pos"]), "executed": True,
            "cleanup": "stop_requested", **data}, code)
        controller.record_result(decision, result, obs)
        return decision

    def test_real_far_failure_repositions_before_trying_another_tree(self):
        controller, obs = self.setup_goal()
        self.fail_far(controller, obs)
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["pos"]), ("position_for_task", [1.5, 68, 8.5]))
        self.assertEqual(decision.goal, "explore")
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        candidate = controller.next_action(obs)
        self.assertEqual((candidate.name, candidate.args["expected_name"]), ("gather_block", "birch_log"))
        self.assertEqual(controller.state()["explore_steps"], 1)
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)

    def test_failed_tree_column_is_excluded_but_only_for_short_cooldown(self):
        controller, obs = self.setup_goal()
        first = self.fail_far(controller, obs)
        obs["status"]["pos"] = [1.5, 68, 8.5]
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["blocks"] = [row for row in obs["blocks"] if row["pos"][:1] == [5]]
        self.assertIsNone(controller._gather("logs", obs, controller._context({}, obs)))
        expiry = controller.state()["recent_results"][-1]["gather_navigation"]["expires_at"]
        with patch("core.autonomous_tasks.time.time", return_value=expiry + .1):
            action = controller._gather("logs", obs, controller._context({}, obs))
        self.assertEqual(action.args["pos"], first.args["pos"])

    def test_fresh_reachable_nearby_log_can_be_taken_without_another_path_attempt(self):
        controller, obs = self.setup_goal()
        self.fail_far(controller, obs)
        obs["blocks"].append({"name": "oak_log", "pos": [2, 68, 8], "can_see": True, "can_dig": True})
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["pos"]), ("gather_block", [2, 68, 8]))
        controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertEqual(controller.next_action(obs).name, "position_for_task")

    def test_nearby_but_unproven_reach_still_requires_stance_recovery(self):
        controller, obs = self.setup_goal()
        self.fail_far(controller, obs)
        obs["blocks"].append({"name": "oak_log", "pos": [2, 68, 8], "can_see": True})
        self.assertEqual(controller.next_action(obs).name, "position_for_task")

    def test_no_stale_unsafe_or_exhausted_stance_can_be_used(self):
        for change in ({"safe_stand_candidates": []}, {"local_geometry_origin": [20, 68, 8.5]},
                       {"safe_stand_candidates": [{"pos": [1.5, 68, 8.5], "support": "oak_leaves"}]}):
            with self.subTest(change=change):
                controller, obs = self.setup_goal()
                self.fail_far(controller, obs)
                obs.update(change)
                self.assertIsNone(controller.next_action(obs))
                self.assertEqual(controller.state()["explore_steps"], 0)
        controller, obs = self.setup_goal(max_explore_steps=0)
        self.fail_far(controller, obs)
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")

    def test_short_recovery_stance_does_not_allow_same_cell_or_new_stair_permission(self):
        for target in ([.5, 68, 8.5], [.9, 68, 8.5], [1.5, 69, 8.5], [4.5, 68, 8.5]):
            with self.subTest(target=target):
                controller, obs = self.setup_goal()
                self.fail_far(controller, obs)
                obs["safe_stand_candidates"] = [{"pos": target, "support": "grass_block"}]
                self.assertIsNone(controller.next_action(obs))
                self.assertEqual(controller.state()["explore_steps"], 0)

    def test_hurt_life_unknown_and_deadline_remain_above_local_recovery(self):
        for change in ({"health": 2}, {"last_death": {"t": 1}}, {"dimension": "the_nether"},
                       {"nearest_hostile": {"name": "skeleton", "dist": 2}}):
            controller, obs = self.setup_goal()
            self.fail_far(controller, obs)
            obs["status"].update(change)
            self.assertIsNone(controller.next_action(obs))
        for code in ("request_timeout", "action_unconfirmed", "invalid_response"):
            controller, obs = self.setup_goal()
            self.fail_far(controller, obs, code=code)
            self.assertNotIn("gather_navigation", controller.state()["recent_results"][-1])
            self.assertIsNone(controller.next_action(obs))
        controller, obs = self.setup_goal(max_steps=1)
        self.fail_far(controller, obs)
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["reason"], "task_budget_exhausted")

    def test_uncollected_or_occluded_resource_is_not_treated_as_navigation_failure(self):
        for code, detail in (("gather_unverified", {"block_removed": True}),
                             ("target_occluded", {}), ("path_not_found", {"block_removed": True}),
                             ("path_not_found", {"gained": {"oak_log": 1}})):
            controller, obs = self.setup_goal()
            self.fail_far(controller, obs, code=code, **detail)
            self.assertNotIn("gather_navigation", controller.state()["recent_results"][-1])

    def test_restart_keeps_negative_hint_but_requires_current_stance_evidence(self):
        controller, obs = self.setup_goal()
        self.fail_far(controller, obs)
        restored = AutonomousTaskController(saved=controller.state())
        decision = restored.next_action(obs)
        self.assertEqual(decision.name, "position_for_task")
        restored = AutonomousTaskController(saved=controller.state())
        obs["safe_stand_candidates"] = []
        self.assertIsNone(restored.next_action(obs))

    def test_failed_local_route_can_then_use_only_fresh_leaf_recovery(self):
        controller, obs = self.setup_goal()
        self.fail_far(controller, obs)
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "position_for_task")
        blocker = [1, 69, 8]
        obs["blocks"].append({"name": "oak_leaves", "pos": blocker, "fresh": True,
            "can_see": True, "diggable": True, "properties": {"waterlogged": False}})
        controller.record_result(decision, SkillResult(False, "Leaf blocks this route", {
            "executed": False, "origin": list(obs["status"]["pos"]),
            "blocked_cell": blocker, "block": "oak_leaves"}, "unsafe_route"), obs)
        clear = controller.next_action(obs)
        self.assertEqual((clear.name, clear.args["pos"]), ("clear_foliage", blocker))
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)
        self.assertEqual(controller.state()["explore_steps"], 1)


if __name__ == "__main__":
    unittest.main()
