import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import main as entrypoint
from agent.survival_planner import Decision
from core.config import DEFAULTS
from core.sleep_controller import SleepController
from core.survival_harness import SurvivalHarness
from core.survival_state import SurvivalJournal
from debug.chat_debugger import ChatDebugger
from skills import SkillResult


def await_result(controller):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = controller.tick(automatic=False)
        if result is not None:
            return result
        time.sleep(0.005)
    raise AssertionError("worker did not finish")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(username="SleepBot")
        self.client.get_status.return_value = {"ok": True, "result": {
            "ready": True, "alive": True, "busy": None, "is_day": False}}

    def test_single_worker_and_pause_prevents_automatic_restart(self):
        entered = threading.Event()

        def execute(**kwargs):
            entered.set()
            self.assertTrue(kwargs["cancel_event"].wait(2))
            return SkillResult(False, "cancelled", error_code="cancelled")

        skill = Mock()
        skill.execute.side_effect = execute
        controller = SleepController(self.client, DEFAULTS["sleep"], skill=skill)
        self.addCleanup(controller.close)
        controller.tick()
        self.assertTrue(entered.wait(1))
        self.assertFalse(controller.request_sleep())
        controller.tick()
        self.assertEqual(skill.execute.call_count, 1)
        controller.pause()
        self.client.stop.assert_called_once()
        self.assertEqual(await_result(controller).error_code, "cancelled")
        controller._next_attempt = 0
        controller.tick()
        self.assertEqual(skill.execute.call_count, 1)

    def test_daytime_or_busy_does_not_start_skill(self):
        skill = Mock()
        controller = SleepController(self.client, DEFAULTS["sleep"], skill=skill)
        self.addCleanup(controller.close)
        for change in ({"is_day": True}, {"busy": "move_to"}, {"ready": False}, {"alive": False}):
            status = {"ready": True, "alive": True, "busy": None, "is_day": False, **change}
            self.client.get_status.return_value = {"ok": True, "result": status}
            controller.tick()
        skill.execute.assert_not_called()

    def test_failed_skill_is_logged_and_throttled(self):
        skill = Mock()
        skill.execute.return_value = SkillResult(False, "no bed", error_code="no_bed_nearby")
        recorder = Mock()
        controller = SleepController(self.client, DEFAULTS["sleep"], recorder=recorder, skill=skill)
        self.addCleanup(controller.close)
        controller.request_sleep()
        result = await_result(controller)
        self.assertEqual(result.error_code, "no_bed_nearby")
        controller.tick()
        self.assertEqual(skill.execute.call_count, 1)
        events = [call.args[2] for call in recorder.record.call_args_list]
        self.assertEqual(events, ["SLEEP_STARTED", "SLEEP_RESULT"])

    def test_close_cancels_then_joins_worker(self):
        entered = threading.Event()
        finished = threading.Event()

        def execute(**kwargs):
            entered.set()
            kwargs["cancel_event"].wait(2)
            finished.set()
            return SkillResult(False, "cancelled", error_code="cancelled")

        skill = Mock()
        skill.execute.side_effect = execute
        controller = SleepController(self.client, DEFAULTS["sleep"], skill=skill)
        controller.request_sleep()
        self.assertTrue(entered.wait(1))
        controller.close()
        self.assertTrue(finished.is_set())
        self.assertFalse(controller.busy)
        self.assertFalse(controller.request_sleep())
        controller.close()

    def test_unknown_controller_settings_are_not_forwarded_to_skill(self):
        skill = Mock()
        skill.execute.return_value = SkillResult(True, "completed")
        settings = {**DEFAULTS["sleep"], "unsupported_option": "private-value"}
        controller = SleepController(self.client, settings, skill=skill)
        self.addCleanup(controller.close)
        controller.request_sleep()
        self.assertTrue(await_result(controller).success)
        self.assertNotIn("unsupported_option", skill.execute.call_args.kwargs)
        self.assertEqual(set(skill.execute.call_args.kwargs), set(controller.SKILL_OPTIONS) | {"cancel_event"})


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(username="SleepBot")
        self.stop = Mock()
        self.sleep = Mock(return_value=True)
        self.debugger = ChatDebugger(self.client, allowed_usernames=["TestPlayer"],
                                     on_stop=self.stop, on_sleep=self.sleep)

    def send(self, text, seq=1):
        self.client.get_events.return_value = [{"type": "chat", "text": text, "seq": seq}]
        self.debugger.poll_and_dispatch()

    def test_formatted_allowlisted_chat_reaches_real_callbacks(self):
        self.send("<TestPlayer> #stop")
        self.stop.assert_called_once()
        self.send("<TestPlayer> #sleep", 2)
        self.sleep.assert_called_once()
        self.assertEqual(self.debugger.last_seq, 2)

    def test_untrusted_anonymous_and_non_commands_are_ignored(self):
        for message in ("<OtherPlayer> #stop", "#stop", "<SleepBot> #stop", "<TestPlayer> please #stop"):
            self.send(message)
        self.stop.assert_not_called()
        self.client.chat.assert_not_called()

    def test_reply_rate_limit_does_not_hide_completed_stop(self):
        self.client.chat.side_effect = RuntimeError("rate limited")
        self.send("<TestPlayer> #stop")
        self.stop.assert_called_once()


class SurvivalResumeEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = copy.deepcopy(DEFAULTS)
        self.config["agent"]["mode"] = "survival"
        self.config["llm"]["enabled"] = False
        self.config["debug"]["flight_recorder"]["enabled"] = False
        self.config["debug"]["monitor"].update(enabled=False, console=False)
        self.config["survival"].update(state_dir=self.temp.name, max_steps=1,
                                         max_runtime_seconds=5, poll_interval_seconds=0.001)
        self.client = Mock(username="ResumeTestBot", base_url="http://fake-game.invalid/api/v1", token="fixture-token")
        self.status = {"ready": True, "alive": True, "busy": None, "health": 20, "food": 20,
                       "gamemode": "survival", "dimension": "overworld", "pos": [8, 64, 3],
                       "is_day": True, "thundering": False, "on_ground": True}
        self.client.login.return_value = copy.deepcopy(self.status)
        self.client.get_status.return_value = {"ok": True, "result": self.status}
        self.journal = SurvivalJournal(self.temp.name, endpoint=self.client.base_url,
                                       username=self.client.username, world_id=self.config["survival"]["world_id"])
        self.pending = {"decision": {"name": "gather_block", "args": {"pos": [2, 64, 0], "expected_name": "oak_log"}},
                        "before_counts": {"oak_log": 0}}
        self.journal.update(pending=copy.deepcopy(self.pending))
        self.collector = Mock()
        self.collector.collect.return_value = {"fresh": True, "status": copy.deepcopy(self.status),
                                               "counts": {"oak_log": 1}, "blocks": [], "entities": [], "inventory": []}
        self.planner = Mock()
        self.planner.progress.return_value = {}
        self.planner.choose.return_value = Decision("wait", {"seconds": 1}, "Use new state", "observe")
        self.skills = Mock()
        self.controller = None

    def run_main(self, arguments, interrupt_idle=False):
        def construct(client, settings, sleep_settings, recorder=None, planner=None):
            self.controller = SurvivalHarness(client, settings, sleep_settings,
                planner=self.planner, collector=self.collector, skills=self.skills, journal=self.journal)
            self.controller.resume = Mock(wraps=self.controller.resume)
            return self.controller

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(entrypoint, "load_config", return_value=copy.deepcopy(self.config)), \
                patch.object(entrypoint, "MinecraftAgentClient", return_value=self.client) as client_factory, \
                patch("core.survival_harness.SurvivalHarness", side_effect=construct), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            if interrupt_idle:
                # No worker starts while the persisted action remains paused.
                with patch.object(entrypoint.time, "sleep", side_effect=KeyboardInterrupt):
                    code = entrypoint.main(arguments)
            else:
                code = entrypoint.main(arguments)
        return code, stdout.getvalue() + stderr.getvalue(), client_factory

    def test_default_restart_remains_paused_and_preserves_pending(self):
        code, output, _ = self.run_main([], interrupt_idle=True)
        self.assertEqual(code, 0, output)
        self.assertTrue(self.controller.paused)
        self.controller.resume.assert_not_called()
        self.collector.collect.assert_not_called()
        self.skills.execute.assert_not_called()
        self.assertEqual(json.loads(self.journal.path.read_text())["pending"], self.pending)
        self.client.logout.assert_called_once()

    def test_explicit_resume_reobserves_and_replans_without_replaying_saved_action(self):
        code, output, _ = self.run_main(["--resume"])
        self.assertEqual(code, 2, output)  # One new decision consumed the budget.
        self.controller.resume.assert_called_once()
        self.collector.collect.assert_called_once()
        self.assertEqual(self.planner.choose.call_args.args[0]["status"]["pos"], [8, 64, 3])
        self.assertEqual(self.planner.choose.call_args.args[0]["counts"], {"oak_log": 1})
        self.skills.execute.assert_not_called()
        self.assertIsNone(json.loads(self.journal.path.read_text())["pending"])
        self.client.logout.assert_called_once()

    def test_once_does_not_implicitly_resume_an_interrupted_action(self):
        code, output, _ = self.run_main(["--once"])
        self.assertEqual(code, 1, output)
        self.assertIn("--resume", output)
        self.controller.resume.assert_not_called()
        self.collector.collect.assert_not_called()
        self.skills.execute.assert_not_called()
        self.assertEqual(self.journal.state["pending"], self.pending)
        self.client.logout.assert_called_once()

    def test_once_with_explicit_resume_runs_only_a_fresh_decision(self):
        code, output, _ = self.run_main(["--once", "--resume"])
        self.assertEqual(code, 0, output)
        self.collector.collect.assert_called_once()
        self.planner.choose.assert_called_once()
        self.skills.execute.assert_not_called()
        self.assertIsNone(self.journal.state["pending"])
        self.client.logout.assert_called_once()

    def test_resume_rejects_sleep_mode_before_login_or_controller_creation(self):
        for config_mode, arguments in (("sleep", ["--resume"]),
                                       ("survival", ["--mode", "sleep", "--resume"])):
            with self.subTest(config_mode=config_mode):
                self.config["agent"]["mode"] = config_mode
                code, output, client_factory = self.run_main(arguments)
                self.assertEqual(code, 1, output)
                self.assertIn("only available in survival mode", output)
                client_factory.assert_not_called()
                self.assertIsNone(self.controller)

    def test_mode_override_allows_explicit_resume_and_config_check_stays_offline(self):
        self.config["agent"]["mode"] = "sleep"
        code, output, client_factory = self.run_main(["--mode", "survival", "--resume", "--check-config"])
        self.assertEqual(code, 0, output)
        client_factory.assert_not_called()
        self.assertIsNone(self.controller)


if __name__ == "__main__":
    unittest.main()
