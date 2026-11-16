"""Real collector/planner/harness/skills with loopback fake HTTP and fake game."""

import copy
from pathlib import Path
import tempfile
import threading
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
            "autonomy": {"enabled": False},  # Food emergencies remain local in either planner mode.
            "state_dir": str(Path(self.temp.name) / "state"), "poll_interval_seconds": .001,
            "retry_seconds": .001, "max_runtime_seconds": 20}
        gate = ModelRequestGate(self.endpoint.base_url, "offline-key", directory=Path(self.temp.name) / "gate")
        brain = OpenAIBrain(self.endpoint.base_url, "offline-key", "offline-model", timeout_seconds=2, request_gate=gate)
        planner = LLMSurvivalPlanner({"enabled": True, "model": "offline-model", "max_repair_attempts": 0}, settings, brain=brain)
        self.harness = SurvivalHarness(self.world, settings, DEFAULTS["sleep"], planner=planner)
        self.addCleanup(self.harness.close)
        self.addCleanup(self.endpoint.release.set)
        self.action_entered = threading.Event()
        self.action_release = threading.Event()
        execute = self.harness.skills.execute

        def paused_execute(name, args, **kwargs):
            self.action_entered.set()
            if not self.action_release.wait(2):
                raise AssertionError("Test did not release the local action")
            return execute(name, args, **kwargs)

        self.harness.skills.execute = paused_execute
        self.addCleanup(self.action_release.set)

    def start_local_food_action(self):
        self.assertIsInstance(self.harness.collector, DailyObservationCollector)
        self.assertTrue(self.harness._start())
        self.assertTrue(self.action_entered.wait(1), "Actual collector did not authorize the verified nearby local food action")
        self.assertEqual(self.harness.context["survival_constraints"]["scenario"], "low_health")
        self.assertFalse(self.harness.context["survival_constraints"]["model_allowed"])
        self.assertFalse(self.harness._model_worker.pending)
        self.assertEqual(self.endpoint.requests, [])

    def finish(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.harness.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.001)
        self.fail("Local food cycle did not finish")

    def test_stable_local_food_action_survives_monitor_without_requesting_model(self):
        self.start_local_food_action()
        # Hold the real skill before its first write across actual monitor passes.
        for _ in range(3):
            self.harness._next_monitor = 0
            self.assertIsNone(self.harness.tick(automatic=False))
            self.assertFalse(self.harness._cancel.is_set())
        self.assertEqual(self.world.counts.get("carrot", 0), 0)
        self.action_release.set()
        result = self.finish()
        self.assertTrue(result.success, result)
        self.assertEqual(self.world.counts["carrot"], 1)
        self.assertEqual(self.world.blocks[(1, 64, 0)]["name"], "air")
        self.assertEqual(len([row for row in self.world.calls if row[0] == "dig"]), 1)
        self.assertIsNone(self.harness.planner._pending)
        self.assertEqual(self.endpoint.requests, [])

    def assert_local_food_preempted(self):
        self.harness._next_monitor = 0
        self.assertIsNone(self.harness.tick(automatic=False))
        self.assertTrue(self.harness._cancel.is_set())
        self.action_release.set()
        result = self.finish()
        self.assertFalse(result.success, result)
        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual(self.world.counts.get("carrot", 0), 0)
        self.assertFalse([row for row in self.world.calls if row[0] == "dig"])
        self.assertEqual(len(self.endpoint.requests), 0)
        self.assertIsNone(self.harness.planner._pending)

    def test_health_drop_still_preempts_local_food_action(self):
        self.start_local_food_action()
        self.world.status["health"] = 5
        self.assert_local_food_preempted()

    def test_nearby_hostile_still_preempts_local_food_action(self):
        self.start_local_food_action()
        self.world.status["nearest_hostile"] = {"name": "zombie", "dist": 2}
        self.assert_local_food_preempted()


if __name__ == "__main__":
    unittest.main()
