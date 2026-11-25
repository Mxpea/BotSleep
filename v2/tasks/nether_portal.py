"""L3b EnterNetherTask: stateless recursive speedrun portal pipeline (<= 1 write/tick).

Real Minecraft Java obsidian casting physics & uneven-terrain mold hardening:
- Pouring `water_bucket` directly ONTO a `lava` source block replaces lava with water (NO obsidian!).
- Instead, surround upper layers (`y = ay+1..ay+3`) and uneven-terrain drop-offs with bottom-up
  cobblestone side-mold containment walls (left, right, back) so lava/water never spill, build a
  3-step cobblestone work stair for upper-layer downward LOS (`y >= ay+2`), pour lava into the
  pocket, quench from the open front `quench_cell`, and scoop the water back cleanly.
"""

from __future__ import annotations

from typing import List, Optional, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import AIR_BLOCKS, Coord3, LIQUID_BLOCKS, NON_SOLID_BLOCKS, block_coord, dist3
from v2.primitives.base import PrimitiveContext
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.fluid import FluidPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.explore import ExploreUntilTask


class EnterNetherTask(Task):
    """Stateless self-healing Nether Portal cast (with cobblestone side-mold walls), ignite, and entry task."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue) -> None:
        super().__init__("enter_nether")
        self.ctx = ctx
        self.catalogue = catalogue
        self._nav = NavigatePrimitive(ctx)
        self._dig = DigCollectPrimitive(ctx)
        self._fluid = FluidPrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._portal_anchor: Optional[Coord3] = None
        self._parked_water_pos: Optional[Coord3] = None

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.dimension == "the_nether"

    @staticmethod
    def _frame_cells(anchor: Coord3) -> List[Coord3]:
        """10 required obsidian coordinates of a 4x5 portal frame (width along X, bottom interior at anchor)."""
        ax, ay, az = anchor
        return [
            # Bottom 2
            (ax, ay - 1, az), (ax + 1, ay - 1, az),
            # Left pillar 3 (bottom to top)
            (ax - 1, ay, az), (ax - 1, ay + 1, az), (ax - 1, ay + 2, az),
            # Right pillar 3 (bottom to top)
            (ax + 2, ay, az), (ax + 2, ay + 1, az), (ax + 2, ay + 2, az),
            # Top 2
            (ax, ay + 3, az), (ax + 1, ay + 3, az),
        ]

    @staticmethod
    def _interior_cells(anchor: Coord3) -> List[Coord3]:
        ax, ay, az = anchor
        return [(ax + w, ay + h, az) for h in (0, 1, 2) for w in (0, 1)]

    @staticmethod
    def _work_stair_cells(anchor: Coord3, target_y: Optional[int] = None) -> List[Coord3]:
        """Compact 3-step work platform at z=az+2 giving clear downward LOS for upper layers (y=ay+1..ay+3)."""
        ax, ay, az = anchor
        # Tier 1 (feet at ay, eye at ay+1.62 > ay+1): reaches both left (ax-1) and right (ax+2) pillars
        tier1 = [
            (ax - 1, ay - 1, az + 2),
            (ax, ay - 1, az + 2),
            (ax + 1, ay - 1, az + 2),
        ]
        if target_y is not None and target_y <= ay + 1:
            return tier1
        # Tier 2 (feet at ay+1, eye at ay+2.62 > ay+2)
        tier2 = [
            (ax, ay, az + 2),
            (ax + 1, ay, az + 2),
        ]
        if target_y is not None and target_y <= ay + 2:
            return tier1 + tier2
        # Tier 3 (feet at ay+2, eye at ay+3.62 > top bar ay+3)
        tier3 = [
            (ax + 1, ay + 1, az + 2),
        ]
        return tier1 + tier2 + tier3

    def _select_portal_anchor(self, bb: Blackboard, lava_sources: List[Coord3]) -> Coord3:
        """Pick a solid, dry surface anchor near the lava pool (never directly on top of lava)."""
        lx, ly, lz = lava_sources[0]
        lava_set: Set[Coord3] = set(lava_sources)
        candidates: List[Tuple[int, int, int]] = [
            (lx - 4, ly, lz),
            (lx + 4, ly, lz),
            (lx, ly, lz - 4),
            (lx, ly, lz + 4),
            (lx - 3, ly, lz),
            (lx + 3, ly, lz),
            (lx - 3, ly, lz - 3),
            (lx + 3, ly, lz + 3),
        ]
        probe: List[Coord3] = []
        for cx, cy, cz in candidates:
            for dx in (0, 1):
                for dz in (0, 1, 2):
                    for dy in (-1, 0, 1, 2):
                        probe.append((cx + dx, cy + dy, cz + dz))
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=5.0)

        best: Optional[Tuple[float, Coord3]] = None
        for cx, cy, cz in candidates:
            # Find ground surface y around cy
            ground_y = cy
            for test_y in (cy + 1, cy, cy - 1):
                g0 = fetched.get((cx, test_y, cz))
                a0 = fetched.get((cx, test_y + 1, cz))
                if g0 is not None and g0.name not in NON_SOLID_BLOCKS and (a0 is None or a0.name in AIR_BLOCKS):
                    ground_y = test_y
                    break
            # Reject if bottom bar or quench floor sits on liquid
            bad = False
            solid_score = 0
            for dx in (0, 1):
                for dz in (0, 1, 2):
                    g = fetched.get((cx + dx, ground_y, cz + dz))
                    above = fetched.get((cx + dx, ground_y + 1, cz + dz))
                    if (cx + dx, ground_y, cz + dz) in lava_set or (g is not None and g.name in LIQUID_BLOCKS):
                        bad = True
                        break
                    if above is not None and above.name in LIQUID_BLOCKS:
                        bad = True
                        break
                    if g is not None and g.name not in NON_SOLID_BLOCKS:
                        solid_score += 1
                if bad:
                    break
            if bad:
                continue
            anchor = (cx, ground_y + 2, cz)
            score = solid_score * 10.0 - dist3(bb.pos, (cx + 0.5, float(ground_y + 1), cz + 2.5)) * 0.5
            if best is None or score > best[0]:
                best = (score, anchor)

        return best[1] if best is not None else (lx - 3, ly + 2, lz)

    def _find_solid_floor_air_spot(self, bb: Blackboard, anchor: Coord3) -> Optional[Coord3]:
        ax, ay, az = anchor
        reserved = set(self._frame_cells(anchor)) | set(self._interior_cells(anchor)) | set(self._work_stair_cells(anchor))
        ground_y = ay - 1
        candidates = [
            (ax + dx, ground_y, az + dz)
            for dx in (-3, -2, -1, 0, 1, 2, 3)
            for dz in (3, 2, -2, -3)
            if (ax + dx, ground_y, az + dz) not in reserved
        ]
        probe: List[Coord3] = []
        for c in candidates:
            probe.append(c)
            probe.append((c[0], c[1] - 1, c[2]))
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe)
        for c in candidates:
            ent = fetched.get(c)
            below = fetched.get((c[0], c[1] - 1, c[2]))
            if ent is not None and ent.name in AIR_BLOCKS and below is not None and below.name not in NON_SOLID_BLOCKS:
                return c
        return None

    def _lowest_unfilled_in_column(self, bb: Blackboard, target: Coord3, base_y: int) -> Coord3:
        """Walk downward from target to base_y until a cell with an orthogonal solid neighbor is found (no floating blocks)."""
        mx, my, mz = target
        ys = list(range(my, base_y - 1, -1))
        probe: List[Coord3] = []
        for y in ys:
            probe.extend([
                (mx, y, mz),
                (mx, y - 1, mz), (mx + 1, y, mz), (mx - 1, y, mz),
                (mx, y, mz + 1), (mx, y, mz - 1), (mx, y + 1, mz),
            ])
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)
        for y in ys:
            ent = fetched.get((mx, y, mz))
            if ent is not None and ent.name not in NON_SOLID_BLOCKS:
                continue
            adj = (
                (mx, y - 1, mz), (mx + 1, y, mz), (mx - 1, y, mz),
                (mx, y, mz + 1), (mx, y, mz - 1), (mx, y + 1, mz),
            )
            if any((fetched.get(a) is not None and fetched[a].name not in NON_SOLID_BLOCKS) for a in adj):
                return (mx, y, mz)
        return (mx, base_y, mz)

    def _next_mold_cell(self, bb: Blackboard, anchor: Coord3, target_cell: Coord3, quench_cell: Coord3) -> Optional[Coord3]:
        """Return ONE missing cobblestone side-mold containment block for upper layers (y>=ay+1) or uneven drop-offs."""
        tx, ty, tz = target_cell
        _ax, ay, _az = anchor
        qx, _qy, qz = quench_cell
        qdz = qz - tz
        # 3 non-quench horizontal containment sides around target_cell: back (opposite quench), left, right
        back_cell = (tx, ty, tz - qdz) if qdz != 0 else (tx - (qx - tx), ty, tz)
        if qdz != 0:
            side_cells: List[Coord3] = [back_cell, (tx - 1, ty, tz), (tx + 1, ty, tz)]
        else:
            side_cells = [back_cell, (tx, ty, tz - 1), (tx, ty, tz + 1)]

        # Also check quench_cell support below so water never pours into a pit
        quench_support = (quench_cell[0], quench_cell[1] - 1, quench_cell[2])

        probe: List[Coord3] = [quench_support]
        for s in side_cells:
            probe.append(s)
            probe.append((s[0], s[1] - 1, s[2]))
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)

        # 1) Ensure solid floor under quench_cell first
        qsup = fetched.get(quench_support)
        if qsup is None or qsup.name in NON_SOLID_BLOCKS:
            return self._lowest_unfilled_in_column(bb, quench_support, ay - 1)

        # 2) Ensure containment walls on all upper layers (y >= ay+1) OR anywhere a side has a drop-off on uneven terrain!
        is_upper = ty >= ay + 1
        frame_set = set(self._frame_cells(anchor))
        for s in side_cells:
            if s == quench_cell or s in frame_set:
                continue
            sent = fetched.get(s)
            if sent is not None and sent.name not in NON_SOLID_BLOCKS:
                continue  # already solid (obsidian, cobblestone, terrain)
            s_below = fetched.get((s[0], s[1] - 1, s[2]))
            has_dropoff = s_below is None or s_below.name in NON_SOLID_BLOCKS
            if is_upper or has_dropoff:
                return self._lowest_unfilled_in_column(bb, s, ay - 1)

        return None

    def _pick_quench_cell(self, bb: Blackboard, target: Coord3) -> Tuple[Optional[Coord3], Optional[Coord3]]:
        """Choose an adjacent air cell (preferring player-facing +Z outside frame plane) with clear pour LOS."""
        tx, ty, tz = target
        reserved = set(self._frame_cells(self._portal_anchor)) if self._portal_anchor else set()
        candidates: List[Coord3] = [
            c for c in (
                (tx, ty, tz + 1),
                (tx, ty, tz - 1),
                (tx - 1, ty, tz),
                (tx + 1, ty, tz),
            )
            if c not in reserved
        ]
        # Prefer +Z (player/stair side) first, then closest to player
        candidates.sort(key=lambda c: (0 if c == (tx, ty, tz + 1) else 1, dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5))))
        probe: List[Coord3] = []
        for c in candidates:
            probe.append(c)
            probe.append((c[0], c[1] - 1, c[2]))
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)

        # First pass: adjacent non-solid cell that ALREADY has solid support below AND clear pour LOS
        for c in candidates:
            ent = fetched.get(c)
            below = fetched.get((c[0], c[1] - 1, c[2]))
            if ent is not None and ent.name in (AIR_BLOCKS | {"lava"}) and below is not None and below.name not in NON_SOLID_BLOCKS:
                plan = self.ctx.gateway.plan_liquid_interaction(bb.dimension, c, bb.pos, mode="pour")
                if plan.status in {"READY", "CLEAR_OCCLUDER"}:
                    return c, None
        # Second pass: adjacent cell where we can place cobblestone support below it
        for c in candidates:
            ent = fetched.get(c)
            below = fetched.get((c[0], c[1] - 1, c[2]))
            if ent is not None and ent.name in (AIR_BLOCKS | {"lava"}) and (below is None or below.name in NON_SOLID_BLOCKS):
                return c, (c[0], c[1] - 1, c[2])
        return None, None

    def _find_nearest_lava_sources(self, bb: Blackboard, radius: int = 64) -> List[Coord3]:
        rows = self.ctx.gateway.scan_blocks(
            bb.dimension,
            ["lava"],
            radius=radius,
            limit=32,
            properties={"level": 0},
        )
        out: List[Coord3] = []
        for r in rows:
            pos = r.get("pos")
            props = r.get("properties") or {}
            if isinstance(pos, list) and len(pos) == 3 and str(props.get("level", "0")) == "0":
                out.append((int(pos[0]), int(pos[1]), int(pos[2])))
        out.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)))
        return out

    def _place_block_safe(self, bb: Blackboard, anchor: Coord3, cell: Coord3, *, allow_dirt: bool = False) -> str:
        """Place cobblestone (or dirt for non-lava work stair if requested), acquiring cobblestone batch if empty."""
        item = "cobblestone"
        if bb.inventory_counts.get("cobblestone", 0) < 1:
            if allow_dirt and bb.inventory_counts.get("dirt", 0) >= 1:
                item = "dirt"
            else:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "cobblestone", 16))
        fx, fy, fz = block_coord(bb.pos)
        if cell in {(fx, fy, fz), (fx, fy + 1, fz)}:
            out = self._nav.step_to(bb, (cell[0] + 0.5, float(fy), cell[2] + 2.5), tolerance=0.5, xz_only=True)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
        out = self._place.place_at(bb, item, cell)
        return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

    def _place_cobble_safe(self, bb: Blackboard, anchor: Coord3, cell: Coord3) -> str:
        return self._place_block_safe(bb, anchor, cell, allow_dirt=False)

    def on_tick(self, bb: Blackboard) -> str:
        if bb.dimension == "the_nether":
            return "COMPLETED"

        # If currently running a cobblestone batch acquisition child, finish the batch cleanly and clear it once done!
        if self._active_child is not None and self._active_child.name.startswith("acquire:cobblestone"):
            if self._active_child.is_finished(bb):
                self._clear_child(bb)
            else:
                return self.run_child(bb, self._active_child)

        # 1) If a lit nether_portal already exists nearby -> walk straight into it!
        portals = self.ctx.gateway.scan_blocks(bb.dimension, ["nether_portal"], radius=32, limit=4)
        if portals:
            ppos = portals[0]["pos"]
            out = self._nav.step_to(bb, (ppos[0] + 0.5, float(ppos[1]), ppos[2] + 0.5), tolerance=0.5)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # 2) Ensure speedrun portal kit: iron_pickaxe, flint_and_steel, water/empty bucket
        inv = bb.inventory_counts
        if inv.get("iron_pickaxe", 0) < 1:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "iron_pickaxe", 1))
        if inv.get("flint_and_steel", 0) < 1:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "flint_and_steel", 1))
        has_any_bucket = (inv.get("water_bucket", 0) + inv.get("lava_bucket", 0) + inv.get("bucket", 0)) >= 1
        if not has_any_bucket:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "water_bucket", 1))
        if inv.get("water_bucket", 0) < 1 and inv.get("lava_bucket", 0) < 1 and self._portal_anchor is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "water_bucket", 1))

        # 3) Locate stationary lava pool within 64m (properties={"level": 0} filters out flowing lava!)
        lava_sources = self._find_nearest_lava_sources(bb, radius=64)
        if not lava_sources and self._portal_anchor is None:
            return self.run_child(
                bb,
                ExploreUntilTask(
                    self.ctx,
                    lambda _bb, gw: bool(
                        gw.scan_blocks(_bb.dimension, ["lava"], radius=48, limit=4, properties={"level": 0})
                    ),
                    name="find_lava_pool",
                ),
            )

        # 4) Anchor portal site on solid dry ground beside the lava pool
        if self._portal_anchor is None and lava_sources:
            self._portal_anchor = self._select_portal_anchor(bb, lava_sources)
            if self.ctx.gateway.db is not None:
                self.ctx.gateway.db.save_landmark(bb.dimension, "lava_pool", "portal_site", self._portal_anchor)

        assert self._portal_anchor is not None
        anchor = self._portal_anchor
        # Protect portal frame, mold walls, and work stair from Pathfinder3D micro-digging!
        ax, ay, az = anchor
        protected: Set[Coord3] = set(self._frame_cells(anchor)) | set(self._work_stair_cells(anchor))
        for dx in range(-2, 4):
            for dy in range(-1, 4):
                for dz in (-1, 0, 1):
                    protected.add((ax + dx, ay + dy, az + dz))
        bb.set("protected_cells", protected, writer=self.name)
        site_stand = (anchor[0] + 0.5, float(anchor[1] - 1), anchor[2] + 2.5)
        if dist3(bb.pos, site_stand) > 7.0:
            out = self._nav.step_to(bb, site_stand, tolerance=3.0)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # 5) Inspect all 10 portal obsidian cells + 6 interior cells in one batch (freshness <= 3s!)
        frame = self._frame_cells(anchor)
        interior = self._interior_cells(anchor)
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, frame + interior, max_age=3.0)
        missing_frame = [c for c in frame if (fetched.get(c) is None or fetched[c].name != "obsidian")]

        if not missing_frame:
            # Clear any temporary mold/scaffold blocks left inside the 2x3 portal interior before lighting!
            for ic in interior:
                ient = fetched.get(ic)
                if ient is not None and ient.name not in (AIR_BLOCKS | {"fire", "nether_portal"}):
                    out = self._dig.dig_block(bb, ic, collect=True)
                    return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
            # Frame complete & interior clear! Ignite bottom obsidian with flint_and_steel!
            out = self._fluid.ignite(bb, (anchor[0], anchor[1] - 1, anchor[2]))
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # 6) Cast next missing obsidian frame cell:
        target_cell = missing_frame[0]
        if inv.get("obsidian", 0) > 0:
            out = self._place.place_at(bb, "obsidian", target_cell)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        cur_ent = fetched.get(target_cell)
        # 6a) If target_cell holds uneven terrain or a temporary cobblestone mold block from an adjacent cell, dig it!
        if cur_ent is not None and cur_ent.name not in (AIR_BLOCKS | {"lava", "water", "obsidian"}):
            out = self._dig.dig_block(bb, target_cell, collect=True)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # 6b) If target_cell already has stationary lava -> quench via adjacent quench_cell!
        if cur_ent is not None and cur_ent.name == "lava":
            if inv.get("water_bucket", 0) < 1:
                if self._parked_water_pos is not None and inv.get("bucket", 0) > 0:
                    pw = self.ctx.gateway.get_block(bb.dimension, self._parked_water_pos, max_age=3.0)
                    if pw is not None and pw.name == "water" and str(pw.properties.get("level", "0")) == "0":
                        out = self._fluid.pickup_liquid(bb, self._parked_water_pos)
                        if out.status in {"OK", "PROGRESSED"}:
                            if out.status == "OK":
                                self._parked_water_pos = None
                            return "RUNNING"
                        if out.status == "UNKNOWN":
                            return "UNKNOWN"
                        self._parked_water_pos = None
                        return "RUNNING"
                    else:
                        self._parked_water_pos = None
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "water_bucket", 1))

            quench_cell, need_support = self._pick_quench_cell(bb, target_cell)
            if quench_cell is None:
                bb.last_failure_reason = "no_adjacent_quench_cell"
                return "NEEDS_AI"
            if need_support is not None:
                sup_to_place = self._lowest_unfilled_in_column(bb, need_support, anchor[1] - 1)
                return self._place_cobble_safe(bb, anchor, sup_to_place)

            out = self._fluid.pour_liquid(bb, quench_cell, bucket_item="water_bucket")
            if out.status == "OK":
                self._parked_water_pos = quench_cell
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # 6c) Ensure stepped cobblestone work platform exists before casting upper layers (y >= ay+1)
        if target_cell[1] >= anchor[1] + 1:
            stair_cells = self._work_stair_cells(anchor, target_y=target_cell[1])
            stair_fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, stair_cells, max_age=5.0)
            for sc in stair_cells:
                se = stair_fetched.get(sc)
                if se is None or se.name in NON_SOLID_BLOCKS:
                    return self._place_block_safe(bb, anchor, self._lowest_unfilled_in_column(bb, sc, anchor[1] - 1), allow_dirt=True)

        # 6d) Ensure solid support directly below target_cell (for bottom frame or upper layers)
        support_cell = (target_cell[0], target_cell[1] - 1, target_cell[2])
        sup = self.ctx.gateway.get_block(bb.dimension, support_cell, allow_fetch=True)
        if sup is None or sup.name in NON_SOLID_BLOCKS:
            return self._place_cobble_safe(bb, anchor, self._lowest_unfilled_in_column(bb, support_cell, anchor[1] - 1))

        # 6e) Ensure cobblestone side-mold containment walls (y >= ay+1 or uneven drop-offs) before pouring lava!
        preferred_quench = (target_cell[0], target_cell[1], target_cell[2] + 1)
        mold_cell = self._next_mold_cell(bb, anchor, target_cell, preferred_quench)
        if mold_cell is not None:
            return self._place_cobble_safe(bb, anchor, mold_cell)

        # 6f) Fetch lava_bucket (parking water_bucket first if needed) and pour into the contained mold pocket!
        if inv.get("lava_bucket", 0) < 1:
            if inv.get("bucket", 0) < 1:
                if inv.get("water_bucket", 0) > 0:
                    water_spot = self._find_solid_floor_air_spot(bb, anchor)
                    if water_spot is None:
                        bb.last_failure_reason = "no_solid_spot_to_park_water"
                        return "NEEDS_AI"
                    out = self._fluid.pour_liquid(bb, water_spot, bucket_item="water_bucket")
                    if out.status == "OK":
                        self._parked_water_pos = water_spot
                    return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "bucket", 1))
            if not lava_sources:
                bb.last_failure_reason = "lava_pool_exhausted"
                return "NEEDS_AI"
            out = self._fluid.pickup_liquid(bb, lava_sources[0])
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        out = self._fluid.pour_liquid(bb, target_cell, bucket_item="lava_bucket")
        return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

    def stop(self, bb: Blackboard) -> None:
        bb.set("protected_cells", None, writer=self.name)
        super().stop(bb)
