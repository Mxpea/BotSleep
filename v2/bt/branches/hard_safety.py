"""L2 HardSafety branches: PendingUnknownGuard + Lava/Fire/Drown/Combat/Threat preemption.

Includes v22/v31/v32 battlefield rules:
- Canopy & sealed burrow ground-mob exemption: when elevated `|dy| >= 3` above a ground melee
  mob (zombie/spider) and NOT recently hurt, do NOT infinitely preempt canopy descent or crafting.
- 3-condition walled-threat exemption: exempt a threat for 15s ONLY when (1) not recently hurt,
  (2) ray_cells LOS is occluded by solid blocks, and (3) retreat failed >= 2 times with
  path_not_found/stalled AND dist > 3.5. Any hurt event clears temporary exemptions.
- Dead-end / clear-LOS fallback: when retreat fails with clear LOS or recently_hurt, melee
  strike for knockback if dist <= 3.5 with a weapon, else dig/cap emergency sealed burrow.
"""

from __future__ import annotations

from collections import deque
import math
import time
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.domain import (
    AIR_BLOCKS,
    FOOD_ITEMS,
    HAZARD_BLOCKS,
    HOSTILE_MOBS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    PASS_THROUGH_BLOCKS,
    RANGED_MOBS,
    SCAFFOLD_BLOCKS,
    Coord3,
    block_coord,
    dist2_xz,
    dist3,
    pick_best_item,
)
from v2.gateway.surface_swim import SurfaceSwimExecutor
from v2.gateway.world_proxy import VirtualWorldGateway, ray_cells
from v2.kernel.client import APIError
from v2.kernel.journal import PendingTransactionJournal

_WEAPON_BASE_DAMAGE: Dict[str, float] = {
    "netherite_axe": 10.0,
    "diamond_axe": 9.0,
    "iron_axe": 9.0,
    "stone_axe": 9.0,
    "netherite_sword": 8.0,
    "diamond_sword": 7.0,
    "golden_axe": 7.0,
    "wooden_axe": 7.0,
    "iron_sword": 6.0,
    "stone_sword": 5.0,
    "golden_sword": 4.0,
    "wooden_sword": 4.0,
}

_MATERIAL_RANK: Dict[str, int] = {
    "netherite": 6,
    "diamond": 5,
    "iron": 4,
    "stone": 3,
    "golden": 2,
    "wooden": 1,
}


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
    """Reactive survival reflexes: lava/fire escape, drowning, emergency eating, combat/shield, and terrain-aware evasion."""

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
        self._eat_cooldown_until: float = 0.0
        self._equip_fail_until: float = 0.0
        self._evasion_active: bool = False
        self._evasion_dwell_until: float = 0.0
        self._shield_raised: bool = False
        self._drown_last_pos: Optional[Tuple[float, float, float]] = None
        self._drown_stalls: int = 0
        self._last_all_near: List[Tuple[float, Dict[str, Any]]] = []
        self._last_retreat_failed: bool = False
        self._last_write_error_code: str = ""
        self._burrow_origin: Optional[Coord3] = None
        self._burrow_deep: bool = False
        self._burrow_settle_stalls: int = 0
        self._burrow_cooldown_until: float = 0.0

        # Burst-damage & hurt tracking
        self._prev_health: Optional[float] = None
        self._hp_history: Deque[Tuple[float, float]] = deque(maxlen=32)
        self._burst_until: float = 0.0

        # Walled-threat & aquatic-retreat exemption / stall tracking
        self._retreat_path_fails: Dict[int, int] = {}
        self._exempt_threats: Dict[int, float] = {}
        self._pending_retreat_check: Optional[Tuple[int, Tuple[float, float, float], float]] = None
        self._retreat_blocked_eid: Optional[int] = None
        self._swim_retreat_stalls: Dict[int, int] = {}
        self._swim_retreat_check: Optional[Tuple[int, Tuple[float, float, float], float]] = None

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory reset on preemption (zero HTTP calls)."""
        self._burrow_origin = None
        self._burrow_deep = False
        self._burrow_settle_stalls = 0
        self._shield_raised = False
        self._pending_retreat_check = None
        self._swim_retreat_check = None
        super().stop(bb)

    @staticmethod
    def _is_recently_hurt(bb: Blackboard, hp_dropped: bool = False) -> bool:
        if hp_dropped or any(e.get("type") == "hurt" for e in bb.recent_events):
            return True
        return bb.last_hurt_at > 0.0 and (time.time() - bb.last_hurt_at) <= 6.0

    def _collect_actionable_threats(
        self,
        bb: Blackboard,
        max_radius: float,
        *,
        recently_hurt: Optional[bool] = None,
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """Collect actionable hostiles within max_radius, honoring canopy/burrow/walled exemptions."""
        px, py, pz = bb.pos
        hurt = self._is_recently_hurt(bb) if recently_hurt is None else recently_hurt
        in_burrow = bool(bb.get("in_sealed_burrow", False))
        now_mono = time.monotonic()

        if hurt:
            self._exempt_threats.clear()
        else:
            expired = [eid for eid, exp in self._exempt_threats.items() if exp <= now_mono]
            for eid in expired:
                self._exempt_threats.pop(eid, None)

        candidates: List[Tuple[float, Dict[str, Any]]] = []
        for ent in bb.entities:
            name = str(ent.get("name") or "")
            etype = str(ent.get("type") or "")
            if name not in HOSTILE_MOBS and etype != "hostile":
                continue
            epos = ent.get("pos")
            if not isinstance(epos, list) or len(epos) != 3:
                continue
            eid = int(ent.get("id", -1))
            if not hurt and eid in self._exempt_threats:
                continue
            d3 = dist3((px, py, pz), epos)
            if d3 > max_radius:
                continue
            dy = py - float(epos[1])
            is_ranged = name in RANGED_MOBS

            if in_burrow and not hurt:
                continue
            if not is_ranged and dy >= 3.0 and not hurt:
                continue
            candidates.append((d3, ent))

        candidates.sort(key=lambda pair: pair[0])
        return candidates

    def _find_actionable_threat(
        self,
        bb: Blackboard,
        *,
        recently_hurt: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Apply enter/exit radius hysteresis + dwell timer and publish active threat coords."""
        now = time.monotonic()
        repulsion_scan_r = max(self.threat_exit_radius + 4.0, 16.0)
        all_near = self._collect_actionable_threats(bb, repulsion_scan_r, recently_hurt=recently_hurt)
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

    def _write_action(
        self,
        bb: Blackboard,
        endpoint: str,
        payload: Dict[str, Any],
        fn: Any,
    ) -> Tuple[Status, Optional[Dict[str, Any]], bool]:
        self._last_write_error_code = ""
        tx_id = self.journal.begin_write(endpoint, payload, dimension=bb.dimension, pos=bb.pos)
        bb.writes_this_tick += 1
        try:
            env = fn()
        except APIError as err:
            self._last_write_error_code = err.code
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
    def _best_melee_weapon(counts: Dict[str, int]) -> Optional[str]:
        """Rank inventory _sword/_axe by real MC damage, breaking ties by material tier."""
        best_score: Optional[Tuple[float, int, int]] = None
        best_name: Optional[str] = None
        for name, cnt in counts.items():
            if cnt <= 0 or not name.endswith(("_sword", "_axe")):
                continue
            mat = name.rsplit("_", 1)[0]
            mat_rank = _MATERIAL_RANK.get(mat, 0)
            dmg = _WEAPON_BASE_DAMAGE.get(name, 4.0 + mat_rank)
            score = (dmg, mat_rank, 1 if name.endswith("_sword") else 0)
            if best_score is None or score > best_score:
                best_score = score
                best_name = name
        return best_name

    @classmethod
    def _has_melee_weapon(cls, counts: Dict[str, int]) -> bool:
        return cls._best_melee_weapon(counts) is not None

    @staticmethod
    def _held_item_name(bb: Blackboard) -> Optional[str]:
        for source in (bb.status.get("held"), bb.inventory.get("held")):
            if isinstance(source, dict) and isinstance(source.get("name"), str):
                return source["name"]
        return None

    def _strike_with_best_weapon(
        self,
        bb: Blackboard,
        threat: Dict[str, Any],
        tname: str,
        *,
        max_hits: int = 2,
    ) -> Status:
        """Equip highest-damage sword/axe if not held, otherwise execute knockback attack."""
        best = self._best_melee_weapon(bb.inventory_counts)
        now = time.monotonic()
        if best is not None and self._held_item_name(bb) != best and now >= self._equip_fail_until:
            bb.last_action_outcome = f"safety_equip:{best}"
            status_ret, _, ok = self._write_action(
                bb,
                "equip",
                {"item": best, "slot": "hand"},
                lambda _b=best: self.gateway.client.equip(_b, slot="hand"),
            )
            if not ok:
                self._equip_fail_until = now + 4.0
            return status_ret

        eid = int(threat["id"])
        bb.last_action_outcome = f"safety_strike:{tname}"
        status_ret, _, _ = self._write_action(
            bb,
            "attack",
            {"entity_id": eid},
            lambda _id=eid, _mh=int(max_hits): self.gateway.client.attack(_id, timeout_ms=2500, max_hits=_mh),
        )
        return status_ret

    def _is_los_occluded(self, bb: Blackboard, epos: Sequence[float], *, prefetch: bool = True) -> bool:
        """Return True if a solid block lies along ray_cells between player eye and entity eye."""
        px, py, pz = bb.pos
        eye = (px, py + 1.62, pz)
        ent_eye = (float(epos[0]), float(epos[1]) + 1.5, float(epos[2]))
        fx, fy, fz = block_coord((px, py, pz))
        ent_cell = block_coord(ent_eye)
        skip = {(fx, fy, fz), (fx, fy + 1, fz), ent_cell}
        cells = [c for c in ray_cells(eye, ent_eye)[:24] if c not in skip]
        if not cells:
            return False
        if prefetch:
            fetched = self.gateway.prefetch_blocks(bb.dimension, cells)
            return any(
                (ent := fetched.get(c)) is not None and ent.name not in NON_SOLID_BLOCKS
                for c in cells
            )
        return any(
            (ent := self.gateway.get_block(bb.dimension, c, allow_fetch=False)) is not None
            and ent.name not in NON_SOLID_BLOCKS
            for c in cells
        )

    @staticmethod
    def _pick_emergency_food(bb: Blackboard, recent_burst_damage: bool) -> Optional[str]:
        """Select safe emergency food: normal food only when food < 18, or golden apple anytime."""
        counts = bb.inventory_counts
        has_egap = counts.get("enchanted_golden_apple", 0) > 0
        has_gapple = counts.get("golden_apple", 0) > 0 or has_egap
        gapple_name = "enchanted_golden_apple" if has_egap else "golden_apple"

        normal_avail = [
            (0 if name == "rotten_flesh" else 1, val, name)
            for name, val in FOOD_ITEMS.items()
            if name != "golden_apple" and counts.get(name, 0) > 0
        ]
        normal_avail.sort(reverse=True)
        best_normal = normal_avail[0][2] if normal_avail else None

        if bb.food < 18 and best_normal is not None:
            if has_gapple and (bb.health <= 6.0 or recent_burst_damage):
                return gapple_name
            return best_normal
        if has_gapple:
            return gapple_name
        return None

    def _is_safe_eating_window(self, bb: Blackboard, closest_dist: float) -> bool:
        if bool(bb.get("in_sealed_burrow", False)) or closest_dist >= 5.5:
            return True
        return all(
            self._is_los_occluded(bb, ent["pos"], prefetch=True)
            for d_ent, ent in self._last_all_near
            if d_ent < 5.5
        )

    def _try_exempt_walled_threat(
        self,
        bb: Blackboard,
        threat: Dict[str, Any],
        d: float,
        recently_hurt: bool,
        occluded: bool,
    ) -> bool:
        """Exempt wall-occluded threat for 45s when unhurt + occluded + (>=1 fail at d>3.5 or >=3 fails close)."""
        if recently_hurt or not occluded:
            return False
        eid = int(threat.get("id", -1))
        fails = self._retreat_path_fails.get(eid, 0)
        if eid < 0 or fails < (1 if d > 3.5 else 3):
            return False
        self._exempt_threats[eid] = time.monotonic() + 45.0
        self._retreat_path_fails.pop(eid, None)
        if self._retreat_blocked_eid == eid:
            self._retreat_blocked_eid = None
        self._evasion_active = False
        self._last_retreat_failed = False
        bb.set("threat_hysteresis_active", False, writer=self.name)
        return True

    _BURROW_PROTECTED = frozenset({
        "bedrock", "obsidian", "crafting_table", "furnace", "chest", "blast_furnace", "smoker", "nether_portal",
    })

    def _lower_shield(self, bb: Blackboard) -> Status:
        self._shield_raised = False
        bb.last_action_outcome = "safety_shield_lower"
        status_ret, _, _ = self._write_action(
            bb, "posture", {"sneak": False, "shield": False, "jump": False},
            lambda: self.gateway.client.posture(sneak=False, shield=False, jump=False),
        )
        return status_ret

    def _is_dry_ground_cell(self, dimension: str, cell: Coord3, *, allow_fetch: bool = True) -> bool:
        cx, cy, cz = cell
        floor = self.gateway.get_block(dimension, (cx, cy - 1, cz), allow_fetch=allow_fetch)
        feet = self.gateway.get_block(dimension, (cx, cy, cz), allow_fetch=allow_fetch)
        head = self.gateway.get_block(dimension, (cx, cy + 1, cz), allow_fetch=allow_fetch)
        if floor is None or floor.name in (NON_SOLID_BLOCKS | HAZARD_BLOCKS):
            return False
        for ent in (feet, head):
            if ent is None or ent.name not in PASS_THROUGH_BLOCKS or ent.name in HAZARD_BLOCKS:
                return False
        return True

    def _find_dry_stand_cell(
        self,
        dimension: str,
        bx: int,
        fy: int,
        bz: int,
        *,
        allow_fetch: bool = True,
        dys: Sequence[int] = (0, 1, -1),
    ) -> Optional[Coord3]:
        for dy in dys:
            cand = (bx, fy + dy, bz)
            if self._is_dry_ground_cell(dimension, cand, allow_fetch=allow_fetch):
                return cand
        return None

    def _select_terrain_retreat_target(
        self,
        bb: Blackboard,
        threat: Dict[str, Any],
        d: float,
    ) -> Tuple[float, Optional[float], float, bool]:
        """Compute multi-threat repulsion points and select dry retreat using bounded single-batch probes."""
        px, py, pz = bb.pos
        fy = math.floor(py)
        start_cell = block_coord((px, py, pz))
        all_threat_pos: List[Tuple[float, float, float]] = [
            tuple(float(v) for v in ent["pos"]) for _, ent in self._last_all_near
        ]
        if not all_threat_pos:
            ep = threat["pos"]
            all_threat_pos = [(float(ep[0]), float(ep[1]), float(ep[2]))]

        vx, vz = 0.0, 0.0
        for tx, _, tz in all_threat_pos:
            dx, dz = px - tx, pz - tz
            dist_xz = math.hypot(dx, dz)
            if dist_xz < 1e-4:
                continue
            w = 1.0 / max(1.0, dist_xz * dist_xz)
            vx += (dx / dist_xz) * w
            vz += (dz / dist_xz) * w
        norm = math.hypot(vx, vz)
        if norm < 1e-6:
            ep = threat["pos"]
            vx, vz = px - float(ep[0]), pz - float(ep[2])
            norm = math.hypot(vx, vz)
        ux, uz = (vx / norm, vz / norm) if norm >= 1e-6 else (1.0, 0.0)

        retreat_step = min(max(5.5, self.threat_exit_radius - d + 2.0), 8.5)
        angles = (0.0, 45.0, -45.0, 90.0, -90.0, 135.0, -135.0)

        def _probe_pass(dist_cand: float, y_offsets: Sequence[int], stand_dys: Sequence[int]) -> Optional[Tuple[float, Optional[float], float]]:
            probe_coords: List[Coord3] = []
            raw_candidates: List[Tuple[int, int, Tuple[Tuple[int, int], ...], float]] = []
            seen_xz: set[Tuple[int, int]] = set()
            for deg in angles:
                rad = math.radians(deg)
                ca, sa = math.cos(rad), math.sin(rad)
                dx = ux * ca - uz * sa
                dz = ux * sa + uz * ca
                bx = math.floor(px + dx * dist_cand)
                bz = math.floor(pz + dz * dist_cand)
                if (bx, bz) in seen_xz or (bx, bz) == (start_cell[0], start_cell[2]):
                    continue
                seen_xz.add((bx, bz))
                samples: List[Tuple[int, int]] = []
                for frac in (0.35, 0.5, 0.7):
                    sx = math.floor(px + dx * (dist_cand * frac))
                    sz = math.floor(pz + dz * (dist_cand * frac))
                    if (sx, sz) != (start_cell[0], start_cell[2]) and (sx, sz) != (bx, bz) and (sx, sz) not in samples:
                        samples.append((sx, sz))
                        probe_coords.append((sx, fy - 1, sz))
                        probe_coords.append((sx, fy, sz))
                raw_candidates.append((bx, bz, tuple(samples), abs(deg)))
                for oy in y_offsets:
                    probe_coords.append((bx, fy + oy, bz))
            if probe_coords:
                self.gateway.prefetch_blocks(bb.dimension, probe_coords)

            dry_goals: List[Tuple[float, float, Coord3]] = []
            for bx, bz, samples, abs_deg in raw_candidates:
                blocked_mid = False
                for sx, sz in samples:
                    s_floor = self.gateway.get_block(bb.dimension, (sx, fy - 1, sz), allow_fetch=False)
                    s_feet = self.gateway.get_block(bb.dimension, (sx, fy, sz), allow_fetch=False)
                    if s_floor is not None and s_floor.name in (LIQUID_BLOCKS | HAZARD_BLOCKS):
                        blocked_mid = True
                        break
                    if s_feet is not None and s_feet.name not in PASS_THROUGH_BLOCKS:
                        blocked_mid = True
                        break
                if blocked_mid:
                    continue
                stand = self._find_dry_stand_cell(bb.dimension, bx, fy, bz, allow_fetch=False, dys=stand_dys)
                if stand is None:
                    continue
                min_t_dist = min(
                    math.dist((stand[0] + 0.5, float(stand[1]), stand[2] + 0.5), tp)
                    for tp in all_threat_pos
                )
                score = min_t_dist - (abs_deg / 180.0) * 1.5
                dry_goals.append((score, abs_deg, stand))

            if not dry_goals:
                return None
            dry_goals.sort(key=lambda item: (-item[0], item[1]))
            dest = dry_goals[0][2]
            ry = float(dest[1]) if dest[1] != fy else None
            return dest[0] + 0.5, ry, dest[2] + 0.5

        best = _probe_pass(retreat_step, (-1, 0, 1), (0,))
        if best is None:
            best = _probe_pass(4.5, (-2, -1, 0, 1, 2), (0, 1, -1))
        if best is not None:
            return best[0], best[1], best[2], True

        return round(px + ux * retreat_step, 1), None, round(pz + uz * retreat_step, 1), False

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
        return pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS) is not None

    def _step_emergency_burrow(self, bb: Blackboard) -> Optional[Status]:
        if self._burrow_origin is None:
            if time.monotonic() < self._burrow_cooldown_until:
                return None
            if not self._can_safe_burrow(bb):
                return None
            self._burrow_origin = block_coord(bb.pos)
            self._burrow_deep = False
            self._burrow_settle_stalls = 0

        bx, start_fy, bz = self._burrow_origin
        cur = block_coord(bb.pos)
        if (cur[0], cur[2]) != (bx, bz) and cur[1] >= start_fy - 1:
            self._burrow_origin = None
            self._burrow_deep = False
            self._burrow_settle_stalls = 0
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
                self._burrow_settle_stalls = 0
                return None
            if cur[1] - 1 == target_y and floor_check.name in NON_SOLID_BLOCKS:
                self._burrow_origin = None
                self._burrow_deep = False
                self._burrow_settle_stalls = 0
                return None
            if dig_ent is not None and dig_ent.name not in AIR_BLOCKS:
                if dig_ent.name in (LIQUID_BLOCKS | HAZARD_BLOCKS | self._BURROW_PROTECTED):
                    self._burrow_origin = None
                    self._burrow_deep = False
                    self._burrow_settle_stalls = 0
                    return None
                self._burrow_settle_stalls = 0
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
            self._burrow_settle_stalls += 1
            if self._burrow_settle_stalls >= 3:
                self._burrow_origin = None
                self._burrow_deep = False
                self._burrow_settle_stalls = 0
                self._burrow_cooldown_until = time.monotonic() + 20.0
                return None
            bb.last_action_outcome = "safety_burrow_settle"
            return Status.RUNNING

        self._burrow_settle_stalls = 0

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

        now_mono = time.monotonic()
        hp_dropped = self._prev_health is not None and bb.health < self._prev_health
        self._hp_history.append((now_mono, bb.health))
        while self._hp_history and (now_mono - self._hp_history[0][0]) > 4.0:
            self._hp_history.popleft()
        max_recent_hp = max((hp for _, hp in self._hp_history), default=bb.health)
        event_burst = any(
            e.get("type") == "hurt" and float(e.get("damage") or e.get("amount") or 0.0) >= 4.0
            for e in bb.recent_events
        )
        if (max_recent_hp - bb.health) >= 4.0 or event_burst:
            self._burst_until = now_mono + 6.0
        elif bb.health >= 19.0:
            self._burst_until = 0.0
        recent_burst_damage = (now_mono < self._burst_until) or bool(bb.get("recent_burst_damage", False))

        recently_hurt = self._is_recently_hurt(bb, hp_dropped=hp_dropped)
        self._prev_health = bb.health
        if recently_hurt:
            self._exempt_threats.clear()
            self._retreat_path_fails.clear()
            self._pending_retreat_check = None
            self._retreat_blocked_eid = None

        if not st.get("in_water") and not st.get("submerged"):
            self._swim_retreat_stalls.clear()
            self._swim_retreat_check = None
        elif self._swim_retreat_check is not None:
            s_eid, s_pos, s_dist = self._swim_retreat_check
            self._swim_retreat_check = None
            ent_match = next(
                (
                    e for e in bb.entities
                    if int(e.get("id", -1)) == s_eid and isinstance(e.get("pos"), list) and len(e["pos"]) == 3
                ),
                None,
            )
            if ent_match is not None:
                d_now = dist3(bb.pos, ent_match["pos"])
                moved = dist3(bb.pos, s_pos)
                if moved < 0.35 and (d_now - s_dist) < 0.35:
                    self._swim_retreat_stalls[s_eid] = self._swim_retreat_stalls.get(s_eid, 0) + 1
                    self._retreat_path_fails[s_eid] = self._retreat_path_fails.get(s_eid, 0) + 1
                else:
                    self._swim_retreat_stalls[s_eid] = 0

        if self._pending_retreat_check is not None:
            prev_eid, start_pos, start_dist = self._pending_retreat_check
            self._pending_retreat_check = None
            ent_match = next(
                (
                    e for e in bb.entities
                    if int(e.get("id", -1)) == prev_eid and isinstance(e.get("pos"), list) and len(e["pos"]) == 3
                ),
                None,
            )
            if ent_match is not None:
                d_now = dist3(bb.pos, ent_match["pos"])
                moved = dist3(bb.pos, start_pos)
                if moved < 0.35 and (d_now - start_dist) < 0.35:
                    self._retreat_path_fails[prev_eid] = self._retreat_path_fails.get(prev_eid, 0) + 1
                else:
                    self._retreat_path_fails[prev_eid] = 0

        # 1) In lava or on fire -> immediate water bucket douse or jump-escape
        if st.get("in_lava") or st.get("on_fire"):
            px, py, pz = bb.pos
            if bb.inventory_counts.get("water_bucket", 0) > 0 and now_mono >= self._douse_cooldown_until:
                feet = block_coord((px, py, pz))
                bb.last_action_outcome = "safety_douse_water"
                status_ret, res, ok = self._write_action(
                    bb, "interact", {"pos": list(feet), "item": "water_bucket"},
                    lambda: self.gateway.client.interact(pos=list(feet), item="water_bucket"),
                )
                if not ok or (res is not None and res.get("changed") is False and not res.get("used")):
                    self._douse_cooldown_until = now_mono + 4.0
                return status_ret
            bb.last_action_outcome = "safety_escape_lava"
            status_ret, _, _ = self._write_action(
                bb, "control", {"forward": True, "jump": True, "ms": 400, "stop_on": []},
                lambda: self.gateway.client.control(forward=True, jump=True, ms=400, stop_on=[]),
            )
            return status_ret

        # 2) Low oxygen / drowning -> dig ceiling if trapped, else surface buoyancy pulse
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

        # 2b) Void-island cliff/edge guard: avoid stepping off sheer drop into void_air
        if (
            not st.get("in_water")
            and not st.get("submerged")
            and not bool(bb.get("in_sealed_burrow", False))
        ):
            bx, by, bz = block_coord(bb.pos)
            # Check up to 6 blocks down from cached blocks (zero forced RPCs on steady ground)
            below_col = [self.gateway.get_block(bb.dimension, (bx, by - dy, bz), allow_fetch=False) for dy in range(1, 7)]
            is_void_or_cliff = all(b is not None and b.name in AIR_BLOCKS for b in below_col) and (
                not bool(st.get("on_ground", True))
                or by <= -59
                or any(b is not None and b.name == "void_air" for b in below_col)
            )
            if is_void_or_cliff:
                safe_nb = None
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nb_below = self.gateway.get_block(bb.dimension, (bx + dx, by - 1, bz + dz), allow_fetch=False)
                    nb_feet = self.gateway.get_block(bb.dimension, (bx + dx, by, bz + dz), allow_fetch=False)
                    if (
                        nb_below is not None and nb_below.name not in (NON_SOLID_BLOCKS | HAZARD_BLOCKS)
                        and (nb_feet is None or nb_feet.name in NON_SOLID_BLOCKS)
                    ):
                        safe_nb = (bx + dx + 0.5, float(by), bz + dz + 0.5)
                        break
                if safe_nb is not None:
                    bb.last_action_outcome = "safety_void_edge_guard"
                    status_ret, _, _ = self._write_action(
                        bb,
                        "move_to",
                        {"x": safe_nb[0], "y": safe_nb[1], "z": safe_nb[2], "range": 0.8, "stop_on": []},
                        lambda _s=safe_nb: self.gateway.client.move_to(x=_s[0], y=_s[1], z=_s[2], range=0.8, timeout_ms=2500, stop_on=[]),
                    )
                    return status_ret

        # 3) Sealed-burrow exit check (dawn arrived without close hostiles OR healed >= 14.0 and safe)
        tod = int(st.get("time_of_day", 1000)) % 24000
        is_night = bb.dimension == "overworld" and (12541 <= tod <= 23458)
        if bool(bb.get("in_sealed_burrow", False)):
            close_hostiles = [
                ent for ent in bb.entities
                if (str(ent.get("name") or "") in HOSTILE_MOBS or str(ent.get("type") or "") == "hostile")
                and isinstance(ent.get("pos"), list) and len(ent["pos"]) == 3
                and dist3(bb.pos, ent["pos"]) <= self.threat_radius
            ]
            if (not is_night and not close_hostiles) or (bb.health >= 14.0 and not recently_hurt and not close_hostiles):
                bb.set("in_sealed_burrow", False, writer=self.name)
                self._burrow_origin = None
                self._burrow_deep = False

        if self._burrow_origin is not None and (not is_night and not self._last_retreat_failed) and bb.health >= 14.0:
            self._burrow_origin = None
            self._burrow_deep = False

        # 4) Actionable nearby hostile threat (hysteresis + night burrow + safe eating + shield + combat/retreat)
        threat = self._find_actionable_threat(bb, recently_hurt=recently_hurt)

        if (
            not bool(bb.get("in_sealed_burrow", False))
            and is_night
            and bb.health <= 10.0
            and not st.get("in_water")
            and not st.get("submerged")
            and (self._burrow_origin is not None or pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS) is not None)
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
            self._retreat_blocked_eid = None
            if self._shield_raised or bool(st.get("blocking", False)):
                return self._lower_shield(bb)
            return Status.FAILURE

        epos = threat["pos"]
        px, py, pz = bb.pos
        d = dist3((px, py, pz), epos)
        tname = str(threat.get("name") or "")
        eid = int(threat.get("id", -1))
        occluded = self._is_los_occluded(bb, epos, prefetch=True)

        if self._try_exempt_walled_threat(bb, threat, d, recently_hurt, occluded):
            if self._shield_raised or bool(st.get("blocking", False)):
                return self._lower_shield(bb)
            return Status.FAILURE

        # Continue in-progress dead-end burrow if started
        if self._burrow_origin is not None and not bool(bb.get("in_sealed_burrow", False)):
            burrow_res = self._step_emergency_burrow(bb)
            if burrow_res is not None:
                return burrow_res

        # Safe emergency eating BEFORE non-urgent retreat (only when food/health qualify AND safe window holds)
        if now_mono >= self._eat_cooldown_until and (bb.health <= 10.0 or recent_burst_damage):
            food_item = self._pick_emergency_food(bb, recent_burst_damage)
            if food_item is not None and self._is_safe_eating_window(bb, d):
                self._shield_raised = False
                bb.last_action_outcome = f"safety_eat:{food_item}"
                status_ret, _, ok = self._write_action(
                    bb,
                    "use_item",
                    {"item": food_item},
                    lambda _f=food_item: self.gateway.client.use_item(_f),
                )
                if not ok:
                    self._eat_cooldown_until = now_mono + 3.0
                return status_ret

        # Dead-end / clear-LOS fallback when previous retreat was blocked with path_not_found
        has_weapon = self._has_melee_weapon(bb.inventory_counts)
        if self._retreat_blocked_eid == eid and (not occluded or recently_hurt):
            if tname != "creeper" and ((d <= 3.5 and has_weapon) or d <= 2.5):
                return self._strike_with_best_weapon(bb, threat, tname, max_hits=2 if has_weapon else 1)
            burrow_res = self._step_emergency_burrow(bb)
            if burrow_res is not None:
                return burrow_res

        # Close-range (< 3.0m) or healthy (<= 3.5m) melee knockback strike against non-creepers with clear LOS
        if not occluded and tname != "creeper" and has_weapon and (d < 3.0 or (d <= 3.5 and bb.health >= 12.0)):
            return self._strike_with_best_weapon(bb, threat, tname)

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

        # Terrain-aware retreat via Pathfinder3D
        rx, ry, rz, found_path = self._select_terrain_retreat_target(bb, threat, d)
        if not found_path:
            if not occluded or recently_hurt:
                if tname != "creeper" and ((d <= 3.5 and has_weapon) or d <= 2.5):
                    return self._strike_with_best_weapon(bb, threat, tname, max_hits=2 if has_weapon else 1)
                burrow_res = self._step_emergency_burrow(bb)
                if burrow_res is not None:
                    return burrow_res
            elif d > 3.5 and eid >= 0 and self._retreat_path_fails.get(eid, 0) >= 1:
                self._retreat_path_fails[eid] = self._retreat_path_fails.get(eid, 0) + 1
                if self._try_exempt_walled_threat(bb, threat, d, recently_hurt, occluded):
                    if self._shield_raised or bool(st.get("blocking", False)):
                        return self._lower_shield(bb)
                    return Status.FAILURE

        if bool(st.get("in_water") or st.get("submerged")):
            if eid >= 0 and self._swim_retreat_stalls.get(eid, 0) >= 2:
                if tname != "creeper" and (not occluded or recently_hurt) and ((d <= 3.5 and has_weapon) or d <= 2.5):
                    self._swim_retreat_stalls[eid] = 0
                    return self._strike_with_best_weapon(bb, threat, tname, max_hits=2 if has_weapon else 1)
                self._exempt_threats[eid] = now_mono + (6.0 if recently_hurt else 15.0)
                self._swim_retreat_stalls[eid] = 0
                self._evasion_active = False
                bb.set("threat_hysteresis_active", False, writer=self.name)
                if self._shield_raised or bool(st.get("blocking", False)):
                    return self._lower_shield(bb)
                bb.last_action_outcome = f"safety_swim_retreat_yield:{tname}"
                return Status.FAILURE
            bb.last_action_outcome = f"safety_swim_retreat:{tname}"
            swim_target_y = (float(ry) if ry is not None else py) + 0.5
            swim_res = self._swimmer.swim_step((rx, swim_target_y, rz), st)
            bb.writes_this_tick += 1
            if swim_res.status == "UNKNOWN":
                bb.set("pending_frozen", True, writer=self.name)
                return Status.RUNNING
            ok = swim_res.status not in {"FAILED"}
            self._last_retreat_failed = not ok
            if eid >= 0:
                self._swim_retreat_check = (eid, bb.pos, d)
            return Status.RUNNING

        move_payload: Dict[str, Any] = {"x": rx, "z": rz, "range": 1.5, "timeout_ms": 2200, "stop_on": []}
        if ry is not None:
            move_payload["y"] = ry
        bb.last_action_outcome = f"safety_retreat:{tname}"
        status_ret, _, ok = self._write_action(
            bb,
            "move_to",
            move_payload,
            lambda: self.gateway.client.move_to(x=rx, y=ry, z=rz, range=1.5, timeout_ms=2200, stop_on=[]),
        )
        self._last_retreat_failed = (not ok) or (not found_path)
        if not ok:
            self._pending_retreat_check = None
            if eid >= 0 and (self._last_write_error_code in {"path_not_found", "timeout", "stalled"} or not found_path):
                self._retreat_path_fails[eid] = self._retreat_path_fails.get(eid, 0) + 1
                self._retreat_blocked_eid = eid
                self._try_exempt_walled_threat(bb, threat, d, recently_hurt, occluded)
        else:
            self._retreat_blocked_eid = None
            if eid >= 0:
                self._pending_retreat_check = (eid, bb.pos, d)
        return status_ret
