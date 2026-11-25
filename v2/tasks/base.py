"""L3b AltoClef-style stateless recursive Task ABC and MacroSequenceTask.

Every Task implements:
- `is_finished(bb) -> bool`: pure inspection of Blackboard/world state.
- `on_tick(bb) -> str`: self-inspects and executes AT MOST ONE atomic primitive write.
- `stop(bb) -> None`: 100% pure-memory reset; NEVER performs network I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from v2.bt.blackboard import Blackboard


class Task(ABC):
    """Stateless recursive goal node satisfying L2 GoalTaskProtocol."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._active_child: Optional["Task"] = None
        self.local_failures: int = 0

    def tick(self, bb: Blackboard) -> str:
        if self.is_finished(bb):
            self._clear_child(bb)
            return "COMPLETED"
        return self.on_tick(bb)

    def run_child(self, bb: Blackboard, child: "Task") -> str:
        if self._active_child is not None and self._active_child.name != child.name:
            self._active_child.stop(bb)
        self._active_child = child
        res = child.tick(bb)
        if res == "COMPLETED":
            self._active_child = None
            return "RUNNING"  # Parent will advance on next macro-tick
        return res

    def _clear_child(self, bb: Blackboard) -> None:
        if self._active_child is not None:
            self._active_child.stop(bb)
            self._active_child = None

    @abstractmethod
    def is_finished(self, bb: Blackboard) -> bool:
        """Return True iff the world/inventory already satisfies this goal."""

    @abstractmethod
    def on_tick(self, bb: Blackboard) -> str:
        """Advance at most ONE atomic write action toward this goal."""

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory stop propagated down active children. Zero network calls."""
        self._clear_child(bb)


class MacroSequenceTask(Task):
    """Stateless sequence: on each tick runs the first sub-task whose is_finished(bb) is False."""

    def __init__(self, name: str, subtasks: Sequence[Task]) -> None:
        super().__init__(name)
        self.subtasks: List[Task] = list(subtasks)

    def is_finished(self, bb: Blackboard) -> bool:
        return all(t.is_finished(bb) for t in self.subtasks)

    def on_tick(self, bb: Blackboard) -> str:
        for task in self.subtasks:
            if not task.is_finished(bb):
                res = self.run_child(bb, task)
                if res in {"FAILED_LOCAL", "NEEDS_AI"}:
                    return res
                return "RUNNING"
        return "COMPLETED"
