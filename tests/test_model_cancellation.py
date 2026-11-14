"""Planner/worker/gate cancellation with mocked HTTP; no provider or game I/O."""

import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent.brain import LLMError, OpenAIBrain
from agent.llm_planner import LLMSurvivalPlanner
from core.config import DEFAULTS
from core.model_gate import ModelRequestGate
from core.model_worker import SerialModelWorker


class Completion:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "Wait",
            "tool_calls": [{"id": "cancel-test", "type": "function", "function": {
                "name": "wait", "arguments": '{"seconds":1}'}}]}}]}


class ModelCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gate = ModelRequestGate("https://offline.invalid/v1", "offline-key", directory=self.temp.name)
        self.brain = OpenAIBrain("https://offline.invalid/v1", "offline-key", "offline-model", request_gate=self.gate)
        self.planner = LLMSurvivalPlanner({"enabled": True, "model": "offline-model"},
            copy.deepcopy(DEFAULTS["survival"]), brain=self.brain)
        self.worker = SerialModelWorker(self.planner)
        self.observation = {"fresh": True, "status": {}, "counts": {}}

    def wait_done(self, seconds=1):
        deadline = time.monotonic() + seconds
        while not self.worker.done and time.monotonic() < deadline:
            time.sleep(.001)
        return self.worker.done

    def test_cancel_while_waiting_for_provider_slot_never_sends_queued_request(self):
        entered = threading.Event()
        original = self.brain.chat_completion

        def tracked(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)

        with patch.object(self.brain, "chat_completion", side_effect=tracked), \
                patch("agent.brain.requests.post", return_value=Completion()) as post:
            try:
                # Another request owns this credential's slot. Cancellation must
                # finish the waiter without touching that owner's inflight debt.
                with self.gate.request():
                    self.worker.start(self.observation, {})
                    self.assertTrue(entered.wait(1))
                    self.worker.cancel()
                    drained_before_release = self.wait_done(.5)
                    self.assertEqual(self.gate.snapshot()["state"], "inflight")
                    post.assert_not_called()
                self.assertTrue(self.wait_done())
                self.assertIsNone(self.worker.take(self.observation))
                self.assertTrue(drained_before_release, "Cancelled planner remained queued behind the provider slot")
                post.assert_not_called()
                self.assertEqual(self.gate.snapshot()["state"], "idle")
                self.assertIsNone(self.planner._pending)
            finally:
                self.worker.close(self.observation)

    def test_cancel_dispatched_http_keeps_slot_until_response_and_rejects_new_start(self):
        entered, release = threading.Event(), threading.Event()

        def post(*args, **kwargs):
            entered.set()
            release.wait(2)
            return Completion()

        with patch("agent.brain.requests.post", side_effect=post) as send:
            try:
                self.worker.start(self.observation, {})
                self.assertTrue(entered.wait(1))
                self.worker.cancel()
                self.assertTrue(self.worker.pending)
                self.assertFalse(self.worker.done)
                self.assertEqual(self.gate.snapshot()["state"], "inflight")
                with self.assertRaises(LLMError) as caught:
                    self.worker.start(self.observation, {})
                self.assertEqual(caught.exception.code, "llm_busy")
                release.set()
                self.assertTrue(self.wait_done())
                self.assertIsNone(self.worker.take(self.observation))
                self.assertEqual(send.call_count, 1)
                self.assertEqual(self.gate.snapshot()["state"], "idle")
                self.assertIsNone(self.planner._pending)
            finally:
                release.set()
                self.worker.close(self.observation)


if __name__ == "__main__":
    unittest.main()
