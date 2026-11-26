"""L2 Zero-dependency reactive Behavior Tree microkernel.

NON-NEGOTIABLE INVARIANT:
`Node.stop(bb)` MUST BE 100% PURE MEMORY. It is strictly forbidden to issue any HTTP read/write
inside `stop()`. Preemption by `PendingUnknownGuard` or `HardSafetyBranch` calls `stop()` on
previously active lower-priority branches; pure-memory `stop()` guarantees unknown-write freeze.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Optional

from v2.bt.blackboard import Blackboard


class Status(Enum):
    SUCCESS = "SUCCESS"
    RUNNING = "RUNNING"
    FAILURE = "FAILURE"


class Node(ABC):
    """Base Behavior Tree node with pure-memory stop() lifecycle."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.last_status: Optional[Status] = None

    def tick(self, bb: Blackboard) -> Status:
        status = self.update(bb)
        self.last_status = status
        return status

    @abstractmethod
    def update(self, bb: Blackboard) -> Status:
        """Execute one reactive tick."""

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory reset when preempted or completed. NEVER perform network I/O here."""
        self.last_status = None


class PrioritySelector(Node):
    """Reactive memory=False Selector: re-evaluates children from index 0 every tick.

    When a higher-priority child returns RUNNING or SUCCESS, all previously RUNNING
    lower-priority children have their pure-memory `stop(bb)` invoked immediately.
    """

    def __init__(self, name: str, children: List[Node]) -> None:
        super().__init__(name)
        self.children = list(children)
        self._active_index: Optional[int] = None

    def update(self, bb: Blackboard) -> Status:
        for idx, child in enumerate(self.children):
            status = child.tick(bb)
            if status != Status.FAILURE:
                if self._active_index is not None and self._active_index != idx:
                    prev = self.children[self._active_index]
                    prev.stop(bb)
                    bb.log_event(self.name, "preempt", f"{child.name} preempted {prev.name}")
                self._active_index = idx if status == Status.RUNNING else None
                bb.active_branch = child.name
                return status
        if self._active_index is not None:
            self.children[self._active_index].stop(bb)
            self._active_index = None
        return Status.FAILURE

    def stop(self, bb: Blackboard) -> None:
        if self._active_index is not None:
            self.children[self._active_index].stop(bb)
            self._active_index = None
        super().stop(bb)


class Sequence(Node):
    """Reactive Sequence: runs children in order while each returns SUCCESS."""

    def __init__(self, name: str, children: List[Node]) -> None:
        super().__init__(name)
        self.children = list(children)

    def update(self, bb: Blackboard) -> Status:
        for child in self.children:
            status = child.tick(bb)
            if status != Status.SUCCESS:
                return status
        return Status.SUCCESS

    def stop(self, bb: Blackboard) -> None:
        for child in self.children:
            child.stop(bb)
        super().stop(bb)
