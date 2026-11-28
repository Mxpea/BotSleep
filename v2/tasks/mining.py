"""L3b MineResourceTask: cave-first ore harvesting + safe 3-high staircase descent to Y=16 / Y=-54 + 2-high tunneling."""

from __future__ import annotations

from collections import deque
from typing import Optional, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import (
    AIR_BLOCKS,
    Coord3,
    FOOD_ITEMS,
    HAZARD_BLOCKS,
    LIQUID_BLOCKS,
    LOG_BLOCKS,
    NON_SOLID_BLOCKS,
    PLANK_ITEMS,
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


_DEEP_OPPORTUNISTIC_ITEMS = frozenset({
    "raw_iron", "raw_gold", "diamond", "redstone", "lapis_lazuli", "raw_copper",
})


def deep_mining_ready(bb: Blackboard) -> bool:
    """Return True when the player holds a safe deep-Y (-53) mining and survival kit."""
    inv = bb.inventory_counts
    if not _has_pickaxe_tier(inv, "iron_pickaxe"):
        return False
    if inv.get("water_bucket", 0) < 1:
        return False
    if (inv.get("cobblestone", 0) + inv.get("cobbled_deepslate", 0)) < 16:
        return False
    edible_food = sum(int(inv.get(name, 0)) for name in FOOD_ITEMS if name != "rotten_flesh")
    if edible_food < 4:
        return False
    return bb.health >= 14.0


class MineResourceTask(Task):
    """Conservative enclosed mining: safe staircase descent to Y=16 / Y=-54, 1x2 tunneling, cave avoidance, protected return treads."""

    CLOSE_ORE_RADIUS = 5
    CLOSE_ORE_MAX_DY = 2.0
    OPPORTUNISTIC_ORE_RADIUS = 5
    TORCH_INTERVAL_STEPS = 8
    PROTECTED_TRAIL_MAX = 64

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
        if target_y is not None:
            self.target_y = target_y
        elif gather and gather.preferred_y is not None:
            self.target_y = -53 if gather.preferred_y <= -54 else gather.preferred_y
        else:
            self.target_y = 16
        self._dig = DigCollectPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._dir_idx: int = 0
        self._dirs = ((1, 0), (0, 1), (-1, 0), (0, -1))
        self._tried_mine_shaft: bool = False
        self._entry_reached: bool = False
        self._mine_shaft_stalls: int = 0
        self._mine_shaft_last_pos: Optional[Tuple[float, float, float]] = None
        self._surface_anchor_saved: bool = False
        self._protected_treads: Set[Coord3] = set()
        self._protected_tread_order: deque[Coord3] = deque()
        self._visited_air: Set[Coord3] = set()
        self._visited_air_order: deque[Coord3] = deque()
        self._last_pos_cell: Optional[Coord3] = None
        self._last_step_cell: Optional[Coord3] = None
        self._steps_since_torch: int = 0

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.item, 0) >= self.count

    def stop(self, bb: Blackboard) -> None:
        self._dig.clear_cache()
        self._tried_mine_shaft = False
        self._entry_reached = False
        self._mine_shaft_stalls = 0
        self._mine_shaft_last_pos = None
        self._surface_anchor_saved = False
        self._protected_treads.clear()
        self._protected_tread_order.clear()
        self._visited_air.clear()
        self._visited_air_order.clear()
        self._last_pos_cell = None
        self._last_step_cell = None
        self._steps_since_torch = 0
        super().stop(bb)

    PROTECTED_STATIONS = frozenset({
        "crafting_table", "furnace", "chest", "blast_furnace", "smoker",
        "obsidian", "nether_portal",
    })

    @staticmethod
    def _track_bounded(seen: Set[Coord3], order: deque[Coord3], cell: Coord3, limit: int) -> None:
        if cell in seen:
            return
        seen.add(cell)
        order.append(cell)
        while len(order) > limit:
            seen.discard(order.popleft())

    def _record_step_and_protect(self, fx: int, fy: int, fz: int) -> None:
        """Protect underfoot return staircase treads and remember our own carved tunnel."""
        cur = (fx, fy, fz)
        self._track_bounded(self._visited_air, self._visited_air_order, cur, self.PROTECTED_TRAIL_MAX * 2)
        self._track_bounded(self._visited_air, self._visited_air_order, (fx, fy + 1, fz), self.PROTECTED_TRAIL_MAX * 2)
        self._track_bounded(self._protected_treads, self._protected_tread_order, (fx, fy - 1, fz), self.PROTECTED_TRAIL_MAX)
        if self._last_pos_cell is not None and self._last_pos_cell != cur:
            px, py, pz = self._last_pos_cell
            self._track_bounded(self._protected_treads, self._protected_tread_order, (px, py - 1, pz), self.PROTECTED_TRAIL_MAX)
            if (px, pz) != (fx, fz):
                self._last_step_cell = self._last_pos_cell
                if fy < 58:
                    self._steps_since_torch += 1
        self._last_pos_cell = cur

    def _is_safe_to_dig(self, bb: Blackboard, cell: Coord3, *, allow_shallow_floor_water: bool = False) -> bool:
        if cell in self._protected_treads:
            return False
        cx, cy, cz = cell
        lateral = [(cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy, cz + 1), (cx, cy, cz - 1)]
        check = [(cx, cy, cz), (cx, cy + 1, cz), (cx, cy - 1, cz)] + lateral
        extra = [(nx, cy + 1, nz) for nx, _, nz in lateral] + [(nx, cy - 1, nz) for nx, _, nz in lateral]
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
        if cell_below is not None and (cell_below.name in HAZARD_BLOCKS or cell_below.name in LIQUID_BLOCKS):
            return False
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

    def _direction_opens_into_cave(
        self,
        bb: Blackboard,
        fx: int,
        fy: int,
        fz: int,
        dx: int,
        dz: int,
        *,
        descending: bool,
    ) -> bool:
        """Return True when advancing in (dx, dz) underground breaks into a natural cave or lava hazard."""
        if fy >= 58:
            return False
        nx, nz = fx + dx, fz + dz
        ny = fy - 1 if descending else fy
        lx, lz = -dz, dx
        envelope = [
            (nx + dx, ny, nz + dz),
            (nx + dx, ny + 1, nz + dz),
            (nx + lx, ny, nz + lz),
            (nx + lx, ny + 1, nz + lz),
            (nx - lx, ny, nz - lz),
            (nx - lx, ny + 1, nz - lz),
            (nx, ny + (3 if descending else 2), nz),
        ]
        drop_cell = (nx + dx, ny - 1, nz + dz)
        next_step = [(nx, ny, nz), (nx, ny + 1, nz)]
        probe = envelope + [drop_cell] + next_step
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)
        if any((ent := fetched.get(c)) is not None and ent.name in HAZARD_BLOCKS for c in probe):
            return True
        natural_air = sum(
            1
            for c in envelope
            if c not in self._visited_air and (ent := fetched.get(c)) is not None and ent.name in AIR_BLOCKS
        )
        if natural_air >= 4:
            return True
        next_open = all(
            c not in self._visited_air and (ent := fetched.get(c)) is not None and ent.name in AIR_BLOCKS
            for c in next_step
        )
        front_open = all(
            c not in self._visited_air and (ent := fetched.get(c)) is not None and ent.name in AIR_BLOCKS
            for c in envelope[:2]
        )
        drop_ent = fetched.get(drop_cell)
        if (next_open or front_open) and drop_ent is not None and drop_ent.name in (AIR_BLOCKS | LIQUID_BLOCKS | HAZARD_BLOCKS):
            return True
        return False

    def _try_place_torch(self, bb: Blackboard, fx: int, fy: int, fz: int) -> Optional[str]:
        """Place a torch on the behind-player step cell every TORCH_INTERVAL_STEPS underground when available."""
        if (
            fy >= 58
            or bb.writes_this_tick >= 1
            or bb.inventory_counts.get("torch", 0) < 1
            or self._steps_since_torch < self.TORCH_INTERVAL_STEPS
            or self._last_step_cell is None
        ):
            return None
        target = self._last_step_cell
        if target in {(fx, fy, fz), (fx, fy + 1, fz)}:
            return None
        if dist3(bb.pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)) > 4.0:
            self._steps_since_torch = 0
            return None
        if self.ctx.gateway.unreachable.is_blocked(bb.dimension, target, "place"):
            self._steps_since_torch = 0
            return None
        below = (target[0], target[1] - 1, target[2])
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, [target, below], max_age=3.0)
        t_ent = fetched.get(target)
        b_ent = fetched.get(below)
        if (
            t_ent is None
            or t_ent.name not in AIR_BLOCKS
            or b_ent is None
            or b_ent.name in (NON_SOLID_BLOCKS | HAZARD_BLOCKS | LIQUID_BLOCKS)
        ):
            self._steps_since_torch = 0
            return None
        p_out = self._place.place_at(bb, "torch", target)
        if p_out.status in {"OK", "PROGRESSED"}:
            self._steps_since_torch = 0
            return "RUNNING"
        if p_out.status == "UNKNOWN":
            return "UNKNOWN"
        if p_out.status == "FAILED_LOCAL":
            self._steps_since_torch = 0
        if bb.writes_this_tick >= 1:
            return "RUNNING"
        return None

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
                if prev_surf is None or dist3(prev_surf, (fx, fy, fz)) > 64.0:
                    save_anchor_landmark(db, bb, "last_surface_anchor", "surface_entry", (fx, fy, fz), writer="mining")
                self._surface_anchor_saved = True
        else:
            prev = load_anchor_landmark(db, bb, "last_mine_anchor", "mine_shaft", writer="mining")
            if prev is None or dist3(prev, (fx, fy, fz)) >= 4.0:
                save_anchor_landmark(db, bb, "last_mine_anchor", "mine_shaft", (fx, fy, fz), writer="mining")

    def _mine_opportunistic_ores(self, bb: Blackboard, fy: int) -> Optional[str]:
        """When underground, grab adjacent safe dry gold, diamond, or coal veins within close radius."""
        if fy > 48 or self.item not in _DEEP_OPPORTUNISTIC_ITEMS:
            return None
        inv = bb.inventory_counts
        if _has_pickaxe_tier(inv, "iron_pickaxe"):
            rich_types = tuple(
                t for t in ("diamond_ore", "deepslate_diamond_ore", "gold_ore", "deepslate_gold_ore")
                if t not in set(self.catalogue.gathering[self.item].block_types)
            )
            if rich_types:
                opp = self._dig.dig_nearest_of_types(
                    bb,
                    rich_types,
                    radius=self.OPPORTUNISTIC_ORE_RADIUS,
                    collect=True,
                    max_dy=self.CLOSE_ORE_MAX_DY,
                    candidate_filter=lambda c: self._is_safe_to_dig(bb, c, allow_shallow_floor_water=False),
                )
                if opp.status in {"OK", "PROGRESSED"}:
                    return "RUNNING"
                if opp.status == "UNKNOWN":
                    return "UNKNOWN"
                if bb.writes_this_tick >= 1:
                    return "RUNNING"
        if inv.get("coal", 0) < 4 and self.item != "coal":
            opp_coal = self._dig.dig_nearest_of_types(
                bb,
                ("coal_ore", "deepslate_coal_ore"),
                radius=self.OPPORTUNISTIC_ORE_RADIUS,
                collect=True,
                max_dy=self.CLOSE_ORE_MAX_DY,
                candidate_filter=lambda c: self._is_safe_to_dig(bb, c, allow_shallow_floor_water=False),
            )
            if opp_coal.status in {"OK", "PROGRESSED"}:
                return "RUNNING"
            if opp_coal.status == "UNKNOWN":
                return "UNKNOWN"
        return None

    def _advance_tunnel_step(
        self,
        bb: Blackboard,
        fx: int,
        fy: int,
        fz: int,
        gather_types: set[str],
        clear_collect: bool,
        *,
        descending: bool,
        avoid_caves: bool,
    ) -> Optional[str]:
        """Try all 4 cardinal directions in 1 tick for staircase descent or 1x2 horizontal tunneling."""
        for _ in range(len(self._dirs)):
            dx, dz = self._dirs[self._dir_idx % len(self._dirs)]
            if avoid_caves and self._direction_opens_into_cave(bb, fx, fy, fz, dx, dz, descending=descending):
                self._dir_idx += 1
                continue
            if descending:
                check_cells = [(fx + dx, fy + 1, fz + dz), (fx + dx, fy, fz + dz), (fx + dx, fy - 1, fz + dz)]
                floor_cell = (fx + dx, fy - 2, fz + dz)
                step_y = float(fy - 1)
            else:
                check_cells = [(fx + dx, fy + 1, fz + dz), (fx + dx, fy, fz + dz)]
                floor_cell = (fx + dx, fy - 1, fz + dz)
                step_y = float(fy)
            fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, check_cells + [floor_cell], max_age=3.0)
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
            for cell in check_cells:
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
                    if not self._is_safe_to_dig(bb, cell, allow_shallow_floor_water=False):
                        blocked_by_hazard = True
                        break
                    d_out = self._dig.dig_block(bb, cell, collect=is_target or clear_collect)
                    return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
            if blocked_by_hazard:
                self._dir_idx += 1
                continue
            n_out = self._nav.step_to(bb, (fx + dx + 0.5, step_y, fz + dz + 0.5), tolerance=0.6)
            return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status
        return None

    def on_tick(self, bb: Blackboard) -> str:
        gather = self.catalogue.gathering.get(self.item)
        if gather is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, self.item, self.count))

        # 1) Ensure pickaxe tier (>= min_pickaxe, never downgrade iron_pickaxe -> stone_pickaxe!)
        if gather.min_pickaxe and not _has_pickaxe_tier(bb.inventory_counts, gather.min_pickaxe):
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, gather.min_pickaxe, 1))

        fx, fy, fz = block_coord(bb.pos)
        # 1b) Before descending deep from surface, secure >= 12 plank-equivalents when surface trees exist
        if fy >= 58 and self.target_y <= 32 and self.item in _DEEP_OPPORTUNISTIC_ITEMS:
            inv = bb.inventory_counts
            wood_eq = (
                4 * sum(inv.get(l, 0) for l in LOG_BLOCKS)
                + sum(inv.get(p, 0) for p in PLANK_ITEMS)
                + (inv.get("stick", 0) // 2)
            )
            if wood_eq < 12 and self.ctx.gateway.scan_blocks(bb.dimension, list(LOG_BLOCKS), radius=24, limit=1):
                need_logs = max(1, (12 - wood_eq + 3) // 4)
                return self.run_child(
                    bb,
                    AcquireItemTask(self.ctx, self.catalogue, "oak_log", inv.get("oak_log", 0) + need_logs),
                )

        self._update_mining_anchors(bb, fx, fy, fz)
        self._record_step_and_protect(fx, fy, fz)

        torch_res = self._try_place_torch(bb, fx, fy, fz)
        if torch_res is not None:
            return torch_res

        # 2) Close dry-only ore veins near active tunnel (avoid pulling bot into open wet caves)
        out = self._dig.dig_nearest_of_types(
            bb,
            gather.block_types,
            radius=self.CLOSE_ORE_RADIUS,
            collect=True,
            max_dy=self.CLOSE_ORE_MAX_DY,
            candidate_filter=lambda c: self._is_safe_to_dig(bb, c, allow_shallow_floor_water=False),
        )
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        if bb.writes_this_tick >= 1:
            return "RUNNING"

        opp = self._mine_opportunistic_ores(bb, fy)
        if opp is not None:
            return opp

        # 2.5) Above y >= 58: walk back into remembered surface_entry shaft mouth first, then reuse mine_shaft
        if fy >= 58 and not self._tried_mine_shaft:
            db = self.ctx.gateway.db
            entry = load_anchor_landmark(db, bb, "last_surface_anchor", "surface_entry", writer="mining")
            shaft = load_anchor_landmark(db, bb, "last_mine_anchor", "mine_shaft", writer="mining")
            reuse_target: Optional[Coord3] = None
            entering_mouth = False
            if entry is not None and shaft is not None and not self._entry_reached and dist3(bb.pos, entry) <= 64.0:
                if dist3(bb.pos, entry) <= 3.0:
                    self._entry_reached = True
                    self._mine_shaft_stalls = 0
                    self._mine_shaft_last_pos = None
                else:
                    reuse_target = entry
                    entering_mouth = True
            if reuse_target is None and shaft is not None and dist3(bb.pos, shaft) <= 64.0:
                reuse_target = shaft
            if reuse_target is not None:
                if self._mine_shaft_last_pos is not None and dist3(self._mine_shaft_last_pos, bb.pos) < 0.35:
                    self._mine_shaft_stalls += 1
                elif self._mine_shaft_last_pos is not None:
                    self._mine_shaft_stalls = 0
                self._mine_shaft_last_pos = bb.pos
                if self._mine_shaft_stalls >= 10:
                    self._tried_mine_shaft = True
                else:
                    n_out = self._nav.step_to(bb, (reuse_target[0] + 0.5, float(reuse_target[1]), reuse_target[2] + 0.5), tolerance=2.0)
                    if n_out.status == "UNKNOWN":
                        return "UNKNOWN"
                    if n_out.status == "OK":
                        if entering_mouth:
                            self._entry_reached = True
                            self._mine_shaft_stalls = 0
                            self._mine_shaft_last_pos = None
                        else:
                            self._tried_mine_shaft = True
                        if bb.writes_this_tick >= 1:
                            return "RUNNING"
                    elif n_out.status == "PROGRESSED":
                        return "RUNNING"
                    elif n_out.status == "FAILED_LOCAL":
                        self._mine_shaft_stalls += 1
                        if self._mine_shaft_stalls >= 10:
                            self._tried_mine_shaft = True
                        if bb.writes_this_tick >= 1:
                            return "RUNNING"
            else:
                self._tried_mine_shaft = True

        gather_types = set(gather.block_types)
        clear_collect = (
            bb.inventory_counts.get("cobblestone", 0)
            + bb.inventory_counts.get("cobbled_deepslate", 0)
        ) < 8

        cave_passes = (True, False) if fy < 58 else (False,)

        # 3) Step-driven staircase descent toward target_y (prefer enclosed solid rock first)
        if fy > self.target_y:
            for avoid_caves in cave_passes:
                step_res = self._advance_tunnel_step(
                    bb,
                    fx,
                    fy,
                    fz,
                    gather_types,
                    clear_collect,
                    descending=True,
                    avoid_caves=avoid_caves,
                )
                if step_res is not None:
                    return step_res
            # All 4 staircase directions blocked by drops/hazards -> try horizontal 2-high tunnel instead of spinning!

        # 4) Horizontal 1x2 tunnel (head then feet, prefer enclosed solid rock first)
        for avoid_caves in cave_passes:
            step_res = self._advance_tunnel_step(
                bb,
                fx,
                fy,
                fz,
                gather_types,
                clear_collect,
                descending=False,
                avoid_caves=avoid_caves,
            )
            if step_res is not None:
                return step_res

        bb.last_failure_reason = "mine:all_directions_blocked"
        return "NEEDS_AI"
