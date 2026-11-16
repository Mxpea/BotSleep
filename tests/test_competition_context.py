"""Competition native requests retain current daily evidence, never invent it."""

import copy
from dataclasses import asdict
import json
import unittest

from agent.brain import LLMError
from agent.llm_planner import LLMSurvivalPlanner
from skills import SkillResult
from test_llm_planner import Brain, observation, response


EVIDENCE_FIELDS = ("daily_evidence", "furnaces", "containers", "armor", "shelter")


def observed_workstations():
    obs = observation(time_of_day=1000)
    common = {"fresh": True, "verified": True, "dimension": "overworld"}
    obs.update(
        furnaces=[{**common, "pos": [2, 64, 0], "name": "furnace",
                   "input": {"name": "raw_iron", "count": 1}, "fuel": None, "output": None,
                   "burning": True, "info": {"burning": True}}],
        containers=[{**common, "pos": [2, 64, 1], "name": "chest",
                     "items": [{"name": "bread", "count": 3}]}],
        armor={"head": {"name": "iron_helmet", "count": 1}},
        shelter={**common, "pos": [0, 64, 0], "structure_complete": True,
                 "shelter_verified": False, "inside": False,
                 "door": {"pos": [0, 64, -2], "open": True, "frame_verified": True}},
        daily_evidence={"shelter": {**common, "structure_complete": True,
                        "return_route": {"verified": True, "path": [[0, 64, -1], [0, 64, 0]]}}},
    )
    return obs


class CompetitionContextTests(unittest.TestCase):
    def planner(self, *responses):
        brain = Brain(*responses)
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0},
                                     {"objective": "competition"}, brain=brain)
        return planner, brain

    def current_observation(self, brain):
        messages = brain.requests[-1]["messages"]
        return json.loads(next(row["content"] for row in reversed(messages)
                               if row["role"] == "user"))["observation"]

    def test_native_competition_request_includes_fresh_windows_and_shelter(self):
        planner, brain = self.planner(response("wait"))
        obs = observed_workstations()
        original = copy.deepcopy(obs)
        planner.choose(obs, {})
        sent = self.current_observation(brain)
        for key in EVIDENCE_FIELDS:
            self.assertEqual(sent[key], obs[key], key)
        names = {tool["function"]["name"] for tool in brain.requests[0]["tools"]}
        self.assertTrue({"select_task", "smelt_collect", "take_items", "set_door"} <= names)
        self.assertEqual(obs, original, "Preparing a request must not mutate fresh evidence")

    def test_historical_feedback_does_not_replace_new_furnace_slots(self):
        planner, brain = self.planner(response("wait"), response("wait", call_id="after_smelting"))
        before = observed_workstations()
        decision = planner.choose(before, {})
        planner.observe_result(decision, SkillResult(True, "Waited for the furnace"), before)
        after = copy.deepcopy(before)
        after["furnaces"][0].update(input=None, output={"name": "iron_ingot", "count": 1},
                                     burning=False, info={"burning": False})
        planner.choose(after, {})
        sent = self.current_observation(brain)
        self.assertEqual(sent["furnaces"], after["furnaces"])
        feedback = next(row for row in brain.requests[-1]["messages"] if row["role"] == "tool")
        self.assertEqual(feedback["tool_call_id"], "call_1")
        historical = json.loads(feedback["content"])["observation"]
        self.assertTrue(all(key not in historical for key in EVIDENCE_FIELDS))

    def test_partial_feedback_remains_unknown_and_cannot_start_a_model_request(self):
        planner, brain = self.planner(response("wait"), response("gather_block",
            {"pos": [3, 64, 0], "expected_name": "oak_log"}, call_id="after_safety"))
        decision = planner.choose(observed_workstations(), {})
        partial = observation()
        partial.update(observation_scope="safety", inventory_fresh=False, terrain_fresh=False,
                       inventory=[], counts={}, blocks=[])
        planner.observe_result(decision, SkillResult(False, "Fresh danger", {"executed": False},
                                                     "safety_changed"), partial)
        with self.assertRaises(LLMError) as caught:
            planner.choose(partial, {})
        self.assertEqual(caught.exception.code, "llm_unsafe_action")
        self.assertEqual(len(brain.requests), 1)
        planner.choose(observation(), {})  # Missing station data stays missing.
        self.assertTrue(all(key not in self.current_observation(brain) for key in EVIDENCE_FIELDS))
        feedback = next(row for row in brain.requests[-1]["messages"] if row["role"] == "tool")
        historical = json.loads(feedback["content"])["observation"]
        self.assertIsNone(historical["counts"])
        self.assertFalse(historical["inventory_fresh"])
        self.assertEqual(historical["observation_scope"], "safety")
        self.assertTrue(all(key not in historical for key in EVIDENCE_FIELDS))

    def test_only_matching_protected_result_is_deduplicated(self):
        for matching in (True, False):
            with self.subTest(matching=matching):
                planner, brain = self.planner(response("wait"), response("wait", call_id="next"))
                obs = observed_workstations()
                decision = planner.choose(obs, {})
                result = SkillResult(True, "Verified local result", {"executed": True, "count": 2})
                planner.observe_result(decision, result, obs)
                context = {"last_result": asdict(result)}
                if not matching:
                    context["last_result"]["data"]["count"] = 3
                original = copy.deepcopy(context)
                planner.choose(obs, context)
                messages = brain.requests[-1]["messages"]
                current = json.loads(messages[-1]["content"])
                self.assertEqual("last_result" in current["context"], not matching)
                tools = [row for row in messages if row["role"] == "tool"]
                self.assertEqual(len(tools), 1)
                self.assertEqual(tools[0]["tool_call_id"], "call_1")
                self.assertEqual(json.loads(tools[0]["content"])["result"], asdict(result))
                for key in EVIDENCE_FIELDS:
                    self.assertEqual(current["observation"][key], obs[key])
                self.assertEqual(context, original)

    def test_oversized_catalogue_refuses_request_without_truncating_latest_feedback(self):
        from core.competition_tasks import CompetitionTasks

        planner, brain = self.planner(response("wait"), response("wait", call_id="retry"))
        obs = observed_workstations()
        decision = planner.choose(obs, {})
        result = SkillResult(True, "Exact verified result", {"executed": True, "count": 2})
        planner.observe_result(decision, result, obs)
        history = planner.history
        columns = ["id", "title", "difficulty", "points", "eligibility", "availability", "completion"]
        rows = [[row.get(key) for key in columns] for row in CompetitionTasks({}).snapshot()["tasks"]]
        # Simulate future catalogue descriptions growing beyond the request cap.
        for row in rows:
            row[1] = str(row[1]) + " extended catalogue title" * 40
        context = {"competition": {"task_columns": columns, "tasks": rows}, "last_result": asdict(result)}
        original = copy.deepcopy(context)
        with self.assertRaises(LLMError) as caught:
            planner.choose(obs, context)
        self.assertEqual(caught.exception.code, "llm_context_limit")
        self.assertEqual(caught.exception.details["limit_bytes"], 48000)
        self.assertGreater(caught.exception.details["context_bytes"], 48000)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual(planner.history, history)
        self.assertEqual(context, original)
        self.assertTrue(planner._undelivered_result)

        planner.choose(obs, {})
        messages = brain.requests[-1]["messages"]
        self.assertLessEqual(planner._request_bytes(messages), 48000)
        self.assertEqual([row for row in messages if row["role"] in {"assistant", "tool"}], history)


if __name__ == "__main__":
    unittest.main()
