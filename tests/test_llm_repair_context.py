"""Bound failed-goal repair requests without provider calls or lost feedback."""
import json
import unittest
from dataclasses import asdict

from agent.brain import LLMError
from agent.llm_planner import LLMSurvivalPlanner
from agent.tools import SURVIVAL_TASK_NAMES
from skills import SkillResult
from test_llm_planner import Brain, observation, response


class RepairContextTests(unittest.TestCase):
    def planner(self, *responses, objective="daily"):
        brain = Brain(*responses)
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0},
                                    {"objective": objective}, brain=brain)
        return planner, brain

    @staticmethod
    def context(task="gather", **args):
        return {"task_execution": {"task_id": "failed-goal", "task": task,
                "status": "needs_ai", "args": {"task": task, **args}}}

    def test_gather_repair_catalog_is_goal_scoped_and_observation_bound(self):
        planner, brain = self.planner(response("wait"))
        planner.choose(observation({"wooden_pickaxe": 1}), self.context(resource="cobblestone"))
        names = {tool["function"]["name"] for tool in brain.requests[0]["tools"]}
        self.assertTrue({"run_survival_task", "wait", "eat_food", "auto_sleep", "retreat_from",
                         "fight_hostile", "move_to", "position_for_task", "gather_block", "craft_item"} <= names)
        self.assertFalse({"clear_foliage", "hunt_animal", "smelt_start", "place_bed", "hunt_with_lava"} & names)
        self.assertLessEqual(len(names), 12)
        self.assertEqual(len(planner.tool_definitions), 35)

    def test_task_catalogue_stays_bounded_and_raw_iron_keeps_its_real_skill(self):
        planner, _ = self.planner(response("wait"))
        obs = observation()
        obs["blocks"] += [{"name": "iron_ore", "pos": [6, 64, 0]},
                          {"name": "oak_leaves", "pos": [1, 64, 0]}]
        for task in SURVIVAL_TASK_NAMES:
            with self.subTest(task=task):
                tools, routing, task_id = planner._request_catalog(self.context(task), obs)
                self.assertLessEqual(len(tools), 16)
                self.assertEqual(routing["atomic_repair_remaining"], 2)
                self.assertEqual(task_id, "failed-goal")
        tools, _, _ = planner._request_catalog(self.context(resource="raw_iron"), obs)
        self.assertIn("gather_resource", {tool["function"]["name"] for tool in tools})
        self.assertIn("clear_foliage", {tool["function"]["name"] for tool in tools})

    def test_unrelated_tool_cannot_escape_via_format_repair_or_spend_action_credit(self):
        planner, brain = self.planner(response("place_bed", {"pos": [2, 64, 0], "item": "white_bed"}, "hidden"),
                                     response("wait", call_id="fixed"))
        planner.max_repair_attempts = 1
        result = planner.choose(observation({"white_bed": 1}), self.context(resource="cobblestone"))
        self.assertEqual(result.name, "wait")
        self.assertEqual(brain.requests[0]["tools"], brain.requests[1]["tools"])
        feedback = next(row for row in brain.requests[1]["messages"] if row["role"] == "tool")
        self.assertEqual(feedback["tool_call_id"], "hidden")
        self.assertEqual(json.loads(feedback["content"])["error_code"], "llm_invalid_arguments")
        self.assertEqual(planner._task_repair_uses, {})

    def test_large_failed_goal_keeps_latest_native_pair_and_safety_within_48000_bytes(self):
        planner, brain = self.planner(response("run_survival_task", {
            "task": "gather", "resource": "cobblestone", "count": 8}, "stone-goal"),
            response("move_to", {"pos": [8.5, 64, .5]}, "repair"))
        obs = observation({"wooden_pickaxe": 1, "bread": 2})
        context = self.context(resource="cobblestone", count=8)
        task = context["task_execution"]
        task.update(steps=8, reason="no_safe_local_action_or_unavailable_prerequisite",
                    recent_results=[{"name": "position_for_task", "success": True,
                                     "target": [i, 64, 2]} for i in range(8)])
        decision = planner.choose(obs, {})
        terminal = SkillResult(False, "Eight observed positions exhausted", {
            "task_execution": task, "completed": False, "executed": True}, "task_needs_ai")
        planner.observe_result(decision, terminal, obs)
        context.update(last_result=asdict(terminal), remaining_steps=100, remaining_seconds=350,
                       survival_constraints={"scenario": "normal", "model_allowed": True,
                                             "threat_radius": 16, "unknown_writes": False},
                       memory={"places": [{"name": "工作站候选" * 30, "pos": [i, 64, 4],
                                           "fresh": False} for i in range(12)]})
        repair = planner.choose(obs, context)
        self.assertEqual(repair.name, "move_to")
        request = brain.requests[-1]
        self.assertEqual(planner.MAX_REQUEST_BYTES, 48000)
        self.assertLessEqual(planner._request_bytes(request["messages"], request["tools"]), 48000)
        self.assertGreater(planner._request_bytes(request["messages"], planner.tool_definitions), 48000)
        assistant = next(row for row in request["messages"] if row.get("tool_calls"))
        feedback = next(row for row in request["messages"] if row["role"] == "tool")
        self.assertEqual(assistant["tool_calls"][0]["id"], "stone-goal")
        self.assertEqual(feedback["tool_call_id"], "stone-goal")
        self.assertEqual(json.loads(feedback["content"])["result"], planner._compact_value(asdict(terminal)))
        current = json.loads(request["messages"][-1]["content"])
        self.assertEqual(current["observation"]["counts"], obs["counts"])
        self.assertEqual(current["observation"]["status"]["health"], 20)
        self.assertEqual(current["context"]["survival_constraints"], context["survival_constraints"])
        self.assertEqual(current["context"]["task_execution"]["reason"], task["reason"])
        self.assertEqual(planner._task_repair_uses, {"failed-goal": 1})

    def test_food_restriction_takes_precedence_over_failed_production_goal(self):
        planner, brain = self.planner(response("wait"))
        context = self.context(resource="cobblestone")
        context["survival_constraints"] = {"scenario": "low_health", "model_allowed": True}
        planner.choose(observation(health=6), context)
        names = {tool["function"]["name"] for tool in brain.requests[0]["tools"]}
        self.assertTrue({"harvest_crop", "inspect_container", "take_items", "smelt_collect"} <= names)
        self.assertFalse({"gather_block", "move_to", "clear_foliage", "hunt_animal"} & names)
        self.assertEqual(planner._task_repair_uses, {})


if __name__ == "__main__":
    unittest.main()
