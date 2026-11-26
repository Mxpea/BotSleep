"""L3b MineResourceTask: cave-first ore harvesting + safe 3-high staircase descent to Y=16 / Y=-54 + 2-high tunneling."""

from __future__ import annotations

from typing import Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import (
    AIR_BLOCKS,
    Coord3,
    HAZARD_BLOCKS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    SCAFFOLD_BLOCKS,
    WATER_BLOCKS,
    block_coord,
    dist3,
    pick_best_item,
)
from v2.primitives.base import PrimitiveContext
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.acquire import AcquireItemTask, _has_pickaxe_tier
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.surface import is_underground, load_anchor_landmark, save_anchor_landmark


class MineResourceTask(Task):
    """Cave-first resource mining with safe step-driven Y-band descent and 2-high branch tunneling (<= 1 write/tick)."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        item: str,
        count: int,
        *,
        target_y: Optional[int] = None,
    ) -> None:
        super().__init__(f"mine:{item}x{count}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.item = item
        self.count = max(1, int(count))
        gather = catalogue.gathering.get(item)
        self.target_y = target_y if target_y is not None else (gather.preferred_y if gather and gather.preferred_y is not None else 16)
        self._dig = DigCollectPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._dir_idx: int = 0
        self._dirs = ((1, 0), (0, 1), (-1, 0), (0, -1))
        self._tried_mine_shaft: bool = False
        self._mine_shaft_stalls: int = 0
        self._mine_shaft_last_pos: Optional[Tuple[float, float, float]] = None
        self._surface_anchor_saved: bool = False

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.item, 0) >= self.count

    def stop(self, bb: Blackboard) -> None:
        self._dig.clear_cache()
        self._tried_mine_shaft = False
        self._mine_shaft_stalls = 0
        self._mine_shaft_last_pos = None
        self._surface_anchor_saved = False
        super().stop(bb)

    PROTECTED_STATIONS = frozenset({
        "crafting_table", "furnace", "chest", "blast_furnace", "smoker",
        "obsidian", "nether_portal",
    })

    def _is_safe_to_dig(self, bb: Blackboard, cell: Coord3, *, allow_shallow_floor_water: bool = False) -> bool:
        cx, cy, cz = cell
        lateral = [(cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy, cz + 1), (cx, cy, cz - 1)]
        check = [(cx, cy, cz), (cx, cy + 1, cz)] + lateral
        extra = [(cx, cy - 1, cz)] + [(nx, cy + 1, nz) for nx, _, nz in lateral] + [(nx, cy - 1, nz) for nx, _, nz in lateral]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, check + (extra if allow_shallow_floor_water else []), max_age=3.0)
        target_ent = fetched.get((cx, cy, cz))
        if target_ent is not None and (
            target_ent.name in self.PROTECTED_STATIONS
            or target_ent.name.endswith("_bed")
            or target_ent.name in HAZARD_BLOCKS
            or target_ent.name in LIQUID_BLOCKS
        ):
            return False
        overhead = fetched.get((cx, cy + 1, cz))
        if overhead is not None and (overhead.name in HAZARD_BLOCKS or overhead.name in LIQUID_BLOCKS):
            return False
        cell_below = fetched.get((cx, cy - 1, cz))
        for nx, ny, nz in lateral:
            ent = fetched.get((nx, ny, nz))
            if ent is None:
                continue
            if ent.name in HAZARD_BLOCKS:
                return False
            if ent.name in WATER_BLOCKS:
                if not allow_shallow_floor_water:
                    return False
                above_w = fetched.get((nx, cy + 1, nz))
                below_w = fetched.get((nx, cy - 1, nz))
                is_static_shallow = (
                    ent.name == "water"
                    and str(ent.properties.get("level", "0")) == "0"
                    and (above_w is None or above_w.name not in WATER_BLOCKS)
                    and (below_w is not None and below_w.name not in NON_SOLID_BLOCKS)
                    and (cell_below is not None and cell_below.name not in NON_SOLID_BLOCKS)
                )
                if not is_static_shallow:
                    return False
        return True

    def _try_plug_water_cell(self, bb: Blackboard, cell: Coord3) -> Optional[str]:
        scaffold = pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS)
        if scaffold is None or self.ctx.gateway.unreachable.is_blocked(bb.dimension, cell, "place"):
            return None
        p_out = self._place.place_at(bb, scaffold, cell)
        if p_out.status == "UNKNOWN":
            return "UNKNOWN"
        if p_out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"
        return "FAILED_WRITE" if bb.writes_this_tick >= 1 else None

    def _update_mining_anchors(self, bb: Blackboard, fx: int, fy: int, fz: int) -> None:
        db = self.ctx.gateway.db
        if fy >= 58:
            if not self._surface_anchor_saved:
                prev_surf = load_anchor_landmark(db, bb, "last_surface_anchor", "surface_entry", writer="mining")
                if (
                    prev_surf is None
                    or dist3(prev_surf, (fx, fy, fz)) > 48.0
                    or (not is_underground(bb, self.ctx.gateway) and fy >= prev_surf[1])
                ):
                    save_anchor_landmark(db, bb, "last_surface_anchor", "surface_entry", (fx, fy, fz), writer="mining")
                self._surface_anchor_saved = True
        else:
            prev = load_anchor_landmark(db, bb, "last_mine_anchor", "mine_shaft", writer="mining")
            if prev is None or dist3(prev, (fx, fy, fz)) >= 4.0:
                save_anchor_landmark(db, bb, "last_mine_anchor", "mine_shaft", (fx, fy, fz), writer="mining")

    def on_tick(self, bb: Blackboard) -> str:
        gather = self.catalogue.gathering.get(self.item)
        if gather is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, self.item, self.count))

        # 1) Ensure pickaxe tier (>= min_pickaxe, never downgrade iron_pickaxe -> stone_pickaxe!)
        if gather.min_pickaxe and not _has_pickaxe_tier(bb.inventory_counts, gather.min_pickaxe):
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, gather.min_pickaxe, 1))

        fx, fy, fz = block_coord(bb.pos)
        self._update_mining_anchors(bb, fx, fy, fz)

        # 2) Cave-first: scan exposed ore veins within 24m near current elevation (ignore sealed far-Y pockets)
        out = self._dig.dig_nearest_of_types(
            bb,
            gather.block_types,
            radius=24,
            collect=True,
            max_dy=4.0,
            candidate_filter=lambda c: self._is_safe_to_dig(bb, c, allow_shallow_floor_water=True),
        )
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"
        if out.status == "UNKNOWN":
            return "UNKNOWN"

        # 2.5) Above y >= 58: try reusing remembered mine_shaft within 48m before digging a brand-new staircase
        if fy >= 58 and not self._tried_mine_shaft:
            shaft = load_anchor_landmark(self.ctx.gateway.db, bb, "last_mine_anchor", "mine_shaft", writer="mining")
            if shaft is not None and dist3(bb.pos, shaft) <= 48.0:
                if self._mine_shaft_last_pos is not None and dist3(self._mine_shaft_last_pos, bb.pos) < 0.35:
                    self._mine_shaft_stalls += 1
                elif self._mine_shaft_last_pos is not None:
                    self._mine_shaft_stalls = 0
                self._mine_shaft_last_pos = bb.pos
                if self._mine_shaft_stalls >= 6:
                    self._tried_mine_shaft = True
                else:
                    n_out = self._nav.step_to(bb, (shaft[0] + 0.5, float(shaft[1]), shaft[2] + 0.5), tolerance=2.0)
                    if n_out.status == "UNKNOWN":
                        return "UNKNOWN"
                    if n_out.status == "OK":
                        self._tried_mine_shaft = True
                        if bb.writes_this_tick >= 1:
                            return "RUNNING"
                    elif n_out.status == "PROGRESSED":
                        return "RUNNING"
                    elif n_out.status == "FAILED_LOCAL":
                        self._mine_shaft_stalls += 1
                        if self._mine_shaft_stalls >= 6:
                            self._tried_mine_shaft = True
                        if bb.writes_this_tick >= 1:
                            return "RUNNING"
            else:
                self._tried_mine_shaft = True

        gather_types = set(gather.block_types)
        clear_collect = bb.inventory_counts.get("cobblestone", 0) < 16

        # 3) Step-driven staircase descent toward target_y: try all 4 cardinal directions in 1 tick (never spin in place!)
        if fy > self.target_y:
            for _ in range(len(self._dirs)):
                dx, dz = self._dirs[self._dir_idx % len(self._dirs)]
                stair_cells = [(fx + dx, fy + 1, fz + dz), (fx + dx, fy, fz + dz), (fx + dx, fy - 1, fz + dz)]
                floor_after = (fx + dx, fy - 2, fz + dz)
                fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, stair_cells + [floor_after], max_age=3.0)
                floor_ent = fetched.get(floor_after)
                if floor_ent is not None and floor_ent.name in WATER_BLOCKS:
                    plug_res = self._try_plug_water_cell(bb, floor_after)
                    if plug_res in {"RUNNING", "UNKNOWN"}:
                        return plug_res
                    self._dir_idx += 1
                    if plug_res == "FAILED_WRITE":
                        return "RUNNING"
                    continue
                if floor_ent is None or floor_ent.name in NON_SOLID_BLOCKS or floor_ent.name in HAZARD_BLOCKS:
                    self._dir_idx += 1
                    continue
                blocked_by_hazard = False
                for cell in stair_cells:
                    ent = fetched.get(cell)
                    if ent is not None and ent.name not in AIR_BLOCKS:
                        if ent.name in WATER_BLOCKS:
                            plug_res = self._try_plug_water_cell(bb, cell)
                            if plug_res in {"RUNNING", "UNKNOWN"}:
                                return plug_res
                            blocked_by_hazard = True
                            if plug_res == "FAILED_WRITE":
                                self._dir_idx += 1
                                return "RUNNING"
                            break
                        is_target = ent.name in gather_types
                        if not self._is_safe_to_dig(bb, cell, allow_shallow_floor_water=is_target):
                            blocked_by_hazard = True
                            break
                        d_out = self._dig.dig_block(bb, cell, collect=is_target or clear_collect)
                        return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
                if blocked_by_hazard:
                    self._dir_idx += 1
                    continue
                n_out = self._nav.step_to(bb, (fx + dx + 0.5, float(fy - 1), fz + dz + 0.5), tolerance=0.6)
                return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status
            # All 4 staircase directions blocked by drops/hazards -> try horizontal 2-high tunnel instead of spinning!

        # 4) Horizontal 2-high tunnel (head then feet) trying all 4 directions in 1 tick
        for _ in range(len(self._dirs)):
            dx, dz = self._dirs[self._dir_idx % len(self._dirs)]
            head_cell = (fx + dx, fy + 1, fz + dz)
            feet_cell = (fx + dx, fy, fz + dz)
            floor_cell = (fx + dx, fy - 1, fz + dz)
            fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, [head_cell, feet_cell, floor_cell], max_age=3.0)
            floor_ent = fetched.get(floor_cell)
            if floor_ent is not None and floor_ent.name in WATER_BLOCKS:
                plug_res = self._try_plug_water_cell(bb, floor_cell)
                if plug_res in {"RUNNING", "UNKNOWN"}:
                    return plug_res
                self._dir_idx += 1
                if plug_res == "FAILED_WRITE":
                    return "RUNNING"
                continue
            if floor_ent is None or floor_ent.name in NON_SOLID_BLOCKS or floor_ent.name in HAZARD_BLOCKS:
                self._dir_idx += 1
                continue
            blocked_by_hazard = False
            for cell in (head_cell, feet_cell):
                ent = fetched.get(cell)
                if ent is not None and ent.name not in AIR_BLOCKS:
                    if ent.name in WATER_BLOCKS:
                        plug_res = self._try_plug_water_cell(bb, cell)
                        if plug_res in {"RUNNING", "UNKNOWN"}:
                            return plug_res
                        blocked_by_hazard = True
                        if plug_res == "FAILED_WRITE":
                            self._dir_idx += 1
                            return "RUNNING"
                        break
                    is_target = ent.name in gather_types
                    if not self._is_safe_to_dig(bb, cell, allow_shallow_floor_water=is_target):
                        blocked_by_hazard = True
                        break
                    d_out = self._dig.dig_block(bb, cell, collect=is_target or clear_collect)
                    return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
            if blocked_by_hazard:
                self._dir_idx += 1
                continue
            n_out = self._nav.step_to(bb, (fx + dx + 0.5, float(fy), fz + dz + 0.5), tolerance=0.6)
            return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status

        bb.last_failure_reason = "mine:all_directions_blocked"
        return "NEEDS_AI"
