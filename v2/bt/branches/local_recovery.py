"""L2 LocalRecoveryBranch: urgent eat, canopy descent, water shore escape, and cached/throttled auto-sleep."""

from __future__ import annotations

from collections import deque
import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.branches.unstuck import PhysicalUnstuckBranch
from v2.bt.kernel import Node, Status
from v2.domain import (
    AIR_BLOCKS,
    ARMOR_EQUIP_SLOT_BY_INVENTORY_KEY,
    ARMOR_RANK,
    BED_BLOCKS,
    Coord3,
    FOOD_ITEMS,
    HAZARD_BLOCKS,
    LEAF_BLOCKS,
    LOG_BLOCKS,
    NON_SOLID_BLOCKS,
    PASS_THROUGH_BLOCKS,
    PICKAXE_TIERS,
    SCAFFOLD_BLOCKS,
    SOFT_OCCLUDERS,
    WATER_BLOCKS,
    block_coord,
    dist3,
    pick_best_item,
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
_WATER_HARD_ROCK_BANK_BLOCKS = frozenset({
    "stone", "deepslate", "cobblestone", "cobbled_deepslate",
    "andesite", "diorite", "granite", "tuff", "sandstone",
})
_CARDINAL_OFFSETS_2D: Tuple[Tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
_CARDINAL_OFFSETS_3D: Tuple[Coord3, ...] = (
    (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
)


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
        self._equip_cooldown_until: Dict[str, float] = {}

    def _pick_swim_target(self, bb: Blackboard) -> Tuple[float, float, float]:
        """Prefetch 5x3x5 (+headroom) around player and BFS for the nearest reachable standable dry shore cell."""
        fx, fy, fz = block_coord(bb.pos)
        coords = [
            (fx + dx, fy + dy, fz + dz)
            for dx in range(-2, 3)
            for dz in range(-2, 3)
            for dy in (-1, 0, 1, 2)
        ]
        fetched = self.gateway.prefetch_blocks(bb.dimension, coords)

        def is_standable_shore(cand: Coord3) -> bool:
            below = fetched.get((cand[0], cand[1] - 1, cand[2]))
            feet = fetched.get(cand)
            head = fetched.get((cand[0], cand[1] + 1, cand[2]))
            return (
                feet is not None and feet.name in PASS_THROUGH_BLOCKS and feet.name not in HAZARD_BLOCKS
                and (head is None or (head.name in PASS_THROUGH_BLOCKS and head.name not in HAZARD_BLOCKS))
                and below is not None and below.name not in NON_SOLID_BLOCKS and below.name not in HAZARD_BLOCKS
            )

        def is_passable_swim(cell: Coord3) -> bool:
            ent = fetched.get(cell)
            head = fetched.get((cell[0], cell[1] + 1, cell[2]))
            return (
                ent is not None and ent.name in NON_SOLID_BLOCKS and ent.name not in HAZARD_BLOCKS and ent.name != "lava"
                and (head is None or (head.name in NON_SOLID_BLOCKS and head.name not in HAZARD_BLOCKS and head.name != "lava"))
            )

        start: Coord3 = (fx, fy, fz)
        queue: deque[Coord3] = deque([start])
        visited: Set[Coord3] = {start}
        while queue:
            cx, cy, cz = queue.popleft()
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = cx + dx, cz + dz
                if abs(nx - fx) > 2 or abs(nz - fz) > 2:
                    continue
                # Check diagonal climb-up shore (cy + 1) and same-level shore (cy)
                for ny in (cy + 1, cy):
                    if ny < fy - 1 or ny > fy + 1:
                        continue
                    cand = (nx, ny, nz)
                    if cand != start and is_standable_shore(cand):
                        if ny == cy + 1:
                            above_cur = fetched.get((cx, cy + 2, cz))
                            if above_cur is not None and (above_cur.name not in NON_SOLID_BLOCKS or above_cur.name in HAZARD_BLOCKS):
                                continue
                        return (cand[0] + 0.5, float(cand[1]) + 0.5, cand[2] + 0.5)
                nxt = (nx, cy, nz)
                if nxt not in visited and is_passable_swim(nxt):
                    visited.add(nxt)
                    queue.append(nxt)
            if cy + 1 <= fy + 1:
                up = (cx, cy + 1, cz)
                if up not in visited and is_passable_swim(up):
                    visited.add(up)
                    queue.append(up)

        if self._swim_consec > 0:
            bx, bz = _SHORE_BEARINGS[(self._swim_consec - 1) % len(_SHORE_BEARINGS)]
            return (bb.pos[0] + bx * 3.0, bb.pos[1] + 1.0, bb.pos[2] + bz * 3.0)
        return (bb.pos[0], bb.pos[1] + 2.0, bb.pos[2])

    def _dig_swim_cell(self, bb: Blackboard, c: Coord3, outcome: str) -> Status:
        bb.last_action_outcome = outcome
        res_status = self._write_action(
            bb, "dig", {"pos": list(c), "collect": False},
            lambda _c=c: self.gateway.client.dig(list(_c), collect=False),
        )
        if res_status == Status.RUNNING and not bb.get("pending_frozen"):
            self.gateway.write_through_dig(bb.dimension, c)
        elif res_status == Status.FAILURE:
            self.gateway.unreachable.record_failure(bb.dimension, c, "dig", bb.last_action_outcome)
        return res_status

    def _try_dig_swim_ceiling(self, bb: Blackboard) -> Optional[Status]:
        """If trapped in water beneath a ceiling or riverbank overhang, dig an opening or climb-out step."""
        fx, fy, fz = block_coord(bb.pos)
        for dy in (2, 1):
            c = (fx, fy + dy, fz)
            ent = self.gateway.get_block(bb.dimension, c, allow_fetch=True)
            if (
                ent is not None
                and ent.name not in NON_SOLID_BLOCKS
                and ent.name not in HAZARD_BLOCKS
                and ent.name != "bedrock"
                and not self.gateway.unreachable.is_blocked(bb.dimension, c, "dig")
            ):
                return self._dig_swim_cell(bb, c, "recovery_swim_dig_ceiling")

        bank_coords = [
            (fx + dx, fy + dy, fz + dz)
            for dy in (1, 0)
            for dx, dz in _CARDINAL_OFFSETS_2D
        ]
        has_pickaxe = pick_best_item(bb.inventory_counts, PICKAXE_TIERS) is not None
        probe_coords: List[Coord3] = list(bank_coords)
        if has_pickaxe:
            for dx, dz in _CARDINAL_OFFSETS_2D:
                cx, cy, cz = fx + dx, fy + 1, fz + dz
                for ndx, ndy, ndz in _CARDINAL_OFFSETS_3D:
                    probe_coords.append((cx + ndx, cy + ndy, cz + ndz))
        bank_fetched = self.gateway.prefetch_blocks(bb.dimension, probe_coords)

        for c in bank_coords:
            ent = bank_fetched.get(c)
            if (
                ent is not None
                and ent.name in _WATER_BANK_DIG_BLOCKS
                and ent.name not in NON_SOLID_BLOCKS
                and ent.name not in HAZARD_BLOCKS
                and not self.gateway.unreachable.is_blocked(bb.dimension, c, "dig")
            ):
                return self._dig_swim_cell(bb, c, "recovery_swim_dig_bank")

        if has_pickaxe:
            hard_candidates: List[Tuple[int, Coord3]] = []
            for dx, dz in _CARDINAL_OFFSETS_2D:
                c = (fx + dx, fy + 1, fz + dz)
                ent = bank_fetched.get(c)
                if (
                    ent is None
                    or ent.name not in _WATER_HARD_ROCK_BANK_BLOCKS
                    or ent.name in HAZARD_BLOCKS
                    or ent.name in {"bedrock", "obsidian"}
                    or self.gateway.unreachable.is_blocked(bb.dimension, c, "dig")
                ):
                    continue
                if any(
                    (nbr := bank_fetched.get((c[0] + ndx, c[1] + ndy, c[2] + ndz))) is not None
                    and nbr.name in HAZARD_BLOCKS
                    for ndx, ndy, ndz in _CARDINAL_OFFSETS_3D
                ):
                    continue
                above = bank_fetched.get((c[0], c[1] + 1, c[2]))
                has_headroom = 0 if (above is not None and above.name in NON_SOLID_BLOCKS) else 1
                hard_candidates.append((has_headroom, c))
            hard_candidates.sort(key=lambda item: item[0])
            if hard_candidates:
                return self._dig_swim_cell(bb, hard_candidates[0][1], "recovery_swim_dig_bank")
        return None

    def _try_plug_water(self, bb: Blackboard) -> Optional[Status]:
        """Plug adjacent or feet water cells with scaffold blocks to kill flow and pillar out."""
        scaffold = pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS)
        if scaffold is None:
            return None
        fx, fy, fz = block_coord(bb.pos)
        candidates: List[Coord3] = [
            (fx + dx, fy + dy, fz + dz)
            for dy in (0, 1)
            for dx, dz in _CARDINAL_OFFSETS_2D
        ]
        candidates.append((fx, fy, fz))
        probe_coords: List[Coord3] = []
        for c in candidates:
            probe_coords.append(c)
            for ndx, ndy, ndz in _CARDINAL_OFFSETS_3D:
                probe_coords.append((c[0] + ndx, c[1] + ndy, c[2] + ndz))
        fetched = self.gateway.prefetch_blocks(bb.dimension, probe_coords)

        for cell in candidates:
            if self.gateway.unreachable.is_blocked(bb.dimension, cell, "place"):
                continue
            ent = fetched.get(cell)
            if ent is None or ent.name not in WATER_BLOCKS:
                continue
            nbrs = [fetched.get((cell[0] + ndx, cell[1] + ndy, cell[2] + ndz)) for ndx, ndy, ndz in _CARDINAL_OFFSETS_3D]
            if any(nbr is not None and nbr.name in HAZARD_BLOCKS for nbr in nbrs):
                continue
            if not any(nbr is not None and nbr.name not in NON_SOLID_BLOCKS for nbr in nbrs):
                continue
            bb.last_action_outcome = "recovery_swim_plug_water"
            res_status = self._write_action(
                bb, "place", {"item": scaffold, "pos": list(cell)},
                lambda _s=scaffold, _c=cell: self.gateway.client.place_block(_s, _c[0], _c[1], _c[2]),
            )
            if res_status == Status.RUNNING and not bb.get("pending_frozen"):
                self.gateway.write_through_place(bb.dimension, cell, scaffold)
            elif res_status == Status.FAILURE:
                self.gateway.unreachable.record_failure(bb.dimension, cell, "place", bb.last_action_outcome)
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

    def _try_auto_equip(self, bb: Blackboard) -> Optional[Status]:
        """Auto-equip strictly better armor piece or offhand shield using L0 domain tables."""
        now = time.monotonic()
        armor_state = bb.inventory.get("armor") if isinstance(bb.inventory.get("armor"), dict) else {}
        suffix_by_inv_key = {"head": "_helmet", "chest": "_chestplate", "legs": "_leggings", "feet": "_boots"}

        for inv_key, suffix in suffix_by_inv_key.items():
            cur_piece = armor_state.get(inv_key)
            cur_name = cur_piece.get("name", "") if isinstance(cur_piece, dict) else ""
            cur_rank = next((r for mat, r in ARMOR_RANK.items() if cur_name.startswith(mat + "_")), 0)
            best_name: Optional[str] = None
            best_rank = cur_rank
            for item_name, cnt in bb.inventory_counts.items():
                if cnt > 0 and item_name.endswith(suffix) and now >= self._equip_cooldown_until.get(item_name, 0.0):
                    rank = next((r for mat, r in ARMOR_RANK.items() if item_name.startswith(mat + "_")), 0)
                    if rank > best_rank:
                        best_rank = rank
                        best_name = item_name
            if best_name is not None:
                equip_slot = ARMOR_EQUIP_SLOT_BY_INVENTORY_KEY[inv_key]
                bb.last_action_outcome = f"recovery_equip:{best_name}"
                res = self._write_action(
                    bb, "equip", {"item": best_name, "slot": equip_slot},
                    lambda bn=best_name, es=equip_slot: self.gateway.client.equip(bn, slot=es),
                )
                if res == Status.FAILURE:
                    self._equip_cooldown_until[best_name] = now + 10.0
                return res

        offhand = bb.inventory.get("offhand")
        off_name = offhand.get("name", "") if isinstance(offhand, dict) else ""
        if off_name != "shield" and bb.inventory_counts.get("shield", 0) > 0 and now >= self._equip_cooldown_until.get("shield", 0.0):
            bb.last_action_outcome = "recovery_equip:shield"
            res = self._write_action(
                bb, "equip", {"item": "shield", "slot": "off-hand"},
                lambda: self.gateway.client.equip("shield", slot="off-hand"),
            )
            if res == Status.FAILURE:
                self._equip_cooldown_until["shield"] = now + 10.0
            return res
        return None

    def update(self, bb: Blackboard) -> Status:
        st = bb.status
        in_water = bool(st.get("in_water") or st.get("submerged"))
        on_ground = bool(st.get("on_ground", False))
        if in_water:
            dry_streak = 0
            bb.set("last_wet_at", time.monotonic(), writer=self.name)
        elif on_ground:
            dry_streak = int(bb.get("dry_footing_streak", 0) or 0) + 1
        else:
            dry_streak = int(bb.get("dry_footing_streak", 0) or 0)
        bb.set("dry_footing_streak", dry_streak, writer=self.name)

        # 0) Auto-equip best armor / offhand shield when not drowning
        if not (in_water and (bool(st.get("submerged")) or bb.oxygen < 16)):
            equip_res = self._try_auto_equip(bb)
            if equip_res is not None:
                return equip_res

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
                        if dig_res == Status.RUNNING:
                            self._swim_consec = 0
                            self._swim_anchor = bb.pos
                        return dig_res
                    plug_res = self._try_plug_water(bb)
                    if plug_res is not None:
                        if plug_res == Status.RUNNING:
                            self._swim_consec = 0
                            self._swim_anchor = bb.pos
                        return plug_res
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
