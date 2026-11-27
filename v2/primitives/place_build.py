"""L3a Primitive 3: PlaceBuildPrimitive (workstations, beds, doors, single-block structure completion)."""

from __future__ import annotations

from typing import Optional

from v2.bt.blackboard import Blackboard
from v2.domain import AIR_BLOCKS, Coord3, HAZARD_BLOCKS, LIQUID_BLOCKS, NON_SOLID_BLOCKS, block_coord, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive


class PlaceBuildPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)
        self._auto_place_failed_at: Optional[tuple[str, Coord3]] = None

    def _has_known_no_support(self, bb: Blackboard, cell: Coord3) -> bool:
        """Return True only when all 6 orthogonal neighbors are known and all are non-solid."""
        cx, cy, cz = cell
        nbs = (
            (cx + 1, cy, cz),
            (cx - 1, cy, cz),
            (cx, cy + 1, cz),
            (cx, cy - 1, cz),
            (cx, cy, cz + 1),
            (cx, cy, cz - 1),
        )
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, nbs, max_age=2.0)
        ents = [fetched.get(c) for c in nbs]
        if any(e is None for e in ents):
            return False
        return all(e.name in NON_SOLID_BLOCKS for e in ents if e is not None)

    def place_at(self, bb: Blackboard, item: str, target: Coord3) -> ActionOutcome:
        """Approach if needed, otherwise place ONE block and update voxel cache write-through."""
        if bb.inventory_counts.get(item, 0) < 1:
            bb.last_failure_reason = f"place:missing_{item}"
            return ActionOutcome("FAILED_LOCAL", "place", reason=f"missing_{item}")
        t = (int(target[0]), int(target[1]), int(target[2]))
        fx, fy, fz = block_coord(bb.pos)
        if t in {(fx, fy, fz), (fx, fy + 1, fz)}:
            # Step 2.5 blocks away laterally with tight tolerance so place_block never fails with blocked_by_self
            return self._nav.step_to(bb, (t[0] + 0.5, float(fy), t[2] + 2.5), tolerance=0.5, xz_only=True)
        center = (t[0] + 0.5, t[1] + 0.5, t[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)

        if self._has_known_no_support(bb, t):
            self.ctx.gateway.unreachable.record_failure(bb.dimension, t, "place", "no_support_precheck")
            bb.last_failure_reason = "place:no_support_precheck"
            return ActionOutcome("FAILED_LOCAL", "place", reason="no_support_precheck")

        out = self.ctx.execute_write(
            bb,
            "place",
            {"item": item, "pos": list(t)},
            lambda: self.ctx.client.place_block(item, t[0], t[1], t[2]),
            unknown_center=t,
            record_unreachable_action="place",
        )
        if out.status == "OK":
            self.ctx.gateway.write_through_place(bb.dimension, t, item)
            if item in {"crafting_table", "furnace", "chest"} and self.ctx.gateway.db is not None:
                self.ctx.gateway.db.save_landmark(bb.dimension, item, f"{item}_{t[0]}_{t[1]}_{t[2]}", t)
        return out

    def place_near_player(self, bb: Blackboard, item: str) -> ActionOutcome:
        """Use upstream POST /place {item} (1ms autoPlaceSite + auto-sneak) first; fall back to local probing on no_support."""
        if bb.inventory_counts.get(item, 0) < 1:
            bb.last_failure_reason = f"place:missing_{item}"
            return ActionOutcome("FAILED_LOCAL", "place", reason=f"missing_{item}")
        if bool(bb.status.get("in_water") or bb.status.get("submerged")):
            bb.last_failure_reason = "place:in_water_no_support"
            return ActionOutcome("FAILED_LOCAL", "place", reason="in_water_no_support")

        cur_cell = block_coord(bb.pos)
        # 1) Try upstream 1ms in-memory autoPlaceSite unless it already returned no_support at this exact cell
        if (
            self._auto_place_failed_at != (bb.dimension, cur_cell)
            and not self.ctx.gateway.unreachable.is_blocked(bb.dimension, cur_cell, "place_auto")
        ):
            out = self.ctx.execute_write(
                bb,
                "place",
                {"item": item},
                lambda: self.ctx.client.place_block(item),
                unknown_center=cur_cell,
                record_unreachable_action="place_auto",
            )
            if out.status == "OK":
                self._auto_place_failed_at = None
                raw_pos = out.details.get("pos")
                if isinstance(raw_pos, list) and len(raw_pos) == 3:
                    placed = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
                    self.ctx.gateway.write_through_place(bb.dimension, placed, item)
                    if item in {"crafting_table", "furnace", "chest"} and self.ctx.gateway.db is not None:
                        self.ctx.gateway.db.save_landmark(bb.dimension, item, f"{item}_{placed[0]}_{placed[1]}_{placed[2]}", placed)
                return out
            if out.status == "UNKNOWN":
                return out
            if out.reason == "no_support":
                self._auto_place_failed_at = (bb.dimension, cur_cell)
                # Strict Single-Write Macro-Tick: local probe runs on next tick
                return ActionOutcome("PROGRESSED", "place", reason="auto_place_no_support_fallback")
            return out

        # 2) Fallback: local block probing around player (fy, fy+1, fy-1) skipping blacklisted cells
        fx, fy, fz = cur_cell
        offsets = [(2, 0), (-2, 0), (0, 2), (0, -2), (1, 1), (-1, 1), (1, -1), (-1, -1), (1, 0), (-1, 0), (0, 1), (0, -1)]
        dy_order = (0, 1, -1)
        coords = []
        for oy in dy_order:
            for ox, oz in offsets:
                coords.append((fx + ox, fy + oy, fz + oz))
                coords.append((fx + ox, fy + oy - 1, fz + oz))
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, coords)
        for oy in dy_order:
            for ox, oz in offsets:
                cell = (fx + ox, fy + oy, fz + oz)
                if self.ctx.gateway.unreachable.is_blocked(bb.dimension, cell, "place"):
                    continue
                below = (fx + ox, fy + oy - 1, fz + oz)
                c_ent = fetched.get(cell)
                b_ent = fetched.get(below)
                if c_ent is not None and c_ent.name in AIR_BLOCKS and b_ent is not None and b_ent.name not in NON_SOLID_BLOCKS:
                    self._auto_place_failed_at = None
                    return self.place_at(bb, item, cell)

        # 3) Narrow tunnel / corner fallback: safely dig a 1x1 side alcove with solid floor and zero liquid/hazard neighbors
        protected_blocks = frozenset({
            "bedrock", "obsidian", "crafting_table", "furnace", "chest",
            "blast_furnace", "smoker", "nether_portal",
        })
        alcove_cands = [(fx + dx, fy, fz + dz) for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))]
        alcove_probe = []
        for cx, cy, cz in alcove_cands:
            alcove_probe.extend([
                (cx, cy, cz),
                (cx, cy - 1, cz),
                (cx, cy + 1, cz),
                (cx + 1, cy, cz),
                (cx - 1, cy, cz),
                (cx, cy, cz + 1),
                (cx, cy, cz - 1),
            ])
        alcove_fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, alcove_probe, max_age=3.0)
        for cell in alcove_cands:
            cx, cy, cz = cell
            if self.ctx.gateway.unreachable.is_blocked(bb.dimension, cell, "dig"):
                continue
            if self.ctx.gateway.unreachable.is_blocked(bb.dimension, cell, "place"):
                continue
            target_ent = alcove_fetched.get(cell)
            below_ent = alcove_fetched.get((cx, cy - 1, cz))
            if below_ent is None or below_ent.name in (NON_SOLID_BLOCKS | HAZARD_BLOCKS | LIQUID_BLOCKS):
                continue
            if target_ent is None or target_ent.name in (AIR_BLOCKS | HAZARD_BLOCKS | LIQUID_BLOCKS | protected_blocks):
                continue
            if target_ent.name.endswith("_bed"):
                continue
            neighbors = (
                alcove_fetched.get((cx, cy + 1, cz)),
                alcove_fetched.get((cx + 1, cy, cz)),
                alcove_fetched.get((cx - 1, cy, cz)),
                alcove_fetched.get((cx, cy, cz + 1)),
                alcove_fetched.get((cx, cy, cz - 1)),
            )
            if any(nb is not None and nb.name in (HAZARD_BLOCKS | LIQUID_BLOCKS) for nb in neighbors):
                continue
            dig_out = self.ctx.execute_write(
                bb,
                "dig",
                {"pos": list(cell), "collect": False},
                lambda _c=cell: self.ctx.client.dig(list(_c), collect=False),
                unknown_center=cell,
                record_unreachable_action="dig",
            )
            if dig_out.status == "OK":
                self.ctx.gateway.write_through_dig(bb.dimension, cell)
                self._auto_place_failed_at = (bb.dimension, cur_cell)
                return ActionOutcome("PROGRESSED", "dig", reason="place_alcove_cleared", details={"pos": list(cell)})
            return dig_out

        bb.last_failure_reason = "place:no_adjacent_clear_spot"
        return ActionOutcome("FAILED_LOCAL", "place", reason="no_adjacent_clear_spot")
