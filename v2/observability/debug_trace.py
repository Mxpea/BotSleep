"""L4/Observability DebugStateTracer: timestamp-aligned state-machine + LLM + API trace."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import block_coord
from v2.observability.heatmap import describe_task_chain, normalize_fail_reason

_TRACE_EVENTS = frozenset({
    "LLM_IO",
    "LLM_RESPONSE",
    "LLM_DISCARDED",
    "LLM_ERROR",
    "API_ERROR",
    "COMPETITION_SCORE",
    "CONNECTED",
    "STARTUP",
})

_KEY_INV_KEYWORDS = (
    "pickaxe", "sword", "axe", "shovel", "bucket", "flint", "iron", "diamond",
    "gold", "obsidian", "coal", "log", "planks", "cobblestone", "deepslate",
    "furnace", "crafting_table", "bed", "beef", "porkchop", "mutton", "bread", "torch",
)


def _compact_inventory(counts: Dict[str, int], *, limit: int = 16) -> Dict[str, int]:
    if not counts:
        return {}
    priority: List[Tuple[int, str, int]] = []
    for k, v in counts.items():
        iv = int(v)
        if iv <= 0:
            continue
        is_key = 0 if any(kw in k for kw in _KEY_INV_KEYWORDS) else 1
        priority.append((is_key, k, iv))
    priority.sort()
    return {k: v for _, k, v in priority[:limit]}


class DebugStateTracer:
    """Writes millisecond-aligned state-machine transitions and key LLM/error events to JSONL."""

    def __init__(
        self,
        output_dir: Path | str,
        *,
        username: str = "",
        enabled: bool = True,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.username = str(username or "")
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self.trace_path: Optional[Path] = None
        if self.enabled:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self.trace_path = self.output_dir / f"v2_debug_trace_{stamp}.jsonl"

        self._prev_state: Optional[Tuple[str, str, str, str, str, bool, int]] = None
        self._prev_state_dict: Optional[Dict[str, Any]] = None
        self._last_activity_seen_len: int = 0
        self._last_activity_ts: float = 0.0

    def _append_entry(self, entry: Dict[str, Any]) -> None:
        if not self.enabled or self.trace_path is None:
            return
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with self.trace_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def _collect_preempts(self, bb: Blackboard) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for act in list(bb.activity):
            if act.timestamp > self._last_activity_ts:
                if act.op == "preempt":
                    out.append({"tick": act.tick, "node": act.node, "summary": act.summary})
        if bb.activity:
            self._last_activity_ts = max(self._last_activity_ts, bb.activity[-1].timestamp)
        return out[-6:]

    def record_tick(self, tick_idx: int, bb: Blackboard, goal_obj: Any, elapsed_ms: float) -> Optional[Dict[str, Any]]:
        """Emit a STATE_TRANSITION line when BT branch, task chain, status, fail, dim, freeze, or death changes."""
        branch = str(bb.active_branch or "idle")
        task_chain = describe_task_chain(goal_obj)
        goal_status = str(bb.goal_status or "IDLE")
        fail_norm = normalize_fail_reason(bb.last_failure_reason)
        dim = bb.dimension
        frozen = bool(bb.get("pending_frozen"))
        death_count = len(bb.death_positions)

        state_key = (branch, task_chain, goal_status, fail_norm, dim, frozen, death_count)
        new_dict = {
            "branch": branch,
            "task_chain": task_chain,
            "goal_status": goal_status,
            "fail": fail_norm,
            "dim": dim,
            "frozen": frozen,
            "deaths": death_count,
        }
        preempts = self._collect_preempts(bb)
        should_emit = (tick_idx == 0) or (self._prev_state != state_key) or bool(preempts)
        if not should_emit:
            return None

        now = time.time()
        pos = bb.pos
        entry = {
            "ts": round(now, 3),
            "ts_ms": int(now * 1000),
            "event": "STATE_TRANSITION",
            "player": self.username,
            "data": {
                "tick": int(tick_idx),
                "dim": dim,
                "pos": [round(float(pos[0]), 2), round(float(pos[1]), 2), round(float(pos[2]), 2)],
                "block": list(block_coord(pos)),
                "hp": round(float(bb.health), 1),
                "food": int(bb.food),
                "from": self._prev_state_dict or {},
                "to": new_dict,
                "outcome": str(bb.last_action_outcome or ""),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "preempts": preempts,
                "inv": _compact_inventory(bb.inventory_counts),
            },
        }
        self._prev_state = state_key
        self._prev_state_dict = new_dict
        self._append_entry(entry)
        return entry

    def on_flight_event(self, entry: Dict[str, Any]) -> None:
        """Forward key LLM/API_ERROR/score events with millisecond timestamps into the unified debug trace."""
        if not isinstance(entry, dict):
            return
        etype = str(entry.get("event") or "")
        if etype not in _TRACE_EVENTS:
            return
        ts_ms = entry.get("ts_ms")
        if not isinstance(ts_ms, int):
            ts_val = entry.get("ts")
            ts_ms = int(float(ts_val) * 1000) if isinstance(ts_val, (int, float)) else int(time.time() * 1000)
        forwarded = {
            "ts": entry.get("ts", round(ts_ms / 1000.0, 3)),
            "ts_ms": ts_ms,
            "event": etype,
            "component": entry.get("component", ""),
            "level": entry.get("level", "INFO"),
            "player": self.username,
            "data": entry.get("data") or {},
        }
        self._append_entry(forwarded)
