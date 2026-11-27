"""L3b Deterministic ReturnToSurfaceTask: safely staircases out of caves/mines back to open sky."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import (
    AIR_BLOCKS,
    Coord3,
    HAZARD_BLOCKS,
    LEAF_BLOCKS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    PICKAXE_TIERS,
    SCAFFOLD_BLOCKS,
    SOFT_OCCLUDERS,
    WATER_BLOCKS,
    block_coord,
    dist3,
    pick_best_item,
)
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.primitives.base import PrimitiveContext
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.base import Task


def save_anchor_landmark(db: Any, bb: Blackboard, bb_slot: str, kind: str, pos: Coord3, *, writer: str = "surface") -> None:
    """Persist an anchor coordinate to Blackboard and UnifiedWorldDB landmarks (or duck-typed mock)."""
    coord = (int(pos[0]), int(pos[1]), int(pos[2]))
    bb.set(bb_slot, coord, writer=writer)
    if db is None:
        return
    if hasattr(db, "save_landmark"):
        db.save_landmark(bb.dimension, kind, "latest", coord)
    elif hasattr(db, "upsert_landmark"):
        db.upsert_landmark(bb.dimension, kind, "latest", coord)


def load_anchor_landmark(db: Any, bb: Blackboard, bb_slot: str, kind: str, *, writer: str = "surface") -> Optional[Coord3]:
    """Read an anchor coordinate from Blackboard first, falling back to UnifiedWorldDB landmarks."""
    cached = bb.get(bb_slot)
    if isinstance(cached, (list, tuple)) and len(cached) == 3:
        return (int(cached[0]), int(cached[1]), int(cached[2]))
    if db is None:
        return None
    row: Optional[dict] = None
    if hasattr(db, "list_landmarks"):
        rows = db.list_landmarks(bb.dimension, kind=kind)
        row = next((r for r in rows if r.get("name") == "latest"), rows[0] if rows else None)
    elif hasattr(db, "get_landmark"):
        row = db.get_landmark(bb.dimension, kind, "latest")
    if isinstance(row, dict):
        pos = row.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 3:
            coord = (int(pos[0]), int(pos[1]), int(pos[2]))
            bb.set(bb_slot, coord, writer=writer)
            return coord
    return None


_PROTECTED_SURFACE_BLOCKS = frozenset({
    "bedrock", "obsidian", "crafting_table", "furnace", "chest",
    "blast_furnace", "smoker", "nether_portal",
})
_SOFT_SURFACE_DIG_BLOCKS = SOFT_OCCLUDERS | LEAF_BLOCKS | frozenset({
    "sand", "red_sand", "gravel", "clay", "mud", "snow_block",
})


def is_underground(bb: Blackboard, gateway: VirtualWorldGateway, *, min_surface_y: int = 58) -> bool:
    """Return True when overworld bot sits below min_surface_y beneath a solid ceiling."""
    if bb.dimension != "overworld":
        return False
    fx, fy, fz = block_coord(bb.pos)
    if fy >= min_surface_y:
        return False
    column = [(fx, fy + dy, fz) for dy in range(2, 8)]
    fetched = gateway.prefetch_blocks(bb.dimension, column, max_age=4.0)
    return any(
        (ent := fetched.get(c)) is not None and ent.name not in (AIR_BLOCKS | LEAF_BLOCKS | NON_SOLID_BLOCKS)
        for c in column
    )


class ReturnToSurfaceTask(Task):
    """Steps up a safe 3-high staircase until reaching surface Y or continuous open sky."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        *,
        target_y: int = 62,
        max_steps: int = 96,
    ) -> None:
        super().__init__("return_to_surface")
        self.ctx = ctx
        self.target_y = int(target_y)
        self.max_steps = max(1, int(max_steps))
        self._dig = DigCollectPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._dir_idx: int = 0
        self._steps: int = 0
        self._dirs = ((1, 0), (0, 1), (-1, 0), (0, -1))
        self._surface_route_exhausted: bool = False
        self._surface_route_stalls: int = 0
        self._surface_route_last_pos: Optional[Tuple[float, float, float]] = None
        self._no_pick_hard_digs: int = 0
        self._open_pillar_stalls: int = 0
        self._open_pillar_last_y: Optional[int] = None

    def stop(self, bb: Blackboard) -> None:
        self._dig.clear_cache()
        self._surface_route_exhausted = False
        self._surface_route_stalls = 0
        self._surface_route_last_pos = None
        self._no_pick_hard_digs = 0
        self._open_pillar_stalls = 0
        self._open_pillar_last_y = None
        super().stop(bb)

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

    def _find_plug_water_neighbor(self, bb: Blackboard, cell: Coord3) -> Optional[Coord3]:
        """If cell is blocked only by lateral water (no lava / overhead liquid), return that water neighbor to plug."""
        cx, cy, cz = cell
        lateral = [(cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy, cz + 1), (cx, cy, cz - 1)]
        probe = [(cx, cy, cz), (cx, cy + 1, cz)] + lateral
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)
        target_ent = fetched.get((cx, cy, cz))
        if target_ent is not None and (target_ent.name in _PROTECTED_SURFACE_BLOCKS or target_ent.name.endswith("_bed")):
            return None
        overhead = fetched.get((cx, cy + 1, cz))
        if overhead is not None and (overhead.name in HAZARD_BLOCKS or overhead.name in LIQUID_BLOCKS):
            return None
        if any((ent := fetched.get(c)) is not None and ent.name in HAZARD_BLOCKS for c in lateral):
            return None
        for c in lateral:
            ent = fetched.get(c)
            if ent is not None and ent.name in WATER_BLOCKS:
                return c
        return None

    def is_finished(self, bb: Blackboard) -> bool:
        if bb.dimension != "overworld":
            return True
        fx, fy, fz = block_coord(bb.pos)
        if fy >= self.target_y:
            return True
        sky_col = [(fx, fy + dy, fz) for dy in range(2, 12)]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, sky_col, max_age=3.0)
        return all(
            (ent := fetched.get(c)) is not None and ent.name in (AIR_BLOCKS | LEAF_BLOCKS)
            for c in sky_col
        )

    def _is_safe_to_dig(self, bb: Blackboard, cell: Coord3) -> bool:
        cx, cy, cz = cell
        probe = [(cx, cy, cz), (cx, cy + 1, cz), (cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy, cz + 1), (cx, cy, cz - 1)]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)
        target_ent = fetched.get((cx, cy, cz))
        if target_ent is not None and (
            target_ent.name in _PROTECTED_SURFACE_BLOCKS or target_ent.name.endswith("_bed")
        ):
            return False
        return not any(
            (ent := fetched.get(c)) is not None and (ent.name in HAZARD_BLOCKS or ent.name in LIQUID_BLOCKS)
            for c in probe
        )

    def on_tick(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = "surface_escape_budget_exhausted"
            return "NEEDS_AI"

        # 1) Fast-path return via remembered surface_entry within 160m (bounded to max 8 stalled/blocked ticks)
        if not self._surface_route_exhausted:
            anchor = load_anchor_landmark(self.ctx.gateway.db, bb, "last_surface_anchor", "surface_entry", writer="surface")
            if anchor is not None and dist3(bb.pos, anchor) <= 160.0:
                anchor_center = (anchor[0] + 0.5, float(anchor[1]), anchor[2] + 0.5)
                if dist3(bb.pos, anchor_center) <= 1.8 and anchor[1] < self.target_y:
                    self._surface_route_exhausted = True
                else:
                    if self._surface_route_last_pos is not None and dist3(self._surface_route_last_pos, bb.pos) < 0.35:
                        self._surface_route_stalls += 1
                    elif self._surface_route_last_pos is not None:
                        self._surface_route_stalls = 0
                    self._surface_route_last_pos = bb.pos
                    if self._surface_route_stalls >= 8:
                        self._surface_route_exhausted = True
                    else:
                        n_out = self._nav.step_to(
                            bb,
                            anchor_center,
                            tolerance=1.5,
                            avoid_hard_rock_without_pick=True,
                        )
                        if n_out.status == "UNKNOWN":
                            return "UNKNOWN"
                        if n_out.status == "OK":
                            self._surface_route_exhausted = True
                            if bb.writes_this_tick >= 1:
                                return "RUNNING"
                        elif n_out.status == "PROGRESSED":
                            return "RUNNING"
                        elif n_out.status == "FAILED_LOCAL":
                            self._surface_route_stalls += 1
                            if self._surface_route_stalls >= 8:
                                self._surface_route_exhausted = True
                            if bb.writes_this_tick >= 1 or not self._surface_route_exhausted:
                                return "RUNNING"
            else:
                self._surface_route_exhausted = True

        # 2) Conservative 3-high staircase fallback (+ fast water plug before rotating direction)
        fx, fy, fz = block_coord(bb.pos)
        if self._open_pillar_last_y is not None and fy > self._open_pillar_last_y:
            self._open_pillar_stalls = 0
        self._open_pillar_last_y = fy
        has_pick = any(bb.inventory_counts.get(p, 0) > 0 for p in PICKAXE_TIERS)
        no_pick_origin = bb.get("surface_no_pick_origin")
        if (
            fy >= self.target_y
            or has_pick
            or (no_pick_origin is not None and dist3(bb.pos, no_pick_origin) >= 12.0)
        ):
            if int(bb.get("surface_no_pick_hard_digs", 0) or 0) != 0:
                bb.set("surface_no_pick_hard_digs", 0, writer="surface")
            if no_pick_origin is not None:
                bb.set("surface_no_pick_origin", None, writer="surface")

        # Without a pickaxe, climb open vertical cave chimneys via scaffold pillar (needs >=4 air above feet so head+1 fits)
        if (
            not has_pick
            and self._surface_route_exhausted
            and self._open_pillar_stalls < 3
            and pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS) is not None
        ):
            sky_open = [(fx, fy + 2, fz), (fx, fy + 3, fz), (fx, fy + 4, fz), (fx, fy + 5, fz)]
            sky_map = self.ctx.gateway.prefetch_blocks(bb.dimension, sky_open, max_age=2.0)
            if all((e := sky_map.get(c)) is not None and e.name in (AIR_BLOCKS | LEAF_BLOCKS) for c in sky_open):
                p_nav = self._nav.step_to(
                    bb,
                    (fx + 0.5, float(fy + 3), fz + 0.5),
                    tolerance=0.8,
                    prefer_3d=True,
                    avoid_hard_rock_without_pick=True,
                )
                if p_nav.status == "UNKNOWN":
                    return "UNKNOWN"
                if p_nav.status in {"OK", "PROGRESSED"}:
                    return "RUNNING"
                self._open_pillar_stalls += 1
                if bb.writes_this_tick >= 1:
                    return "RUNNING"

        # Without a pickaxe, scan for any open/soft-only direction before attempting hard-rock digs
        if not has_pick:
            for cand_i in range(len(self._dirs)):
                idx = (self._dir_idx + cand_i) % len(self._dirs)
                cdx, cdz = self._dirs[idx]
                c_clr = [(fx, fy + 2, fz), (fx + cdx, fy + 2, fz + cdz), (fx + cdx, fy + 1, fz + cdz)]
                c_map = self.ctx.gateway.prefetch_blocks(bb.dimension, c_clr, max_age=3.0)
                if all(
                    (e := c_map.get(cc)) is None
                    or e.name in (AIR_BLOCKS | LEAF_BLOCKS | _SOFT_SURFACE_DIG_BLOCKS)
                    or e.name.endswith(("_log", "_planks"))
                    for cc in c_clr
                ):
                    self._dir_idx = idx
                    break

        for _ in range(len(self._dirs)):
            dx, dz = self._dirs[self._dir_idx % len(self._dirs)]
            # Clear overhead at current column + headroom at next elevated step (fy+1, fy+2)
            clearance = [
                (fx, fy + 2, fz),
                (fx + dx, fy + 2, fz + dz),
                (fx + dx, fy + 1, fz + dz),
            ]
            floor_cell = (fx + dx, fy, fz + dz)
            fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, clearance + [floor_cell], max_age=3.0)

            blocked = False
            for cell in clearance:
                ent = fetched.get(cell)
                if ent is not None and ent.name not in (AIR_BLOCKS | LEAF_BLOCKS):
                    if ent.name in WATER_BLOCKS:
                        plug_res = self._try_plug_water_cell(bb, cell)
                        if plug_res in {"RUNNING", "UNKNOWN"}:
                            return plug_res
                        blocked = True
                        if plug_res == "FAILED_WRITE":
                            self._dir_idx += 1
                            return "RUNNING"
                        break
                    if ent.name in (HAZARD_BLOCKS | LIQUID_BLOCKS) or not self._is_safe_to_dig(bb, cell):
                        water_nb = self._find_plug_water_neighbor(bb, cell)
                        if water_nb is not None:
                            plug_res = self._try_plug_water_cell(bb, water_nb)
                            if plug_res in {"RUNNING", "UNKNOWN"}:
                                return plug_res
                            blocked = True
                            if plug_res == "FAILED_WRITE":
                                self._dir_idx += 1
                                return "RUNNING"
                            break
                        blocked = True
                        break
                    if self.ctx.gateway.unreachable.is_blocked(bb.dimension, cell, "dig"):
                        blocked = True
                        break
                    is_soft_or_wood = (
                        ent.name in _SOFT_SURFACE_DIG_BLOCKS
                        or ent.name.endswith(("_log", "_planks"))
                    )
                    has_pick = any(bb.inventory_counts.get(p, 0) > 0 for p in PICKAXE_TIERS)
                    hard_digs = max(self._no_pick_hard_digs, int(bb.get("surface_no_pick_hard_digs", 0) or 0))
                    if not is_soft_or_wood and not has_pick and hard_digs >= 2:
                        bb.set("surface_no_pick_origin", bb.pos, writer="surface")
                        bb.last_failure_reason = "surface_escape:no_pickaxe"
                        return "NEEDS_AI"
                    d_out = self._dig.dig_block(bb, cell, collect=False)
                    if (
                        not is_soft_or_wood
                        and not has_pick
                        and d_out.status == "OK"
                        and isinstance(d_out.details, dict)
                        and "no proper tool" in str(d_out.details.get("warning") or "").lower()
                    ):
                        self._no_pick_hard_digs = hard_digs + 1
                        bb.set("surface_no_pick_hard_digs", self._no_pick_hard_digs, writer="surface")
                    return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
            if blocked:
                self._dir_idx += 1
                continue

            floor_ent = fetched.get(floor_cell)
            if floor_ent is not None and floor_ent.name in HAZARD_BLOCKS:
                self._dir_idx += 1
                continue
            if floor_ent is None or floor_ent.name in NON_SOLID_BLOCKS:
                scaffold = pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS)
                if scaffold is None or self.ctx.gateway.unreachable.is_blocked(bb.dimension, floor_cell, "place"):
                    self._dir_idx += 1
                    continue
                nbs = [
                    (floor_cell[0] + ox, floor_cell[1] + oy, floor_cell[2] + oz)
                    for ox, oy, oz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
                ]
                nb_map = self.ctx.gateway.prefetch_blocks(bb.dimension, nbs, max_age=3.0)
                if not any((nb_e := nb_map.get(nb)) is not None and nb_e.name not in NON_SOLID_BLOCKS for nb in nbs):
                    self._dir_idx += 1
                    continue
                p_out = self._place.place_at(bb, scaffold, floor_cell)
                return "RUNNING" if p_out.status in {"OK", "PROGRESSED"} else p_out.status

            n_out = self._nav.step_to(
                bb,
                (fx + dx + 0.5, float(fy + 1), fz + dz + 0.5),
                tolerance=0.6,
                avoid_hard_rock_without_pick=True,
            )
            if n_out.status == "FAILED_LOCAL":
                self._dir_idx += 1
                continue
            return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status

        bb.last_failure_reason = "surface_escape:all_directions_blocked"
        return "NEEDS_AI"
