"""L3b EnterNetherTask: stateless recursive speedrun portal pipeline (<= 1 write/tick).

Pipeline evaluated from scratch on every macro-tick:
1. If dimension == 'the_nether' -> COMPLETED.
2. If a lit `nether_portal` exists nearby -> navigate into it!
3. Ensure prerequisites: `iron_pickaxe`, `water_bucket` (or `bucket` near water), `flint_and_steel`, >= 10 `cobblestone`.
4. Locate surface/exposed `lava` pool (scan radius 64, or `ExploreUntilTask`).
5. Multi-hop navigate to lava pool edge (NO 32m cap!).
6. Idempotently check 4x5 portal obsidian frame cells next to the lava pool:
   - For each missing frame obsidian cell: if holding `lava_bucket`, pour lava & cast obsidian with `water_bucket`;
     if holding `bucket`, scoop lava from pool; otherwise place mold/water or cast!
7. Once all 10 frame obsidian blocks exist -> ignite interior with `flint_and_steel`!
8. Step into `nether_portal` -> cross into `the_nether`!
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import AIR_BLOCKS, Coord3, NON_SOLID_BLOCKS, dist3
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
    """Stateless self-healing Nether Portal cast, ignite, and entry task."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue) -> None:
        super().__init__("enter_nether")
        self.ctx = ctx
        self.catalogue = catalogue
        self._nav = NavigatePrimitive(ctx)
        self._dig = DigCollectPrimitive(ctx)
        self._fluid = FluidPrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._portal_anchor: Optional[Coord3] = None

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

    def _find_solid_floor_air_spot(self, bb: Blackboard, anchor: Coord3) -> Optional[Coord3]:
        ax, ay, az = anchor
        reserved = set(self._frame_cells(anchor)) | set(self._interior_cells(anchor))
        ground_y = ay - 1
        candidates = [
            (ax + dx, ground_y, az + dz)
            for dx in (-3, -2, -1, 0, 1, 2, 3)
            for dz in (-3, -2, 2, 3)
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

    def _find_nearest_lava_sources(self, bb: Blackboard, radius: int = 64) -> List[Coord3]:
        rows = self.ctx.gateway.scan_blocks(bb.dimension, ["lava"], radius=radius, limit=32)
        out: List[Coord3] = []
        for r in rows:
            pos = r.get("pos")
            props = r.get("properties") or {}
            if isinstance(pos, list) and len(pos) == 3 and str(props.get("level", "0")) == "0":
                out.append((int(pos[0]), int(pos[1]), int(pos[2])))
        out.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)))
        return out

    def on_tick(self, bb: Blackboard) -> str:
        if bb.dimension == "the_nether":
            return "COMPLETED"

        # 1) If a lit nether_portal already exists nearby -> walk straight into it!
        portals = self.ctx.gateway.scan_blocks(bb.dimension, ["nether_portal"], radius=32, limit=4)
        if portals:
            ppos = portals[0]["pos"]
            out = self._nav.step_to(bb, (ppos[0] + 0.5, float(ppos[1]), ppos[2] + 0.5), tolerance=0.5)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # 2) Ensure speedrun portal kit: iron_pickaxe, flint_and_steel, water/empty bucket, cobblestone
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

        # 3) Locate lava pool within 64m (or explore until one is found)
        lava_sources = self._find_nearest_lava_sources(bb, radius=64)
        if not lava_sources and self._portal_anchor is None:
            return self.run_child(
                bb,
                ExploreUntilTask(
                    self.ctx,
                    lambda _bb, gw: bool(gw.scan_blocks(_bb.dimension, ["lava"], radius=48, limit=4)),
                    name="find_lava_pool",
                ),
            )

        # 4) Anchor portal site right beside the lava pool with bottom bar resting ON solid surface (ay = ly + 2)
        if self._portal_anchor is None and lava_sources:
            lx, ly, lz = lava_sources[0]
            self._portal_anchor = (lx - 3, ly + 2, lz)
            if self.ctx.gateway.db is not None:
                self.ctx.gateway.db.save_landmark(bb.dimension, "lava_pool", "portal_site", self._portal_anchor)

        assert self._portal_anchor is not None
        anchor = self._portal_anchor
        site_stand = (anchor[0] + 0.5, float(anchor[1] - 1), anchor[2] + 2.5)
        if dist3(bb.pos, site_stand) > 6.5:
            out = self._nav.step_to(bb, site_stand, tolerance=3.0)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # 5) Inspect all 10 portal obsidian cells + 6 interior cells in one batch
        frame = self._frame_cells(anchor)
        interior = self._interior_cells(anchor)
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, frame + interior)
        missing_frame = [c for c in frame if (fetched.get(c) is None or fetched[c].name != "obsidian")]

        if not missing_frame:
            # Clear any temporary scaffold blocks left inside the 2x3 portal interior before lighting!
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
        if cur_ent is not None and cur_ent.name == "lava":
            # Quench lava cell directly into obsidian with water_bucket!
            if inv.get("water_bucket", 0) < 1:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "water_bucket", 1))
            out = self._fluid.pour_liquid(bb, target_cell, bucket_item="water_bucket")
            if out.status == "OK":
                self.ctx.gateway.write_through_place(bb.dimension, target_cell, "obsidian")
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # Ensure solid support directly below target_cell (for bottom frame or temporary top-bar scaffold)
        support_cell = (target_cell[0], target_cell[1] - 1, target_cell[2])
        sup = self.ctx.gateway.get_block(bb.dimension, support_cell, allow_fetch=True)
        if sup is None or sup.name in NON_SOLID_BLOCKS:
            if inv.get("cobblestone", 0) < 1:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "cobblestone", 8))
            out = self._place.place_at(bb, "cobblestone", support_cell)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # Need lava_bucket -> if holding water_bucket, park it on a verified solid-floor air cell first
        if inv.get("lava_bucket", 0) < 1:
            if inv.get("bucket", 0) < 1:
                if inv.get("water_bucket", 0) > 0:
                    water_spot = self._find_solid_floor_air_spot(bb, anchor)
                    if water_spot is None:
                        bb.last_failure_reason = "no_solid_spot_to_park_water"
                        return "NEEDS_AI"
                    out = self._fluid.pour_liquid(bb, water_spot, bucket_item="water_bucket")
                    return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "bucket", 1))
            if not lava_sources:
                bb.last_failure_reason = "lava_pool_exhausted"
                return "NEEDS_AI"
            out = self._fluid.pickup_liquid(bb, lava_sources[0])
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        out = self._fluid.pour_liquid(bb, target_cell, bucket_item="lava_bucket")
        return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
