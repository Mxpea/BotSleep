"""Known failed far approaches trigger bounded fresh local recovery, no live I/O."""

import unittest
from unittest.mock import patch

from core.autonomous_tasks import AutonomousTaskController
from core.daily_observation import DailyObservationCollector
from core.survival_state import ObservationCollector
from skills import SkillResult
from skills.positioning import PositioningSkills
from skills.survival import SurvivalSkills
from test_autonomous_tasks import apply_atomic, world
from test_daily_skills import DailyWorld, envelope


class GatherNavigationRecoveryTests(unittest.TestCase):
    def test_walks_level_ground_to_a_low_rooted_trunk_before_collecting(self):
        client = DailyWorld()
        client.status.update(pos=[12.5, 70, .5], is_day=True, thundering=False, time_of_day=4500)
        root = [14, 70, 8]
        client.block(root, "birch_log")
        client.block([14, 69, 8], "dirt")
        client.block([10, 73, -1], "oak_log")
        for x, z in [(12, 0), *((13, z) for z in range(5))]:
            client.block([x, 69, z], "grass_block")
        collector = DailyObservationCollector(client, mainline=True)
        ctl = AutonomousTaskController({"objective": "daily", "autonomy": {"mainline": True}})
        observed_moves = []
        def cut(pos, collect=True):
            self.assertTrue(collect)
            self.assertEqual(pos, root)
            self.assertEqual(client.status["pos"], [13.5, 70, 4.5])
            client.block(pos, "air")
            client.counts["birch_log"] = 1
            return envelope({"dug": "birch_log"})
        client.dig = cut
        for index in range(7):
            obs = world(client.counts, **client.status)
            obs["blocks"] = [{"pos": [10, 73, -1], "name": "oak_log", "can_see": True, "can_dig": True}]
            if not client.counts:
                obs["blocks"].append({"pos": root, "name": "birch_log"})
            obs.update(ObservationCollector(client)._local_geometry(client.status, None))
            collector._ground_log(obs, None)
            if index == 0:
                self.assertEqual(obs["ground_log_approach"]["steps"], 5)
                ctl.start({"task": "gather", "resource": "logs", "count": 1}, obs)
            decision = ctl.next_action(obs)
            if decision is None:
                break
            runner = PositioningSkills(client) if decision.name == "position_for_task" else SurvivalSkills(client)
            result = runner.execute(decision.name, decision.args)
            self.assertTrue(result.success, result)
            observed_moves.append(decision.name)
            ctl.record_result(decision, result, world(client.counts, **client.status))
        self.assertEqual(observed_moves, ["position_for_task"] * 5 + ["gather_block"])
        self.assertEqual(sum(call[0] == "move" for call in client.calls), 5)
        self.assertEqual(ctl.snapshot()["status"], "completed")
        self.assertEqual(client.counts["birch_log"], 1)
        self.assertEqual(ctl.snapshot()["ai_escalations"], 0)

    def test_ground_log_approach_cannot_infer_support_or_visibility_from_an_opaque_stand(self):
        client = DailyWorld()
        client.status.update(pos=[.5, 70, .5], is_day=True, thundering=False, time_of_day=4500)
        target = [0, 70, 3]
        client.block(target, "oak_log")
        client.block([0, 69, 0], "grass_block")
        obs = world({}, **client.status)
        obs.update(local_geometry_origin=list(client.status["pos"]), blocks=[{"name": "oak_log", "pos": target}])
        for ground in ("air", "oak_leaves", "dirt"):
            client.block([0, 69, 3], ground)
            client.block([0, 70, 1], "stone")
            client.block([0, 71, 1], "stone")
            DailyObservationCollector(client, mainline=True)._ground_log(obs, None)
            self.assertNotIn("ground_log_approach", obs)

    def test_ground_logging_respects_mainline_scope_and_a_fresh_nearby_clearable_leaf(self):
        client = DailyWorld()
        obs = world(pos=[.5, 70, .5])
        target, blocker, upper = [6, 70, 0], [1, 71, 0], [2, 72, 0]
        outer = {"name": "birch_log", "pos": target}
        obs.update(local_geometry_origin=list(obs["status"]["pos"]), blocks=[outer])
        DailyObservationCollector(client)._ground_log(obs, None)
        self.assertEqual(client.calls, [])
        hint = {"verified": True, "ground": "dirt", "visibility": {"visible": True},
                "origin": list(obs["status"]["pos"]), "target": target, "name": "birch_log",
                "next": [1.5, 70, .5], "steps": 3}
        obs.update(ground_log_approach=hint, safe_stand_candidates=[{"pos": hint["next"], "support": "dirt"}])
        for settings in ({"objective": "daily"}, {"objective": "competition", "autonomy": {"mainline": True}},
                         {"objective": "daily", "autonomy": {"mainline": False}, "operator_goal": "custom"}):
            ctl = AutonomousTaskController(settings)
            ctl.start({"task": "gather", "resource": "logs", "count": 1}, obs)
            self.assertEqual(ctl.next_action(obs).name, "gather_block")
        ctl = AutonomousTaskController({"objective": "daily", "autonomy": {"mainline": True}, "operator_goal": "stone tools"})
        ctl.start({"task": "gather", "resource": "logs", "count": 1}, obs)
        self.assertEqual(ctl.next_action(obs).name, "position_for_task")
        obs["blocks"].extend([{"name": "oak_log", "pos": upper, "can_see": False, "visibility": {"blocker": blocker}},
                              {"name": "oak_leaves", "pos": blocker, "can_see": True, "fresh": True}])
        ctl = AutonomousTaskController({"objective": "daily", "autonomy": {"mainline": True}})
        ctl.start({"task": "gather", "resource": "logs", "count": 1}, obs)
        clear = ctl.next_action(obs)
        self.assertEqual((clear.name, clear.args["pos"]), ("clear_foliage", blocker))

    def real_occluded_approach(self):
        before = [.5, 70, -6.5]
        obs = world(pos=list(before))
        target, leaf = [5, 72, 0], [4, 73, 0]
        obs["blocks"] = [{"name": "oak_log", "pos": target, "can_see": True, "can_dig": False}]
        ctl = AutonomousTaskController()
        ctl.start({"task": "craft", "item": "wooden_pickaxe", "count": 1}, obs)
        attempt = ctl.next_action(obs)
        self.assertEqual((attempt.name, attempt.args["pos"]), ("gather_block", target))
        obs["status"]["pos"] = [3.6, 72, .4]
        ctl.record_result(attempt, SkillResult(False, "blocked after approach", {
            "pos_before": before, "executed": True, "target": target, "can_see": False, "can_dig": False,
            "visibility": {"state": "occluded", "visible": False, "reason": "blocked_samples", "queries": 2, "blocker": leaf},
            "cleanup": "stop_requested"}, "target_occluded"), obs)
        return ctl, obs, target, leaf

    def test_live_steps_134_142_150_do_not_reenter_the_same_column_after_canopy_escape(self):
        now = 1794987507.96
        with patch("core.autonomous_tasks.time.time", return_value=now):
            ctl, obs, target, _ = self.real_occluded_approach()
            state = ctl.state()
        obs["status"]["pos"] = [3.6, 70, -4.5]
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["blocks"][0]["can_dig"] = True  # Approach is still mandatory at >4.
        obs["blocks"].append({"name": "oak_log", "pos": [5, 73, 0], "can_see": True, "can_dig": True})
        obs["safe_stand_candidates"] = [{"pos": [3.5, 70, -6.5], "support": "grass_block"}]
        for seconds in (14, 28):
            with self.subTest(seconds=seconds), patch("core.autonomous_tasks.time.time", return_value=now + seconds):
                restored = AutonomousTaskController(saved=state)
                restored.interrupt("canopy_escape")
                next_step = restored.next_action(obs)
                self.assertEqual(next_step.name, "position_for_task")
                alternate = {"name": "birch_log", "pos": [8, 72, -6], "can_see": True, "can_dig": True}
                other = {**obs, "blocks": [*obs["blocks"], alternate]}
                replacement = AutonomousTaskController(saved=state).next_action(other)
                self.assertEqual((replacement.name, replacement.args["pos"]), ("gather_block", alternate["pos"]))
                near = {**obs, "status": {**obs["status"], "pos": [3.5, 72, .5]}}
                direct = AutonomousTaskController(saved=state).next_action(near)
                self.assertEqual((direct.name, direct.args["pos"]), ("gather_block", target))
        with patch("core.autonomous_tasks.time.time", return_value=now + 31):
            self.assertEqual(AutonomousTaskController(saved=state).next_action(obs).args["pos"], target)

    def test_only_confirmed_removal_of_the_observed_blocker_reopens_a_far_tree(self):
        for verified in (False, True):
            ctl, obs, target, leaf = self.real_occluded_approach()
            obs["blocks"] = [{"name": "oak_log", "pos": target, "can_see": False, "visibility": {"blocker": leaf}},
                             {"name": "oak_leaves", "pos": leaf, "fresh": True, "can_see": True}]
            decision = ctl.next_action(obs)
            self.assertEqual((decision.name, decision.args["pos"]), ("clear_foliage", leaf))
            result = SkillResult(True, "leaf result", {"executed": True, "verified": verified, "block_removed": True,
                                "remaining_block": "air", "pos": leaf, "cleared": "oak_leaves"})
            ctl.record_result(decision, result, obs)
            obs["status"]["pos"] = [3.6, 70, -4.5]
            obs["blocks"][0].update(can_see=True, can_dig=True)
            decision = ctl._gather("logs", obs, ctl._context({}, obs))
            if verified:
                self.assertEqual((decision.name, decision.args["pos"]), ("gather_block", target))
            else:
                self.assertIsNone(decision)

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
