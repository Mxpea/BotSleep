"""L2 HardSafety branches: PendingUnknownGuard + Lava/Fire/Drown/Threat preemption.

Includes v22/v31 battlefield rule:
- Canopy & sealed burrow ground-mob exemption: when elevated `|dy| >= 3` above a ground melee
  mob (zombie/spider) and NOT recently hurt, do NOT infinitely preempt canopy descent or crafting.
  Ranged mobs (skeleton/pillager) or actual `hurt` events always trigger threat response.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.domain import (
    AIR_BLOCKS,
    HAZARD_BLOCKS,
    HOSTILE_MOBS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    PASS_THROUGH_BLOCKS,
    RANGED_MOBS,
    SCAFFOLD_BLOCKS,
    WATER_BLOCKS,
    Coord3,
    block_coord,
    dist2_xz,
    dist3,
    pick_best_item,
)
from v2.gateway.surface_swim import SurfaceSwimExecutor
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError
from v2.kernel.journal import PendingTransactionJournal


class PendingUnknownGuard(Node):
    """Highest-priority BT branch: freezes ALL writes whenever a pending unknown transaction exists."""

    def __init__(self, journal: PendingTransactionJournal) -> None:
        super().__init__("PendingUnknownGuard")
        self.journal = journal

    def update(self, bb: Blackboard) -> Status:
        if self.journal.has_pending_unknown():
            tx = self.journal.get_pending()
            bb.set("pending_frozen", True, writer=self.name)
            bb.set("pending_tx", tx, writer=self.name)
            bb.last_action_outcome = "frozen_pending_unknown"
            return Status.RUNNING
        bb.set("pending_frozen", False, writer=self.name)
        return Status.FAILURE


class HardSafetyBranch(Node):
    """Reactive survival reflexes: lava/fire escape, low-oxygen surfacing, shield defense, and hysteresis threat evasion."""

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        threat_radius: float = 8.0,
        threat_exit_radius: Optional[float] = None,
        threat_dwell_seconds: float = 2.5,
        low_oxygen_threshold: int = 10,
    ) -> None:
        super().__init__("HardSafetyBranch")
        self.gateway = gateway
        self.journal = journal
        self.threat_radius = float(threat_radius)
        self.threat_exit_radius = (
            float(threat_exit_radius)
            if threat_exit_radius is not None
            else max(self.threat_radius + 4.0, 12.0)
        )
        self.threat_dwell_seconds = max(0.0, float(threat_dwell_seconds))
        self.low_oxygen_threshold = int(low_oxygen_threshold)
        self._swimmer = SurfaceSwimExecutor(gateway.client, journal)
        self._douse_cooldown_until: float = 0.0
        self._evasion_active: bool = False
        self._evasion_dwell_until: float = 0.0
        self._shield_raised: bool = False
        self._drown_last_pos: Optional[Tuple[float, float, float]] = None
        self._drown_stalls: int = 0
        self._last_all_near: List[Tuple[float, Dict[str, Any]]] = []
        self._last_retreat_failed: bool = False
        self._burrow_origin: Optional[Coord3] = None
        self._burrow_deep: bool = False

    def _collect_actionable_threats(self, bb: Blackboard, max_radius: float) -> List[Tuple[float, Dict[str, Any]]]:
        """Collect all actionable hostiles within max_radius, exempting unreachable canopy/burrow ground melee mobs."""
        px, py, pz = bb.pos
        recently_hurt = (time.time() - bb.last_hurt_at) <= 6.0
        in_burrow = bool(bb.get("in_sealed_burrow", False))

        candidates: List[Tuple[float, Dict[str, Any]]] = []
        for ent in bb.entities:
            name = str(ent.get("name") or "")
            etype = str(ent.get("type") or "")
            if name not in HOSTILE_MOBS and etype != "hostile":
                continue
            epos = ent.get("pos")
            if not isinstance(epos, list) or len(epos) != 3:
                continue
            d3 = dist3((px, py, pz), epos)
            if d3 > max_radius:
                continue
            dy = py - float(epos[1])
            is_ranged = name in RANGED_MOBS

            # Sealed burrow exemption unless actually hurt
            if in_burrow and not recently_hurt:
                continue
            # High canopy / elevation exemption: ground melee mob >= 3 blocks below and not recently hurt
            if not is_ranged and dy >= 3.0 and not recently_hurt:
                continue
            candidates.append((d3, ent))

        candidates.sort(key=lambda pair: pair[0])
        return candidates

    def _find_actionable_threat(self, bb: Blackboard) -> Optional[Dict[str, Any]]:
        """Apply enter/exit radius hysteresis + dwell timer and publish active threat coords for navigation repulsion."""
        now = time.monotonic()
        repulsion_scan_r = max(self.threat_exit_radius + 4.0, 16.0)
        all_near = self._collect_actionable_threats(bb, repulsion_scan_r)
        self._last_all_near = all_near
        bb.set(
            "active_threat_positions",
            [tuple(float(v) for v in ent["pos"]) for _, ent in all_near],
            writer=self.name,
        )

        if not all_near:
            self._evasion_active = False
            bb.set("threat_hysteresis_active", False, writer=self.name)
            return None

        closest_dist, closest_ent = all_near[0]
        if not self._evasion_active:
            if closest_dist <= self.threat_radius:
                self._evasion_active = True
                self._evasion_dwell_until = now + self.threat_dwell_seconds
                bb.set("threat_hysteresis_active", True, writer=self.name)
                return closest_ent
            bb.set("threat_hysteresis_active", False, writer=self.name)
            return None

        # Hysteresis active: keep evading until threat clears threat_exit_radius AND dwell timer elapses
        if closest_dist <= self.threat_exit_radius or now < self._evasion_dwell_until:
            if closest_dist <= self.threat_radius:
                self._evasion_dwell_until = now + self.threat_dwell_seconds
            bb.set("threat_hysteresis_active", True, writer=self.name)
            return closest_ent

        self._evasion_active = False
        bb.set("threat_hysteresis_active", False, writer=self.name)
        return None

    @staticmethod
    def _has_shield(bb: Blackboard) -> bool:
        if bb.inventory_counts.get("shield", 0) > 0:
            return True
        off = bb.inventory.get("offhand")
        return isinstance(off, dict) and off.get("name") == "shield"

    def _write_action(self, bb: Blackboard, endpoint: str, payload: Dict[str, Any], fn: Any) -> Tuple[Status, Optional[Dict[str, Any]], bool]:
        tx_id = self.journal.begin_write(endpoint, payload, dimension=bb.dimension, pos=bb.pos)
        bb.writes_this_tick += 1
        try:
            env = fn()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(bb.dimension, block_coord(bb.pos))
                bb.set("pending_frozen", True, writer=self.name)
                bb.last_action_outcome = "safety_unknown"
                return Status.RUNNING, None, False
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            bb.last_action_outcome = f"safety_failed:{err.code}"
            return Status.RUNNING, None, False
        self.journal.resolve_write(tx_id, "confirmed")
        res = env.get("result") if isinstance(env, dict) else None
        return Status.RUNNING, (res if isinstance(res, dict) else {}), True

    @staticmethod
    def _has_melee_weapon(counts: Dict[str, int]) -> bool:
        return any(k.endswith(("_sword", "_axe")) and v > 0 for k, v in counts.items())

    _SCAFFOLD_DROPPING_BLOCKS = frozenset({
        "stone", "cobblestone", "cobbled_deepslate", "deepslate", "dirt", "grass_block",
        "netherrack", "andesite", "diorite", "granite", "oak_planks", "spruce_planks", "birch_planks",
    })
    _BURROW_PROTECTED = frozenset({
        "bedrock", "obsidian", "crafting_table", "furnace", "chest", "blast_furnace", "smoker", "nether_portal",
    })

    def _is_dry_retreat_path(self, bb: Blackboard, tx: float, tz: float) -> bool:
        px, py, pz = bb.pos
        fy = math.floor(py)
        dist = math.hypot(tx - px, tz - pz)
        steps = max(2, int(math.ceil(dist / 1.5)))
        seen_cols: set[Tuple[int, int]] = {(math.floor(px), math.floor(pz))}
        for i in range(1, steps + 1):
            frac = i / steps
            bx = math.floor(px + (tx - px) * frac)
            bz = math.floor(pz + (tz - pz) * frac)
            if (bx, bz) in seen_cols:
                continue
            seen_cols.add((bx, bz))
            feet = self.gateway.get_block(bb.dimension, (bx, fy, bz), allow_fetch=True)
            head = self.gateway.get_block(bb.dimension, (bx, fy + 1, bz), allow_fetch=True)
            floor = self.gateway.get_block(bb.dimension, (bx, fy - 1, bz), allow_fetch=True)
            for ent in (feet, head, floor):
                if ent is not None and ent.name in (LIQUID_BLOCKS | HAZARD_BLOCKS):
                    return False
            if floor is None:
                return False
            if feet is not None and feet.name not in PASS_THROUGH_BLOCKS:
                # Allow 1-block step up only if head and fy+2 are dry pass-through
                head2 = self.gateway.get_block(bb.dimension, (bx, fy + 2, bz), allow_fetch=True)
                if (
                    (head is not None and head.name not in PASS_THROUGH_BLOCKS)
                    or (head2 is not None and head2.name not in PASS_THROUGH_BLOCKS)
                ):
                    return False
            else:
                if head is not None and head.name not in PASS_THROUGH_BLOCKS:
                    return False
                if floor.name in NON_SOLID_BLOCKS:
                    sub = self.gateway.get_block(bb.dimension, (bx, fy - 2, bz), allow_fetch=True)
                    if sub is None or sub.name in (NON_SOLID_BLOCKS | HAZARD_BLOCKS):
                        return False
        return True

    def _select_dry_retreat_target(
        self,
        bb: Blackboard,
        epos: Any,
        d: float,
    ) -> Tuple[float, float, bool]:
        px, _, pz = bb.pos
        vx, vz = px - float(epos[0]), pz - float(epos[2])
        norm = math.hypot(vx, vz)
        if norm < 1e-6:
            ux, uz = 1.0, 0.0
        else:
            ux, uz = vx / norm, vz / norm
        retreat_step = min(max(6.0, self.threat_exit_radius - d + 2.5), 10.0)
        distances = tuple(dict.fromkeys([retreat_step, min(retreat_step, 6.0), 4.0]))
        primary_angles = (0.0, 45.0, -45.0, 90.0, -90.0)
        fallback_angles = (135.0, -135.0)
        for angle_group, dist_list in ((primary_angles, distances), (fallback_angles, (4.0,))):
            for dist_cand in dist_list:
                for deg in angle_group:
                    rad = math.radians(deg)
                    ca, sa = math.cos(rad), math.sin(rad)
                    dx = ux * ca - uz * sa
                    dz = ux * sa + uz * ca
                    cx = round(px + dx * dist_cand, 1)
                    cz = round(pz + dz * dist_cand, 1)
                    if self._is_dry_retreat_path(bb, cx, cz):
                        return cx, cz, True
        return round(px + ux * retreat_step, 1), round(pz + uz * retreat_step, 1), False

    def _can_safe_burrow(self, bb: Blackboard) -> bool:
        fx, fy, fz = block_coord(bb.pos)
        d1 = self.gateway.get_block(bb.dimension, (fx, fy - 1, fz), allow_fetch=True)
        d2 = self.gateway.get_block(bb.dimension, (fx, fy - 2, fz), allow_fetch=True)
        floor = self.gateway.get_block(bb.dimension, (fx, fy - 3, fz), allow_fetch=True)
        if d1 is None or d1.name in (AIR_BLOCKS | LIQUID_BLOCKS | HAZARD_BLOCKS | self._BURROW_PROTECTED) or d1.name.endswith("_bed"):
            return False
        if d2 is None or d2.name in (LIQUID_BLOCKS | HAZARD_BLOCKS | self._BURROW_PROTECTED) or d2.name.endswith("_bed"):
            return False
        if floor is None or floor.name in (NON_SOLID_BLOCKS | HAZARD_BLOCKS):
            return False
        for dy in (-1, -2):
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = self.gateway.get_block(bb.dimension, (fx + dx, fy + dy, fz + dz), allow_fetch=True)
                if nb is not None and nb.name in (LIQUID_BLOCKS | HAZARD_BLOCKS):
                    return False
        has_scaffold = pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS) is not None
        if not has_scaffold:
            has_scaffold = (
                d1.name in self._SCAFFOLD_DROPPING_BLOCKS
                or d2.name in self._SCAFFOLD_DROPPING_BLOCKS
            )
        return has_scaffold

    def _step_emergency_burrow(self, bb: Blackboard) -> Optional[Status]:
        if self._burrow_origin is None:
            if not self._can_safe_burrow(bb):
                return None
            self._burrow_origin = block_coord(bb.pos)
            self._burrow_deep = False

        bx, start_fy, bz = self._burrow_origin
        cur = block_coord(bb.pos)
        if (cur[0], cur[2]) != (bx, bz) and cur[1] >= start_fy - 1:
            self._burrow_origin = None
            self._burrow_deep = False
            return None

        target_y = start_fy - (3 if self._burrow_deep else 2)
        cap_y = target_y + 2

        if cur[1] > target_y:
            dig_cell = (bx, cur[1] - 1, bz)
            floor_check = self.gateway.get_block(bb.dimension, (bx, cur[1] - 2, bz), allow_fetch=True)
            dig_ent = self.gateway.get_block(bb.dimension, dig_cell, allow_fetch=True)
            if floor_check is None or floor_check.name in (LIQUID_BLOCKS | HAZARD_BLOCKS):
                self._burrow_origin = None
                self._burrow_deep = False
                return None
            if cur[1] - 1 == target_y and floor_check.name in NON_SOLID_BLOCKS:
                self._burrow_origin = None
                self._burrow_deep = False
                return None
            if dig_ent is not None and dig_ent.name not in AIR_BLOCKS:
                if dig_ent.name in (LIQUID_BLOCKS | HAZARD_BLOCKS | self._BURROW_PROTECTED):
                    self._burrow_origin = None
                    self._burrow_deep = False
                    return None
                bb.last_action_outcome = "safety_burrow_dig"
                status_ret, _, ok = self._write_action(
                    bb,
                    "dig",
                    {"pos": list(dig_cell), "collect": True},
                    lambda _c=dig_cell: self.gateway.client.dig(list(_c), collect=True),
                )
                if ok:
                    self.gateway.write_through_dig(bb.dimension, dig_cell)
                else:
                    self._burrow_origin = None
                    self._burrow_deep = False
                return status_ret
            # Already dug; allow gravity settle
            bb.last_action_outcome = "safety_burrow_settle"
            return Status.RUNNING

        cap_cell = (bx, cap_y, bz)
        cap_ent = self.gateway.get_block(bb.dimension, cap_cell, max_age=2.0, allow_fetch=True)
        if cap_ent is not None and cap_ent.name not in NON_SOLID_BLOCKS:
            bb.set("in_sealed_burrow", True, writer=self.name)
            bb.last_action_outcome = "safety_burrow_sealed"
            self._burrow_origin = None
            self._burrow_deep = False
            self._last_retreat_failed = False
            return Status.FAILURE

        scaffold = pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS)
        if scaffold is None:
            self._burrow_origin = None
            self._burrow_deep = False
            return None

        bb.last_action_outcome = "safety_burrow_cap"
        status_ret, _, ok = self._write_action(
            bb,
            "place",
            {"item": scaffold, "pos": list(cap_cell)},
            lambda _s=scaffold, _c=cap_cell: self.gateway.client.place_block(_s, _c[0], _c[1], _c[2]),
        )
        if ok:
            self.gateway.write_through_place(bb.dimension, cap_cell, scaffold)
            bb.set("in_sealed_burrow", True, writer=self.name)
            bb.last_action_outcome = "safety_burrow_sealed"
            self._burrow_origin = None
            self._burrow_deep = False
            self._last_retreat_failed = False
            self._evasion_active = False
            bb.set("threat_hysteresis_active", False, writer=self.name)
        elif "no_support" in bb.last_action_outcome and not self._burrow_deep:
            d3 = self.gateway.get_block(bb.dimension, (bx, target_y - 1, bz), allow_fetch=True)
            f3 = self.gateway.get_block(bb.dimension, (bx, target_y - 2, bz), allow_fetch=True)
            if (
                d3 is not None and d3.name not in (NON_SOLID_BLOCKS | LIQUID_BLOCKS | HAZARD_BLOCKS | self._BURROW_PROTECTED)
                and f3 is not None and f3.name not in (NON_SOLID_BLOCKS | HAZARD_BLOCKS)
            ):
                self._burrow_deep = True
            else:
                self._burrow_origin = None
        else:
            self._burrow_origin = None
            self._burrow_deep = False
        return status_ret

    def update(self, bb: Blackboard) -> Status:
        st = bb.status
        if not st.get("alive", True):
            return Status.RUNNING

        # 1) In lava or on fire -> immediate water bucket douse (with cooldown on failure) or jump-escape
        if st.get("in_lava") or st.get("on_fire"):
            px, py, pz = bb.pos
            now = time.monotonic()
            if bb.inventory_counts.get("water_bucket", 0) > 0 and now >= self._douse_cooldown_until:
                feet = block_coord((px, py, pz))
                bb.last_action_outcome = "safety_douse_water"
                status_ret, res, ok = self._write_action(
                    bb, "interact", {"pos": list(feet), "item": "water_bucket"},
                    lambda: self.gateway.client.interact(pos=list(feet), item="water_bucket"),
                )
                if not ok or (res is not None and res.get("changed") is False and not res.get("used")):
                    self._douse_cooldown_until = now + 4.0
                return status_ret
            bb.last_action_outcome = "safety_escape_lava"
            status_ret, _, _ = self._write_action(
                bb, "control", {"forward": True, "jump": True, "ms": 400},
                lambda: self.gateway.client.control(forward=True, jump=True, ms=400),
            )
            return status_ret

        # 2) Low oxygen / drowning -> dig ceiling if trapped beneath stone, else pure surface buoyancy pulse
        if st.get("submerged") and bb.oxygen <= self.low_oxygen_threshold:
            if self._drown_last_pos is not None and (bb.pos[1] - self._drown_last_pos[1]) < 0.35:
                self._drown_stalls += 1
            else:
                self._drown_stalls = 0
            self._drown_last_pos = bb.pos
            if self._drown_stalls >= 1:
                fx, fy, fz = block_coord(bb.pos)
                for dy in (2, 1):
                    c = (fx, fy + dy, fz)
                    ent = self.gateway.get_block(bb.dimension, c, allow_fetch=True)
                    if ent is not None and ent.name not in NON_SOLID_BLOCKS and ent.name not in HAZARD_BLOCKS and ent.name != "bedrock":
                        bb.last_action_outcome = "safety_surface_dig_ceiling"
                        status_ret, _, ok = self._write_action(
                            bb, "dig", {"pos": list(c), "collect": False},
                            lambda _c=c: self.gateway.client.dig(list(_c), collect=False),
                        )
                        if ok:
                            self.gateway.write_through_dig(bb.dimension, c)
                            self._drown_stalls = 0
                        return status_ret
            res = self._swimmer.swim_step((bb.pos[0], bb.pos[1] + 2.0, bb.pos[2]), st)
            bb.writes_this_tick += 1
            if res.status == "UNKNOWN":
                bb.set("pending_frozen", True, writer=self.name)
            bb.last_action_outcome = f"safety_surface:{res.status.lower()}"
            return Status.RUNNING
        self._drown_stalls = 0
        self._drown_last_pos = None

        # 3) Night sealed-burrow exit check (dawn arrived OR healed >= 14.0 and safe)
        tod = int(st.get("time_of_day", 1000)) % 24000
        is_night = bb.dimension == "overworld" and (12541 <= tod <= 23458)
        recently_hurt = (time.time() - bb.last_hurt_at) <= 6.0
        if bool(bb.get("in_sealed_burrow", False)):
            if not is_night:
                bb.set("in_sealed_burrow", False, writer=self.name)
                self._burrow_origin = None
                self._burrow_deep = False
            elif bb.health >= 14.0 and not recently_hurt:
                close_hostiles = [
                    ent for ent in bb.entities
                    if (str(ent.get("name") or "") in HOSTILE_MOBS or str(ent.get("type") or "") == "hostile")
                    and isinstance(ent.get("pos"), list) and len(ent["pos"]) == 3
                    and dist3(bb.pos, ent["pos"]) <= self.threat_radius
                ]
                if not close_hostiles:
                    bb.set("in_sealed_burrow", False, writer=self.name)
                    self._burrow_origin = None
                    self._burrow_deep = False

        if not is_night or bb.health >= 14.0:
            self._burrow_origin = None
            self._burrow_deep = False

        # 4) Actionable nearby hostile threat (with enter/exit hysteresis + shield posture + emergency night burrow)
        threat = self._find_actionable_threat(bb)

        if (
            not bool(bb.get("in_sealed_burrow", False))
            and is_night
            and bb.health <= 8.0
            and not st.get("in_water")
            and not st.get("submerged")
        ):
            has_ranged = any(str(e.get("name") or "") in RANGED_MOBS for _, e in self._last_all_near)
            should_burrow = (
                self._burrow_origin is not None
                or (
                    bool(self._last_all_near)
                    and (
                        len(self._last_all_near) >= 2
                        or has_ranged
                        or self._last_retreat_failed
                        or not self._has_shield(bb)
                    )
                )
            )
            if should_burrow:
                burrow_res = self._step_emergency_burrow(bb)
                if burrow_res is not None:
                    return burrow_res

        if threat is None:
            self._last_retreat_failed = False
            if self._shield_raised or bool(st.get("blocking", False)):
                self._shield_raised = False
                bb.last_action_outcome = "safety_shield_lower"
                status_ret, _, _ = self._write_action(
                    bb, "posture", {"sneak": False, "shield": False, "jump": False},
                    lambda: self.gateway.client.posture(sneak=False, shield=False, jump=False),
                )
                return status_ret
            return Status.FAILURE

        epos = threat["pos"]
        px, py, pz = bb.pos
        d = dist3((px, py, pz), epos)
        tname = str(threat.get("name") or "")

        # Raise shield via posture(shield=True) against ranged mobs, creepers, or low-HP melee threats
        wants_shield = (tname in RANGED_MOBS or tname == "creeper" or bb.health < 12.0) and self._has_shield(bb)
        if wants_shield and not bool(st.get("blocking", False)) and not self._shield_raised:
            bb.last_action_outcome = f"safety_shield_raise:{tname}"
            status_ret, _, ok = self._write_action(
                bb, "posture", {"sneak": False, "shield": True, "jump": False},
                lambda: self.gateway.client.posture(sneak=False, shield=True, jump=False),
            )
            if ok:
                self._shield_raised = True
            return status_ret

        # If healthy and melee-range with an actual sword/axe and not a creeper, strike once; otherwise dry-deflected retreat!
        if d <= 3.5 and bb.health >= 12.0 and tname != "creeper" and self._has_melee_weapon(bb.inventory_counts):
            bb.last_action_outcome = f"safety_strike:{tname}"
            status_ret, _, _ = self._write_action(
                bb, "attack", {"entity_id": threat["id"]},
                lambda: self.gateway.client.attack(int(threat["id"]), timeout_ms=2500, max_hits=2),
            )
            return status_ret

        rx, rz, found_dry = self._select_dry_retreat_target(bb, epos, d)
        bb.last_action_outcome = f"safety_retreat:{tname}"
        status_ret, _, ok = self._write_action(
            bb, "move_to", {"x": rx, "z": rz, "range": 1.5, "timeout_ms": 4000},
            lambda: self.gateway.client.move_to(x=rx, z=rz, range=1.5, timeout_ms=4000),
        )
        self._last_retreat_failed = (not ok) or (not found_dry)
        return status_ret
