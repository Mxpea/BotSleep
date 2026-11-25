"""L2 PhysicalUnstuckBranch: 4-Level Universal Physical Unstuck Ladder.

Triggered by LoopWatchdogNode's Bounding-Sphere progress invariant (or explicit unstuck_needed):
- L1: Bounded /control jump+strafe pulse (escapes fence/stair/corner snags with zero terrain edits).
- L2: Local 3x3x3 soft-block clear (digs obstructing leaves/vines/grass/dirt/snow, never under feet).
- L3: Vertical / tall-canopy lateral-to-trunk descent OR pit staircase climb via Pathfinder3D.
- L4: Sector exclusion + 32-48m relocation relay away from trapped geometry.

Strict invariants:
- Executes AT MOST ONE write endpoint call per BT tick, wrapped in PendingTransactionJournal.
- Pure-memory stop(bb) (zero network I/O on preemption).
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.domain import (
    AIR_BLOCKS,
    Coord3,
    HAZARD_BLOCKS,
    LEAF_BLOCKS,
    LOG_BLOCKS,
    NON_SOLID_BLOCKS,
    SOFT_OCCLUDERS,
    block_coord,
    dist2_xz,
    dist3,
)
from v2.gateway.multihop_navigator import MultiHopNavigator
from v2.gateway.pathfinder_3d import Pathfinder3D
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError
from v2.kernel.journal import PendingTransactionJournal


UNSTUCK_SOFT_BLOCKS = SOFT_OCCLUDERS | LEAF_BLOCKS | frozenset({
    "cobweb", "sweet_berry_bush", "moss_block", "azalea", "flowering_azalea",
    "oak_sapling", "birch_sapling", "spruce_sapling", "jungle_sapling",
    "acacia_sapling", "dark_oak_sapling", "mangrove_roots", "muddy_mangrove_roots",
})

_RELOCATE_BEARINGS: Tuple[Tuple[float, float], ...] = (
    (1.0, 0.0),
    (0.707, 0.707),
    (0.0, 1.0),
    (-0.707, 0.707),
    (-1.0, 0.0),
    (-0.707, -0.707),
    (0.0, -1.0),
    (0.707, -0.707),
)


class PhysicalUnstuckBranch(Node):
    """Executes one escalated step from the 4-level physical unstuck ladder when watchdog flags a stall."""

    SECTOR_TTL_SECONDS = 300.0
    RELOCATE_DISTANCE = 40.0  # 32-48m relocation band

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        pathfinder: Optional[Pathfinder3D] = None,
        navigator: Optional[MultiHopNavigator] = None,
    ) -> None:
        super().__init__("PhysicalUnstuckBranch")
        self.gateway = gateway
        self.journal = journal
        self.pathfinder = pathfinder or Pathfinder3D(gateway)
        self.navigator = navigator or MultiHopNavigator(gateway, journal, pathfinder=self.pathfinder)
        self._l1_cycle: int = 0
        self._l4_bearing_idx: int = 0

    def _write_action(
        self,
        bb: Blackboard,
        endpoint: str,
        payload: Dict[str, Any],
        fn: Callable[[], Dict[str, Any]],
        *,
        unknown_center: Optional[Coord3] = None,
        outcome_label: str,
    ) -> Tuple[Status, bool]:
        tx_id = self.journal.begin_write(endpoint, payload, dimension=bb.dimension, pos=bb.pos)
        bb.writes_this_tick += 1
        try:
            fn()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(bb.dimension, unknown_center or block_coord(bb.pos))
                bb.set("pending_frozen", True, writer=self.name)
                bb.last_action_outcome = "unstuck_unknown"
                return Status.RUNNING, False
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            bb.last_action_outcome = f"{outcome_label}:failed:{err.code}"
            if unknown_center is not None:
                self.gateway.unreachable.record_failure(bb.dimension, unknown_center, endpoint, err.code)
            return Status.RUNNING, False
        self.journal.resolve_write(tx_id, "confirmed")
        bb.last_action_outcome = outcome_label
        return Status.RUNNING, True

    def _run_l1_control_pulse(self, bb: Blackboard) -> Status:
        """L1: bounded /control jump + rotating strafe pulse."""
        patterns = (
            {"forward": True, "right": True, "jump": True},
            {"forward": True, "left": True, "jump": True},
            {"back": True, "right": True, "jump": True},
            {"back": True, "left": True, "jump": True},
        )
        pat = patterns[self._l1_cycle % len(patterns)]
        self._l1_cycle += 1
        payload = {**pat, "ms": 500, "wait": True}
        status, _ = self._write_action(
            bb,
            "control",
            payload,
            lambda: self.gateway.client.control(
                forward=bool(pat.get("forward", False)),
                back=bool(pat.get("back", False)),
                left=bool(pat.get("left", False)),
                right=bool(pat.get("right", False)),
                jump=True,
                ms=500,
                wait=True,
            ),
            outcome_label="unstuck_l1_control_pulse",
        )
        return status

    def _try_l2_soft_clear(self, bb: Blackboard) -> Optional[Status]:
        """L2: clear ONE soft obstructing block in local 3x3x3 (excluding directly under feet)."""
        fx, fy, fz = block_coord(bb.pos)
        under_feet = (fx, fy - 1, fz)
        coords: List[Coord3] = []
        # Prioritize head/feet ring (dy=1, dy=0) before dy=2
        for dy in (1, 0, 2):
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    c = (fx + dx, fy + dy, fz + dz)
                    if c == under_feet:
                        continue
                    coords.append(c)
        # Force fresh exact 3x3x3 fetch when physically stuck so stale/inferred air never hides an obstacle!
        fetched = self.gateway.prefetch_blocks(bb.dimension, coords, max_age=0.0)
        candidates: List[Coord3] = []
        for c in coords:
            ent = fetched.get(c)
            if ent is None or ent.name in AIR_BLOCKS or ent.name in HAZARD_BLOCKS:
                continue
            if ent.name in UNSTUCK_SOFT_BLOCKS and not self.gateway.unreachable.is_blocked(bb.dimension, c, "dig"):
                candidates.append(c)
        if not candidates:
            return None
        target = candidates[0]
        status, ok = self._write_action(
            bb,
            "dig",
            {"pos": list(target), "collect": False},
            lambda: self.gateway.client.dig(list(target), collect=False),
            unknown_center=target,
            outcome_label="unstuck_l2_soft_clear",
        )
        if ok:
            self.gateway.write_through_dig(bb.dimension, target)
        return status

    def _safe_drop_depth(self, dimension: str, cell: Coord3, fetched: Dict[Coord3, Any]) -> Optional[int]:
        """Return drop distance (0..4) to safe solid/water below `cell`, or None if lethal/hazard."""
        cx, cy, cz = cell
        for d in range(1, 6):
            ent = fetched.get((cx, cy - d, cz))
            if ent is None:
                return None
            if ent.name in HAZARD_BLOCKS:
                return None
            if ent.name == "water":
                return d - 1
            if ent.name not in NON_SOLID_BLOCKS:
                return (d - 1) if (d - 1) <= 4 else None
        return None

    def try_canopy_lateral_to_trunk(self, bb: Blackboard) -> Optional[Status]:
        """L3a: When perched on tall leaves (>4m drop), move laterally toward a trunk or safe-drop neighbor."""
        fx, fy, fz = block_coord(bb.pos)
        below = self.gateway.get_block(bb.dimension, (fx, fy - 1, fz), allow_fetch=True)
        if below is None or below.name not in (LEAF_BLOCKS | LOG_BLOCKS):
            return None

        # Check drop directly below feet
        col_probe = [(fx, fy - d, fz) for d in range(2, 7)]
        col_fetched = self.gateway.prefetch_blocks(bb.dimension, col_probe, max_age=5.0)
        direct_safe = False
        for d in range(2, 7):
            ent = col_fetched.get((fx, fy - d, fz))
            if ent is None:
                break
            if ent.name in HAZARD_BLOCKS:
                break
            if ent.name == "water" or ent.name not in NON_SOLID_BLOCKS:
                direct_safe = (d - 1) <= 4 or ent.name == "water"
                break
        if direct_safe and below.name in LEAF_BLOCKS:
            # Safe 1-4m drop right under feet -> dig leaf directly
            status, ok = self._write_action(
                bb,
                "dig",
                {"pos": [fx, fy - 1, fz], "collect": False},
                lambda: self.gateway.client.dig([fx, fy - 1, fz], collect=False),
                unknown_center=(fx, fy - 1, fz),
                outcome_label="unstuck_l3_canopy_drop",
            )
            if ok:
                self.gateway.write_through_dig(bb.dimension, (fx, fy - 1, fz))
            return status

        # Unsafe drop directly below -> scan nearby trunks (`*_log`) and lateral leaf/solid bridges
        logs = self.gateway.scan_blocks(bb.dimension, list(LOG_BLOCKS), radius=8, limit=16)
        trunk_xz: Optional[Tuple[int, int]] = None
        if logs:
            # Prefer a log column at or below current Y (actual tree trunk, not higher branch)
            trunk_rows = [
                r for r in logs
                if isinstance(r.get("pos"), list) and len(r["pos"]) == 3 and int(r["pos"][1]) <= fy
            ]
            pool = trunk_rows or logs
            pool.sort(key=lambda r: dist2_xz(bb.pos, r["pos"]))
            tpos = pool[0]["pos"]
            trunk_xz = (int(tpos[0]), int(tpos[2]))

        # Probe 5x5 lateral cells at fy-1..fy+1 plus drop columns
        ring_offsets = [
            (dx, dz)
            for dx in range(-2, 3)
            for dz in range(-2, 3)
            if (dx, dz) != (0, 0)
        ]
        probe: List[Coord3] = []
        for dx, dz in ring_offsets:
            nx, nz = fx + dx, fz + dz
            for oy in (-5, -4, -3, -2, -1, 0, 1):
                probe.append((nx, fy + oy, nz))
        fetched = self.gateway.prefetch_blocks(bb.dimension, probe, max_age=5.0)

        # Look for an immediate safe-drop cell or trunk-adjacent lateral step
        best_step: Optional[Tuple[float, Coord3, Optional[Coord3]]] = None
        cur_trunk_dist = math.hypot(fx - trunk_xz[0], fz - trunk_xz[1]) if trunk_xz else 999.0

        for dx, dz in ring_offsets:
            if abs(dx) > 2 or abs(dz) > 2:
                continue
            nx, nz = fx + dx, fz + dz
            floor_ent = fetched.get((nx, fy - 1, nz))
            feet_ent = fetched.get((nx, fy, nz))
            head_ent = fetched.get((nx, fy + 1, nz))
            if floor_ent is None or feet_ent is None or head_ent is None:
                continue

            # Candidate 1: neighbor has solid/leaf floor to step onto toward trunk or toward a lower safe drop
            if floor_ent.name not in NON_SOLID_BLOCKS and floor_ent.name not in HAZARD_BLOCKS:
                if self.gateway.unreachable.is_blocked(bb.dimension, (nx, fy, nz), "move_to"):
                    continue
                blocker: Optional[Coord3] = None
                if head_ent.name not in AIR_BLOCKS:
                    if head_ent.name in UNSTUCK_SOFT_BLOCKS and not self.gateway.unreachable.is_blocked(bb.dimension, (nx, fy + 1, nz), "dig"):
                        blocker = (nx, fy + 1, nz)
                    else:
                        continue
                elif feet_ent.name not in AIR_BLOCKS:
                    if feet_ent.name in UNSTUCK_SOFT_BLOCKS and not self.gateway.unreachable.is_blocked(bb.dimension, (nx, fy, nz), "dig"):
                        blocker = (nx, fy, nz)
                    else:
                        continue

                # Score: strongly favor stepping closer to trunk, or stepping onto a cell with safe <=4m drop
                step_score = 100.0
                if trunk_xz is not None:
                    nd = math.hypot(nx - trunk_xz[0], nz - trunk_xz[1])
                    if nd < cur_trunk_dist - 0.1 or (floor_ent.name in LOG_BLOCKS and nd <= 1.5):
                        step_score = nd
                drop_d = self._safe_drop_depth(bb.dimension, (nx, fy, nz), fetched)
                if drop_d is not None and drop_d <= 4:
                    step_score = min(step_score, float(drop_d) * 0.5)
                if step_score < 50.0:
                    if best_step is None or step_score < best_step[0]:
                        best_step = (step_score, (nx, fy, nz), blocker)

            # Candidate 2: neighbor is air with a safe <= 4m drop or water landing directly below!
            elif floor_ent.name in AIR_BLOCKS and feet_ent.name in AIR_BLOCKS and head_ent.name in AIR_BLOCKS:
                drop_d = self._safe_drop_depth(bb.dimension, (nx, fy, nz), fetched)
                if drop_d is not None and drop_d <= 4 and abs(dx) <= 1 and abs(dz) <= 1:
                    landing_y = fy - drop_d
                    if best_step is None or (-1.0) < best_step[0]:
                        best_step = (-1.0, (nx, landing_y, nz), None)

        if best_step is None:
            # If already standing ON a trunk log (`below.name in LOG_BLOCKS`) with solid trunk below, dig 1 spiral step
            if below.name in LOG_BLOCKS:
                sub_log = self.gateway.get_block(bb.dimension, (fx, fy - 2, fz), allow_fetch=True)
                if sub_log is not None and sub_log.name not in NON_SOLID_BLOCKS and sub_log.name not in HAZARD_BLOCKS:
                    status, ok = self._write_action(
                        bb,
                        "dig",
                        {"pos": [fx, fy - 1, fz], "collect": True},
                        lambda: self.gateway.client.dig([fx, fy - 1, fz], collect=True),
                        unknown_center=(fx, fy - 1, fz),
                        outcome_label="unstuck_l3_trunk_step_down",
                    )
                    if ok:
                        self.gateway.write_through_dig(bb.dimension, (fx, fy - 1, fz))
                    return status
            return None

        _, target_cell, blocker = best_step
        if blocker is not None:
            status, ok = self._write_action(
                bb,
                "dig",
                {"pos": list(blocker), "collect": False},
                lambda b=blocker: self.gateway.client.dig(list(b), collect=False),
                unknown_center=blocker,
                outcome_label="unstuck_l3_canopy_clear_lateral",
            )
            if ok:
                self.gateway.write_through_dig(bb.dimension, blocker)
            return status

        status, _ = self._write_action(
            bb,
            "move_to",
            {"x": target_cell[0] + 0.5, "y": target_cell[1], "z": target_cell[2] + 0.5, "range": 0.6},
            lambda tc=target_cell: self.gateway.client.move_to(
                x=tc[0] + 0.5, y=float(tc[1]), z=tc[2] + 0.5, range=0.6, timeout_ms=5000
            ),
            unknown_center=target_cell,
            outcome_label="unstuck_l3_canopy_lateral_to_trunk",
        )
        return status

    def _try_l3_vertical_or_canopy(self, bb: Blackboard) -> Optional[Status]:
        """L3: tall-canopy lateral-to-trunk descent OR 3D staircase pit escape."""
        canopy_res = self.try_canopy_lateral_to_trunk(bb)
        if canopy_res is not None:
            return canopy_res

        # Otherwise attempt a 3D staircase climb upward (+4 Y, +4 X/Z) to escape pits/holes
        fx, fy, fz = block_coord(bb.pos)
        dx, dz = _RELOCATE_BEARINGS[self._l1_cycle % len(_RELOCATE_BEARINGS)]
        climb_goal = (fx + int(round(dx * 4)), fy + 3, fz + int(round(dz * 4)))
        out = self.navigator.step_toward(
            (climb_goal[0] + 0.5, float(climb_goal[1]), climb_goal[2] + 0.5),
            bb.status,
            tolerance=1.5,
            inventory_counts=bb.inventory_counts,
            prefer_3d=True,
            threat_positions=bb.get("active_threat_positions"),
        )
        if out.action_taken != "none":
            bb.writes_this_tick += 1
        if out.status == "UNKNOWN":
            bb.set("pending_frozen", True, writer=self.name)
            bb.last_action_outcome = "unstuck_unknown"
            return Status.RUNNING
        if out.status in {"PROGRESSED", "REACHED"}:
            bb.last_action_outcome = f"unstuck_l3_vertical:{out.action_taken}"
            return Status.RUNNING
        return None

    def _run_l4_sector_relocate(self, bb: Blackboard) -> Status:
        """L4: exclude stuck 16x16 sector and relocate 32-48m in a fresh non-excluded direction."""
        px, py, pz = bb.pos
        now = time.time()
        excluded: Dict[Tuple[int, int], float] = dict(bb.get("excluded_sectors") or {})
        # Prune expired exclusions
        excluded = {k: v for k, v in excluded.items() if v > now}
        cur_sector = (math.floor(px / 16.0), math.floor(pz / 16.0))
        excluded[cur_sector] = now + self.SECTOR_TTL_SECONDS
        bb.set("excluded_sectors", excluded, writer=self.name)
        self.gateway.unreachable.record_failure(bb.dimension, block_coord(bb.pos), "unstuck_sector", "l4_relocate")

        threats = bb.get("active_threat_positions") or ()
        chosen_target: Optional[Tuple[float, float, float]] = None
        for offset in range(len(_RELOCATE_BEARINGS)):
            idx = (self._l4_bearing_idx + offset) % len(_RELOCATE_BEARINGS)
            bx, bz = _RELOCATE_BEARINGS[idx]
            tx = px + bx * self.RELOCATE_DISTANCE
            tz = pz + bz * self.RELOCATE_DISTANCE
            sec = (math.floor(tx / 16.0), math.floor(tz / 16.0))
            if sec in excluded:
                continue
            if any(math.hypot(tx - tp[0], tz - tp[2]) < 12.0 for tp in threats):
                continue
            self._l4_bearing_idx = (idx + 1) % len(_RELOCATE_BEARINGS)
            chosen_target = (round(tx, 1), py, round(tz, 1))
            break

        if chosen_target is None:
            bx, bz = _RELOCATE_BEARINGS[self._l4_bearing_idx % len(_RELOCATE_BEARINGS)]
            self._l4_bearing_idx = (self._l4_bearing_idx + 1) % len(_RELOCATE_BEARINGS)
            chosen_target = (round(px + bx * self.RELOCATE_DISTANCE, 1), py, round(pz + bz * self.RELOCATE_DISTANCE, 1))

        out = self.navigator.step_toward(
            chosen_target,
            bb.status,
            tolerance=3.0,
            inventory_counts=bb.inventory_counts,
            xz_only=True,
            threat_positions=threats,
            nearby_entities=bb.entities,
        )
        if out.action_taken != "none":
            bb.writes_this_tick += 1
        if out.status == "UNKNOWN":
            bb.set("pending_frozen", True, writer=self.name)
            bb.last_action_outcome = "unstuck_unknown"
            return Status.RUNNING
        bb.last_action_outcome = f"unstuck_l4_relocate:{out.status.lower()}"
        bb.log_event(self.name, "l4_relocate", f"excluded={cur_sector} target=({chosen_target[0]},{chosen_target[2]})")
        # Reset ladder level after L4 so future stalls escalate cleanly from L1
        bb.set("unstuck_level", 0, writer=self.name)
        bb.set("unstuck_origin", None, writer=self.name)
        return Status.RUNNING

    def update(self, bb: Blackboard) -> Status:
        if not bb.get("unstuck_needed"):
            return Status.FAILURE

        level = max(1, min(4, int(bb.get("unstuck_level", 1) or 1)))
        bb.set("unstuck_needed", False, writer=self.name)
        bb.log_event(self.name, "unstuck_trigger", f"level=L{level}")

        if level == 1:
            return self._run_l1_control_pulse(bb)

        if level == 2:
            res = self._try_l2_soft_clear(bb)
            if res is not None:
                return res
            # Nothing soft in 3x3x3 -> escalate directly to L3 on this tick!
            level = 3
            bb.set("unstuck_level", 3, writer=self.name)

        if level == 3:
            res = self._try_l3_vertical_or_canopy(bb)
            if res is not None:
                return res
            # No vertical/canopy escape -> escalate directly to L4 on this tick!
            level = 4
            bb.set("unstuck_level", 4, writer=self.name)

        return self._run_l4_sector_relocate(bb)
