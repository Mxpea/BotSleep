"""L3b Stateless recursive AcquireItemTask and SmeltItemTask ('20 Questions' DAG solver)."""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, FUEL_ITEMS, PICKAXE_TIERS, PLANK_ITEMS, dist3
from v2.primitives.base import PrimitiveContext
from v2.primitives.container import ContainerPrimitive
from v2.primitives.craft import CraftPrimitive
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.fluid import FluidPrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.explore import ExploreUntilTask


def _has_pickaxe_tier(counts: Dict[str, int], required: str) -> bool:
    if required not in PICKAXE_TIERS:
        return True
    req_idx = PICKAXE_TIERS.index(required)
    return any(counts.get(tier, 0) > 0 for tier in PICKAXE_TIERS[: req_idx + 1])


class SmeltItemTask(Task):
    """Stateless multi-furnace batch smelting task (supports 1..max_furnaces parallel furnaces)."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        output_item: str,
        count: int,
        *,
        max_furnaces: int = 2,
    ) -> None:
        super().__init__(f"smelt:{output_item}x{count}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.output_item = output_item
        self.count = max(1, int(count))
        self.max_furnaces = max(1, int(max_furnaces))
        self._container = ContainerPrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.output_item, 0) >= self.count

    def _pick_fuel(self, counts: Dict[str, int]) -> Optional[str]:
        for fuel in FUEL_ITEMS:
            if counts.get(fuel, 0) > 0:
                return fuel
        return None

    def on_tick(self, bb: Blackboard) -> str:
        smelt = self.catalogue.smelting.get(self.output_item)
        if smelt is None:
            bb.last_failure_reason = f"no_smelt_rule:{self.output_item}"
            return "FAILED_LOCAL"

        have = bb.inventory_counts.get(self.output_item, 0)
        missing = max(0, self.count - have)
        if missing == 0:
            return "COMPLETED"

        # 1) Check placed furnaces within 16m
        furnaces = self.ctx.gateway.scan_blocks(bb.dimension, ["furnace"], radius=16, limit=6)
        if not furnaces:
            if bb.inventory_counts.get("furnace", 0) > 0:
                out = self._place.place_near_player(bb, "furnace")
                return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "furnace", 1))

        # 2) Ensure enough raw input item
        raw_have = bb.inventory_counts.get(smelt.input_item, 0)
        if raw_have < missing:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, smelt.input_item, missing))

        # 3) Ensure fuel
        fuel = self._pick_fuel(bb.inventory_counts)
        if fuel is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "coal", max(1, math.ceil(missing / 8))))

        # 4) Smelt batch in nearest furnace (1 write)
        furnaces.sort(key=lambda r: float(r.get("dist", 999.0)))
        fpos = tuple(int(v) for v in furnaces[0]["pos"])
        out = self._container.smelt_batch_step(
            bb,
            fpos,
            input_item=smelt.input_item,
            input_count=missing,
            fuel_item=fuel,
            fuel_count=max(1, math.ceil(missing / 8)),
            harvest_output=True,
        )
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"
        return out.status


class AcquireItemTask(Task):
    """AltoClef '20 Questions' recursive solver over 886 recipes, smelting, fluids, and block gathering."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        item: str,
        count: int = 1,
        *,
        _depth: int = 0,
    ) -> None:
        super().__init__(f"acquire:{item}x{count}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.item = item
        self.count = max(1, int(count))
        self._depth = _depth
        self._dig = DigCollectPrimitive(ctx)
        self._craft = CraftPrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._fluid = FluidPrimitive(ctx)

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.item, 0) >= self.count

    def _acquire_liquid_bucket(self, bb: Blackboard, liquid_name: str) -> str:
        if bb.inventory_counts.get("bucket", 0) < 1:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "bucket", 1, _depth=self._depth + 1))
        rows = self.ctx.gateway.scan_blocks(bb.dimension, [liquid_name], radius=48, limit=24)
        sources: List[Coord3] = []
        for r in rows:
            pos = r.get("pos")
            props = r.get("properties") or {}
            if isinstance(pos, list) and len(pos) == 3 and str(props.get("level", "0")) == "0":
                c = (int(pos[0]), int(pos[1]), int(pos[2]))
                if not self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "fluid_pickup"):
                    sources.append(c)
        if not sources:
            return self.run_child(
                bb,
                ExploreUntilTask(
                    self.ctx,
                    lambda _bb, gw: bool(gw.scan_blocks(_bb.dimension, [liquid_name], radius=32, limit=4)),
                    name=f"find_{liquid_name}",
                ),
            )
        sources.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)))
        out = self._fluid.pickup_liquid(bb, sources[0])
        return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

    def on_tick(self, bb: Blackboard) -> str:
        if self._depth > 10:
            bb.last_failure_reason = f"recursion_depth_exceeded:{self.item}"
            return "NEEDS_AI"

        have = bb.inventory_counts.get(self.item, 0)
        if have >= self.count:
            return "COMPLETED"
        missing = self.count - have

        # 1) Special fluid buckets
        if self.item == "water_bucket":
            return self._acquire_liquid_bucket(bb, "water")
        if self.item == "lava_bucket":
            return self._acquire_liquid_bucket(bb, "lava")

        # 2) Smeltable items (iron_ingot, gold_ingot, glass, cooked food, ...)
        if self.item in self.catalogue.smelting:
            return self.run_child(bb, SmeltItemTask(self.ctx, self.catalogue, self.item, self.count))

        # 3) Direct world gathering (logs, stone->cobblestone, coal, raw_iron, gravel->flint, ...)
        gather = self.catalogue.gathering.get(self.item)
        if gather is not None:
            if gather.min_pickaxe and not _has_pickaxe_tier(bb.inventory_counts, gather.min_pickaxe):
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, gather.min_pickaxe, 1, _depth=self._depth + 1))
            out = self._dig.dig_nearest_of_types(bb, gather.block_types, radius=32, collect=True)
            if out.status in {"OK", "PROGRESSED"}:
                return "RUNNING"
            if out.reason == "no_exposed_block_found":
                return self.run_child(
                    bb,
                    ExploreUntilTask(
                        self.ctx,
                        lambda _bb, gw: bool(gw.scan_blocks(_bb.dimension, list(gather.block_types), radius=24, limit=4)),
                        name=f"find_{self.item}",
                    ),
                )
            bb.last_failure_reason = f"gather_{self.item}:{out.reason}"
            return out.status

        # 4) Craftable items (886-item TaskCatalogue!)
        rec = self.catalogue.get_best_recipe(self.item, bb.inventory_counts)
        if rec is None:
            bb.last_failure_reason = f"unknown_production_route:{self.item}"
            return "NEEDS_AI"

        batches = math.ceil(missing / rec.output_count)

        # Ensure 3x3 crafting table exists within 16m or in inventory (NEVER duplicates existing table!)
        if rec.needs_table:
            if self._craft.find_nearby_table(bb) is None:
                if bb.inventory_counts.get("crafting_table", 0) < 1:
                    return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "crafting_table", 1, _depth=self._depth + 1))
                out = self._place.place_near_player(bb, "crafting_table")
                return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status

        # Check all recipe ingredients recursively
        for ing, per_batch in rec.ingredients.items():
            req = per_batch * batches
            if bb.inventory_counts.get(ing, 0) < req:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, ing, req, _depth=self._depth + 1))

        # All prerequisites met -> execute ONE craft action
        out = self._craft.craft(bb, self.item, missing, needs_table=rec.needs_table)
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"
        bb.last_failure_reason = f"craft_{self.item}:{out.reason}"
        return out.status
