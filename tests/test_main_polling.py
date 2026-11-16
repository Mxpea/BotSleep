"""Main-loop scheduling with a fake clock and transport; no live services."""

import copy
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

import main as entrypoint
import core.survival_harness as harness_module
from agent.survival_planner import Decision
from core.client import APIError
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from skills import SkillResult


class Clock:
    def __init__(self, stop_at=20):
        self.now = 0.0
        self.stop_at = stop_at
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return 1700000000 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now = round(self.now + seconds, 10)
        if self.now >= self.stop_at:
            raise KeyboardInterrupt


class TimedFuture:
    def __init__(self, clock, ready_at, value):
        self.clock, self.ready_at, self.value = clock, ready_at, value

    def done(self):
        return self.clock.now >= self.ready_at

    def result(self):
        if not self.done():
            raise AssertionError("A pending worker result was consumed")
        return self.value


class ScriptedExecutor:
    """Only the worker is scripted; real harness scheduling handles its result."""
    def __init__(self, clock, scripts, observation, active_decisions=False):
        self.clock, self.scripts, self.observation = clock, list(scripts), observation
        self.active_decisions = active_decisions
        self.started_at = []

    def submit(self, function, *args):
        if not self.scripts:
            raise AssertionError("Unexpected extra action/model cycle")
        self.started_at.append(self.clock.now)
        duration, decision, result = self.scripts.pop(0)
        if self.active_decisions:
            function.__self__._active_decision = decision
        return TimedFuture(self.clock, round(self.clock.now + duration, 10),
                           (decision, result, copy.deepcopy(self.observation)))

    def shutdown(self, **kwargs):
        pass


class MainPollingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = copy.deepcopy(DEFAULTS)
        self.config["agent"]["mode"] = "survival"
        self.config["llm"]["enabled"] = False
        self.config["survival"].update(state_dir=temp.name, objective="starter",
            poll_interval_seconds=2, retry_seconds=5, max_steps=20, max_runtime_seconds=100)
        self.config["debug"]["flight_recorder"]["enabled"] = False
        self.config["debug"]["monitor"].update(enabled=False, console=False, heartbeat_seconds=2)
        self.config["debug"]["allowed_usernames"] = ["TestPlayer"]
        self.clock = Clock()
        self.status = {"ready": True, "alive": True, "busy": None, "health": 20,
            "food": 20, "dimension": "overworld", "gamemode": "survival", "is_day": True,
            "thundering": False, "pos": [0.5, 64, 0.5], "on_ground": True}
        self.observation = {"fresh": True, "status": self.status, "counts": {},
                            "inventory": [], "blocks": [], "entities": []}
        self.client = Mock(username="PollingTestBot", base_url="http://fake.invalid/api/v1", token="fake")
        self.client.login.return_value = copy.deepcopy(self.status)
        self.status_reads, self.event_reads, self.records = [], [], []
        self.client.get_status.side_effect = self.read_status
        self.client.get_events.side_effect = self.read_events
        self.recorder = Mock()
        self.recorder.redact.side_effect = lambda value: value
        self.recorder.record.side_effect = lambda *args: self.records.append((self.clock.now, args))
        self.monitor = Mock(address="http://fake-monitor.invalid")
        self.monitor.start.return_value = self.monitor
        self.planner = Mock(uses_llm=False)
        self.planner.progress.return_value = {}
        self.collector, self.skills = Mock(), Mock()
        self.runner = None

    def read_status(self, **kwargs):
        self.status_reads.append((self.clock.now, kwargs))
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def read_events(self, **kwargs):
        self.event_reads.append(self.clock.now)
        return []

    def run_main(self, scripts=(), arguments=(), *, paused=False, next_attempt=0, active_decisions=False):
        self.executor = ScriptedExecutor(self.clock, scripts, self.observation, active_decisions)

        def construct(client, settings, sleep_settings, recorder=None, planner=None):
            self.runner = SurvivalHarness(client, settings, sleep_settings, recorder=recorder,
                planner=self.planner, collector=self.collector, skills=self.skills)
            self.runner._executor.shutdown(wait=True)
            self.runner._executor = self.executor
            # Scripted results represent a worker that already observed this
            # life/dimension; a first-ever lifecycle observation is a real wakeup.
            self.runner._update_lifecycle(self.status)
            self.runner._paused = paused
            self.runner._next_attempt = next_attempt
            self.runner.tick = Mock(wraps=self.runner.tick)
            return self.runner

        with ExitStack() as stack:
            stack.enter_context(patch.object(entrypoint, "load_config", return_value=copy.deepcopy(self.config)))
            stack.enter_context(patch.object(entrypoint, "MinecraftAgentClient", return_value=self.client))
            stack.enter_context(patch.object(entrypoint, "FlightRecorder", return_value=self.recorder))
            stack.enter_context(patch("debug.monitor.MonitorServer", return_value=self.monitor))
            stack.enter_context(patch.object(harness_module, "SurvivalHarness", side_effect=construct))
            stack.enter_context(patch.object(harness_module, "time", self.clock))
            stack.enter_context(patch.object(entrypoint, "time", self.clock))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            return entrypoint.main(list(arguments))

    def test_completed_relay_starts_next_cycle_without_two_configured_sleeps(self):
        scripts = [(.35, Decision("retreat_from", {"pos": [9, 64, 0]}, "Retreat", "safety"),
                    SkillResult(True, "Reached relay", {"retreat_complete": False, "evacuation_stage": "relay"})),
                   (.1, Decision("goal_complete", {}, "Verified", "complete"), SkillResult(True, "Done"))]
        self.assertEqual(self.run_main(scripts), 0)
        self.assertEqual(self.executor.started_at, [0, .5])
        self.assertEqual(self.clock.now, .6)
        self.assertTrue(all(interval == .1 for interval in self.clock.sleeps))
        self.assertEqual(self.runner.steps, 2)
        self.assertTrue(self.runner.completed)
        self.client.logout.assert_called_once()

    def test_wait_keeps_its_deadline_and_does_not_repeat_observation_or_planning(self):
        scripts = [(.1, Decision("wait", {"seconds": 4}, "Wait", "observe"), SkillResult(True, "Waiting")),
                   (.1, Decision("goal_complete", {}, "Done", "complete"), SkillResult(True, "Done"))]
        self.assertEqual(self.run_main(scripts), 0)
        self.assertEqual(self.executor.started_at, [0, 4.1])
        # Only the independently throttled idle lifecycle check reads status.
        self.assertEqual(self.status_reads, [(.2, {"timeout_seconds": 2.0})])
        self.collector.collect.assert_not_called()
        self.planner.choose.assert_not_called()
        self.skills.execute.assert_not_called()

    def test_known_failure_backoff_does_not_restart_with_every_fast_tick(self):
        self.clock.stop_at = 4
        failure = SkillResult(False, "Known path failure", error_code="path_not_found")
        scripts = [(.1, Decision("move_to", {"pos": [2, 64, 0]}, "Move", "explore"), failure)]
        self.assertEqual(self.run_main(scripts), 0)
        self.assertEqual(self.executor.started_at, [0])
        self.assertEqual(self.runner.failures, 1)
        self.assertEqual(self.runner._next_attempt, 5.1)
        self.assertEqual(len(self.status_reads), 1)
        self.collector.collect.assert_not_called()
        self.planner.choose.assert_not_called()
        self.client.stop.assert_not_called()

    def test_paused_fast_ticks_leave_debug_heartbeat_and_idle_reads_throttled(self):
        self.clock.stop_at = 6.1
        self.config["debug"]["enable_in_game_commands"] = True
        self.config["debug"]["monitor"]["enabled"] = True
        self.assertEqual(self.run_main(paused=True), 0)
        self.assertGreater(self.runner.tick.call_count, 50)
        self.assertEqual(self.event_reads, [0, 2, 4, 6])
        self.assertEqual([t for t, kw in self.status_reads if not kw], [0, 2, 4, 6])
        self.assertEqual([t for t, kw in self.status_reads if kw], [0, 5])
        self.assertEqual([t for t, args in self.records if args[2] == "HEARTBEAT"], [0, 2, 4, 6])
        self.assertEqual(self.executor.started_at, [])
        self.planner.choose.assert_not_called()
        self.collector.collect.assert_not_called()
        self.client.stop.assert_not_called()
        self.monitor.close.assert_called_once()

    def test_inflight_action_safety_observations_keep_their_configured_cadence(self):
        self.clock.stop_at = 5.3
        scripts = [(5.1, Decision("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "Wood", "wood"),
                    SkillResult(True, "Verified worker result"))]
        self.assertEqual(self.run_main(scripts, active_decisions=True), 0)
        self.assertEqual([t for t, kw in self.status_reads if not kw], [.1, 2.1, 4.1])
        self.assertEqual([t for t, kw in self.status_reads if kw], [5.2])
        self.assertEqual(self.executor.started_at, [0])
        self.planner.choose.assert_not_called()
        self.collector.collect.assert_not_called()
        self.client.stop.assert_not_called()

    def test_failed_debug_poll_is_also_throttled(self):
        self.clock.stop_at = 4.1
        self.config["debug"]["enable_in_game_commands"] = True
        self.config["debug"]["monitor"]["enabled"] = True

        def failing_events(**kwargs):
            self.event_reads.append(self.clock.now)
            raise APIError("network_error", "Fake debug read failure")

        self.client.get_events.side_effect = failing_events
        self.assertEqual(self.run_main(paused=True), 0)
        self.assertEqual(self.event_reads, [0, 2, 4])
        self.assertEqual([t for t, args in self.records if args[2] == "DEBUG_POLL_FAILED"], [0, 2, 4])
        self.assertEqual(self.executor.started_at, [])

    def test_once_keeps_one_result_and_success_or_failure_exit_code(self):
        for success in (True, False):
            with self.subTest(success=success):
                self.clock.now = 0
                result = SkillResult(success, "One result", error_code=None if success else "path_not_found")
                scripts = [(.1, Decision("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "Wood", "wood"), result)]
                self.assertEqual(self.run_main(scripts, ["--once"]), 0 if success else 1)
                self.assertEqual(self.executor.started_at, [0])
                self.assertEqual(self.runner.steps, 1)
                self.assertTrue(all(not call.kwargs["automatic"] for call in self.runner.tick.call_args_list))
                self.assertTrue(self.runner._closed)

    def test_sleep_mode_keeps_configured_loop_and_debug_cadence(self):
        self.config["agent"]["mode"] = "sleep"
        self.config["sleep"]["poll_interval_seconds"] = 2
        self.config["debug"]["enable_in_game_commands"] = True
        self.clock.stop_at = 6
        self.assertEqual(self.run_main(), 0)
        self.assertIsNone(self.runner)
        self.assertEqual(self.clock.sleeps, [2, 2, 2])
        self.assertEqual(self.event_reads, [0, 2, 4])
        self.assertEqual([t for t, _ in self.status_reads], [0, 2, 4])
        self.client.stop.assert_not_called()
        self.client.logout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
