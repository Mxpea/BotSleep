"""L2 Namespaced Blackboard + bounded ActivityStream for reactive BT state sharing."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import time
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class ActivityEntry:
    tick: int
    timestamp: float
    node: str
    key: str
    op: str  # "write" | "event" | "preempt"
    summary: str = ""


class Blackboard:
    """Shared per-tick observation + runtime memory with bounded audit stream."""

    def __init__(self, *, max_activity: int = 200) -> None:
        self.tick_index: int = 0
        self.status: Dict[str, Any] = {}
        self.inventory: Dict[str, Any] = {}
        self.inventory_counts: Dict[str, int] = {}
        self.entities: List[Dict[str, Any]] = []
        self.recent_events: List[Dict[str, Any]] = []
        self.last_event_seq: int = 0
        self.last_hurt_at: float = 0.0
        self.last_hurt_source: str = ""
        self.death_positions: List[Tuple[str, List[float]]] = []

        # Active BT & goal telemetry
        self.active_branch: str = "idle"
        self.active_goal_name: str = ""
        self.goal_status: str = "IDLE"  # IDLE | RUNNING | COMPLETED | FAILED_LOCAL | NEEDS_AI
        self.last_failure_reason: str = ""
        self.last_action_outcome: str = ""
        self.writes_this_tick: int = 0
        self.paused: bool = False
        self.stop_requested: bool = False

        # Namespaced arbitrary key-value slots
        self._data: Dict[str, Any] = {}
        self.activity: Deque[ActivityEntry] = deque(maxlen=max_activity)

    def set(self, key: str, value: Any, *, writer: str = "system") -> None:
        self._data[key] = value
        self.activity.append(ActivityEntry(self.tick_index, time.time(), writer, key, "write", str(type(value).__name__)))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def log_event(self, node: str, op: str, summary: str) -> None:
        self.activity.append(ActivityEntry(self.tick_index, time.time(), node, "", op, summary))

    def begin_tick(self) -> None:
        self.tick_index += 1
        self.writes_this_tick = 0

    @property
    def pos(self) -> Tuple[float, float, float]:
        raw = self.status.get("pos") or [0.0, 64.0, 0.0]
        return (float(raw[0]), float(raw[1]), float(raw[2]))

    @property
    def dimension(self) -> str:
        return str(self.status.get("dimension") or "overworld")

    @property
    def health(self) -> float:
        return float(self.status.get("health", 20.0))

    @property
    def food(self) -> int:
        return int(self.status.get("food", 20))

    @property
    def oxygen(self) -> int:
        return int(self.status.get("oxygen", 20))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "tick": self.tick_index,
            "pos": list(self.pos),
            "dimension": self.dimension,
            "health": self.health,
            "food": self.food,
            "oxygen": self.oxygen,
            "active_branch": self.active_branch,
            "active_goal": self.active_goal_name,
            "goal_status": self.goal_status,
            "last_failure": self.last_failure_reason,
            "inventory_items": len(self.inventory_counts),
        }
