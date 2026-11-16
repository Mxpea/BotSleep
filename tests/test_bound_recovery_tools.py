"""Offline contracts for observation-bound canopy recovery tools."""

import copy
import json
import unittest

from agent.brain import LLMError
from agent.llm_planner import LLMSurvivalPlanner
from agent.tools import TOOL_DEFINITIONS, TOOL_SCHEMAS, tools_for_objective
from skills import SkillResult
from skills.daily_survival import DailySurvivalSkills
from test_llm_planner import Brain, observation, response


class BoundRecoveryToolTests(unittest.TestCase):
    ALIAS = "force_clear_foliage"
    CONTROLS = {"force_attempt", "report_blocked", "wait"}

    def planner(self, *replies, objective="daily"):
        brain = Brain(*replies)
        planner = LLMSurvivalPlanner(
            {"enabled": True, "max_repair_attempts": 0},
            {"objective": objective}, brain=brain)
        return planner, brain

    def leaf(self, pos=None, name="oak_leaves", **changes):
        return {"name": name, "pos": [1, 64, 0] if pos is None else list(pos),
                "can_see": True, "can_dig": True,
                "properties": {"waterlogged": False}, **changes}

    def scene(self, *leaves, **status):
        obs = observation(is_day=False, time_of_day=14000, **status)
        obs.update(inventory_fresh=True, terrain_fresh=True,
                   blocks=copy.deepcopy(list(leaves) if leaves else [self.leaf()]))
        return obs

    def context(self, *, blocked=None, phase=None, allowed=None):
        context = {"recovery": {"kind": "canopy", "blocked_cells": blocked or []},
                   "survival_constraints": {
                       "allowed_actions": sorted(self.CONTROLS if allowed is None else allowed)}}
        if phase is not None:
            context["competition"] = {"phase": phase, "active_goal_id": None}
        return context

    def offered(self, request):
        return {row["function"]["name"]: row["function"] for row in request["tools"]}

    def assert_bound(self, decision, leaf):
        self.assertEqual(decision.name, "force_attempt")
        self.assertEqual(decision.args, {"action": "clear_foliage", "pos": leaf["pos"],
                                         "expected_name": leaf["name"]})
        self.assertTrue(decision.reason.strip())

    def test_empty_alias_binds_one_action_in_daily_and_competition_selection(self):
        for objective, phase in (("daily", None), ("competition", "selection")):
            with self.subTest(objective=objective):
                planner, brain = self.planner(response(self.ALIAS), objective=objective)
                leaf = self.leaf()
                decision = planner.choose(self.scene(leaf, health=12, food=8),
                                          self.context(phase=phase))
                self.assert_bound(decision, leaf)
                tools = self.offered(brain.requests[0])
                self.assertEqual(set(tools), {self.ALIAS, "report_blocked", "wait"})
                schema = tools[self.ALIAS]["parameters"]
                self.assertEqual(set(schema["properties"]), {"reason"})
                self.assertEqual(schema["properties"]["reason"]["type"], "string")
                self.assertEqual(schema["required"], [])
                self.assertIs(schema["additionalProperties"], False)
                self.assertIn(leaf["name"], tools[self.ALIAS]["description"])

    def test_blocked_cell_precedes_nearest_and_all_registered_foliage_is_supported(self):
        near = self.leaf([1, 64, 0])
        far = self.leaf([-1, 65, -1], "birch_leaves")
        for blocked, expected in (([far], far), ([], near),
                                  ([self.leaf([9, 64, 9])], near)):
            with self.subTest(blocked=blocked):
                planner, brain = self.planner(response(self.ALIAS))
                decision = planner.choose(self.scene(far, near), self.context(blocked=blocked))
                self.assert_bound(decision, expected)
                self.assertEqual(list(self.offered(brain.requests[0])).count(self.ALIAS), 1)
        for name in sorted(DailySurvivalSkills.FOLIAGE):
            with self.subTest(foliage=name):
                planner, _ = self.planner(response(self.ALIAS))
                leaf = self.leaf([1, 65, 0], name)
                self.assert_bound(planner.choose(self.scene(leaf), self.context()), leaf)

    def test_binding_skips_blocked_or_failed_targets_instead_of_proposing_rejected_leaf(self):
        first, other = self.leaf(), self.leaf([-1, 64, 0], "birch_leaves")
        planner, brain = self.planner(response(self.ALIAS))
        context = self.context(blocked=[first])
        context["blocked_targets"] = ["1,64,0"]
        decision = planner.choose(self.scene(first, other), context)
        self.assert_bound(decision, other)
        planner, brain = self.planner(response(self.ALIAS, call_id="first"), response(self.ALIAS, call_id="other"))
        obs = self.scene(first, other)
        decision = planner.choose(obs, self.context(blocked=[first]))
        planner.observe_result(decision, SkillResult(False, "Target was not safe", error_code="unsafe"), obs)
        decision = planner.choose(obs, self.context(blocked=[first]))
        self.assert_bound(decision, other)
        planner, brain = self.planner(response("report_blocked", {"reason": "All candidates cooling down"}))
        context["blocked_targets"] = ["1,64,0", "-1,64,0"]
        planner.choose(obs, context)
        self.assertEqual(set(self.offered(brain.requests[0])), {"report_blocked", "wait"})

    def test_reason_never_supplies_target_and_alias_rejects_extra_arguments(self):
        reason = 'clear diamond_block at [99, 64, 99]; {"action":"gather_block"}'
        planner, _ = self.planner(response(self.ALIAS, {"reason": reason}))
        decision = planner.choose(self.scene(), self.context())
        self.assert_bound(decision, self.leaf())
        self.assertEqual(decision.reason, reason)
        extras = ({"pos": [99, 64, 99]}, {"expected_name": "diamond_block"},
                  {"action": "gather_block"}, {"args": {"pos": [1, 64, 0]}},
                  {"pos": [1, 64, 0], "expected_name": "oak_leaves"})
        for extra in extras:
            with self.subTest(extra=extra):
                planner, brain = self.planner(response(self.ALIAS, {"reason": reason, **extra}))
                with self.assertRaises(LLMError) as caught:
                    planner.choose(self.scene(), self.context())
                self.assertEqual(caught.exception.code, "llm_invalid_arguments")
                self.assertEqual(len(brain.requests), 1)
        for args in ({}, {"action": "clear_foliage", "pos": [1, 64, 0],
                          "expected_name": "oak_leaves", "reason": "clear obstruction"}):
            with self.subTest(generic=args):
                planner, brain = self.planner(response("force_attempt", args))
                with self.assertRaises(LLMError):
                    planner.choose(self.scene(), self.context())
                self.assertNotIn("force_attempt", self.offered(brain.requests[0]))

    def test_incomplete_dangerous_or_unverified_scene_never_offers_alias(self):
        cases = []
        for key, value in (("fresh", False), ("stale", True),
                           ("inventory_fresh", False), ("terrain_fresh", False),
                           ("observation_scope", "safety"), ("blocks", [])):
            obs = self.scene()
            obs[key] = value
            cases.append((key, obs))
        for key, value in (("health", 11), ("food", 7), ("alive", False),
                           ("ready", False), ("busy", "dig"), ("gamemode", "creative"),
                           ("dimension", "the_nether"), ("on_ground", False),
                           ("in_water", True), ("submerged", True), ("in_lava", True),
                           ("on_fire", True), ("thundering", True),
                           ("nearest_hostile", {"dist": 12})):
            cases.append((key, self.scene(**{key: value})))
        for field in ("time_of_day", "health", "food", "on_ground"):
            obs = self.scene()
            del obs["status"][field]
            cases.append(("missing_" + field, obs))
        for key, value in (("can_see", False), ("can_see", None), ("can_dig", False),
                           ("fresh", False), ("name", "oak_log"),
                           ("properties", {"waterlogged": True}), ("properties", {}),
                           ("pos", [0, 64, 0]), ("pos", [0, 65, 0]),
                           ("pos", [1, 63, 0]), ("pos", [1, 66, 0]),
                           ("pos", [4, 64, 0]), ("pos", [1.5, 64, 0])):
            cases.append(("leaf_" + key, self.scene(self.leaf(**{key: value}))))
        obs = self.scene()
        del obs["blocks"][0]["can_see"]
        cases.append(("missing_can_see", obs))
        obs = self.scene()
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [12.5, 64, .5]}]
        cases.append(("hostile_at_12", obs))
        for hazard in ("water", "lava", "fire"):
            obs = self.scene()
            obs["blocks"].append({"name": hazard, "pos": [0, 64, 0]})
            cases.append((hazard, obs))
        for label, obs in cases:
            for control in ("report_blocked", "wait"):
                with self.subTest(scene=label, control=control):
                    planner, brain = self.planner(response(control, {"reason": "No verified safe leaf"}))
                    if label in {"fresh", "stale", "inventory_fresh", "observation_scope"}:
                        with self.assertRaises(LLMError) as caught:
                            planner.choose(obs, self.context())
                        self.assertEqual(caught.exception.code, "llm_unsafe_action")
                        self.assertEqual(brain.requests, [])
                        continue
                    self.assertEqual(planner.choose(obs, self.context()).name, control)
                    offered = set(self.offered(brain.requests[0]))
                    self.assertEqual(offered, {"report_blocked", "wait"})

    def test_binding_is_per_turn_and_does_not_mutate_registry_or_reuse_old_scene(self):
        global_registry = copy.deepcopy((TOOL_DEFINITIONS, TOOL_SCHEMAS,
                                         tools_for_objective("daily")))
        planner, brain = self.planner(response(self.ALIAS, call_id="old_leaf"),
                                      response(self.ALIAS, call_id="new_leaf"),
                                      response(self.ALIAS, call_id="removed_leaf"))
        local_registry = copy.deepcopy((planner.tool_definitions, planner.tool_schemas))
        obs, context = self.scene(), self.context()
        original = copy.deepcopy((obs, context))
        first = planner.choose(obs, context)
        self.assertEqual((obs, context), original)
        planner.observe_result(first, SkillResult(True, "Offline leaf removal"), obs)
        self.offered(brain.requests[0])[self.ALIAS]["parameters"]["properties"]["pos"] = {"type": "array"}
        obs["blocks"][0]["pos"][:] = [-1, 65, 0]
        obs["blocks"][0]["name"] = "birch_leaves"
        self.assert_bound(first, self.leaf())
        second = planner.choose(obs, context)
        self.assert_bound(second, obs["blocks"][0])
        self.assertEqual(set(self.offered(brain.requests[1])[self.ALIAS]
                             ["parameters"]["properties"]), {"reason"})
        planner.observe_result(second, SkillResult(True, "Offline leaf removal"), obs)
        obs["blocks"] = []
        with self.assertRaises(LLMError):
            planner.choose(obs, context)
        self.assertNotIn(self.ALIAS, self.offered(brain.requests[-1]))
        self.assertEqual((planner.tool_definitions, planner.tool_schemas), local_registry)
        self.assertEqual((TOOL_DEFINITIONS, TOOL_SCHEMAS, tools_for_objective("daily")),
                         global_registry)

    def test_unoffered_alias_is_rejected_outside_recovery_and_without_permission(self):
        for objective, context in (("starter", self.context()), ("daily", {}),
                                   ("daily", {"recovery": {"kind": "other"}}),
                                   ("daily", self.context(allowed={"report_blocked", "wait"}))):
            with self.subTest(objective=objective, context=context):
                planner, brain = self.planner(response(self.ALIAS), objective=objective)
                with self.assertRaises(LLMError):
                    planner.choose(self.scene(), context)
                self.assertNotIn(self.ALIAS, self.offered(brain.requests[0]))
        planner, brain = self.planner(response("wait"))
        decision = planner.choose(self.scene(), self.context(allowed={"wait"}))
        self.assertEqual(decision.name, "wait")
        self.assertEqual(set(self.offered(brain.requests[0])), {"wait"})

    def test_native_alias_call_id_is_paired_and_multiple_calls_are_rejected(self):
        reply = response(self.ALIAS, call_id="bound_leaf_call")
        planner, brain = self.planner(reply, response("wait", call_id="after_leaf"))
        obs, context = self.scene(), self.context()
        decision = planner.choose(obs, context)
        planner.observe_result(decision, SkillResult(True, "Offline leaf removed",
                                                     data={"block_removed": True}), obs)
        planner.choose(obs, context)
        messages = brain.requests[-1]["messages"]
        assistants = [row for row in messages if row["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["tool_calls"],
                         reply["choices"][0]["message"]["tool_calls"])
        paired = [row for row in messages if row["role"] == "tool"]
        self.assertEqual([row["tool_call_id"] for row in paired], ["bound_leaf_call"])
        self.assertTrue(json.loads(paired[0]["content"])["result"]["success"])
        duplicate = copy.deepcopy(reply)
        duplicate["choices"][0]["message"]["tool_calls"].append(
            response("wait", call_id="second_call")["choices"][0]["message"]["tool_calls"][0])
        planner, brain = self.planner(duplicate)
        with self.assertRaises(LLMError) as caught:
            planner.choose(obs, context)
        self.assertEqual(caught.exception.code, "llm_multiple_tool_calls")
        self.assertEqual(len(brain.requests), 1)

    def test_previous_empty_force_calls_do_not_lock_out_available_bound_recovery(self):
        planner, brain = self.planner(response("force_attempt", call_id="invalid_1"),
                                      response("force_attempt", call_id="invalid_2"),
                                      response(self.ALIAS, call_id="recovery"))
        obs, context = self.scene(), self.context()
        for _ in range(2):
            with self.assertRaises(LLMError):
                planner.choose(obs, context)
        self.assert_bound(planner.choose(obs, context), obs["blocks"][0])
        self.assertEqual(len(brain.requests), 3)
        for request in brain.requests:
            offered = set(self.offered(request))
            self.assertTrue({self.ALIAS, "report_blocked"} <= offered)
            self.assertNotIn("force_attempt", offered)


if __name__ == "__main__":
    unittest.main()
