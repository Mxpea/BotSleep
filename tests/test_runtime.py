import threading
import time
import unittest
from unittest.mock import Mock

from core.config import DEFAULTS
from core.sleep_controller import SleepController
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


if __name__ == "__main__":
    unittest.main()
