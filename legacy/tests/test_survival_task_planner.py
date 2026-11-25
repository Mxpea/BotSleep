"""Offline high-level task protocol, safety and exact-feedback budget tests."""

import copy
from dataclasses import asdict
import json
import unittest

from agent.brain import LLMError
from agent.llm_planner import LLMSurvivalPlanner
from agent.tools import SURVIVAL_TASK_NAMES, tools_for_objective
from core.competition_tasks import CompetitionTasks
from skills import SkillResult
from test_competition_context import EVIDENCE_FIELDS, observed_workstations
from test_llm_planner import Brain, observation, response


class SurvivalTaskPlannerTests(unittest.TestCase):
    def planner(self, *responses, objective="daily", enabled=True, operator_goal=""):
        brain = Brain(*responses)
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0},
            {"objective": objective, "autonomy": {"enabled": enabled}, "operator_goal": operator_goal}, brain=brain)
        return planner, brain

    def test_catalog_is_additive_and_disable_keeps_atomic_tools(self):
        for objective, size in (("starter", 9), ("daily", 35), ("competition", 40)):
            tools = tools_for_objective(objective)
            names = {row["function"]["name"] for row in tools}
            self.assertEqual(len(tools), size)
            self.assertEqual("run_survival_task" in names, objective != "starter")
            planner, _ = self.planner(objective=objective, enabled=False)
            self.assertEqual(set(planner.tool_schemas), names)
            visible, routing, _ = planner._request_catalog({})
            enabled_names = {tool["function"]["name"] for tool in visible}
            self.assertNotIn("run_survival_task", enabled_names)
            self.assertTrue({"gather_block", "craft_item", "move_to", "wait"} <= enabled_names)
            self.assertEqual(enabled_names, names - {"run_survival_task", "retreat_from", "fight_hostile"})

    def test_normal_autonomy_requests_expose_goals_safety_and_competition_metadata_only(self):
        normal={"run_survival_task","wait","eat_food","auto_sleep"}
        metadata={"select_task","update_progress","finish_task","force_exit_task","end_competition"}
        for objective,size in (("daily",35),("competition",40)):
            with self.subTest(objective=objective):
                planner,brain=self.planner(response("wait"),objective=objective)
                planner.choose(observation(health=8.3),{})
                names={tool["function"]["name"] for tool in brain.requests[0]["tools"]}
                self.assertEqual(names,normal | (metadata if objective=="competition" else set()))
                self.assertEqual(len(planner.tool_definitions),size)
                self.assertTrue({"gather_block","craft_item","move_to","clear_foliage"}<=set(planner.tool_schemas))
                self.assertLess(planner._request_bytes(brain.requests[0]["messages"],brain.requests[0]["tools"]),
                                planner._request_bytes(brain.requests[0]["messages"]))

    def test_ready_competition_combat_goal_can_request_a_bounded_attack(self):
        planner, _ = self.planner(objective="competition")
        view = {"competition": {"active_goal": {"availability": "ready", "required_skills": ["fight_hostile"]}}}
        tools, _, _ = planner._request_catalog(view)
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("fight_hostile", names)
        self.assertNotIn("retreat_from", names)

    def test_hidden_atomic_is_rejected_and_bounded_repair_uses_same_permissions(self):
        planner,brain=self.planner(response("gather_block",{"pos":[3,64,0],"expected_name":"oak_log"},"hidden"),
            response("run_survival_task",{"task":"gather","resource":"logs"},"goal"))
        planner.max_repair_attempts=1
        decision=planner.choose(observation(),{})
        self.assertEqual(decision.name,"run_survival_task")
        self.assertEqual(len(brain.requests),2)
        self.assertEqual(brain.requests[0]["tools"],brain.requests[1]["tools"])
        self.assertNotIn("gather_block",{t["function"]["name"] for t in brain.requests[1]["tools"]})
        rejected=next(m for m in brain.requests[1]["messages"] if m["role"]=="tool")
        self.assertEqual(rejected["tool_call_id"],"hidden")
        self.assertEqual(json.loads(rejected["content"])["error_code"],"llm_invalid_arguments")
        self.assertFalse(json.loads(rejected["content"])["executed"])
        self.assertEqual(planner._task_repair_uses,{})

    def test_repeated_hidden_atomic_cannot_escape_through_repair(self):
        planner,brain=self.planner(response("move_to",{"pos":[4,64,0]},"one"),
                                    response("move_to",{"pos":[5,64,0]},"two"))
        planner.max_repair_attempts=1
        with self.assertRaises(LLMError) as caught: planner.choose(observation(),{})
        self.assertEqual(caught.exception.code,"llm_invalid_arguments")
        self.assertEqual(len(brain.requests),2)
        self.assertIsNone(planner._pending)
        self.assertEqual([m["tool_call_id"] for m in planner.history if m["role"]=="tool"],["one","two"])

    def test_needs_ai_allows_only_two_atomic_repairs_per_task_and_keeps_old_pairs(self):
        planner,brain=self.planner(response("run_survival_task",{"task":"gather","resource":"logs"},"goal"),
            response("gather_block",{"pos":[3,64,0],"expected_name":"oak_log"},"repair1"),
            response("craft_item",{"item":"oak_planks","count":4},"repair2"),
            response("gather_block",{"pos":[3,64,0],"expected_name":"oak_log"},"hidden3"),
            response("gather_block",{"pos":[3,64,0],"expected_name":"oak_log"},"new_goal_repair"))
        obs=observation()
        task=planner.choose(obs,{})
        terminal=SkillResult(False,"Local path exhausted",{"task_execution":{"task_id":"goal-A","status":"needs_ai"}},"task_needs_ai")
        planner.observe_result(task,terminal,obs)
        context={"task_execution":{"task_id":"goal-A","status":"needs_ai"},"last_result":asdict(terminal)}
        first=planner.choose(obs,context)
        self.assertEqual(first.name,"gather_block")
        planner.observe_result(first,SkillResult(True,"One log verified",{"gained":{"oak_log":1}}),observation({"oak_log":1}))
        second=planner.choose(observation({"oak_log":1}),context)
        self.assertEqual(second.name,"craft_item")
        planner.observe_result(second,SkillResult(True,"Planks verified",{"gained":4}),observation({"oak_planks":4}))
        with self.assertRaises(LLMError) as caught: planner.choose(observation({"oak_planks":4}),context)
        self.assertEqual(caught.exception.code,"llm_invalid_arguments")
        self.assertNotIn("gather_block",{t["function"]["name"] for t in brain.requests[3]["tools"]})
        historical=[m for m in brain.requests[3]["messages"] if m["role"]=="tool"]
        self.assertEqual([m["tool_call_id"] for m in historical],["goal","repair1","repair2"])
        self.assertEqual(json.loads(historical[0]["content"])["result"],asdict(terminal))
        self.assertEqual(planner._task_repair_uses["goal-A"],2)
        context["task_execution"]["task_id"]="goal-B"
        self.assertEqual(planner.choose(observation({"oak_planks":4}),context).name,"gather_block")
        self.assertEqual(planner._task_repair_uses["goal-B"],1)

    def test_parse_repair_and_wait_do_not_spend_atomic_recovery_allowance(self):
        planner,brain=self.planner(response("teleport",{},"invalid"),response("wait",call_id="safe_wait"),
            response("gather_block",{"pos":[3,64,0],"expected_name":"oak_log"},"first_action"))
        planner.max_repair_attempts=1
        context={"task_execution":{"task_id":"recover-A","status":"needs_ai"}}
        decision=planner.choose(observation(),context)
        self.assertEqual(decision.name,"wait")
        self.assertEqual(planner._task_repair_uses,{})
        planner.observe_result(decision,SkillResult(True,"Waited"),observation())
        self.assertEqual(planner.choose(observation(),context).name,"gather_block")
        self.assertEqual(planner._task_repair_uses,{"recover-A":1})
        self.assertEqual(len(brain.requests),3)

    def test_unknown_cancelled_or_unidentified_task_cannot_unlock_atomic_tools(self):
        contexts=[{"task_execution":{"status":"needs_ai"}},
                  {"task_execution":{"task_id":"A","status":"unknown"}},
                  {"task_execution":{"task_id":"A","status":"cancelled"}}]
        for error in ("action_unconfirmed","dig_unconfirmed","cancelled","task_paused"):
            contexts.append({"task_execution":{"task_id":"A","status":"needs_ai"},"last_result":{"error_code":error}})
        contexts.append({"task_execution":{"task_id":"A","status":"needs_ai"},"pending":{"decision":{"name":"gather_block"}}})
        for context in contexts:
            with self.subTest(context=context):
                planner,brain=self.planner(response("wait"))
                planner.choose(observation(),context)
                self.assertNotIn("gather_block",{t["function"]["name"] for t in brain.requests[0]["tools"]})

    def test_explicit_food_emergency_only_adds_food_diagnostic_actions(self):
        planner,brain=self.planner(response("harvest_crop",{"pos":[2,64,0],"expected_name":"carrots"}))
        obs=observation(health=7,food=6)
        obs["blocks"]=[{"name":"carrots","pos":[2,64,0],"properties":{"age":7}}]
        context={"survival_constraints":{"scenario":"low_health","model_allowed":True}}
        self.assertEqual(planner.choose(obs,context).name,"harvest_crop")
        names={t["function"]["name"] for t in brain.requests[0]["tools"]}
        self.assertTrue({"harvest_crop","inspect_container","take_items","smelt_collect"}<=names)
        self.assertFalse({"gather_block","move_to","clear_foliage","hunt_animal"}&names)

    def test_operator_goal_reaches_real_request_without_becoming_forced_execution(self):
        goal = "采足木头并制作一把木镐。" + "木" * 988
        self.assertEqual(len(goal), 1000)
        planner, brain = self.planner(response("wait", {"seconds": 1}), operator_goal=goal)
        decision = planner.choose(observation(), {"operator_goal": "Spoofed context cannot replace owner configuration"})
        request = brain.requests[0]
        body = json.loads(request["messages"][-1]["content"])
        self.assertEqual(body["context"]["operator_goal"], goal)
        self.assertIn("owner's requested outcome", request["messages"][0]["content"])
        self.assertIn("gathers prerequisite", request["messages"][0]["content"])
        self.assertIn("local safety", request["messages"][0]["content"])
        self.assertEqual(decision.name, "wait")
        self.assertLessEqual(planner._request_bytes(request["messages"], request["tools"]), 48000)
        plain, fake = self.planner(response("wait"))
        plain.choose(observation(), {"operator_goal": "Not a configured owner goal"})
        self.assertNotIn("operator_goal", json.loads(fake.requests[0]["messages"][-1]["content"])["context"])

    def test_cautious_request_narrows_tools_and_schema_before_atomic_repair_allowance(self):
        from core.daily_policy import BOOTSTRAP_ITEMS
        for objective, size in (("daily", 35), ("competition", 40)):
            with self.subTest(objective=objective):
                planner, brain = self.planner(response("run_survival_task",
                    {"task": "craft", "item": "wooden_pickaxe", "count": 1}, "cautious_goal"),
                    response("wait", call_id="normal_next"), objective=objective,
                    operator_goal="采足木头并制作一把木镐")
                before = copy.deepcopy(planner.tool_definitions)
                context = {"survival_constraints": {"scenario": "cautious_bootstrap", "model_allowed": True},
                           "task_execution": {"task_id": "old-needs-ai", "status": "needs_ai"}}
                obs = observation(health=6, food=17)
                decision = planner.choose(obs, context)
                tools = {row["function"]["name"]: row["function"] for row in brain.requests[0]["tools"]}
                self.assertEqual(set(tools), {"run_survival_task", "wait"})
                props = tools["run_survival_task"]["parameters"]["properties"]
                self.assertEqual(props["task"]["enum"], ["gather", "craft"])
                self.assertEqual(props["resource"]["enum"], ["logs"])
                self.assertEqual(set(props["item"]["enum"]), set(BOOTSTRAP_ITEMS))
                self.assertEqual(props["count"]["maximum"], 8)
                self.assertEqual(len(planner.tool_definitions), size)
                self.assertEqual(planner.tool_definitions, before)
                self.assertEqual(planner.tool_schemas["run_survival_task"]["properties"]["count"]["maximum"], 64)
                self.assertEqual(planner._task_repair_uses, {})
                planner.observe_result(decision, SkillResult(True, "Tool verified", {"gained": 1}), observation({"wooden_pickaxe": 1}))
                planner.choose(observation({"wooden_pickaxe": 1}), {})
                self.assertEqual(len(brain.requests[1]["tools"]), 4 if objective == "daily" else 9)
                self.assertEqual([m["tool_call_id"] for m in brain.requests[1]["messages"] if m["role"] == "tool"], ["cautious_goal"])

    def test_cautious_schema_rejection_and_repair_use_same_request_permissions(self):
        for invalid in ({"task": "starter_tools"}, {"task": "gather", "resource": "coal"},
                        {"task": "gather", "resource": "logs", "count": 9},
                        {"task": "craft", "item": "stone_pickaxe", "count": 1}):
            with self.subTest(args=invalid):
                planner, brain = self.planner(response("run_survival_task", invalid, "rejected"),
                    response("run_survival_task", {"task": "craft", "item": "wooden_pickaxe", "count": 1}, "fixed"))
                planner.max_repair_attempts = 1
                context = {"survival_constraints": {"scenario": "cautious_bootstrap", "model_allowed": True}}
                decision = planner.choose(observation(health=6, food=17), context)
                self.assertEqual(decision.args["item"], "wooden_pickaxe")
                self.assertEqual(len(brain.requests), 2)
                self.assertEqual(brain.requests[0]["tools"], brain.requests[1]["tools"])
                feedback = [m for m in brain.requests[1]["messages"] if m["role"] == "tool"]
                self.assertEqual([m["tool_call_id"] for m in feedback], ["rejected"])
                self.assertEqual(json.loads(feedback[0]["content"])["error_code"], "llm_invalid_arguments")

    def test_cautious_hidden_atomic_is_rejected_and_disabled_autonomy_only_waits(self):
        context = {"survival_constraints": {"scenario": "cautious_bootstrap", "model_allowed": True},
                   "task_execution": {"task_id": "old-task", "status": "needs_ai"}}
        planner, brain = self.planner(response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}))
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(health=6, food=17), context)
        self.assertEqual(caught.exception.code, "llm_invalid_arguments")
        self.assertIsNone(planner._pending)
        self.assertEqual(planner._task_repair_uses, {})
        disabled, fake = self.planner(response("wait"), enabled=False)
        self.assertEqual(disabled.choose(observation(health=6, food=17), context).name, "wait")
        self.assertEqual([t["function"]["name"] for t in fake.requests[0]["tools"]], ["wait"])

    def test_cautious_parser_uses_frozen_schema_even_if_context_changes_during_request(self):
        context = {"survival_constraints": {"scenario": "cautious_bootstrap", "model_allowed": True}}
        planner, brain = self.planner(response("run_survival_task", {"task": "gather", "resource": "logs", "count": 9}))
        actual = brain.chat_completion
        def response_after_change(*args, **kwargs):
            result = actual(*args, **kwargs)
            context["survival_constraints"]["scenario"] = "normal"
            return result
        brain.chat_completion = response_after_change
        with self.assertRaises(LLMError) as caught:
            planner.choose(observation(health=6, food=17), context)
        self.assertEqual(caught.exception.code, "llm_invalid_arguments")
        self.assertIsNone(planner._pending)

    def test_competition_coordinate_prose_dedup_keeps_executable_schema(self):
        daily = {row["function"]["name"]: row["function"]["parameters"] for row in tools_for_objective("daily")}
        competition = {row["function"]["name"]: row["function"]["parameters"] for row in tools_for_objective("competition")}
        def executable(schema):
            if isinstance(schema, dict):
                return {key: executable(value) for key, value in schema.items() if key != "description"}
            if isinstance(schema, list): return [executable(value) for value in schema]
            return schema
        for name, schema in daily.items():
            with self.subTest(tool=name):
                self.assertEqual(executable(schema), executable(competition[name]))

    def test_goal_does_not_need_current_resources_and_defaults_are_applied(self):
        for task in SURVIVAL_TASK_NAMES:
            args = {"task": task}
            if task == "gather": args["resource"] = "logs"
            if task in {"craft", "smelt"}: args["item"] = "iron_pickaxe" if task == "craft" else "iron_ingot"
            with self.subTest(task=task):
                planner, _ = self.planner(response("run_survival_task", {**args, "reason": "A bounded goal"}))
                obs = observation(); obs.update(blocks=[], entities=[], counts={}, inventory=[])
                decision = planner.choose(obs, {})
                self.assertEqual(decision.args, {**args, "count": 8, "radius": 32})
                self.assertEqual(decision.goal, task)
                self.assertEqual(decision.reason, "A bounded goal")

    def test_unsupported_recipe_is_left_to_executor_without_inventing_support(self):
        planner, _ = self.planner(response("run_survival_task", {"task": "craft", "item": "diamond_pickaxe"}))
        self.assertEqual(planner.choose(observation(), {}).args["item"], "diamond_pickaxe")

    def test_invalid_task_combinations_coordinates_and_queues_are_rejected(self):
        invalid = [
            {}, {"task": "dragon"}, {"task": "gather"}, {"task": "craft"}, {"task": "smelt"},
            {"task": "gather", "resource": "logs", "item": "oak_log"},
            {"task": "craft", "item": "stick", "resource": "logs"},
            {"task": "smelt", "item": "iron_ingot", "resource": "raw_iron"},
            {"task": "food", "item": "bread"}, {"task": "maintenance", "resource": "coal"},
            {"task": "gather", "resource": "obsidian"}, {"task": "craft", "item": "../secret"},
            {"task": "farm", "pos": [1, 64, 1]}, {"task": "bed", "actions": [{"name": "teleport"}]},
            {"task": "food", "count": True}, {"task": "food", "count": 0}, {"task": "food", "count": 65},
            {"task": "food", "radius": 3}, {"task": "food", "radius": 65}, {"task": "food", "radius": 4.5}]
        for args in invalid:
            with self.subTest(args=args):
                planner, _ = self.planner(response("run_survival_task", args))
                with self.assertRaises(LLMError) as caught:
                    planner.choose(observation(), {})
                self.assertEqual(caught.exception.code, "llm_invalid_arguments")
                self.assertIsNone(planner._pending)

    def test_fresh_full_observation_is_required_to_start_task(self):
        for changes in ({"fresh": False}, {"fresh": None}, {"stale": True},
                        {"observation_scope": "safety"}, {"inventory_fresh": False}, {"terrain_fresh": False}):
            with self.subTest(changes=changes):
                planner, _ = self.planner(response("run_survival_task", {"task": "food"}))
                obs = observation(); obs.update(changes)
                with self.assertRaises(LLMError) as caught: planner.choose(obs, {})
                self.assertEqual(caught.exception.code, "llm_unsafe_action")
        for status in ({"alive": False}, {"ready": False}, {"busy": "dig"}):
            planner, _ = self.planner(response("run_survival_task", {"task": "food"}))
            with self.subTest(status=status), self.assertRaises(LLMError):
                planner.choose(observation(**status), {})

    def test_active_pending_and_unknown_cannot_be_overwritten(self):
        contexts = [{"pending": {"decision": {"name": "gather_block"}}}, {"action_state": "unknown"},
                    {"last_result": {"error_code": "action_unconfirmed"}},
                    {"last_result": {"data": {"action_state": "unknown"}}}]
        contexts += [{"task_execution": {"status": status}} for status in ("running", "suspended", "unknown")]
        for context in contexts:
            with self.subTest(context=context):
                planner, _ = self.planner(response("run_survival_task", {"task": "maintenance"}))
                before = copy.deepcopy(context)
                with self.assertRaises(LLMError) as caught: planner.choose(observation(), context)
                self.assertEqual(caught.exception.code, "llm_unsafe_action")
                self.assertEqual(context, before)

    def test_terminal_summary_may_select_a_different_task(self):
        for status in ("idle", "completed", "needs_ai", "cancelled"):
            with self.subTest(status=status):
                planner, brain = self.planner(response("run_survival_task", {"task": "food"}))
                context = {"task_execution": {"status": status, "progress": {"current": 3, "target": 8, "unit": "items"}}}
                self.assertEqual(planner.choose(observation(), context).name, "run_survival_task")
                body = json.loads(brain.requests[-1]["messages"][-1]["content"])
                self.assertEqual(body["context"]["task_execution"], context["task_execution"])

    def test_long_task_keeps_one_pending_call_until_actual_final_feedback(self):
        planner, brain = self.planner(response("run_survival_task", {"task": "gather", "resource": "logs"}),
                                      response("wait", call_id="after_task"))
        decision = planner.choose(observation(), {})
        self.assertEqual(planner.history, [])
        for _ in range(3):
            with self.assertRaises(LLMError) as caught:
                planner.choose(observation(), {"task_execution": {"status": "running"}})
            self.assertEqual(caught.exception.code, "llm_missing_feedback")
        self.assertEqual(len(brain.requests), 1)
        result = SkillResult(False, "Task needs a different approach", {
            "status": "needs_ai", "executed": True, "steps": 4, "gained": 2}, "task_no_progress")
        planner.observe_result(decision, result, observation({"oak_log": 2}))
        planner.choose(observation({"oak_log": 2}), {"task_execution": {"status": "needs_ai"}})
        tools = [row for row in brain.requests[-1]["messages"] if row["role"] == "tool"]
        self.assertEqual(len(brain.requests), 2)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(tools[0]["content"])["result"], asdict(result))

    def test_count_or_radius_change_does_not_bypass_same_failed_goal(self):
        planner, brain = self.planner(response("run_survival_task", {"task": "gather", "resource": "logs"}),
            response("run_survival_task", {"task": "gather", "resource": "logs", "count": 4, "radius": 16}))
        obs = observation()
        decision = planner.choose(obs, {})
        planner.observe_result(decision, SkillResult(False, "No safe progress", {"status": "needs_ai"}, "task_no_progress"), obs)
        with self.assertRaises(LLMError) as caught: planner.choose(obs, {})
        self.assertEqual(caught.exception.code, "llm_repeated_failure")

    def test_disabled_task_is_rejected_but_existing_atomic_action_still_works(self):
        planner, _ = self.planner(response("run_survival_task", {"task": "food"}), enabled=False)
        with self.assertRaises(LLMError) as caught: planner.choose(observation(), {})
        self.assertEqual(caught.exception.code, "llm_invalid_arguments")
        planner, _ = self.planner(response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}), enabled=False)
        self.assertEqual(planner.choose(observation(), {}).name, "gather_block")

    def test_task_context_is_bounded_and_redacted_without_mutation(self):
        planner, brain = self.planner(response("wait"))
        context = {"task_execution": {"status": "needs_ai", "progress": {"current": 1, "target": 8, "unit": "items"},
                   "api_key": "private-value", "reason": "large" * 500, "events": list(range(1000))}}
        before = copy.deepcopy(context)
        planner.choose(observation(), context)
        task = json.loads(brain.requests[-1]["messages"][-1]["content"])["context"]["task_execution"]
        self.assertEqual(task["api_key"], "[REDACTED]")
        self.assertLessEqual(len(task["reason"]), 512)
        self.assertLessEqual(len(task["events"]), 64)
        self.assertEqual(task["progress"], context["task_execution"]["progress"])
        self.assertEqual(context, before)

    def test_competition_full_catalogue_current_evidence_and_latest_task_pair_fit_48kb(self):
        planner, brain = self.planner(response("run_survival_task", {"task": "craft", "item": "shield", "count": 1}),
                                      response("wait", call_id="next_goal"), objective="competition",
                                      operator_goal="采足木头并制作一把木镐；当前安全条件不允许时等待。")
        obs = observed_workstations()
        decision = planner.choose(obs, {})
        result = SkillResult(True, "Craft goal verified", {"status": "completed", "task": "craft",
            "item": "shield", "steps": 12, "gained": 1, "executed": True})
        planner.observe_result(decision, result, obs)
        snapshot = CompetitionTasks({}).snapshot()
        columns = ["id", "title", "difficulty", "points", "eligibility", "availability", "completion"]
        context = {"competition": {"phase": snapshot["phase"], "active_goal_id": None,
            "progress_card": snapshot["progress_card"], "score": snapshot["score"],
            "remaining_seconds": snapshot["remaining_seconds"], "advancements": snapshot["advancements"],
            "selectable_goal_ids": [row["id"] for row in snapshot["tasks"] if row["selectable"]],
            "catalog_assessment": {key: snapshot["catalog_assessment"][key]
                                   for key in ("checked", "total", "known_count", "complete")},
            "task_columns": columns, "tasks": [[row.get(key) for key in columns] for row in snapshot["tasks"]]},
            "last_result": asdict(result), "task_execution": {"status": "completed", "task": "craft",
                "progress": {"current": 1, "target": 1, "unit": "shield"}, "steps": 12}}
        planner.choose(obs, context)
        messages = brain.requests[-1]["messages"]
        self.assertLessEqual(planner._request_bytes(messages,brain.requests[-1]["tools"]), 48000)
        current = json.loads(messages[-1]["content"])
        self.assertEqual(current["context"]["competition"]["tasks"], context["competition"]["tasks"])
        self.assertEqual(len(current["context"]["competition"]["tasks"]), 125)
        self.assertEqual(current["context"]["task_execution"], context["task_execution"])
        for key in EVIDENCE_FIELDS: self.assertEqual(current["observation"][key], obs[key])
        feedback = [row for row in messages if row["role"] == "tool"]
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(feedback[0]["content"])["result"], asdict(result))
        self.assertNotIn("last_result", current["context"])

    def test_goal_mode_keeps_chapter_and_scout_report_after_planner_restart(self):
        goal = {"task": "craft", "item": "stone_pickaxe", "count": 1}
        planner, brain = self.planner(response("run_survival_task", goal))
        obs = observation()
        ctx = {
            "mainline": {"fresh": True, "goal": goal},
            "last_scout_report": {"aborted_empty": True, "estimated_reach": 195.0},
            "action_hints": [
                {"name": "place_item", "args": {"item": "furnace", "pos": [1, 64, 1]}, "goal": "furnace"},
                {"name": "eat_food", "args": {"item": "bread"}, "goal": "food"},
            ],
        }
        planner.choose(obs, ctx)
        sent = json.loads(brain.requests[-1]["messages"][-1]["content"])["context"]
        self.assertEqual(sent["mainline"], ctx["mainline"])
        self.assertEqual(sent["last_scout_report"]["estimated_reach"], 195.0)
        self.assertEqual([h["name"] for h in sent["action_hints"]], ["eat_food"])
        tools, _, _ = planner._request_catalog({"last_scout_report": ctx["last_scout_report"]}, obs)
        task = next(t["function"] for t in tools if t["function"]["name"] == "run_survival_task")
        self.assertNotIn("scout", task["parameters"]["properties"]["task"]["enum"])

    def test_scout_removed_from_goal_catalog_after_scout_completes_once(self):
        planner, brain = self.planner(
            response("run_survival_task", {"task": "scout"}),
            response("run_survival_task", {"task": "gather", "resource": "raw_iron", "count": 8}),
        )
        obs = observation()
        d1 = planner.choose(obs, {})
        planner.observe_result(d1, SkillResult(True, "Scout completed", {"scout_report": {"estimated_reach": 195.0}}), obs)
        planner.choose(obs, {"last_scout_report": {"estimated_reach": 195.0}})
        second_tools = {t["function"]["name"]: t for t in brain.requests[-1]["tools"]}
        self.assertNotIn("scout", second_tools["run_survival_task"]["function"]["parameters"]["properties"]["task"]["enum"])

    def test_duplicate_furnace_craft_rejected_when_furnace_already_held(self):
        planner, brain = self.planner(
            response("run_survival_task", {"task": "craft", "item": "furnace", "count": 1}),
            response("run_survival_task", {"task": "gather", "resource": "raw_iron", "count": 8}),
        )
        planner.max_repair_attempts = 1
        decision = planner.choose(observation({"furnace": 1, "stone_pickaxe": 1, "cobblestone": 16}), {})
        self.assertEqual((decision.name, decision.args["resource"]), ("run_survival_task", "raw_iron"))
        self.assertEqual(len(brain.requests), 2)

    def test_successful_clear_foliage_resets_repeated_failure_for_retry(self):
        planner, _ = self.planner(
            response("run_survival_task", {"task": "craft", "item": "stone_pickaxe", "count": 1}),
            response("clear_foliage", {"pos": [1, 65, 0], "expected_name": "oak_leaves"}),
            response("run_survival_task", {"task": "craft", "item": "stone_pickaxe", "count": 1}),
        )
        obs = observation({"cobblestone": 8, "stick": 2})
        obs["blocks"] = [{"name": "oak_leaves", "pos": [1, 65, 0], "can_see": True}]
        d1 = planner.choose(obs, {})
        planner.observe_result(d1, SkillResult(False, "Placement blocked", {"task_execution": {"task_id": "t1", "task": "craft", "status": "needs_ai"}}, "task_needs_ai"), obs)
        d2 = planner.choose(obs, {"task_execution": {"task_id": "t1", "task": "craft", "status": "needs_ai"}})
        self.assertEqual(d2.name, "clear_foliage")
        obs2 = observation({"cobblestone": 8, "stick": 2})
        planner.observe_result(d2, SkillResult(True, "Cleared", {"executed": True}), obs2)
        d3 = planner.choose(obs2, {})
        self.assertEqual((d3.name, d3.args["item"]), ("run_survival_task", "stone_pickaxe"))

    def test_disable_sleep_strips_sleep_and_bed_tools_from_planner_catalog(self):
        planner, _ = self.planner(response("wait", {"seconds": 1}))
        planner.disable_sleep = True
        tools, _, _ = planner._request_catalog({}, observation())
        names = {t["function"]["name"] for t in tools}
        self.assertNotIn("auto_sleep", names)
        task = next(t["function"] for t in tools if t["function"]["name"] == "run_survival_task")
        self.assertNotIn("bed", task["parameters"]["properties"]["task"]["enum"])


if __name__ == "__main__":
    unittest.main()
