"""L2 LoopWatchdog: detects cross-step oscillation and no-progress stalls across BT ticks."""

from __future__ import annotations

from collections import deque
import math
from typing import Deque, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status


class LoopWatchdogNode(Node):
    """Tracks recent (pos_bucket, goal, outcome) signatures to flag oscillation before infinite loops."""

    def __init__(self, *, window: int = 12, max_repeats: int = 6) -> None:
        super().__init__("LoopWatchdog")
        self.window = max(4, int(window))
        self.max_repeats = max(3, int(max_repeats))
        self._signatures: Deque[Tuple[int, int, int, str, str]] = deque(maxlen=self.window)
        self.stalled: bool = False
        self.stall_signature: Optional[str] = None

    def update(self, bb: Blackboard) -> Status:
        px, py, pz = bb.pos
        sig = (math.floor(px), math.floor(py), math.floor(pz), bb.active_goal_name, bb.last_action_outcome)
        self._signatures.append(sig)
        # Count occurrences of current signature in recent window (ignore idle)
        if bb.active_goal_name:
            repeats = sum(1 for s in self._signatures if s == sig)
            if repeats >= self.max_repeats:
                self.stalled = True
                self.stall_signature = f"{bb.active_goal_name}@{sig[:3]}:{bb.last_action_outcome}"
                bb.set("watchdog_stalled", True, writer=self.name)
                bb.set("watchdog_reason", self.stall_signature, writer=self.name)
                return Status.SUCCESS
        self.stalled = False
        self.stall_signature = None
        bb.set("watchdog_stalled", False, writer=self.name)
        return Status.SUCCESS

    def reset(self) -> None:
        self._signatures.clear()
        self.stalled = False
        self.stall_signature = None
