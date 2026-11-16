"""Offline recovery-control contracts for pure policy and native-tool planning."""

import copy
import json
import unittest

from agent.brain import LLMError
from agent.llm_planner import LLMSurvivalPlanner
from agent.tools import TOOL_DEFINITIONS, TOOL_SCHEMAS, tools_for_objective
from core.action_permissions import FORCEABLE_ACTIONS
from core.daily_policy import DailySafetyPolicy
from skills import SkillResult
from test_daily_policy import action, observed
from test_llm_planner import Brain, observation, response


class RecoveryControlTests(unittest.TestCase):
    def planner(self, *replies, objective="daily", repairs=0):
        brain = Brain(*replies)
        planner = LLMSurvivalPlanner(
            {"enabled": True, "max_repair_attempts": repairs},
            {"objective": objective}, brain=brain)
        return planner, brain

    def policy(self, **settings):
        return DailySafetyPolicy({"low_health_threshold": 8, "threat_radius": 8,
                                  **settings}, allow_controls=True)

    def night(self, **status):
        obs = observed(**{"health": 19, "is_day": False, "time_of_day": 14000, **status})
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0],
                          "can_see": True, "can_dig": True}]
        return obs

    def force_args(self, **changes):
        return {"action": "gather_block", "pos": [2, 64, 0],
                "expected_name": "oak_log", "reason": "仅覆盖夜晚限制，目标可见", **changes}

    def offered(self, request):
        return {tool["function"]["name"] for tool in request["tools"]}

    def test_registry_counts_controls_and_request_copies_are_isolated(self):
        baseline = copy.deepcopy((TOOL_DEFINITIONS, TOOL_SCHEMAS))
        for objective, count in (("starter", 9), ("daily", 33), ("competition", 38)):
            with self.subTest(objective=objective):
                tools = tools_for_objective(objective)
                schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
                self.assertEqual(len(tools), count)
                self.assertEqual(len(schemas), count)
                if objective == "starter":
                    self.assertTrue({"report_blocked", "force_attempt"}.isdisjoint(schemas))
                else:
                    self.assertEqual(set(schemas["force_attempt"]["properties"]["action"]["enum"]),
                                     FORCEABLE_ACTIONS)
                    self.assertEqual(set(schemas["force_attempt"]["required"]), {"action", "reason"})
                    self.assertIn("reason", schemas["report_blocked"]["required"])
                    schemas["force_attempt"]["properties"]["action"]["enum"].clear()
                self.assertEqual(len(tools_for_objective(objective)), count)
        self.assertEqual((TOOL_DEFINITIONS, TOOL_SCHEMAS), baseline)
        self.assertEqual(set(next(tool for tool in tools_for_objective("daily")
                                  if tool["function"]["name"] == "force_attempt")
                             ["function"]["parameters"]["properties"]["action"]["enum"]), FORCEABLE_ACTIONS)

    def test_force_decision_preserves_flat_action_and_separates_reason(self):
        for objective in ("daily", "competition"):
            with self.subTest(objective=objective):
                args = self.force_args()
                planner, _ = self.planner(response("force_attempt", args), objective=objective)
                decision = planner.choose(self.night())
                self.assertEqual(decision.name, "force_attempt")
                self.assertEqual(decision.args, {key: value for key, value in args.items() if key != "reason"})
                self.assertEqual(decision.reason, args["reason"])
                self.assertNotIn("args", decision.args)
                inner = action(decision.args["action"], **{key: value for key, value in decision.args.items()
                                                         if key != "action"})
                self.assertIsNone(self.policy().validate_force(inner, self.night()))

    def test_report_blocked_defaults_and_exit_require_concrete_reason(self):
        for resolution in (None, "replan", "exit_task"):
            with self.subTest(resolution=resolution):
                args = {"reason": "无可验证的附近资源"}
                if resolution is not None:
                    args["resolution"] = resolution
                planner, _ = self.planner(response("report_blocked", args))
                decision = planner.choose(observation())
                self.assertEqual(decision.name, "report_blocked")
                self.assertEqual(decision.args, {"resolution": resolution or "replan"})
                self.assertEqual(decision.reason, args["reason"])
        for name in ("report_blocked", "force_attempt"):
            for reason in (None, "", " \t ", 42):
                with self.subTest(name=name, reason=reason):
                    args = self.force_args() if name == "force_attempt" else {}
                    args.pop("reason", None)
                    if reason is not None:
                        args["reason"] = reason
                    planner, brain = self.planner(response(name, args))
                    with self.assertRaises(LLMError) as caught:
                        planner.choose(self.night())
                    self.assertEqual(caught.exception.code, "llm_invalid_arguments")
                    self.assertEqual(len(brain.requests), 1)

    def test_force_schema_declares_real_action_required_fields(self):
        tools = tools_for_objective("daily")
        schema = next(row["function"]["parameters"] for row in tools if row["function"]["name"] == "force_attempt")
        branches = {row["properties"]["action"]["enum"][0]: set(row["required"]) for row in schema["anyOf"]}
        self.assertEqual(branches["clear_foliage"], {"pos", "expected_name"})
        self.assertEqual(branches["gather_block"], {"pos", "expected_name"})
        self.assertEqual(branches["craft_item"], {"item"})
        self.assertEqual(branches["place_item"], {"pos", "item"})

    def test_missing_force_target_in_reason_produces_specific_repair_then_valid_call(self):
        broken = {"action": "clear_foliage", "reason": "Clear leaves at [2,64,0] blocking the log"}
        correct = {"action": "clear_foliage", "pos": [2, 64, 0], "expected_name": "birch_leaves",
                   "reason": "One observed leaf blocks the exit"}
        planner, brain = self.planner(response("force_attempt", broken, "missing"),
                                      response("force_attempt", correct, "fixed"), repairs=1)
        obs = self.night()
        obs["blocks"] = [{"name": "birch_leaves", "pos": [2, 64, 0], "can_see": True}]
        decision = planner.choose(obs)
        self.assertEqual(decision.args["pos"], [2, 64, 0])
        self.assertEqual(len(brain.requests), 2)
        feedback = next(m for m in brain.requests[1]["messages"] if m.get("role") == "tool")
        content = json.loads(feedback["content"])
        self.assertIn("missing required keys: expected_name, pos", content["message"])
        self.assertIn("JSON fields", content["message"])
        self.assertFalse(content["executed"])
        planner, brain = self.planner(response("force_attempt", broken))
        with self.assertRaises(LLMError) as caught:
            planner.choose(obs)
        self.assertEqual(caught.exception.details["missing_keys"], ["expected_name", "pos"])
        self.assertIsNone(planner._pending, "Coordinates in reason must never be inferred as arguments")

    def test_repeated_malformed_force_calls_require_a_blockage_report_next_turn(self):
        broken = {"action": "clear_foliage", "reason": "Leaves at [2,64,0]"}
        planner, brain = self.planner(response("force_attempt", broken, "bad1"),
            response("force_attempt", broken, "bad2"),
            response("report_blocked", {"reason": "Cannot form a valid action yet"}, "report"), repairs=1)
        with self.assertRaises(LLMError):
            planner.choose(self.night())
        decision = planner.choose(self.night())
        self.assertEqual(decision.name, "report_blocked")
        self.assertEqual(self.offered(brain.requests[-1]), {"report_blocked"})
        self.assertEqual(len(brain.requests), 3)

    def test_force_rejects_forged_recursive_and_invalid_real_action_arguments(self):
        cases = [self.force_args(action="force_attempt"), self.force_args(action="report_blocked"),
                 self.force_args(action="move_to"), self.force_args(args={"pos": [2, 64, 0]}),
                 self.force_args(material="oak_log"), self.force_args(expected_name="diamond_block"),
                 self.force_args(pos=[2.5, 64, 0]), self.force_args(pos=[2, 64]),
                 {key: value for key, value in self.force_args().items() if key != "pos"}]
        cases += [{"action": "craft_item", "item": "stick", "count": count, "reason": "补充材料"}
                  for count in (0, 17, True)]
        cases.append({"action": "craft_item", "item": "minecraft:stick", "reason": "伪造配方"})
        for args in cases:
            with self.subTest(args=args):
                planner, _ = self.planner(response("force_attempt", args))
                with self.assertRaises(LLMError) as caught:
                    planner.choose(self.night())
                self.assertEqual(caught.exception.code, "llm_invalid_arguments")
        planner, _ = self.planner(response("force_attempt", {
            "action": "craft_item", "item": "stick", "reason": "补充材料"}))
        self.assertEqual(planner.choose(observation()).args,
                         {"action": "craft_item", "item": "stick", "count": 1})

    def test_night_override_and_advisory_turn_preserve_legacy_policy_default(self):
        obs = self.night()
        gather = action("gather_block", pos=[2, 64, 0], expected_name="oak_log")
        baseline = action("wait", seconds=2)
        policy = self.policy()
        self.assertIsNotNone(policy.validate(gather, obs))
        self.assertIsNone(policy.validate_force(gather, obs))
        self.assertIsNone(policy.choose(obs, baseline))
        limits = policy.constraints(obs)
        self.assertEqual(limits["scenario"], "advisory_blocked")
        self.assertTrue(limits["model_allowed"])
        self.assertEqual(set(limits["allowed_actions"]), {"force_attempt", "report_blocked", "wait"})
        legacy = DailySafetyPolicy({"low_health_threshold": 8, "threat_radius": 8})
        self.assertFalse(legacy.constraints(obs)["model_allowed"])
        self.assertEqual(legacy.choose(obs, baseline).name, "wait")
        self.assertEqual(legacy.validate_force(gather, obs), "unsupported_force_action")

    def test_force_danger_matrix_keeps_hard_vetoes(self):
        gather = action("gather_block", pos=[2, 64, 0], expected_name="oak_log")
        cases = [("status", key, value) for key, value in (
            ("health", 8), ("food", 6), ("alive", False), ("ready", False), ("busy", "dig"),
            ("dimension", "the_nether"), ("gamemode", "creative"), ("on_ground", False),
            ("in_vehicle", True), ("on_fire", True), ("in_lava", True), ("in_water", True),
            ("submerged", True), ("thundering", True), ("time_of_day", float("nan")))]
        cases += [("root", key, value) for key, value in (
            ("fresh", False), ("stale", True), ("observation_scope", "safety"),
            ("inventory_fresh", False), ("terrain_fresh", False),
            ("entities", [{"type": "hostile", "name": "zombie", "pos": [12, 64, 0]}]),
            ("terrain", [{"name": "lava", "pos": [1, 64, 0]}]))]
        cases.append(("status", "nearest_hostile", {"dist": 12, "name": "zombie"}))
        for scope, key, value in cases:
            with self.subTest(scope=scope, key=key):
                obs = self.night()
                (obs["status"] if scope == "status" else obs)[key] = value
                self.assertIsNotNone(self.policy().validate_force(gather, obs))
        obs = self.night()
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [15, 64, 0]}]
        self.assertIsNotNone(self.policy(threat_radius=16).validate_force(gather, obs))
        self.assertIsNone(self.policy().validate_force(gather, self.night(health=9, food=7)))

    def test_force_requires_local_matching_visible_targets_stances_and_inventory(self):
        policy = self.policy()
        gather = action("gather_block", pos=[2, 64, 0], expected_name="oak_log")
        for field, value in (("can_see", False), ("can_see", None), ("can_dig", False),
                             ("name", "stone"), ("pos", [3, 64, 0])):
            with self.subTest(field=field, value=value):
                obs = self.night()
                obs["blocks"][0][field] = value
                self.assertIsNotNone(policy.validate_force(gather, obs))
        obs = self.night()
        obs["blocks"] = []
        self.assertIsNotNone(policy.validate_force(gather, obs))
        for name, expected, far in (("gather_block", "oak_log", [5, 64, 0]),
                                    ("clear_foliage", "oak_leaves", [4, 64, 0])):
            with self.subTest(name=name):
                obs = self.night()
                obs["blocks"] = [{"name": expected, "pos": far, "can_see": True}]
                self.assertIsNotNone(policy.validate_force(action(name, pos=far, expected_name=expected), obs))
        obs = self.night()
        stance = action("position_for_task", pos=[2, 64, 0])
        self.assertIsNotNone(policy.validate_force(stance, obs))
        obs["safe_stand_candidates"] = [{"pos": [2, 64, 0]}]
        self.assertIsNone(policy.validate_force(stance, obs))
        for name, item in (("place_item", "oak_planks"), ("place_bed", "white_bed"),
                           ("place_door", "oak_door")):
            with self.subTest(name=name):
                obs = self.night()
                decision = action(name, pos=[2, 64, 0], item=item)
                self.assertIsNotNone(policy.validate_force(decision, obs))
                obs["counts"][item] = 1
                self.assertIsNone(policy.validate_force(decision, obs))

    def test_no_physical_opportunity_can_report_but_explicit_danger_stays_local(self):
        obs = self.night()
        obs["blocks"] = []
        policy = self.policy()
        limits = policy.constraints(obs)
        self.assertTrue(limits["model_allowed"])
        planner, brain = self.planner(response("report_blocked", {"reason": "附近没有可验证目标"}))
        self.assertEqual(planner.choose(obs, {"survival_constraints": limits}).name, "report_blocked")
        self.assertEqual(self.offered(brain.requests[0]), {"force_attempt", "report_blocked", "wait"})
        for fields in ({"on_fire": True}, {"in_water": True}, {"thundering": True},
                       {"nearest_hostile": {"dist": 4, "name": "zombie"}}):
            with self.subTest(fields=fields):
                danger = self.night(**fields)
                self.assertFalse(policy.constraints(danger)["model_allowed"])
                self.assertIsNotNone(policy.choose(danger, action("wait", seconds=2)))

    def test_provider_filters_hidden_tool_and_pairs_one_bounded_repair(self):
        for objective in ("daily", "competition"):
            with self.subTest(objective=objective):
                planner, brain = self.planner(
                    response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}, "hidden"),
                    response("report_blocked", {"reason": "普通采集未获许可"}, "repair"),
                    objective=objective, repairs=1)
                before = copy.deepcopy(planner.tool_definitions)
                context = {"survival_constraints": {"allowed_actions": ["report_blocked", "force_attempt"]}}
                decision = planner.choose(observation(), context)
                self.assertEqual(decision.name, "report_blocked")
                self.assertEqual(len(brain.requests), 2)
                for request in brain.requests:
                    self.assertEqual(self.offered(request), {"report_blocked", "force_attempt", "wait"})
                messages = brain.requests[1]["messages"]
                rejected = [message for message in messages if message["role"] == "tool"]
                self.assertEqual([message["tool_call_id"] for message in rejected], ["hidden"])
                payload = json.loads(rejected[0]["content"])
                self.assertFalse(payload["executed"])
                self.assertEqual(payload["error_code"], "llm_unavailable_tool")
                calls = [call["id"] for message in messages if message["role"] == "assistant"
                         for call in message.get("tool_calls", [])]
                self.assertEqual(calls, ["hidden"])
                self.assertEqual(planner.tool_definitions, before)
        planner, brain = self.planner(response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}),
                                      response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}),
                                      response(), repairs=1)
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(), {"survival_constraints": {"allowed_actions": ["wait"]}})
        self.assertEqual(caught.exception.code, "llm_unavailable_tool")
        self.assertEqual(len(brain.requests), 2)
        self.assertEqual(len(brain.responses), 1)

    def test_three_unchanged_waits_require_a_blockage_report_before_more_waiting(self):
        planner, brain = self.planner(*(response(call_id=f"idle_{index}") for index in range(3)),
            response("report_blocked", {"reason": "No useful nearby action"}, "blocked"), response(call_id="after_report"))
        obs = observation()
        for _ in range(3):
            decision = planner.choose(obs)
            planner.observe_result(decision, SkillResult(True, "Waited"), obs)
        decision = planner.choose(obs)
        self.assertEqual(self.offered(brain.requests[-1]), {"report_blocked"})
        planner.observe_result(decision, SkillResult(True, "Blockage recorded"), obs)
        planner.choose(obs)
        self.assertIn("wait", self.offered(brain.requests[-1]))

    def test_inventory_progress_releases_the_wait_only_report_gate(self):
        planner, brain = self.planner(*(response(call_id=f"idle_{index}") for index in range(4)))
        obs = observation()
        for _ in range(3):
            decision = planner.choose(obs)
            planner.observe_result(decision, SkillResult(True, "Waited"), obs)
        obs["counts"] = {"oak_log": 1}
        planner.choose(obs)
        self.assertIn("craft_item", self.offered(brain.requests[-1]))

    def test_competition_tools_follow_each_fresh_phase_and_permission_intersection(self):
        phases = ("selection", "active", "completed", "selection")
        planner, brain = self.planner(*(response(call_id=f"phase_{index}") for index in range(len(phases))),
                                      objective="competition")
        registry = copy.deepcopy(planner.tool_definitions)
        obs = observation()
        for phase in phases:
            decision = planner.choose(obs, {"competition": {"phase": phase}, "survival_constraints": {
                "allowed_actions": ["force_attempt", "report_blocked", "select_task", "update_progress"]}})
            names = self.offered(brain.requests[-1])
            expected = ({"select_task", "report_blocked", "wait"} if phase == "selection" else
                        {"force_attempt", "report_blocked", "update_progress", "wait"} if phase == "active" else
                        {"wait"})
            self.assertEqual(names, expected)
            planner.observe_result(decision, SkillResult(True, "Offline phase feedback"), obs)
        self.assertEqual(planner.tool_definitions, registry)

    def test_force_bypasses_only_old_advisory_failure_once(self):
        for code in ("safety_changed", "unsafe_time", "no_path"):
            with self.subTest(code=code):
                obs = self.night()
                planner, brain = self.planner(response("gather_block", {
                    "pos": [2, 64, 0], "expected_name": "oak_log"}),
                    response("force_attempt", self.force_args(), "override"))
                ordinary = planner.choose(obs)
                planner.observe_result(ordinary, SkillResult(False, "Offline refusal", error_code=code), obs)
                if code == "no_path":
                    with self.assertRaises(LLMError) as caught:
                        planner.choose(obs)
                    self.assertEqual(caught.exception.code, "llm_repeated_failure")
                else:
                    self.assertEqual(planner.choose(obs).name, "force_attempt")
                self.assertEqual(len(brain.requests), 2)

    def test_failed_force_cannot_repeat_after_wait_or_changed_reason(self):
        obs = self.night()
        planner, brain = self.planner(response("force_attempt", self.force_args(), "first"),
                                      response("wait", call_id="pause"),
                                      response("force_attempt", self.force_args(reason="换个理由仍是同一目标"), "repeat"),
                                      response("report_blocked", {"reason": "该强制尝试已经失败"}, "blocked"), repairs=1)
        forced = planner.choose(obs)
        planner.observe_result(forced, SkillResult(False, "Offline refusal", error_code="unsafe_time"), obs)
        wait = planner.choose(obs)
        planner.observe_result(wait, SkillResult(True, "Offline wait"), obs)
        self.assertEqual(planner.choose(obs).name, "report_blocked")
        self.assertEqual(len(brain.requests), 4)
        rejected = [json.loads(message["content"]) for message in brain.requests[-1]["messages"]
                    if message["role"] == "tool" and message["tool_call_id"] == "repeat"]
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0]["executed"])
        self.assertEqual(rejected[0]["error_code"], "llm_repeated_failure")


if __name__ == "__main__":
    unittest.main()

