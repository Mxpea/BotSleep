import copy
import json
import unittest

from agent.brain import LLMError
from agent.daily_survival import DailySurvivalGoals
from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision
from test_daily_survival import observation
from test_llm_planner import Brain, response


class FoliagePlanningTests(unittest.TestCase):
    def scene(self):
        return observation(blocks=[
            {"name": "oak_log", "pos": [2, 64, 0], "can_see": False, "visibility": {"blocker": [1, 64, 0]}},
            {"name": "oak_leaves", "pos": [1, 64, 0], "fresh": True, "can_see": True}], time_of_day=1000)

    def test_resource_task_proposes_one_fresh_visible_blocker_before_retrying_log(self):
        board = DailySurvivalGoals().build(self.scene())
        wood = next(row for row in board["tasks"] if row["id"] == "wood_reserve")
        self.assertEqual(wood["next_action"]["name"], "clear_foliage")
        self.assertEqual(wood["next_action"]["args"]["pos"], [1, 64, 0])
        self.assertNotEqual(wood["status"], "satisfied")

    def test_unknown_or_nonadjacent_foliage_cannot_be_proposed_for_clearing(self):
        for change in ({"can_see": None}, {"fresh": False}, {"pos": [2, 64, 1]}):
            obs = self.scene()
            obs["blocks"][1].update(change)
            self.assertFalse(any(row.get("next_action", {}).get("name") == "clear_foliage"
                for row in DailySurvivalGoals().build(obs)["tasks"] if row.get("next_action")))

    def test_removed_occlusion_releases_occlusion_and_path_failure(self):
        planner = LLMSurvivalPlanner({"enabled": True, "model": "offline"}, {"objective": "daily", "autonomy": {"enabled": False}}, brain=Brain())
        before = self.scene()
        decision = Decision("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "Gather", "wood")
        failure = {"state": planner._failure_state(before, decision), "code": "target_occluded"}
        after = copy.deepcopy(before)
        after["blocks"][0].update(can_see=True, visibility={"state": "visible"})
        after["blocks"].pop()
        self.assertFalse(planner._unchanged_failure(failure, after, decision))
        failure["code"] = "no_path"
        self.assertFalse(planner._unchanged_failure(failure, after, decision))
        self.assertTrue(planner._unchanged_failure(failure, before, decision))

    def test_diagonal_live_canopy_blocker_is_offered_before_the_log(self):
        obs = observation(blocks=[
            {"name": "birch_log", "pos": [0, 122, -2], "can_see": False, "visibility": {"blocker": [0, 123, -1]}},
            {"name": "birch_leaves", "pos": [0, 123, -1], "fresh": True, "can_see": True}],
            pos=[1.5, 122, .5], time_of_day=7902)
        wood = next(row for row in DailySurvivalGoals().build(obs)["tasks"] if row["id"] == "wood_reserve")
        self.assertEqual(wood["next_action"]["name"], "clear_foliage")
        self.assertEqual(wood["next_action"]["args"]["pos"], [0, 123, -1])

    def test_distant_unknown_logs_cannot_hide_nearby_occluded_target(self):
        planner = LLMSurvivalPlanner({"enabled": True, "model": "offline"}, {"objective": "daily", "autonomy": {"enabled": False}}, brain=Brain())
        obs = self.scene()
        obs["blocks"].extend({"name": "birch_log", "pos": [x, 64, 0]} for x in range(5, 16))
        view = planner._snapshot(obs)
        logs = [row for row in view["blocks"] if row["name"].endswith("_log")]
        self.assertLessEqual(len(logs), 8)
        self.assertIn([2, 64, 0], [row["pos"] for row in logs])
        target = next(row for row in logs if row["pos"] == [2, 64, 0])
        self.assertFalse(target["can_see"])
        self.assertEqual(target["visibility"]["blocker"], [1, 64, 0])

    def test_live_canopy_summary_is_diagnostic_and_cannot_authorize_clearing_support(self):
        args = {"pos": [4, 123, 7], "expected_name": "oak_leaves"}
        brain = Brain(response("clear_foliage", args))
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0},
                                     {"objective": "daily", "autonomy": {"enabled": False}}, brain=brain)
        obs = observation(blocks=[{"name": "oak_log", "pos": [4, 120, 6], "can_see": False,
                    "visibility": {"blocker": [4, 123, 6]}}], pos=[4, 124, 6.6], time_of_day=7464)
        obs["terrain"] = [{"name": "oak_leaves", "pos": [4, 123, 7], "count": 658, "dist": 1.1}]
        original = copy.deepcopy(obs)
        with self.assertRaises(LLMError) as caught:
            planner.choose(obs, {})
        self.assertEqual(caught.exception.code, "llm_unsafe_action")
        self.assertIn("observation.blocks", caught.exception.message)
        self.assertIn("diagnostic only", caught.exception.message)
        self.assertIn("below feet level", caught.exception.message)
        sent = json.loads(brain.requests[0]["messages"][-1]["content"])["observation"]
        self.assertFalse(sent["terrain_summary"]["actionable"])
        self.assertEqual(sent["terrain_summary"]["rows"][0]["nearest_pos"], args["pos"])
        self.assertNotIn("pos", sent["terrain_summary"]["rows"][0])
        self.assertEqual(sent["blocks"], obs["blocks"])
        self.assertIsNone(planner._pending)
        self.assertEqual(obs, original)

    def test_summary_repair_can_select_verified_body_height_foliage_and_preserves_tool_pair(self):
        bad = {"pos": [4, 123, 7], "expected_name": "oak_leaves"}
        good = {"pos": [5, 124, 6], "expected_name": "oak_leaves"}
        brain = Brain(response("clear_foliage", bad, "summary_only"),
                      response("clear_foliage", good, "verified_leaf"))
        planner = LLMSurvivalPlanner({"enabled": True}, {"objective": "daily", "autonomy": {"enabled": False}}, brain=brain)
        obs = observation(blocks=[{"name": "oak_leaves", "pos": good["pos"], "can_see": True,
                                   "fresh": True, "properties": {"waterlogged": False}}],
                          pos=[4, 124, 6.6], time_of_day=7464)
        obs["terrain"] = [{"name": "oak_leaves", "pos": bad["pos"], "count": 658}]
        obs["safe_stand_candidates"] = [{"pos": [5.5, 124, 6.5], "support": "stone"}]
        obs["placement_candidates"] = [{"pos": [5, 124, 6], "support": "stone"}]
        obs["local_geometry_origin"] = obs["status"]["pos"]
        decision = planner.choose(obs, {})
        self.assertEqual((decision.name, decision.args), ("clear_foliage", good))
        messages = brain.requests[-1]["messages"]
        sent = json.loads(next(m["content"] for m in messages if m["role"] == "user"))["observation"]
        self.assertEqual(sent["safe_stand_candidates"], obs["safe_stand_candidates"])
        self.assertEqual(sent["placement_candidates"], obs["placement_candidates"])
        self.assertEqual(sent["local_geometry_origin"], obs["local_geometry_origin"])
        self.assertEqual(sent["blocks"][0]["pos"], good["pos"])
        rejected = next(m for m in messages if m["role"] == "assistant")
        feedback = next(m for m in messages if m["role"] == "tool")
        self.assertEqual(rejected["tool_calls"][0]["id"], feedback["tool_call_id"])
        self.assertEqual(feedback["tool_call_id"], "summary_only")
        self.assertIn("diagnostic only", feedback["content"])
        self.assertLessEqual(planner._request_bytes(messages), planner.MAX_REQUEST_BYTES)


if __name__ == "__main__": unittest.main()
