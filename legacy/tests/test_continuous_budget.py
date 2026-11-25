import copy
import tempfile
import time
import unittest

from unittest.mock import patch

from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from test_task_harness import TaskWorld


class ContinuousBudgetTests(unittest.TestCase):
    def setUp(self):
        self.world = TaskWorld()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.settings = copy.deepcopy(DEFAULTS["survival"])
        self.settings.update(objective="daily", continuous=True, state_dir=state.name,
                             max_steps=1, max_runtime_seconds=1, max_deaths=1,
                             poll_interval_seconds=.001, retry_seconds=.001)
        self.settings["autonomy"]["mainline"] = True

    def harness(self):
        runner = SurvivalHarness(self.world, self.settings, DEFAULTS["sleep"])
        self.addCleanup(runner.close)
        return runner

    def cycle(self, runner):
        runner._next_attempt = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = runner.tick()
            if result is not None:
                return result
            self.assertFalse(runner.paused or runner.finished, runner.snapshot())
            time.sleep(.001)
        self.fail("Daily survival cycle did not return a result")

    def test_real_task_keeps_gathering_after_global_step_and_time_limits(self):
        self.world.block([2, 64, 0], "oak_log")
        runner = self.harness()
        self.assertTrue(self.cycle(runner).data.get("accepted"))
        runner._started -= 10
        self.assertTrue(self.cycle(runner).success)
        self.assertEqual(self.world.counts.get("oak_log"), 1)
        self.assertGreater(runner.steps, self.settings["max_steps"])
        self.assertIsNone(runner.context["remaining_steps"])
        self.assertIsNone(runner.context["remaining_seconds"])
        self.assertFalse(runner.finished)

    def test_repeated_observed_deaths_replan_from_respawned_inventory(self):
        self.world.block([2, 64, 0], "oak_log")
        runner = self.harness()
        self.assertTrue(self.cycle(runner).data.get("accepted"))
        for marker in (10, 20):
            self.world.status.update(alive=False, ready=False, health=0)
            runner._next_idle_monitor = 0
            runner._monitor_idle()
            self.world.status.update(alive=True, ready=True, health=20, last_death={"t": marker})
            self.world.counts.clear()
            runner._next_idle_monitor = 0
            runner._monitor_idle()
            self.assertFalse(runner.paused)
        self.assertEqual(runner.context["death_count"], 2)
        self.assertEqual(runner.context["life_generation"], 2)
        for _ in range(6):
            self.cycle(runner)
            if self.world.counts.get("oak_log"):
                break
        self.assertEqual(self.world.counts.get("oak_log"), 1)
        self.assertEqual(runner.snapshot()["death_count"], 2)

    def test_advisory_clock_survives_restart_and_can_reach_zero_without_stopping(self):
        self.settings["max_runtime_seconds"] = 60
        with patch("core.survival_harness.time.time", return_value=1000):
            runner = self.harness()
            self.assertEqual(runner.snapshot()["budget_advice"]["remaining_seconds"], 60)
            runner.close()
        with patch("core.survival_harness.time.time", return_value=1045):
            restarted = self.harness()
            self.assertEqual(restarted.snapshot()["budget_advice"]["remaining_seconds"], 15)
        with patch("core.survival_harness.time.time", return_value=1061):
            self.assertEqual(restarted.snapshot()["budget_advice"]["remaining_seconds"], 0)
            self.assertIsNone(restarted._remaining_budget()["remaining_seconds"])
            self.assertFalse(restarted.finished)

    def test_manual_pause_remains_in_force_beyond_old_global_limits(self):
        runner = self.harness()
        runner.pause("user_requested")
        runner._started -= 10
        runner.steps = 5
        runner.context["death_count"] = 5
        self.assertIsNone(runner.tick())
        self.assertTrue(runner.paused)
        self.assertFalse(runner.finished)
        self.assertEqual(runner.pause_reason, "user_requested")
        self.assertEqual(self.world.calls, [("status",)])

    def test_exhausted_canopy_waits_and_rechecks_footing_before_a_new_bounded_window(self):
        self.world.status["pos"] = [.5, 70, .5]
        for x, y, name in ((0, 69, "oak_leaves"), (1, 69, "birch_leaves"),
                           (2, 68, "oak_leaves"), (3, 66, "dirt")):
            self.world.block([x, y, 0], name, properties={"waterlogged": False})
        runner = self.harness()
        obs = runner.collector.collect()
        state = runner._recovery_state("canopy_recovery", obs)
        state.update(steps=64, total_steps=64, visited=[[0, 70, 0]], evacuating=True)
        result = self.cycle(runner)
        self.assertTrue(result.data["waiting"])
        self.assertFalse(runner.paused)
        self.assertGreater(state["retry_after"], time.time())
        self.assertFalse(any(c[0] == "move" for c in self.world.calls))
        state["retry_after"] = 0
        runner._prepare_context({**obs, "fresh": False, "stale": True})
        self.assertEqual(state["steps"], 64)
        self.assertTrue(self.cycle(runner).success)
        self.assertEqual((state["steps"], state["total_steps"], state["completed_windows"]), (1, 65, 1))
        self.assertEqual(len([c for c in self.world.calls if c[0] == "move"]), 1)
