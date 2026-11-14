"""Partial safety observations must not masquerade as inventory evidence."""
import json
import unittest

from agent.brain import LLMError
from agent.llm_planner import LLMSurvivalPlanner
from skills import SkillResult
from test_llm_planner import Brain, observation, response


class EmergencyFeedbackTests(unittest.TestCase):
    def partial(self):
        obs = observation()
        obs.update(observation_scope="safety", inventory_fresh=False, terrain_fresh=False,
                   inventory=[], counts={}, blocks=[])
        return obs

    def test_partial_observation_never_requests_model_production(self):
        brain = Brain(response("wait"))
        planner = LLMSurvivalPlanner({"enabled": True}, {"objective": "daily"}, brain=brain)
        with self.assertRaises(LLMError) as caught:
            planner.choose(self.partial(), {})
        self.assertEqual(caught.exception.code, "llm_unsafe_action")
        self.assertEqual(brain.requests, [])

    def test_preempted_model_receives_unknown_inventory_in_paired_feedback(self):
        brain = Brain(response("wait"), response("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}, "after_safety"))
        planner = LLMSurvivalPlanner({"enabled": True}, {"objective": "daily"}, brain=brain)
        decision = planner.choose(observation(), {})
        planner.observe_result(decision, SkillResult(False, "Fresh danger", {"executed": False}, "safety_changed"), self.partial())
        planner.choose(observation(), {})
        feedback = next(row for row in brain.requests[-1]["messages"] if row["role"] == "tool")
        snap = json.loads(feedback["content"])["observation"]
        self.assertIsNone(snap["counts"])
        self.assertFalse(snap["inventory_fresh"])
        self.assertEqual(snap["observation_scope"], "safety")


if __name__ == "__main__":
    unittest.main()
