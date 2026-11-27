"""L1 MultiHopNavigator: abolishes 32m local limit, slices >22m relays, falls back to 3D micro-dig/pillar/bridge.

Enforces the Single-Write Macro-Tick contract: each call to `step_toward()` executes AT MOST ONE
write action wrapped in `PendingTransactionJournal`, then immediately returns control to the caller/BT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from v2.domain import (
    BOAT_ITEMS,
    Coord3,
    HAZARD_BLOCKS,
    LEAF_BLOCKS,
    NON_SOLID_BLOCKS,
    PASS_THROUGH_BLOCKS,
    PICKAXE_TIERS,
    SCAFFOLD_BLOCKS,
    SOFT_OCCLUDERS,
    block_coord,
    dist2_xz,
    dist3,
    pick_best_item,
)

_HARD_ROCK_NO_PICK_BLOCKS = frozenset({
    "stone", "deepslate", "cobblestone", "cobbled_deepslate",
    "andesite", "diorite", "granite", "tuff", "sandstone",
    "red_sandstone", "blackstone", "basalt", "netherrack",
    "obsidian", "iron_ore", "deepslate_iron_ore", "coal_ore",
    "deepslate_coal_ore", "gold_ore", "deepslate_gold_ore",
    "copper_ore", "deepslate_copper_ore", "diamond_ore",
    "deepslate_diamond_ore", "lapis_ore", "deepslate_lapis_ore",
    "redstone_ore", "deepslate_redstone_ore", "emerald_ore",
    "deepslate_emerald_ore",
})
from v2.gateway.pathfinder_3d import NavStep, Pathfinder3D
from v2.gateway.surface_swim import SurfaceSwimExecutor
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError, MinecraftAgentClient
from v2.kernel.journal import PendingTransactionJournal

_WATER_SOFT_DIG_BLOCKS = SOFT_OCCLUDERS | LEAF_BLOCKS | frozenset({
    "sand", "red_sand", "gravel", "clay", "mud", "snow_block",
})


@dataclass(frozen=True)
class NavOutcome:
    status: str  # "REACHED" | "PROGRESSED" | "BLOCKED" | "UNKNOWN" | "NEEDS_SUBTASK"
    action_taken: str = "none"
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class MultiHopNavigator:
    """Two-tier navigator: 18-22m relay slicing + Pathfinder3D micro-dig/bridge/pillar + SurfaceSwim."""

    RELAY_THRESHOLD = 22.0
    RELAY_STEP = 20.0
    MIN_PROGRESS_METERS = 0.35

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        pathfinder: Optional[Pathfinder3D] = None,
        swimmer: Optional[SurfaceSwimExecutor] = None,
    ) -> None:
        self.gateway = gateway
        self.client: MinecraftAgentClient = gateway.client
        self.journal = journal
        self.pathfinder = pathfinder or Pathfinder3D(gateway)
        self.swimmer = swimmer or SurfaceSwimExecutor(self.client, journal)
        self._stall_counts: Dict[Tuple[str, Coord3], int] = {}
        self._last_pos_for_target: Dict[Tuple[str, Coord3], Tuple[float, float, float]] = {}
        self._water_steps: Dict[Tuple[str, Coord3], int] = {}
        self._water_stalls: Dict[Tuple[str, Coord3], int] = {}
        self._water_anchor_pos: Dict[Tuple[str, Coord3], Tuple[float, float, float]] = {}
        self._water_last_pos: Dict[Tuple[str, Coord3], Tuple[float, float, float]] = {}
        self._pending_pillar: Optional[Tuple[str, Coord3, str]] = None

    def _reset_water_state(self, key: Tuple[str, Coord3]) -> None:
        self._water_steps.pop(key, None)
        self._water_stalls.pop(key, None)
        self._water_anchor_pos.pop(key, None)
        self._water_last_pos.pop(key, None)

    def reset_target_state(self, dimension: str, target: Coord3) -> None:
        key = (dimension, target)
        self._stall_counts.pop(key, None)
        self._last_pos_for_target.pop(key, None)
        self._reset_water_state(key)

    def _run_write(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        dimension: str,
        pos: Sequence[float],
        fn: Callable[[], Dict[str, Any]],
        *,
        unknown_center: Optional[Coord3] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[NavOutcome]]:
        tx_id = self.journal.begin_write(endpoint, payload, dimension=dimension, pos=pos)
        try:
            res = fn()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(dimension, unknown_center or block_coord(pos))
                return None, NavOutcome("UNKNOWN", action_taken=endpoint, reason=err.code)
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            return None, NavOutcome("BLOCKED", action_taken=endpoint, reason=err.code, details=err.details)
        self.journal.resolve_write(tx_id, "confirmed")
        return res, None

    def _precision_nudge(
        self,
        dim: str,
        cur_p: Tuple[float, float, float],
        tgt: Tuple[float, float, float],
    ) -> NavOutcome:
        """Bridge upstream goNear() <= (range + 0.5)m deadband with one bounded edge-safe sneak pulse."""
        dx = float(tgt[0]) - cur_p[0]
        dz = float(tgt[2]) - cur_p[2]
        d_xz = math.hypot(dx, dz)
        if d_xz < 1e-4:
            return NavOutcome("PROGRESSED", action_taken="none", reason="precision_aligned")
        yaw = math.atan2(-dx, -dz)
        ms = max(140, min(420, int((d_xz / 1.3) * 1000)))
        payload = {"forward": True, "sneak": True, "yaw": round(yaw, 3), "ms": ms}
        res, err_out = self._run_write(
            "control",
            payload,
            dim,
            cur_p,
            lambda: self.client.control(forward=True, sneak=True, yaw=yaw, ms=ms, wait=True),
        )
        if err_out is not None:
            return err_out
        return NavOutcome(
            "PROGRESSED",
            action_taken="control",
            reason="precision_nudge",
            details=res.get("result", {}) if res else {},
        )

    @staticmethod
    def _deflect_waypoint_from_threats(
        cur_p: Tuple[float, float, float],
        wp_x: float,
        wp_z: float,
        threats: Sequence[Tuple[float, float, float]],
        *,
        avoid_radius: float = 9.0,
    ) -> Tuple[float, float, bool]:
        """If the segment cur_p -> (wp_x, wp_z) passes within avoid_radius of a threat, deflect laterally."""
        if not threats:
            return wp_x, wp_z, False
        dx = wp_x - cur_p[0]
        dz = wp_z - cur_p[2]
        seg_len_sq = dx * dx + dz * dz
        if seg_len_sq < 1e-4:
            return wp_x, wp_z, False
        seg_len = math.sqrt(seg_len_sq)
        for tx, _ty, tz in threats:
            # Closest point on segment cur_p -> wp to threat (tx, tz)
            t = max(0.0, min(1.0, ((tx - cur_p[0]) * dx + (tz - cur_p[2]) * dz) / seg_len_sq))
            cx = cur_p[0] + t * dx
            cz = cur_p[2] + t * dz
            d_perp = math.hypot(cx - tx, cz - tz)
            if d_perp < avoid_radius:
                # Choose perpendicular direction pointing away from the threat
                px1, pz1 = -dz / seg_len, dx / seg_len
                px2, pz2 = dz / seg_len, -dx / seg_len
                mid_x = cur_p[0] + 0.5 * dx
                mid_z = cur_p[2] + 0.5 * dz
                c1 = (mid_x + px1 * (avoid_radius + 2.0), mid_z + pz1 * (avoid_radius + 2.0))
                c2 = (mid_x + px2 * (avoid_radius + 2.0), mid_z + pz2 * (avoid_radius + 2.0))
                if math.hypot(c1[0] - tx, c1[1] - tz) >= math.hypot(c2[0] - tx, c2[1] - tz):
                    return round(c1[0], 1), round(c1[1], 1), True
                return round(c2[0], 1), round(c2[1], 1), True
        return wp_x, wp_z, False

    def _try_water_shoreline_recovery(
        self,
        dim: str,
        cur_p: Tuple[float, float, float],
        tgt: Tuple[float, float, float],
        status: Dict[str, Any],
        inv: Dict[str, int],
        avoid_block_breaking: Optional[Callable[[Coord3], bool]],
        *,
        stalls: int,
    ) -> Optional[NavOutcome]:
        """When water pulses stall, prefetch 3D shore cells and step to dry bank, dig soft overhang, or place scaffold."""
        fx, fy, fz = block_coord(cur_p)
        coords = [
            (fx + dx, fy + dy, fz + dz)
            for dx in range(-2, 3)
            for dz in range(-2, 3)
            for dy in (-1, 0, 1, 2)
        ]
        fetched = self.gateway.prefetch_blocks(dim, coords)

        dry_shores: List[Tuple[float, Coord3]] = []
        for dy in (1, 0):
            for dx in range(-2, 3):
                for dz in range(-2, 3):
                    if dx == 0 and dz == 0:
                        continue
                    cand = (fx + dx, fy + dy, fz + dz)
                    below = fetched.get((cand[0], cand[1] - 1, cand[2]))
                    feet = fetched.get(cand)
                    head = fetched.get((cand[0], cand[1] + 1, cand[2]))
                    if (
                        feet is not None and feet.name in PASS_THROUGH_BLOCKS and feet.name not in HAZARD_BLOCKS
                        and (head is None or (head.name in PASS_THROUGH_BLOCKS and head.name not in HAZARD_BLOCKS))
                        and below is not None and below.name not in NON_SOLID_BLOCKS and below.name not in HAZARD_BLOCKS
                    ):
                        score = dist2_xz((cand[0] + 0.5, cand[1], cand[2] + 0.5), tgt) + 0.25 * math.hypot(dx, dz)
                        dry_shores.append((score, cand))
        dry_shores.sort(key=lambda item: item[0])

        # On first shoreline recovery attempt (stalls == 2), swim straight onto an adjacent unobstructed dry bank
        if stalls == 2 and dry_shores:
            best_shore = dry_shores[0][1]
            if abs(best_shore[0] - fx) <= 1 and abs(best_shore[2] - fz) <= 1:
                swim_res = self.swimmer.swim_step(
                    (best_shore[0] + 0.5, float(best_shore[1]) + 0.5, best_shore[2] + 0.5),
                    status,
                    yaw_offset_rad=0.0,
                )
                if swim_res.status == "UNKNOWN":
                    return NavOutcome("UNKNOWN", action_taken="control", reason=swim_res.reason)
                if swim_res.status == "FAILED":
                    return NavOutcome("BLOCKED", action_taken="control", reason=swim_res.reason)
                return NavOutcome("PROGRESSED", action_taken="control", reason="water_shore_step")

        # Dig obstructing 1-block overhead ceiling or soft bank/overhang
        dig_candidates: List[Coord3] = [(fx, fy + 2, fz), (fx, fy + 1, fz)]
        cardinals = sorted(
            ((1, 0), (-1, 0), (0, 1), (0, -1)),
            key=lambda d: dist2_xz((fx + d[0] + 0.5, fy, fz + d[1] + 0.5), tgt),
        )
        for dy in (1, 0):
            for dx, dz in cardinals:
                dig_candidates.append((fx + dx, fy + dy, fz + dz))
        for c in dig_candidates:
            ent = fetched.get(c)
            if ent is None or ent.name in NON_SOLID_BLOCKS or ent.name in HAZARD_BLOCKS or ent.name == "bedrock":
                continue
            if ent.name not in _WATER_SOFT_DIG_BLOCKS:
                continue
            if avoid_block_breaking is not None and avoid_block_breaking(c):
                continue
            if self.gateway.unreachable.is_blocked(dim, c, "dig"):
                continue
            _, err_out = self._run_write(
                "dig", {"pos": list(c)}, dim, cur_p,
                lambda _c=c: self.client.dig(list(_c), collect=True),
                unknown_center=c,
            )
            if err_out is not None:
                if err_out.status == "BLOCKED":
                    self.gateway.unreachable.record_failure(dim, c, "dig", err_out.reason)
                return err_out
            self.gateway.write_through_dig(dim, c)
            return NavOutcome("PROGRESSED", action_taken="dig", reason="water_shore_clear", details={"pos": list(c)})

        # Place scaffold block at adjacent water/air cell with dry headroom to step onto
        scaffold = pick_best_item(inv, SCAFFOLD_BLOCKS)
        if scaffold is not None:
            for dx, dz in cardinals:
                c = (fx + dx, fy, fz + dz)
                if self.gateway.unreachable.is_blocked(dim, c, "place"):
                    continue
                cell_ent = fetched.get(c)
                above1 = fetched.get((c[0], c[1] + 1, c[2]))
                above2 = fetched.get((c[0], c[1] + 2, c[2]))
                if (
                    cell_ent is not None and cell_ent.name in NON_SOLID_BLOCKS and cell_ent.name not in HAZARD_BLOCKS
                    and above1 is not None and above1.name in PASS_THROUGH_BLOCKS and above1.name not in HAZARD_BLOCKS
                    and above2 is not None and above2.name in PASS_THROUGH_BLOCKS and above2.name not in HAZARD_BLOCKS
                ):
                    has_support = any(
                        (nbr := fetched.get((c[0] + ndx, c[1] + ndy, c[2] + ndz))) is not None
                        and nbr.name not in NON_SOLID_BLOCKS
                        for ndx, ndy, ndz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
                    )
                    if not has_support:
                        continue
                    _, err_out = self._run_write(
                        "place", {"item": scaffold, "pos": list(c)}, dim, cur_p,
                        lambda _s=scaffold, _c=c: self.client.place_block(_s, _c[0], _c[1], _c[2]),
                        unknown_center=c,
                    )
                    if err_out is not None:
                        if err_out.status == "BLOCKED":
                            self.gateway.unreachable.record_failure(dim, c, "place", err_out.reason)
                        return err_out
                    self.gateway.write_through_place(dim, c, scaffold)
                    return NavOutcome("PROGRESSED", action_taken="place", reason="water_shore_scaffold", details={"pos": list(c)})

        # Otherwise steer directly toward any prefetched dry shore cell within radius 2
        if dry_shores:
            best_shore = dry_shores[0][1]
            swim_res = self.swimmer.swim_step(
                (best_shore[0] + 0.5, float(best_shore[1]) + 0.5, best_shore[2] + 0.5),
                status,
                yaw_offset_rad=0.0,
            )
            if swim_res.status == "UNKNOWN":
                return NavOutcome("UNKNOWN", action_taken="control", reason=swim_res.reason)
            if swim_res.status == "FAILED":
                return NavOutcome("BLOCKED", action_taken="control", reason=swim_res.reason)
            return NavOutcome("PROGRESSED", action_taken="control", reason="water_shore_step")
        return None

    def step_toward(
        self,
        target_pos: Sequence[float],
        status: Dict[str, Any],
        *,
        tolerance: float = 2.0,
        inventory_counts: Optional[Dict[str, int]] = None,
        avoid_block_breaking: Optional[Callable[[Coord3], bool]] = None,
        prefer_3d: bool = False,
        xz_only: bool = False,
        threat_positions: Optional[Sequence[Tuple[float, float, float]]] = None,
        nearby_entities: Optional[Sequence[Dict[str, Any]]] = None,
        avoid_hard_rock_without_pick: Optional[bool] = None,
    ) -> NavOutcome:
        """Perform AT MOST ONE bounded navigation/clearing/bridging/pillaring write step toward target_pos."""
        pos = status.get("pos") or [0.0, 64.0, 0.0]
        cur_p = (float(pos[0]), float(pos[1]), float(pos[2]))
        dim = str(status.get("dimension") or "overworld")
        tgt = (float(target_pos[0]), float(target_pos[1]), float(target_pos[2]))
        tgt_cell = block_coord(tgt)
        inv = inventory_counts or {}
        threats = [tuple(float(v) for v in tp) for tp in (threat_positions or ()) if len(tp) == 3]

        distance = dist2_xz(cur_p, tgt) if xz_only else dist3(cur_p, tgt)
        if distance <= tolerance:
            self._pending_pillar = None
            self.reset_target_state(dim, tgt_cell)
            if status.get("in_vehicle"):
                _, err_out = self._run_write("dismount", {}, dim, cur_p, lambda: self.client.dismount())
                if err_out is not None:
                    return err_out
                return NavOutcome("PROGRESSED", action_taken="dismount", reason="dismounted_at_target")
            return NavOutcome("REACHED", action_taken="none", reason="within_tolerance")

        # 0) Complete pending pillar-up placement launched on previous tick while airborne!
        if self._pending_pillar is not None:
            p_dim, p_target, p_item = self._pending_pillar
            if p_dim == dim and inv.get(p_item, 0) > 0:
                if block_coord(cur_p) == p_target and not status.get("on_ground", True):
                    # Still rising toward jump apex (y < p_target.y + 1.0) -> keep armed for next tick!
                    return NavOutcome("PROGRESSED", action_taken="none", reason="pillar_rising")
                self._pending_pillar = None
                if block_coord(cur_p) != p_target and cur_p[1] >= p_target[1] + 0.8:
                    res, err_out = self._run_write(
                        "place", {"item": p_item, "pos": list(p_target)}, dim, cur_p,
                        lambda: self.client.place_block(p_item, p_target[0], p_target[1], p_target[2]),
                        unknown_center=p_target,
                    )
                    if err_out is not None:
                        if err_out.status == "BLOCKED" and err_out.reason == "blocked_by_self":
                            self.gateway.unreachable.record_blocked_edge(dim, p_target, (p_target[0], p_target[1] + 1, p_target[2]))
                        return err_out
                    self.gateway.write_through_place(dim, p_target, p_item)
                    self._stall_counts[(dim, tgt_cell)] = 0
                    return NavOutcome("PROGRESSED", action_taken="place", reason="pillared_up", details={"pos": list(p_target)})
                # Jump landed back on same cell without rising (e.g. ceiling/lag) -> blacklist this pillar edge immediately!
                self.gateway.unreachable.record_blocked_edge(dim, p_target, (p_target[0], p_target[1] + 1, p_target[2]))
            else:
                self._pending_pillar = None

        # 0.5) Already riding a vehicle (e.g. boat crossing water) -> steer via move_to or dismount at shore
        if status.get("in_vehicle"):
            if not status.get("in_water") and dist2_xz(cur_p, tgt) <= max(tolerance + 2.0, 3.5):
                _, err_out = self._run_write("dismount", {}, dim, cur_p, lambda: self.client.dismount())
                if err_out is not None:
                    return err_out
                return NavOutcome("PROGRESSED", action_taken="dismount", reason="dismounted_on_shore")
            rx, rz = tgt[0], tgt[2]
            if dist2_xz(cur_p, tgt) > self.RELAY_THRESHOLD:
                ratio = self.RELAY_STEP / max(1e-6, dist2_xz(cur_p, tgt))
                rx = cur_p[0] + (tgt[0] - cur_p[0]) * min(1.0, ratio)
                rz = cur_p[2] + (tgt[2] - cur_p[2]) * min(1.0, ratio)
            payload = {"x": round(rx, 1), "z": round(rz, 1), "range": max(tolerance, 2.0), "timeout_ms": 10000}
            res, err_out = self._run_write(
                "move_to", payload, dim, cur_p,
                lambda: self.client.move_to(x=payload["x"], z=payload["z"], range=payload["range"], timeout_ms=10000),
            )
            if err_out is not None:
                if err_out.status == "BLOCKED":
                    # Hit shore or obstacle while boating -> dismount on next tick
                    self.gateway.unreachable.record_blocked_edge(dim, block_coord(cur_p), tgt_cell)
                return err_out
            return NavOutcome("PROGRESSED", action_taken="move_to", reason="drive_vehicle", details=res.get("result", {}) if res else {})

        # 1) Water tracking & open long-distance crossing (boat / direct swim only when >10m and unstalled)
        key = (dim, tgt_cell)
        in_water = bool(status.get("in_water") or status.get("submerged"))
        water_stalls = 0
        if in_water:
            prev_steps = self._water_steps.get(key, 0)
            prev_wpos = self._water_last_pos.get(key)
            anchor = self._water_anchor_pos.setdefault(key, cur_p)
            if prev_steps >= 4 and dist3(anchor, cur_p) < 0.5:
                self._reset_water_state(key)
                self.gateway.unreachable.record_failure(dim, tgt_cell, "navigate", "water_stalled_no_displacement")
                self.gateway.unreachable.record_failure(dim, tgt_cell, "navigate", "water_stalled_no_displacement")
                return NavOutcome("BLOCKED", action_taken="control", reason="water_stalled_no_displacement")

            if prev_wpos is not None and dist3(prev_wpos, cur_p) < self.MIN_PROGRESS_METERS:
                water_stalls = self._water_stalls.get(key, 0) + 1
            elif prev_wpos is not None:
                water_stalls = 0
                if dist3(anchor, cur_p) >= 0.5:
                    self._water_anchor_pos[key] = cur_p
                    prev_steps = 0
            else:
                water_stalls = 0
            self._water_steps[key] = prev_steps + 1
            self._water_stalls[key] = water_stalls
            self._water_last_pos[key] = cur_p

            if not status.get("submerged") and dist2_xz(cur_p, tgt) > 10.0 and water_stalls == 0:
                # Mount an already-deployed boat right next to us
                for ent in (nearby_entities or ()):
                    ename = str(ent.get("name") or "")
                    epos = ent.get("pos")
                    if ename.endswith("_boat") or ename == "boat" or ename == "bamboo_raft":
                        if isinstance(epos, list) and len(epos) == 3 and dist3(cur_p, epos) <= 3.5:
                            eid = int(ent["id"])
                            _, err_out = self._run_write(
                                "interact", {"entity_id": eid}, dim, cur_p,
                                lambda _eid=eid: self.client.interact(entity_id=_eid),
                            )
                            if err_out is None:
                                self._water_last_pos[key] = cur_p
                                return NavOutcome("PROGRESSED", action_taken="interact", reason="boarded_boat")
                            if err_out.status == "UNKNOWN":
                                return err_out
                            break
                # Or deploy a boat from inventory on water surface
                boat_item = pick_best_item(inv, BOAT_ITEMS)
                if boat_item is not None and not self.gateway.unreachable.is_blocked(dim, block_coord(cur_p), "deploy_boat"):
                    deploy_cell = list(block_coord(cur_p))
                    _, err_out = self._run_write(
                        "use_item", {"item": boat_item, "target": {"pos": deploy_cell}}, dim, cur_p,
                        lambda bi=boat_item, dc=deploy_cell: self.client.use_item(bi, target={"pos": dc}),
                    )
                    if err_out is None:
                        self._water_last_pos[key] = cur_p
                        return NavOutcome("PROGRESSED", action_taken="use_item", reason="deployed_boat")
                    if err_out.status == "UNKNOWN":
                        return err_out
                    self.gateway.unreachable.record_failure(dim, block_coord(cur_p), "deploy_boat", err_out.reason)

                swim_res = self.swimmer.swim_step(tgt, status)
                if swim_res.status == "UNKNOWN":
                    return NavOutcome("UNKNOWN", action_taken="control", reason=swim_res.reason)
                if swim_res.status == "FAILED":
                    return NavOutcome("BLOCKED", action_taken="control", reason=swim_res.reason)
                return NavOutcome("PROGRESSED", action_taken="control", reason=swim_res.status.lower())

            stalled = water_stalls > 0
            use_3d = True
        else:
            self._reset_water_state(key)
            self.swimmer.reset()
            prev_pos = self._last_pos_for_target.get(key)
            stalled = prev_pos is not None and dist3(prev_pos, cur_p) < self.MIN_PROGRESS_METERS
            if stalled:
                self._stall_counts[key] = self._stall_counts.get(key, 0) + 1
                if self._stall_counts[key] >= 6:
                    self._stall_counts[key] = 0
                    self.gateway.unreachable.record_failure(dim, tgt_cell, "navigate", "stalled_no_displacement")
                    self.gateway.unreachable.record_failure(dim, tgt_cell, "navigate", "stalled_no_displacement")
                    return NavOutcome("BLOCKED", action_taken="move_to", reason="stalled_no_displacement")
            elif prev_pos is not None:
                self._stall_counts[key] = 0
            self._last_pos_for_target[key] = cur_p
            use_3d = prefer_3d or self._stall_counts.get(key, 0) >= 1

        fx_b, fy_b, fz_b = block_coord(cur_p)
        underground_enclosed = fy_b < 58 and any(
            (ent := self.gateway.get_block(dim, (fx_b, fy_b + dy, fz_b), allow_fetch=False)) is not None
            and ent.name not in (NON_SOLID_BLOCKS | LEAF_BLOCKS)
            for dy in range(2, 6)
        )
        relay_step = 12.0 if underground_enclosed else self.RELAY_STEP
        relay_timeout_ms = 5000 if (underground_enclosed or self._stall_counts.get(key, 0) > 0) else 8000
        direct_timeout_ms = 5000 if (underground_enclosed or self._stall_counts.get(key, 0) > 0) else 10000

        # 2) Direct or relay move_to (no 32m hard cap!), with threat-repulsion lateral deflection
        if not use_3d:
            if distance > self.RELAY_THRESHOLD:
                ratio = relay_step / max(1e-6, dist2_xz(cur_p, tgt))
                rx = cur_p[0] + (tgt[0] - cur_p[0]) * min(1.0, ratio)
                rz = cur_p[2] + (tgt[2] - cur_p[2]) * min(1.0, ratio)
                rx, rz, deflected = self._deflect_waypoint_from_threats(cur_p, rx, rz, threats)
                payload = {"x": round(rx, 1), "z": round(rz, 1), "range": 2.0, "timeout_ms": relay_timeout_ms}
                res, err_out = self._run_write(
                    "move_to", payload, dim, cur_p,
                    lambda: self.client.move_to(x=payload["x"], z=payload["z"], range=2.0, timeout_ms=relay_timeout_ms),
                )
            else:
                wx, wz, deflected = self._deflect_waypoint_from_threats(cur_p, tgt[0], tgt[2], threats)
                ry = None if (xz_only or deflected) else tgt[1]
                w_range = 2.0 if deflected else tolerance
                if (not deflected) and (not xz_only) and ry is not None and abs(float(ry) - cur_p[1]) < 0.5:
                    d_exact = dist3(cur_p, tgt)
                    d_xz = dist2_xz(cur_p, tgt)
                    if d_exact <= 0.55 or (stalled and d_xz <= 1.15 and tolerance < 0.6):
                        return self._precision_nudge(dim, cur_p, tgt)
                    if d_exact <= w_range + 0.55:
                        w_range = 0.0
                payload = {"x": round(wx, 2), "y": ry, "z": round(wz, 2), "range": w_range, "timeout_ms": direct_timeout_ms}
                res, err_out = self._run_write(
                    "move_to", payload, dim, cur_p,
                    lambda: self.client.move_to(x=payload["x"], y=ry, z=payload["z"], range=w_range, timeout_ms=direct_timeout_ms),
                )

            if err_out is None:
                return NavOutcome("PROGRESSED", action_taken="move_to", reason="threat_detour" if deflected else "", details=res.get("result", {}) if res else {})
            if err_out.status == "UNKNOWN":
                return err_out
            # move_to failed (e.g. path_not_found across leaves/wall/pit) -> escalate to 3D on next tick
            self._stall_counts[key] = self._stall_counts.get(key, 0) + 1
            return NavOutcome("PROGRESSED", action_taken="move_to_fallback_3d", reason=err_out.reason)

        # 3) Local 3D A* step (micro-dig / pillar-up / bridge / swim / climb_shore / detour walk)
        if in_water and water_stalls >= 2:
            rec_out = self._try_water_shoreline_recovery(
                dim, cur_p, tgt, status, inv, avoid_block_breaking, stalls=water_stalls
            )
            if rec_out is not None:
                return rec_out

        scaffold = pick_best_item(inv, SCAFFOLD_BLOCKS)
        start_cell = block_coord(cur_p)
        sub_goal = tgt_cell
        if dist3(start_cell, tgt_cell) > 9.0:
            ratio = 8.0 / max(1e-6, dist3(start_cell, tgt_cell))
            sub_goal = (
                int(round(start_cell[0] + (tgt_cell[0] - start_cell[0]) * ratio)),
                int(round(start_cell[1] + (tgt_cell[1] - start_cell[1]) * ratio)),
                int(round(start_cell[2] + (tgt_cell[2] - start_cell[2]) * ratio)),
            )

        enforce_rock_guard = (
            avoid_hard_rock_without_pick
            if avoid_hard_rock_without_pick is not None
            else (start_cell[1] < 58)
        )
        no_pickaxe = (
            enforce_rock_guard
            and (inventory_counts is not None)
            and (not any(inv.get(p, 0) > 0 for p in PICKAXE_TIERS))
        )
        plan_avoid = avoid_block_breaking
        if no_pickaxe:
            def _avoid_rock_bare_hand(cell: Coord3, _base=avoid_block_breaking, _d=dim) -> bool:
                if _base is not None and _base(cell):
                    return True
                ent = self.gateway.peek_block(_d, cell)
                return ent is not None and ent.name in _HARD_ROCK_NO_PICK_BLOCKS
            plan_avoid = _avoid_rock_bare_hand

        steps = self.pathfinder.plan(
            dim,
            start_cell,
            sub_goal,
            tolerance=min(tolerance, 2.0),
            has_scaffold=scaffold is not None,
            avoid_block_breaking=plan_avoid,
            threat_positions=threats,
        )
        if not steps:
            if in_water:
                swim_res = self.swimmer.swim_step(tgt, status)
                if swim_res.status == "UNKNOWN":
                    return NavOutcome("UNKNOWN", action_taken="control", reason=swim_res.reason)
                if swim_res.status == "FAILED":
                    return NavOutcome("BLOCKED", action_taken="control", reason=swim_res.reason)
                return NavOutcome("PROGRESSED", action_taken="control", reason=swim_res.status.lower())
            self.gateway.unreachable.record_failure(dim, tgt_cell, "navigate", "no_3d_path")
            return NavOutcome("BLOCKED", action_taken="pathfinder_3d", reason="no_3d_path")

        step: NavStep = steps[0]
        # If an upcoming dig step is already within reach (<= 3.8m), execute the dig directly
        # instead of wasting a tick on a tiny walk adjustment.
        for upcoming in steps[:4]:
            if upcoming.action == "dig" and dist3(cur_p, (upcoming.target[0] + 0.5, upcoming.target[1] + 0.5, upcoming.target[2] + 0.5)) <= 3.8:
                step = upcoming
                break
            if upcoming.action != "walk":
                break

        if step.action in ("swim", "climb_shore") or (in_water and step.action == "walk"):
            swim_tgt = (step.stand_after[0] + 0.5, float(step.stand_after[1]), step.stand_after[2] + 0.5)
            swim_res = self.swimmer.swim_step(swim_tgt, status)
            if swim_res.status == "UNKNOWN":
                return NavOutcome("UNKNOWN", action_taken="control", reason=swim_res.reason)
            if swim_res.status == "FAILED":
                return NavOutcome("BLOCKED", action_taken="control", reason=swim_res.reason)
            reason = "3d_climb_shore" if step.action == "climb_shore" else swim_res.status.lower()
            return NavOutcome("PROGRESSED", action_taken="control", reason=reason, details={"to": list(step.stand_after)})

        if step.action == "dig":
            if no_pickaxe:
                dig_ent = self.gateway.peek_block(dim, step.target)
                if dig_ent is not None and dig_ent.name in _HARD_ROCK_NO_PICK_BLOCKS:
                    self.gateway.unreachable.record_failure(dim, step.target, "dig", "no_pickaxe_for_stone")
                    return NavOutcome("BLOCKED", action_taken="none", reason="no_pickaxe_for_stone", details={"pos": list(step.target)})
            res, err_out = self._run_write(
                "dig", {"pos": list(step.target)}, dim, cur_p,
                lambda: self.client.dig(list(step.target), collect=True),
                unknown_center=step.target,
            )
            if err_out is not None:
                if err_out.status == "BLOCKED":
                    self.gateway.unreachable.record_failure(dim, step.target, "dig", err_out.reason)
                return err_out
            self.gateway.write_through_dig(dim, step.target)
            # Keep 3D active if a 2nd block (e.g. lower half of 1x2 wall) still needs digging
            if step.extra is None:
                self._stall_counts[key] = 0
            dig_reason = "water_shore_clear" if in_water else "cleared_3d_obstacle"
            return NavOutcome("PROGRESSED", action_taken="dig", reason=dig_reason, details={"pos": list(step.target)})

        if step.action == "bridge" and scaffold is not None:
            res, err_out = self._run_write(
                "place", {"item": scaffold, "pos": list(step.target)}, dim, cur_p,
                lambda: self.client.place_block(scaffold, step.target[0], step.target[1], step.target[2]),
                unknown_center=step.target,
            )
            if err_out is not None:
                if err_out.status == "BLOCKED":
                    self.gateway.unreachable.record_blocked_edge(dim, start_cell, step.stand_after)
                    self.gateway.unreachable.record_failure(dim, step.target, "place", err_out.reason)
                return err_out
            self.gateway.write_through_place(dim, step.target, scaffold)
            self._stall_counts[key] = 0
            return NavOutcome("PROGRESSED", action_taken="place", reason="bridged_gap", details={"pos": list(step.target)})

        if step.action == "pillar_up" and scaffold is not None:
            # On real mineflayer (session.js:2255), placing inside own feet cell fails with `blocked_by_self`
            # unless the bot is already airborne (jumping). Emit a bounded jump pulse first (<= 1 write/tick).
            if status.get("on_ground", True) and block_coord(cur_p) == step.target:
                res, err_out = self._run_write(
                    "control", {"jump": True, "ms": 500, "wait": False}, dim, cur_p,
                    lambda: self.client.control(jump=True, ms=500, wait=False),
                )
                if err_out is not None:
                    return err_out
                self._pending_pillar = (dim, step.target, scaffold)
                return NavOutcome("PROGRESSED", action_taken="control", reason="pillar_jump_launch")
            res, err_out = self._run_write(
                "place", {"item": scaffold, "pos": list(step.target)}, dim, cur_p,
                lambda: self.client.place_block(scaffold, step.target[0], step.target[1], step.target[2]),
                unknown_center=step.target,
            )
            if err_out is not None:
                if err_out.status == "BLOCKED" and err_out.reason == "blocked_by_self":
                    self.gateway.unreachable.record_blocked_edge(dim, start_cell, step.stand_after)
                return err_out
            self.gateway.write_through_place(dim, step.target, scaffold)
            self._stall_counts[key] = 0
            return NavOutcome("PROGRESSED", action_taken="place", reason="pillared_up", details={"pos": list(step.target)})

        # Default: walk step (coalesce consecutive plain walk steps up to 6 blocks, exact range=0.4)
        walk_end = step.stand_after
        has_following_non_walk = False
        for s in steps[1:6]:
            if s.action != "walk":
                has_following_non_walk = True
                break
            walk_end = s.stand_after
        walk_center = (walk_end[0] + 0.5, float(walk_end[1]), walk_end[2] + 0.5)
        if abs(walk_center[1] - cur_p[1]) < 0.5 and dist3(cur_p, walk_center) <= 0.55:
            return self._precision_nudge(dim, cur_p, walk_center)
        walk_range = 0.0 if dist3(cur_p, walk_center) <= 0.95 else 0.4
        res, err_out = self._run_write(
            "move_to", {"x": walk_center[0], "y": walk_center[1], "z": walk_center[2], "range": walk_range}, dim, cur_p,
            lambda: self.client.move_to(x=walk_center[0], y=walk_center[1], z=walk_center[2], range=walk_range, timeout_ms=8000),
        )
        if err_out is not None:
            if err_out.status == "BLOCKED":
                self.gateway.unreachable.record_blocked_edge(dim, start_cell, step.stand_after)
            return err_out
        if not has_following_non_walk and not stalled:
            self._stall_counts[key] = 0
        return NavOutcome("PROGRESSED", action_taken="move_to", reason="3d_walk_step", details={"to": list(walk_end)})
