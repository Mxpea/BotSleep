"""Daily context preserves actionable coordinates within its hard size budget."""
import copy
import unittest

from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision
from test_llm_planner import Brain, observation


class DailyContextTests(unittest.TestCase):
    def test_dawn_can_release_a_dusk_safety_rejection_without_erasing_path_failure(self):
        planner = LLMSurvivalPlanner({"enabled": True, "model": "offline"}, {"objective": "daily"}, brain=Brain())
        decision = Decision("gather_block", {"pos": [3, 64, 0], "expected_name": "oak_log"}, "Gather", "wood")
        dusk = observation(time_of_day=11500)
        failure = {"decision": decision, "state": planner._failure_state(dusk, decision), "code": "safety_changed"}
        self.assertTrue(planner._unchanged_failure(failure, observation(time_of_day=11900), decision))
        self.assertFalse(planner._unchanged_failure(failure, observation(time_of_day=1000), decision))
        failure["code"] = "no_path"
        self.assertTrue(planner._unchanged_failure(failure, observation(time_of_day=1000), decision))

    def test_nested_route_and_crop_coordinates_survive_compaction(self):
        planner = LLMSurvivalPlanner({"enabled": True, "model": "offline"}, {"objective": "daily"}, brain=Brain())
        obs = observation(time_of_day=10900)
        path = [[0, 64, -2], [0, 64, -1], [0, 64, 0]]
        crop = {"pos": [2, 64, 1], "farmland_pos": [2, 63, 1], "name": "carrots"}
        obs["daily_evidence"] = {"shelter": {"return_route": {"verified": True, "path": path}},
                                 "food_production": {"growing_crops": [crop]}}
        original = copy.deepcopy(obs)
        view = planner._snapshot(obs)
        self.assertEqual(view["daily_evidence"]["shelter"]["return_route"]["path"], path)
        self.assertEqual(view["daily_evidence"]["food_production"]["growing_crops"], [crop])
        self.assertEqual(view["status"]["time_of_day"], 10900)
        self.assertEqual(obs, original)

    def test_omitted_metadata_is_not_represented_as_confirmed_empty(self):
        self.assertEqual(LLMSurvivalPlanner._compact_value([{"unbounded": "data"}], depth=5), {"summary_omitted": True})
        self.assertEqual(LLMSurvivalPlanner._compact_value([1, 64, 2], depth=8), [1, 64, 2])


if __name__ == "__main__":
    unittest.main()
