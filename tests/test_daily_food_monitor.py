"""Real collector/planner/harness/skills with loopback fake HTTP and fake game."""

import copy
from pathlib import Path
import tempfile
import time
import unittest

from agent.brain import OpenAIBrain
from agent.llm_planner import LLMSurvivalPlanner
from core.config import DEFAULTS
from core.daily_observation import DailyObservationCollector
from core.model_gate import ModelRequestGate
from core.survival_harness import SurvivalHarness
from test_daily_runtime import RuntimeDailyWorld
from test_llm_runtime import FakeChatEndpoint, tool_response


class DailyFoodMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = RuntimeDailyWorld()
        self.world.status.update(health=6, food=4)
        self.world.block([1, 64, 0], "carrots", properties={"age": 7}, mature=True, growth=[7, 7])
        self.world.block([1, 63, 0], "farmland")
        self.endpoint = FakeChatEndpoint([tool_response("harvest_crop", {
            "pos": [1, 64, 0], "expected_name": "carrots"})])
        self.endpoint.release.clear()
        self.addCleanup(self.endpoint.close)
        settings = {**copy.deepcopy(DEFAULTS["survival"]), "objective": "daily",
            "state_dir": str(Path(self.temp.name) / "state"), "poll_interval_seconds": .001,
            "retry_seconds": .001, "max_runtime_seconds": 20}
        gate = ModelRequestGate(self.endpoint.base_url, "offline-key", directory=Path(self.temp.name) / "gate")
        brain = OpenAIBrain(self.endpoint.base_url, "offline-key", "offline-model", timeout_seconds=2, request_gate=gate)
        planner = LLMSurvivalPlanner({"enabled": True, "model": "offline-model", "max_repair_attempts": 0}, settings, brain=brain)
        self.harness = SurvivalHarness(self.world, settings, DEFAULTS["sleep"], planner=planner)
        self.addCleanup(self.harness.close)
        self.addCleanup(self.endpoint.release.set)

    def start_pending_food_request(self):
        self.assertIsInstance(self.harness.collector, DailyObservationCollector)
        self.assertTrue(self.harness._start())
        self.assertTrue(self.endpoint.entered.wait(1), "Actual collector did not authorize the verified nearby food turn")
        self.assertEqual(self.harness.context["survival_constraints"]["scenario"], "low_health")
        self.assertTrue(self.harness.context["survival_constraints"]["model_allowed"])
        self.assertTrue(self.harness._model_worker.pending)

    def finish(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.harness.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.001)
        self.fail("Pending fake model cycle did not finish")

    def test_stable_low_health_food_request_survives_monitor_and_harvests_real_crop(self):
        self.start_pending_food_request()
        # Keep the HTTP response pending across multiple actual monitor passes.
        for _ in range(3):
            self.harness._next_monitor = 0
            self.assertIsNone(self.harness.tick(automatic=False))
            self.assertFalse(self.harness._cancel.is_set())
            self.assertFalse(self.harness._model_worker.cancelled)
        self.assertEqual(self.world.counts.get("carrot", 0), 0)
        self.endpoint.release.set()
        result = self.finish()
        self.assertTrue(result.success, result)
        self.assertEqual(self.world.counts["carrot"], 1)
        self.assertEqual(self.world.blocks[(1, 64, 0)]["name"], "air")
        self.assertEqual(len([row for row in self.world.calls if row[0] == "dig"]), 1)
        self.assertIsNone(self.harness.planner._pending)

    def assert_pending_food_preempted(self):
        self.harness._next_monitor = 0
        self.assertIsNone(self.harness.tick(automatic=False))
        self.assertTrue(self.harness._cancel.is_set())
        self.assertTrue(self.harness._model_worker.cancelled)
        result = self.finish()
        self.assertTrue(result.success, result)
        self.assertFalse(result.data["executed"])
        self.endpoint.release.set()
        self.harness._model_worker.close(self.harness.last_observation)
        self.assertEqual(self.world.counts.get("carrot", 0), 0)
        self.assertFalse([row for row in self.world.calls if row[0] == "dig"])
        self.assertEqual(len(self.endpoint.requests), 1)
        self.assertIsNone(self.harness.planner._pending)

    def test_health_drop_still_preempts_authorized_food_request(self):
        self.start_pending_food_request()
        self.world.status["health"] = 5
        self.assert_pending_food_preempted()

    def test_nearby_hostile_still_preempts_authorized_food_request(self):
        self.start_pending_food_request()
        self.world.status["nearest_hostile"] = {"name": "zombie", "dist": 2}
        self.assert_pending_food_preempted()


if __name__ == "__main__":
    unittest.main()
