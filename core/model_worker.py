"""One pending planning call; local safety need not wait for a remote model."""

import copy
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

from agent.brain import LLMError
from skills import SkillResult


class SerialModelWorker:
    def __init__(self, planner):
        self.planner = planner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-request")
        self._future = None
        self._cancel = threading.Event()

    @property
    def pending(self):
        return self._future is not None

    @property
    def done(self):
        return self._future is not None and self._future.done()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def start(self, observation, context):
        if self.pending:
            raise LLMError("llm_busy", "A previous planning request has not been drained")
        self._cancel = threading.Event()
        self._future = self._executor.submit(self.planner.choose, copy.deepcopy(observation),
                                             copy.deepcopy(context), cancel_event=self._cancel)

    def cancel(self):
        # Never pretend that cancellation of local consumption stopped the
        # provider. Keep the future and its global gate until the HTTP ends.
        self._cancel.set()

    def take(self, observation):
        if not self.done:
            return None
        future, self._future = self._future, None
        try:
            decision = future.result()
        except CancelledError:
            return None
        except LLMError as error:
            if error.code == "llm_cancelled" and self.cancelled:
                return None
            raise
        if self.cancelled:
            # A response could win the cancellation race and open a native tool
            # transaction. Close it with truthful no-execution feedback.
            self.planner.observe_result(decision, SkillResult(False,
                "Local safety superseded this model proposal; no game action was sent",
                {"executed": False}, "safety_changed"), observation)
            return None
        return decision

    def close(self, observation=None):
        self.cancel()
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self.done:
            try:
                self.take(observation or {})
            except LLMError:
                pass  # The provider gate still persists an unknown transport.
