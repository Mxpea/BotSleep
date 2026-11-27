"""L3b Deep Dark sneak & safe container looting tasks (<= 1 write/tick, pure-memory stop)."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, DEEP_DARK_BLOCKS, LOOT_WHITELIST_ITEMS, dist3
from v2.primitives.base import PrimitiveContext
from v2.primitives.container import ContainerPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.base import Task


_VIBRATION_SENSORS: List[str] = ["sculk_sensor", "calibrated_sculk_sensor", "sculk_shrieker"]
_CONTAINER_TYPES: List[str] = ["chest", "barrel"]
_TRAP_BLOCKS = frozenset({"tnt", "stone_pressure_plate"})
_LOOTED_TTL_SECONDS = 600.0


def _as_coord3(raw: Any) -> Optional[Coord3]:
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        return (int(raw[0]), int(raw[1]), int(raw[2]))
    return None


def _sector_key(dimension: str, coord: Coord3) -> Tuple[str, int, int]:
    return (dimension, math.floor(coord[0] / 64.0), math.floor(coord[2] / 64.0))


class AvoidVibrationTask(Task):
    """Deep Dark vibration avoidance: sneak via posture, approach within 8m, take 1-2 sneak steps, reset sneak."""

    def __init__(self, ctx: PrimitiveContext, *, radius: int = 32) -> None:
        super().__init__("avoid_vibration")
        self.ctx = ctx
        self.radius = int(radius)
        self._nav = NavigatePrimitive(ctx)
        self._target_sensor: Optional[Coord3] = None
        self._sneak_engaged: bool = False
        self._sneak_steps_done: int = 0
        self._pending_failure: Optional[str] = None
        self._done: bool = False

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory reset ONLY; never emits network writes inside stop()."""
        self._target_sensor = None
        self._sneak_engaged = False
        self._sneak_steps_done = 0
        self._pending_failure = None
        self._done = False
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        return self._done and bool(bb.get("avoid_vibration_done"))

    def _reset_sneak_write(self, bb: Blackboard) -> str:
        out = self.ctx.execute_write(
            bb,
            "posture",
            {"sneak": False},
            lambda: self.ctx.client.posture(sneak=False),
        )
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        self._sneak_engaged = False
        if out.status == "FAILED_LOCAL":
            return "FAILED_LOCAL"
        return "OK"

    def on_tick(self, bb: Blackboard) -> str:
        if self._pending_failure is not None:
            if self._sneak_engaged or bool(bb.status.get("sneaking")):
                res = self._reset_sneak_write(bb)
                if res == "UNKNOWN":
                    return "UNKNOWN"
            reason = self._pending_failure
            self._pending_failure = None
            bb.last_failure_reason = reason
            return "FAILED_LOCAL"

        if self._target_sensor is None:
            rows = self.ctx.gateway.scan_blocks(bb.dimension, _VIBRATION_SENSORS, radius=self.radius, limit=8)
            if not rows:
                if self._sneak_engaged or bool(bb.status.get("sneaking")):
                    res = self._reset_sneak_write(bb)
                    if res == "UNKNOWN":
                        return "UNKNOWN"
                bb.last_failure_reason = "avoid_vibration:no_sensor"
                return "FAILED_LOCAL"
            rows.sort(key=lambda r: float(r.get("dist", 999.0)))
            coord = _as_coord3(rows[0].get("pos"))
            if coord is None:
                bb.last_failure_reason = "avoid_vibration:no_sensor"
                return "FAILED_LOCAL"
            self._target_sensor = coord

        if not self._sneak_engaged and not bool(bb.status.get("sneaking")):
            out = self.ctx.execute_write(
                bb,
                "posture",
                {"sneak": True},
                lambda: self.ctx.client.posture(sneak=True),
            )
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "FAILED_LOCAL":
                bb.last_failure_reason = f"avoid_vibration:sneak:{out.reason}"
                return "FAILED_LOCAL"
            self._sneak_engaged = True
            return "RUNNING"
        self._sneak_engaged = True

        sensor = self._target_sensor
        center = (sensor[0] + 0.5, float(sensor[1]), sensor[2] + 0.5)
        dist = dist3(bb.pos, center)
        if dist > 8.0:
            nav = self._nav.step_to(bb, center, tolerance=6.0)
            if nav.status == "UNKNOWN":
                return "UNKNOWN"
            if nav.status == "FAILED_LOCAL":
                self._pending_failure = f"avoid_vibration:{nav.reason}"
                return "RUNNING"
            return "RUNNING"

        if self._sneak_steps_done < 2:
            dx = center[0] - bb.pos[0]
            dz = center[2] - bb.pos[2]
            yaw = math.atan2(-dx, -dz)
            if dist <= 2.2:
                yaw += math.pi / 2.0
            out = self.ctx.execute_write(
                bb,
                "control",
                {"forward": True, "sneak": True, "ms": 300},
                lambda _y=yaw: self.ctx.client.control(forward=True, sneak=True, yaw=_y, ms=300),
            )
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "FAILED_LOCAL":
                self._pending_failure = f"avoid_vibration:step:{out.reason}"
                return "RUNNING"
            self._sneak_steps_done += 1
            return "RUNNING"

        res = self._reset_sneak_write(bb)
        if res == "UNKNOWN":
            return "UNKNOWN"
        if res == "FAILED_LOCAL":
            bb.last_failure_reason = "avoid_vibration:unsneak_failed"
            return "FAILED_LOCAL"
        self._done = True
        bb.set("avoid_vibration_done", True, writer=self.name)
        return "COMPLETED"


class LootChestTask(Task):
    """Loot whitelisted items from non-owned chests/barrels with trap rejection, 600s dedup, and high-risk trip caps."""

    def __init__(self, ctx: PrimitiveContext, *, radius: int = 32, max_high_risk_trips: int = 3) -> None:
        super().__init__("loot_chest")
        self.ctx = ctx
        self.radius = int(radius)
        self.max_high_risk_trips = max(1, int(max_high_risk_trips))
        self._container = ContainerPrimitive(ctx)

        self._target_pos: Optional[Coord3] = None
        self._rejected_chests: Set[Coord3] = set()
        self._loot_ops: Optional[List[Dict[str, Any]]] = None
        self._sneak_engaged: bool = False
        self._needs_unsneak: bool = False
        self._pending_completion: bool = False
        self._pending_failure: Optional[str] = None
        self._trip_counted: bool = False
        self._done: bool = False

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory reset ONLY; never emits network writes inside stop()."""
        self._target_pos = None
        self._rejected_chests = set()
        self._loot_ops = None
        self._sneak_engaged = False
        self._needs_unsneak = False
        self._pending_completion = False
        self._pending_failure = None
        self._trip_counted = False
        self._done = False
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        return self._done

    def _owned_chest_set(self, bb: Blackboard) -> Set[Coord3]:
        owned: Set[Coord3] = set()
        for slot in ("outpost_chest", "nether_outpost_chest"):
            c = _as_coord3(bb.get(slot))
            if c is not None:
                owned.add(c)
        for raw in bb.get("owned_chest_coords") or ():
            c = _as_coord3(raw)
            if c is not None:
                owned.add(c)
        db = self.ctx.gateway.db
        if db is not None and hasattr(db, "list_landmarks"):
            for kind in ("outpost_chest", "nether_outpost_chest", "chest"):
                for row in db.list_landmarks(bb.dimension, kind=kind):
                    c = _as_coord3(row.get("pos") if isinstance(row, dict) else None)
                    if c is not None:
                        owned.add(c)
        return owned

    def _active_looted_set(self, bb: Blackboard, now: float) -> Set[Coord3]:
        raw = bb.get("looted_chests")
        out: Set[Coord3] = set()
        if isinstance(raw, dict):
            for key, ts in raw.items():
                if not isinstance(ts, (int, float)):
                    continue
                if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], str):
                    if key[0] != bb.dimension:
                        continue
                    c = _as_coord3(key[1])
                else:
                    c = _as_coord3(key)
                if c is not None and (now - float(ts)) < _LOOTED_TTL_SECONDS:
                    out.add(c)
        elif isinstance(raw, (list, set, tuple)):
            for item in raw:
                c = _as_coord3(item)
                if c is not None:
                    out.add(c)
        return out

    def _mark_looted(self, bb: Blackboard, coord: Coord3) -> None:
        now = time.time()
        raw = bb.get("looted_chests")
        updated: Dict[Any, float] = dict(raw) if isinstance(raw, dict) else {}
        updated[(bb.dimension, coord)] = now
        bb.set("looted_chests", updated, writer=self.name)

    def _has_desert_temple_trap(self, bb: Blackboard, chest_pos: Coord3) -> bool:
        cx, cy, cz = chest_pos
        coords = [
            (cx + dx, cy + dy, cz + dz)
            for dx in range(-3, 4)
            for dy in range(-3, 4)
            for dz in range(-3, 4)
        ]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, coords, max_age=5.0)
        for c in coords:
            ent = fetched.get(c)
            if ent is not None and ent.name in _TRAP_BLOCKS:
                return True
        return False

    def _select_candidate(self, bb: Blackboard) -> Optional[Coord3]:
        rows = self.ctx.gateway.scan_blocks(bb.dimension, _CONTAINER_TYPES, radius=self.radius, limit=24)
        if not rows:
            return None
        rows.sort(key=lambda r: float(r.get("dist", 999.0)))
        owned = self._owned_chest_set(bb)
        looted = self._active_looted_set(bb, time.time())

        for row in rows:
            c = _as_coord3(row.get("pos"))
            if c is None:
                continue
            if c in owned or c in looted or c in self._rejected_chests:
                continue
            if self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "window"):
                continue
            if self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "loot_trap", min_failures=1):
                continue
            if self._has_desert_temple_trap(bb, c):
                self._rejected_chests.add(c)
                self.ctx.gateway.unreachable.record_failure(
                    bb.dimension, c, "loot_trap", "desert_temple_trap", threshold=1
                )
                continue
            return c
        return None

    def _detect_deep_dark_nearby(self, bb: Blackboard) -> bool:
        sensor_types = list(DEEP_DARK_BLOCKS - {"sculk_catalyst"})
        rows = self.ctx.gateway.scan_blocks(bb.dimension, sensor_types, radius=self.radius, limit=4)
        return bool(rows)

    def _handle_exit_unsneak(self, bb: Blackboard) -> Optional[str]:
        if not self._needs_unsneak:
            return None
        if self._sneak_engaged or bool(bb.status.get("sneaking")):
            out = self.ctx.execute_write(
                bb,
                "posture",
                {"sneak": False},
                lambda: self.ctx.client.posture(sneak=False),
            )
            if out.status == "UNKNOWN":
                return "UNKNOWN"
        self._sneak_engaged = False
        self._needs_unsneak = False
        if self._pending_completion:
            self._pending_completion = False
            self._done = True
            return "COMPLETED"
        reason = self._pending_failure or "loot_chest:failed"
        self._pending_failure = None
        bb.last_failure_reason = reason
        return "FAILED_LOCAL"

    def _finish_with_cleanup(self, bb: Blackboard, *, success: bool, failure_reason: str = "") -> str:
        if self._sneak_engaged:
            self._needs_unsneak = True
            self._pending_completion = success
            self._pending_failure = None if success else failure_reason
            return "RUNNING"
        if success:
            self._done = True
            return "COMPLETED"
        bb.last_failure_reason = failure_reason
        return "FAILED_LOCAL"

    def on_tick(self, bb: Blackboard) -> str:
        exit_res = self._handle_exit_unsneak(bb)
        if exit_res is not None:
            return exit_res

        is_deep_dark = self._detect_deep_dark_nearby(bb)
        is_high_risk = (bb.dimension == "the_nether") or is_deep_dark

        if self._target_pos is None:
            cand = self._select_candidate(bb)
            if cand is None:
                return self._finish_with_cleanup(bb, success=False, failure_reason="loot_chest:no_chest")
            self._target_pos = cand

        target = self._target_pos
        s_key = _sector_key(bb.dimension, target)
        if is_high_risk and not self._trip_counted:
            trips = dict(bb.get("high_risk_loot_trips") or {})
            if int(trips.get(s_key, 0)) >= self.max_high_risk_trips:
                return self._finish_with_cleanup(bb, success=False, failure_reason="loot_chest:high_risk_trip_cap")

        if is_deep_dark and not self._sneak_engaged and not bool(bb.status.get("sneaking")):
            out = self.ctx.execute_write(
                bb,
                "posture",
                {"sneak": True},
                lambda: self.ctx.client.posture(sneak=True),
            )
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "FAILED_LOCAL":
                bb.last_failure_reason = f"loot_chest:sneak:{out.reason}"
                return "FAILED_LOCAL"
            self._sneak_engaged = True
            return "RUNNING"

        if self._loot_ops is None:
            view = self._container.inspect_window(bb, target)
            if view.status == "UNKNOWN":
                return "UNKNOWN"
            if view.status == "PROGRESSED":
                return "RUNNING"
            if view.status == "FAILED_LOCAL":
                return self._finish_with_cleanup(bb, success=False, failure_reason=f"loot_chest:{view.reason}")

            if is_high_risk and not self._trip_counted:
                trips = dict(bb.get("high_risk_loot_trips") or {})
                trips[s_key] = int(trips.get(s_key, 0)) + 1
                bb.set("high_risk_loot_trips", trips, writer=self.name)
                self._trip_counted = True

            sections = view.details.get("sections") if isinstance(view.details, dict) else {}
            rows = sections.get("container") if isinstance(sections, dict) else []
            ops: List[Dict[str, Any]] = []
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = row.get("name")
                    cnt = int(row.get("count", 0) or 0)
                    if isinstance(name, str) and name in LOOT_WHITELIST_ITEMS and cnt > 0:
                        ops.append({"op": "take", "from": "container", "item": name, "count": cnt})
            if not ops:
                self._mark_looted(bb, target)
                return self._finish_with_cleanup(bb, success=True)
            self._loot_ops = ops
            return "RUNNING"

        batch = self._loot_ops[:8]
        out = self._container.window_ops(bb, batch, pos=target)
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        if out.status == "PROGRESSED":
            return "RUNNING"
        if out.status == "FAILED_LOCAL":
            return self._finish_with_cleanup(bb, success=False, failure_reason=f"loot_chest:{out.reason}")

        self._loot_ops = self._loot_ops[8:]
        if self._loot_ops:
            return "RUNNING"

        self._mark_looted(bb, target)
        return self._finish_with_cleanup(bb, success=True)
