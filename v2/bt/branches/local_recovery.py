"""L2 LocalRecoveryBranch: urgent eat, canopy descent, water shore escape, and cached/throttled auto-sleep."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.branches.unstuck import PhysicalUnstuckBranch
from v2.bt.kernel import Node, Status
from v2.domain import (
    AIR_BLOCKS,
    BED_BLOCKS,
    Coord3,
    FOOD_ITEMS,
    HAZARD_BLOCKS,
    LEAF_BLOCKS,
    LOG_BLOCKS,
    NON_SOLID_BLOCKS,
    PASS_THROUGH_BLOCKS,
    SOFT_OCCLUDERS,
    block_coord,
    dist3,
)
from v2.gateway.surface_swim import SurfaceSwimExecutor
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError
from v2.kernel.journal import PendingTransactionJournal

_SHORE_BEARINGS: Tuple[Tuple[float, float], ...] = (
    (1.0, 0.0),
    (0.707, 0.707),
    (0.0, 1.0),
    (-0.707, 0.707),
    (-1.0, 0.0),
    (-0.707, -0.707),
    (0.0, -1.0),
    (0.707, -0.707),
)
_WATER_BANK_DIG_BLOCKS = SOFT_OCCLUDERS | LEAF_BLOCKS | frozenset({
    "sand", "red_sand", "gravel", "clay", "mud", "snow_block",
})


class LocalRecoveryBranch(Node):
    """Handles low-priority local self-maintenance before active long-horizon goals."""

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        food_threshold: int = 14,
        low_health_threshold: float = 10.0,
        enable_auto_sleep: bool = True,
        sleep_radius: int = 16,
        bed_scan_interval: float = 8.0,
        unstuck: Optional[PhysicalUnstuckBranch] = None,
    ) -> None:
        super().__init__("LocalRecoveryBranch")
        self.gateway = gateway
        self.journal = journal
        self.food_threshold = int(food_threshold)
        self.low_health_threshold = float(low_health_threshold)
        self.enable_auto_sleep = bool(enable_auto_sleep)
        self.sleep_radius = int(sleep_radius)
        self.bed_scan_interval = max(1.0, float(bed_scan_interval))
        self._swimmer = SurfaceSwimExecutor(gateway.client, journal)
        self._unstuck = unstuck or PhysicalUnstuckBranch(gateway, journal)
        self._last_bed_scan_at: float = 0.0
        self._last_bed_scan_pos: Optional[Tuple[float, float, float]] = None
        self._cached_beds: List[Coord3] = []
        self._swim_consec: int = 0
        self._swim_anchor: Optional[Tuple[float, float, float]] = None
        self._swim_cooldown_until: float = 0.0

    def _pick_swim_target(self, bb: Blackboard) -> Tuple[float, float, float]:
        """Prefetch 3D shore cells and prefer nearby dry bank, rotating bearing when none visible."""
        fx, fy, fz = block_coord(bb.pos)
        coords = [
            (fx + dx, fy + dy, fz + dz)
            for dx in range(-2, 3)
            for dz in range(-2, 3)
            for dy in (-1, 0, 1, 2)
        ]
        fetched = self.gateway.prefetch_blocks(bb.dimension, coords)
        offsets = sorted(
            ((dx, dz) for dx in range(-2, 3) for dz in range(-2, 3) if (dx, dz) != (0, 0)),
            key=lambda d: (d[0] * d[0] + d[1] * d[1], abs(d[0]) + abs(d[1])),
        )
        for dy in (1, 0):
            for dx, dz in offsets:
                cand = (fx + dx, fy + dy, fz + dz)
                below = fetched.get((cand[0], cand[1] - 1, cand[2]))
                feet = fetched.get(cand)
                head = fetched.get((cand[0], cand[1] + 1, cand[2]))
                if (
                    feet is not None and feet.name in PASS_THROUGH_BLOCKS and feet.name not in HAZARD_BLOCKS
                    and (head is None or (head.name in PASS_THROUGH_BLOCKS and head.name not in HAZARD_BLOCKS))
                    and below is not None and below.name not in NON_SOLID_BLOCKS and below.name not in HAZARD_BLOCKS
                ):
                    return (cand[0] + 0.5, float(cand[1]) + 0.5, cand[2] + 0.5)
        if self._swim_consec > 0:
            bx, bz = _SHORE_BEARINGS[(self._swim_consec - 1) % len(_SHORE_BEARINGS)]
            return (bb.pos[0] + bx * 3.0, bb.pos[1] + 1.0, bb.pos[2] + bz * 3.0)
        return (bb.pos[0], bb.pos[1] + 2.0, bb.pos[2])

    def _try_dig_swim_ceiling(self, bb: Blackboard) -> Optional[Status]:
        """If trapped in water beneath a ceiling or soft riverbank overhang, dig an opening."""
        fx, fy, fz = block_coord(bb.pos)
        for dy in (2, 1):
            c = (fx, fy + dy, fz)
            ent = self.gateway.get_block(bb.dimension, c, allow_fetch=True)
            if ent is not None and ent.name not in NON_SOLID_BLOCKS and ent.name not in HAZARD_BLOCKS and ent.name != "bedrock":
                bb.last_action_outcome = "recovery_swim_dig_ceiling"
                res_status = self._write_action(
                    bb, "dig", {"pos": list(c), "collect": False},
                    lambda _c=c: self.gateway.client.dig(list(_c), collect=False),
                )
                if res_status == Status.RUNNING and not bb.get("pending_frozen"):
                    self.gateway.write_through_dig(bb.dimension, c)
                return res_status
        bank_coords = [
            (fx + dx, fy + dy, fz + dz)
            for dy in (1, 0)
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
        ]
        bank_fetched = self.gateway.prefetch_blocks(bb.dimension, bank_coords)
        for c in bank_coords:
            ent = bank_fetched.get(c)
            if (
                ent is not None
                and ent.name in _WATER_BANK_DIG_BLOCKS
                and ent.name not in NON_SOLID_BLOCKS
                and ent.name not in HAZARD_BLOCKS
                and not self.gateway.unreachable.is_blocked(bb.dimension, c, "dig")
            ):
                bb.last_action_outcome = "recovery_swim_dig_bank"
                res_status = self._write_action(
                    bb, "dig", {"pos": list(c), "collect": False},
                    lambda _c=c: self.gateway.client.dig(list(_c), collect=False),
                )
                if res_status == Status.RUNNING and not bb.get("pending_frozen"):
                    self.gateway.write_through_dig(bb.dimension, c)
                return res_status
        return None

    def _find_nearby_beds(self, bb: Blackboard) -> List[Coord3]:
        """Return cached nearby beds, running typed gateway scan at most once per bed_scan_interval (or 6m move)."""
        now = time.monotonic()
        moved_far = (
            self._last_bed_scan_pos is None
            or dist3(bb.pos, self._last_bed_scan_pos) >= 6.0
        )
        if moved_far or (now - self._last_bed_scan_at) >= self.bed_scan_interval:
            rows = self.gateway.scan_blocks(
                bb.dimension,
                list(BED_BLOCKS),
                radius=self.sleep_radius,
                limit=8,
            )
            found: List[Coord3] = []
            for row in rows:
                bpos = row.get("pos")
                if isinstance(bpos, list) and len(bpos) == 3:
                    found.append((int(bpos[0]), int(bpos[1]), int(bpos[2])))
            self._cached_beds = found
            self._last_bed_scan_at = now
            self._last_bed_scan_pos = bb.pos

        valid = [
            c for c in self._cached_beds
            if dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)) <= self.sleep_radius + 2.0
            and not self.gateway.unreachable.is_blocked(bb.dimension, c, "sleep")
        ]
        valid.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)))
        return valid

    def _best_food(self, counts: Dict[str, int]) -> Optional[str]:
        avail = [(val, name) for name, val in FOOD_ITEMS.items() if counts.get(name, 0) > 0]
        if not avail:
            return None
        avail.sort(reverse=True)
        return avail[0][1]

    def _write_action(self, bb: Blackboard, endpoint: str, payload: Dict[str, Any], fn: Any) -> Status:
        tx_id = self.journal.begin_write(endpoint, payload, dimension=bb.dimension, pos=bb.pos)
        bb.writes_this_tick += 1
        try:
            fn()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(bb.dimension, block_coord(bb.pos))
                bb.set("pending_frozen", True, writer=self.name)
                bb.last_action_outcome = "recovery_unknown"
                return Status.RUNNING
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            bb.last_action_outcome = f"recovery_failed:{err.code}"
            return Status.FAILURE
        self.journal.resolve_write(tx_id, "confirmed")
        return Status.RUNNING

    def update(self, bb: Blackboard) -> Status:
        st = bb.status
        in_water = bool(st.get("in_water") or st.get("submerged"))
        on_ground = bool(st.get("on_ground", False))
        if in_water:
            dry_streak = 0
        elif on_ground:
            dry_streak = int(bb.get("dry_footing_streak", 0) or 0) + 1
        else:
            dry_streak = int(bb.get("dry_footing_streak", 0) or 0)
        bb.set("dry_footing_streak", dry_streak, writer=self.name)

        # 1) Urgent eating when hungry or low health
        if (bb.food <= self.food_threshold or (bb.health <= self.low_health_threshold and bb.food < 20)):
            food = self._best_food(bb.inventory_counts)
            if food is not None:
                bb.last_action_outcome = f"recovery_eat:{food}"
                return self._write_action(
                    bb, "use_item", {"item": food},
                    lambda: self.gateway.client.use_item(food),
                )

        # 2) Water shore buoyancy: escape drowning or do a bounded shore-hop without starving AI replan
        if not in_water:
            self._swim_consec = 0
            self._swim_anchor = None
            self._swimmer.reset()
        else:
            needs_air = bool(st.get("submerged")) or bb.oxygen < 16
            now = time.monotonic()
            if needs_air or (not bb.active_goal_name and now >= self._swim_cooldown_until):
                if self._swim_anchor is None:
                    self._swim_anchor = bb.pos
                stalled_in_water = self._swim_consec >= 2 and dist3(bb.pos, self._swim_anchor) < 0.5
                if stalled_in_water:
                    dig_res = self._try_dig_swim_ceiling(bb)
                    if dig_res is not None:
                        self._swim_consec = 0
                        self._swim_anchor = bb.pos
                        return dig_res
                if self._swim_consec >= 3 and (not needs_air or stalled_in_water):
                    # Yield to PhysicalUnstuckBranch & lock LLM replanning until dry footing returns
                    self._swim_cooldown_until = now + 8.0
                    self._swim_consec = 0
                    self._swim_anchor = None
                    bb.set("unstuck_needed", True, writer=self.name)
                    if int(bb.get("unstuck_level", 0) or 0) <= 0:
                        bb.set("unstuck_level", 1, writer=self.name)
                    bb.set("water_replan_lock_until", now + 15.0, writer=self.name)
                    bb.last_action_outcome = "recovery_swim_yield"
                    return Status.FAILURE
                target_pos = self._pick_swim_target(bb)
                res = self._swimmer.swim_step(target_pos, st)
                bb.writes_this_tick += 1
                self._swim_consec += 1
                if res.status == "UNKNOWN":
                    bb.set("pending_frozen", True, writer=self.name)
                bb.last_action_outcome = f"recovery_swim:{res.status.lower()}"
                return Status.RUNNING

        # 3) Canopy descent: safe drop (<= 4m, no lava) -> dig leaf; unsafe tall canopy -> lateral-to-trunk descent!
        fx, fy, fz = block_coord(bb.pos)
        below = self.gateway.get_block(bb.dimension, (fx, fy - 1, fz), allow_fetch=True)
        if below is not None and below.name in LEAF_BLOCKS:
            column = [(fx, fy - d, fz) for d in range(2, 7)]
            fetched = self.gateway.prefetch_blocks(bb.dimension, column)
            safe_drop = False
            for d in range(2, 7):
                ent = fetched.get((fx, fy - d, fz))
                if ent is None:
                    break
                if ent.name in HAZARD_BLOCKS:
                    safe_drop = False
                    break
                if ent.name == "water" or ent.name not in NON_SOLID_BLOCKS:
                    safe_drop = (d - 1) <= 4 or ent.name == "water"
                    break
            if safe_drop:
                bb.last_action_outcome = "recovery_canopy_descend"
                res_status = self._write_action(
                    bb, "dig", {"pos": [fx, fy - 1, fz]},
                    lambda: self.gateway.client.dig([fx, fy - 1, fz], collect=False),
                )
                if res_status == Status.RUNNING and not bb.get("pending_frozen"):
                    self.gateway.write_through_dig(bb.dimension, (fx, fy - 1, fz))
                return res_status
            # Tall canopy (> 4m lethal drop): step laterally toward trunk or safe-drop neighbor without digging underfoot!
            lateral_res = self._unstuck.try_canopy_lateral_to_trunk(bb)
            if lateral_res is not None:
                return lateral_res
        elif below is not None and below.name in LOG_BLOCKS:
            trunk_probe = [(fx, fy - 2, fz)] + [
                (fx + dx, fy - d, fz + dz)
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                for d in (1, 2)
            ]
            fetched_t = self.gateway.prefetch_blocks(bb.dimension, trunk_probe)
            sub_log = fetched_t.get((fx, fy - 2, fz))
            if sub_log is not None and sub_log.name in LOG_BLOCKS:
                has_low_step = any(
                    (ent := fetched_t.get((fx + dx, fy - d, fz + dz))) is not None
                    and ent.name not in NON_SOLID_BLOCKS
                    and ent.name not in LEAF_BLOCKS
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    for d in (1, 2)
                )
                if not has_low_step:
                    trunk_res = self._unstuck.try_canopy_lateral_to_trunk(bb)
                    if trunk_res is not None:
                        return trunk_res

        # 4) Opportunistic auto-sleep at night using cached/throttled typed gateway bed scan
        if self.enable_auto_sleep and not st.get("is_sleeping", False):
            tod = int(st.get("time_of_day", 1000)) % 24000
            if 12541 <= tod <= 23458:
                beds = self._find_nearby_beds(bb)
                if beds:
                    bpos = list(beds[0])
                    dist = math.dist(bb.pos, (bpos[0] + 0.5, bpos[1] + 0.5, bpos[2] + 0.5))
                    if dist > 2.5:
                        bb.last_action_outcome = "recovery_approach_bed"
                        res = self._write_action(
                            bb, "move_to", {"x": bpos[0], "y": bpos[1], "z": bpos[2], "range": 2.0},
                            lambda: self.gateway.client.move_to(bpos[0], bpos[1], bpos[2], range=2.0, timeout_ms=8000),
                        )
                        if res == Status.FAILURE:
                            self.gateway.unreachable.record_failure(bb.dimension, beds[0], "sleep", "approach_failed")
                        return res
                    bb.last_action_outcome = "recovery_sleep"
                    res = self._write_action(
                        bb, "interact", {"pos": bpos},
                        lambda: self.gateway.client.interact(pos=bpos),
                    )
                    if res == Status.FAILURE:
                        self.gateway.unreachable.record_failure(bb.dimension, beds[0], "sleep", "interact_failed")
                    return res

        return Status.FAILURE
