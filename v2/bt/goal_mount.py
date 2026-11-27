"""L2 ActiveGoalMount: executes a mounted L3 stateless GoalTaskProtocol (<= 1 write per tick)."""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status


@runtime_checkable
class GoalTaskProtocol(Protocol):
    name: str

    def tick(self, bb: Blackboard) -> str:
        """Execute at most ONE bounded step. Returns RUNNING | COMPLETED | FAILED_LOCAL | NEEDS_AI."""

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory reset on preemption. MUST NOT perform network I/O."""


class ActiveGoalMount(Node):
    """Mounts an active L3 stateless recursive Task inside the L2 PrioritySelector."""

    def __init__(self, goal: Optional[GoalTaskProtocol] = None) -> None:
        super().__init__("ActiveGoalMount")
        self._goal: Optional[GoalTaskProtocol] = goal

    @property
    def goal(self) -> Optional[GoalTaskProtocol]:
        return self._goal

    def mount(self, goal: Optional[GoalTaskProtocol], bb: Optional[Blackboard] = None) -> None:
        if self._goal is not None and bb is not None:
            self._goal.stop(bb)
        self._goal = goal
        if bb is not None:
            bb.active_goal_name = goal.name if goal is not None else ""
            bb.goal_status = "RUNNING" if goal is not None else "IDLE"

    def update(self, bb: Blackboard) -> Status:
        if self._goal is None or bb.paused:
            bb.goal_status = "IDLE"
            return Status.FAILURE

        if bb.get("watchdog_stalled"):
            reason = str(bb.get("watchdog_reason") or "watchdog_stalled")
            bb.set("last_failed_goal_name", self._goal.name, writer=self.name)
            self._goal.stop(bb)
            self._goal = None
            bb.active_goal_name = ""
            bb.goal_status = "NEEDS_AI"
            bb.last_failure_reason = reason
            bb.log_event(self.name, "watchdog_abort", reason)
            return Status.FAILURE

        bb.active_goal_name = self._goal.name
        outcome = self._goal.tick(bb)
        bb.goal_status = outcome
        if outcome == "RUNNING":
            return Status.RUNNING
        if outcome == "UNKNOWN":
            # Immediately freeze this tick so AsyncLLMReplanNode is NEVER invoked after an unknown write!
            bb.set("pending_frozen", True, writer=self.name)
            return Status.RUNNING
        if outcome == "COMPLETED":
            self._goal = None
            bb.active_goal_name = ""
            return Status.SUCCESS
        # FAILED_LOCAL or NEEDS_AI: record failed goal name, cleanly unmount so PrioritySelector
        # cannot re-tick it next tick and preempt AsyncLLMReplanNode, then fall through to L4.
        bb.set("last_failed_goal_name", bb.active_goal_name, writer=self.name)
        bb.last_failure_reason = bb.last_failure_reason or outcome
        self._goal.stop(bb)
        self._goal = None
        bb.active_goal_name = ""
        return Status.FAILURE

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory stop forwarded to the mounted goal on preemption."""
        if self._goal is not None:
            self._goal.stop(bb)
        super().stop(bb)
