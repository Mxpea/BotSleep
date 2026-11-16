"""Offline acceptance of local competition steps through the real serial harness."""

import copy
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent.daily_survival import DailySurvivalGoals
from agent.llm_planner import LLMSurvivalPlanner
from core.client import APIError
from core.competition_rules import scoring_catalog
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from skills import SkillResult
from test_daily_survival import block, observation
from test_llm_planner import Brain, Recorder, response


MINE = "minecraft:story/mine_stone"
GOAL = "minecraft:story/upgrade_tools"


class SnapshotCollector:
    """Return copies of the evolving fixture, including post-skill inventory."""

    def __init__(self, world):
        self.world = world
        self.snapshots = []

    def collect(self, cancel_event=None):
        snapshot = copy.deepcopy(self.world)
        self.snapshots.append(snapshot)
        return snapshot


class LocalCompetitionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = copy.deepcopy(DEFAULTS["survival"])
        self.settings.update(objective="competition", state_dir=self.directory.name,
                             poll_interval_seconds=.001, retry_seconds=.001)
        self.settings["local_execution"].update(enabled=True, max_steps=24, max_seconds=90)
        self.settings["competition"]["advancement_poll_seconds"] = .001
        self.obs = observation({"wooden_pickaxe": 1, "cobblestone": 3, "stick": 2,
                                "bread": 8, "oak_log": 8, "coal": 8},
                               [block("crafting_table", [2, 64, 0]),
                                block("furnace", [2, 64, 2])], time_of_day=1000)
        self.obs["observation_scope"] = "full"
        self.completed = {MINE}
        self.catalog = scoring_catalog()
        self.advancement_reads = []
        self.client = Mock(username="OfflineLocalCompetitionBot",
                           base_url="http://offline.invalid/api/v1", password="", token="offline")
        self.client.get_status.side_effect = lambda **kwargs: {
            "ok": True, "result": copy.deepcopy(self.obs["status"])}
        self.client.advancements.side_effect = self.advancements
        self.collector = SnapshotCollector(self.obs)
        self.skills = Mock()
        self.skills.execute.side_effect = self.execute_craft
        self.recorder = Recorder()
        # Any accidental real HTTP attempt fails this isolated acceptance test.
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Real HTTP forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def advancements(self, **kwargs):
        self.advancement_reads.append(set(self.completed))
        rows = [{"id": row["id"], "title": row["title"], "frame": "task",
                 "status": "completed" if row["id"] in self.completed else "not_started",
                 "completed_at": None,
                 "progress": {"criteria_done": int(row["id"] in self.completed), "criteria_total": 1}}
                for row in self.catalog]
        return {"ok": True, "result": {"ready": True, "updated_at": int(time.time() * 1000),
            "packets_received": 1, "summary": {"total": len(rows), "completed": len(self.completed),
                "in_progress": 0, "not_started": len(rows) - len(self.completed)}, "advancements": rows}}

    def execute_craft(self, name, args, **kwargs):
        self.assertEqual(name, "craft_item")
        item = args["item"]
        recipes = {"stone_pickaxe": ({"cobblestone": 3, "stick": 2}, 1),
                   "oak_planks": ({"oak_log": 1}, 4)}
        self.assertIn(item, recipes)
        materials, output = recipes[item]
        self.assertEqual(args.get("count", 1), output)
        for material, count in materials.items():
            self.assertGreaterEqual(self.obs["counts"].get(material, 0), count)
            self.obs["counts"][material] -= count
        self.obs["counts"][item] = self.obs["counts"].get(item, 0) + output
        self.obs["inventory"] = [{"name": name, "count": count}
                                 for name, count in self.obs["counts"].items() if count > 0]
        # Advancement evidence is deliberately independent of crafting success.
        return SkillResult(True, "Fixture materials consumed and output acquired", {"gained": {item: output}})

    def harness(self, *messages):
        brain = Brain(*messages)
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, self.settings,
                                     recorder=self.recorder, brain=brain)
        runner = SurvivalHarness(self.client, self.settings, DEFAULTS["sleep"], self.recorder,
                                 planner=planner, skills=self.skills, collector=self.collector)
        self.addCleanup(runner.close)
        return runner, brain

    def step(self, runner):
        # Avoid wall-clock sleeps for retry/advancement scheduling between steps.
        runner._next_attempt = 0
        runner._competition_next_advancement = 0
        self.assertTrue(runner.request_step())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = runner.tick(automatic=False)
            if result is not None:
                self.assertTrue(result.success, result)
                return result
            time.sleep(.001)
        self.fail("Local competition worker did not finish")

    def last_decision(self):
        # snapshot.decision describes in-flight work and is cleared on completion.
        return next(event[3]["decision"] for event in reversed(self.recorder.events)
                    if event[2] == "STEP_RESULT")

    def select_and_craft(self, runner, brain):
        self.step(runner)
        snapshot = runner.snapshot()
        self.assertEqual(self.last_decision()["name"], "select_task")
        self.assertEqual(self.last_decision()["args"], {"goal_id": GOAL})
        self.assertEqual(snapshot["competition"]["active_goal_id"], GOAL)
        self.assertEqual(snapshot["competition"]["phase"], "active")
        self.assertEqual(brain.requests, [])
        self.skills.execute.assert_not_called()
        self.step(runner)
        snapshot = runner.snapshot()
        self.assertEqual(self.last_decision()["name"], "craft_item")
        self.assertEqual(self.last_decision()["args"], {"item": "stone_pickaxe", "count": 1})
        self.skills.execute.assert_called_once()
        self.assertEqual(self.obs["counts"]["stone_pickaxe"], 1)
        self.assertEqual((self.obs["counts"]["cobblestone"], self.obs["counts"]["stick"]), (0, 0))
        self.assertEqual(runner.last_observation["counts"]["stone_pickaxe"], 1)
        self.assertEqual(self.collector.snapshots[-1]["counts"]["stone_pickaxe"], 1)
        self.assertEqual(brain.requests, [])
        self.assertIsNone(snapshot["pending"])
        recent = snapshot["local_execution"]["recent"][-1]
        self.assertEqual((recent["action"], recent["success"], recent["progress"]), ("craft_item", True, True))

    def test_local_selection_craft_and_fresh_server_finish_make_zero_model_requests(self):
        runner, brain = self.harness()
        self.select_and_craft(runner, brain)
        before_score = runner.competition.snapshot()["score"]["raw"]
        self.assertNotIn(GOAL, self.advancement_reads[-1])
        self.completed.add(GOAL)
        reads = self.client.advancements.call_count
        self.step(runner)
        snapshot = runner.snapshot()
        self.assertGreater(self.client.advancements.call_count, reads)
        self.assertIn(GOAL, self.advancement_reads[-1])
        self.assertEqual(self.last_decision()["name"], "finish_task")
        self.assertEqual(snapshot["competition"]["phase"], "selection")
        self.assertIsNone(snapshot["competition"]["active_goal_id"])
        points = next(row["points"] for row in self.catalog if row["id"] == GOAL)
        self.assertEqual(snapshot["competition"]["score"]["raw"], before_score + points)
        self.assertEqual(snapshot["competition"]["last_exit"]["outcome"], "completed")
        self.assertEqual(runner.journal.state["context"]["competition"]["phase"], "selection")
        self.assertFalse(snapshot["finished"], "Finishing one advancement does not end the competition")
        self.assertFalse(snapshot["completed"])
        self.assertIsNone(snapshot["pending"])
        self.skills.execute.assert_called_once()
        self.assertEqual(brain.requests, [])
        decisions = [event[3] for event in self.recorder.events if event[2] == "DECISION"]
        self.assertEqual([row["decision"]["name"] for row in decisions], ["select_task", "craft_item", "finish_task"])
        self.assertEqual([row["source"] for row in decisions], ["local", "local", "local"])

    def test_fresh_server_completion_returns_from_takeover_to_local_finish(self):
        runner, brain = self.harness(response("wait", {"seconds": .25}, call_id="server_wait"))
        self.select_and_craft(runner, brain)
        self.step(runner)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual(runner.snapshot()["local_execution"]["phase"], "model_takeover")
        self.completed.add(GOAL)
        self.step(runner)
        self.assertEqual(self.last_decision()["name"], "finish_task")
        self.assertEqual(runner.competition.snapshot()["phase"], "selection")
        self.assertEqual(len(brain.requests), 1)

    def test_owned_target_without_server_confirmation_hands_over_with_active_task_summary(self):
        runner, brain = self.harness(response("wait", {"seconds": .25}, call_id="takeover_wait"))
        self.select_and_craft(runner, brain)
        before_score = runner.competition.snapshot()["score"]["raw"]
        self.step(runner)
        snapshot = runner.snapshot()
        self.assertEqual(len(brain.requests), 1)
        body = json.loads(brain.requests[0]["messages"][-1]["content"])
        local = body["context"]["local_execution"]
        self.assertEqual((local["phase"], local["reason"], local["steps"]),
                         ("model_takeover", "no_verified_local_step", 1))
        self.assertEqual(local["recent"], [{"action": "craft_item", "success": True,
                                          "error_code": None, "progress": True}])
        self.assertEqual(body["observation"]["counts"]["stone_pickaxe"], 1)
        board = body["context"]["competition"]
        self.assertEqual(board["active_goal_id"], GOAL)
        self.assertEqual(board["active_goal"]["completion"], "not_completed")
        self.assertFalse(board["stale"])
        self.assertEqual(self.last_decision()["name"], "wait")
        self.assertEqual(snapshot["competition"]["phase"], "active")
        self.assertEqual(snapshot["competition"]["active_goal_id"], GOAL)
        self.assertEqual(snapshot["competition"]["score"]["raw"], before_score)
        self.assertEqual(snapshot["local_execution"], local)
        self.assertIsNone(snapshot["pending"])
        self.assertFalse(snapshot["finished"])
        self.skills.execute.assert_called_once()
        self.assertNotIn("finish_task", [event[3]["decision"]["name"]
                                        for event in self.recorder.events if event[2] == "DECISION"])

    def test_unavailable_server_refresh_cannot_finish_from_inventory_or_prior_assessment(self):
        runner, brain = self.harness(response("wait", {"seconds": .25}, call_id="unavailable_wait"))
        self.select_and_craft(runner, brain)
        before_score = runner.competition.snapshot()["score"]["raw"]
        self.completed.add(GOAL)
        self.client.advancements.side_effect = APIError("offline_unavailable", "Synthetic unavailable advancement read")
        self.step(runner)
        snapshot = runner.snapshot()
        self.assertEqual(self.last_decision()["name"], "wait")
        self.assertEqual(snapshot["competition"]["active_goal_id"], GOAL)
        self.assertTrue(snapshot["competition"]["stale"])
        self.assertEqual(snapshot["competition"]["score"]["raw"], before_score)
        self.assertEqual(len(brain.requests), 1)
        body = json.loads(brain.requests[0]["messages"][-1]["content"])
        self.assertTrue(body["context"]["competition"]["stale"])
        self.assertEqual(body["context"]["local_execution"]["phase"], "model_takeover")
        self.skills.execute.assert_called_once()
        self.assertIsNone(snapshot["pending"])

    def test_daily_current_task_action_updates_collector_without_a_model_request(self):
        self.settings["objective"] = "daily"
        self.obs["counts"] = {"bread": 8, "oak_log": 1, "coal": 8}
        board = DailySurvivalGoals(self.settings).build(copy.deepcopy(self.obs))
        current = next(row for row in board["tasks"] if row["id"] == board["active_task_id"])
        action = current["next_action"]
        self.assertEqual(action["name"], "craft_item")
        self.assertEqual(action["args"], {"item": "oak_planks", "count": 4})
        runner, brain = self.harness()
        self.step(runner)
        snapshot = runner.snapshot()
        self.assertEqual((self.last_decision()["name"], self.last_decision()["args"]), (action["name"], action["args"]))
        self.assertEqual(self.collector.snapshots[-1]["counts"]["oak_planks"], 4)
        self.assertEqual(self.obs["counts"]["oak_log"], 0)
        self.assertEqual(snapshot["local_execution"]["recent"][-1]["progress"], True)
        self.assertIsNone(snapshot["pending"])
        self.assertEqual(brain.requests, [])
        self.client.advancements.assert_not_called()
        self.skills.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
