import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.model_gate import ModelGateError, ModelRequestGate


class TransportTimeout(Exception):
    code = "llm_timeout"


class KnownHttpFailure(Exception):
    code = "llm_http_error"


class ModelGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def gate(self, **kwargs):
        return ModelRequestGate("https://example.invalid/v1", "private-key", directory=self.temporary.name, **kwargs)

    def test_two_threads_and_instances_never_overlap(self):
        active = 0
        peak = 0
        errors = []
        barrier = threading.Barrier(2)
        def worker():
            nonlocal active, peak
            try:
                gate = self.gate()
                barrier.wait()
                with gate.request():
                    active += 1
                    peak = max(peak, active)
                    time.sleep(.05)
                    active -= 1
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(peak, 1)
        self.assertEqual(self.gate().snapshot()["state"], "idle")

    def test_timeout_survives_restart_until_explicit_acknowledgement(self):
        with self.assertRaises(TransportTimeout):
            with self.gate().request(): raise TransportTimeout()
        gate = self.gate()
        with self.assertRaises(ModelGateError) as caught:
            with gate.request(): self.fail("Uncertain request was replayed")
        self.assertEqual(caught.exception.code, "llm_request_uncertain")
        gate.acknowledge_provider_idle()
        with gate.request(): pass

    def test_known_http_failure_releases_slot(self):
        with self.assertRaises(KnownHttpFailure):
            with self.gate().request(): raise KnownHttpFailure()
        with self.gate().request(): pass

    def test_crashed_inflight_ledger_is_not_automatically_recovered(self):
        gate = self.gate()
        gate._write({"state": "inflight", "pid": 99999999, "started_at": 1})
        with self.assertRaises(ModelGateError) as caught:
            with gate.request(): self.fail("Dead PID does not prove provider idle")
        self.assertEqual(caught.exception.code, "llm_request_uncertain")

    def test_corrupt_ledger_fails_closed_without_erasing_it(self):
        gate = self.gate()
        gate.state_path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ModelGateError):
            with gate.request(): pass
        self.assertEqual(gate.state_path.read_text(), "broken")

    def test_cancelled_wait_does_not_change_ledger_or_send_request(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(ModelGateError) as caught:
            with self.gate().request(event): self.fail("Cancelled request started")
        self.assertEqual(caught.exception.code, "llm_cancelled")
        self.assertFalse(self.gate().state_path.exists())

    def test_os_lock_prevents_a_second_process_and_contains_no_credentials(self):
        code = ("import sys; from core.model_gate import ModelRequestGate,ModelGateError; "
                "g=ModelRequestGate('https://example.invalid/v1','private-key',directory=sys.argv[1],wait_seconds=.1)\n"
                "try:\n with g.request(): print('entered')\n"
                "except ModelGateError as e: print(e.code)\n")
        with self.gate().request():
            result = subprocess.run([sys.executable, "-B", "-c", code, self.temporary.name],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "llm_busy")
        ledger = self.gate().state_path.read_text()
        self.assertNotIn("private-key", ledger)
        self.assertNotIn("example.invalid", ledger)


if __name__ == "__main__": unittest.main()
