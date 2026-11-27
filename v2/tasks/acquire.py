"""L3b Stateless recursive AcquireItemTask and real-time SmeltItemTask ('20 Questions' DAG solver)."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, FUEL_ITEMS, PICKAXE_TIERS, dist3
from v2.primitives.base import PrimitiveContext
from v2.primitives.container import ContainerPrimitive
from v2.primitives.craft import CraftPrimitive
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.fluid import FluidPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.base import Task
from v2.tasks.catalogue import OVERWORLD_LOG_TYPES, TaskCatalogue
from v2.tasks.explore import ExploreUntilTask
from v2.tasks.surface import (
    ReturnToSurfaceTask,
    is_underground,
    load_anchor_landmark,
    save_anchor_landmark,
)


_RAW_FOOD_INPUTS = frozenset({"beef", "porkchop", "mutton", "chicken", "potato"})
_BATCH_SMELT_INPUTS = frozenset({"raw_iron", "raw_gold"}) | _RAW_FOOD_INPUTS


def _has_pickaxe_tier(counts: Dict[str, int], required: str) -> bool:
    if required not in PICKAXE_TIERS:
        return True
    req_idx = PICKAXE_TIERS.index(required)
    return any(counts.get(tier, 0) > 0 for tier in PICKAXE_TIERS[: req_idx + 1])


def _section_count(window: Dict[str, Any], section: str, item_name: Optional[str] = None) -> int:
    sections = window.get("sections") if isinstance(window.get("sections"), dict) else {}
    rows = sections.get(section)
    if not isinstance(rows, list):
        return 0
    total = 0
    for row in rows:
        if isinstance(row, dict):
            if item_name is None or row.get("name") == item_name:
                total += int(row.get("count", 0) or 0)
    return total


class SmeltItemTask(Task):
    """Stateless real-time smelting task compatible with 10s/item live Minecraft furnaces.

    Lifecycle per tick (<= 1 write):
    1. Ensure a placed furnace exists within 16m (or place/craft one).
    2. Inspect furnace window via `window/view` (or reuse fresh window snapshot from last `window/act`).
    3. If `output` has smelted items -> harvest with `{op: take, from: output}`.
    4. If `input` already holds enough raw ore and furnace is burning/fueled -> wait cleanly without re-mining.
    5. Otherwise gather any remaining missing raw ore / fuel, then load with `{op: put, to: input|fuel}`.
    """

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        output_item: str,
        count: int,
        *,
        max_furnaces: int = 2,
        poll_interval_seconds: float = 0.0,
    ) -> None:
        super().__init__(f"smelt:{output_item}x{count}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.output_item = output_item
        self.count = max(1, int(count))
        self.max_furnaces = max(1, int(max_furnaces))
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._container = ContainerPrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._last_window: Optional[Dict[str, Any]] = None
        self._last_window_pos: Optional[Coord3] = None
        self._last_window_at: float = 0.0
        self._next_inspect_at: float = 0.0
        self._placed_furnace_pos: Optional[Coord3] = None
        self._awaiting_placed_furnace: bool = False
        self._furnace_pickup_attempted: bool = False
        self._batch_mine_active: bool = False
        self._batch_mine_done: bool = False
        self._furnace_landmark_exhausted: bool = False
        self._furnace_nav_stalls: int = 0
        self._furnace_nav_steps: int = 0
        self._furnace_nav_last_pos: Optional[Tuple[float, float, float]] = None
        self._furnace_has_active_batch: bool = False
        self._initial_near_check_done: bool = False

    def _should_pickup_placed_furnace(self, bb: Blackboard) -> bool:
        if self._placed_furnace_pos is None or self._furnace_pickup_attempted:
            return False
        if bb.inventory_counts.get("furnace", 0) != 0:
            return False
        if not any(bb.inventory_counts.get(p, 0) > 0 for p in PICKAXE_TIERS):
            return False
        p = self._placed_furnace_pos
        if dist3(bb.pos, (p[0] + 0.5, p[1] + 0.5, p[2] + 0.5)) > 5.0:
            return False
        win = self._last_window if self._last_window_pos == p else None
        if win is None or _section_count(win, "input") != 0 or _section_count(win, "output") != 0:
            return False
        ent = self.ctx.gateway.peek_block(bb.dimension, p)
        if ent is not None and ent.name != "furnace":
            return False
        return True

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.output_item, 0) >= self.count and not self._should_pickup_placed_furnace(bb)

    def _pick_fuel(self, counts: Dict[str, int]) -> Optional[str]:
        for fuel in FUEL_ITEMS:
            if counts.get(fuel, 0) > 0:
                return fuel
        return None

    def _extract_window(self, details: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if isinstance(details.get("window"), dict):
            return details["window"]
        if isinstance(details.get("sections"), dict):
            return details
        return None

    def on_tick(self, bb: Blackboard) -> str:
        smelt = self.catalogue.smelting.get(self.output_item)
        if smelt is None:
            bb.last_failure_reason = f"no_smelt_rule:{self.output_item}"
            return "FAILED_LOCAL"

        have = bb.inventory_counts.get(self.output_item, 0)
        missing = max(0, self.count - have)
        if missing == 0:
            if self._should_pickup_placed_furnace(bb):
                p = self._placed_furnace_pos
                assert p is not None
                self._furnace_pickup_attempted = True
                self._placed_furnace_pos = None
                ent = self.ctx.gateway.get_block(bb.dimension, p, allow_fetch=True)
                if ent is not None and ent.name == "furnace":
                    out = self.ctx.execute_write(
                        bb,
                        "dig",
                        {"pos": list(p), "collect": True},
                        lambda _p=p: self.ctx.client.dig(list(_p), collect=True),
                        unknown_center=p,
                        record_unreachable_action="dig",
                    )
                    if out.status == "OK":
                        self.ctx.gateway.write_through_dig(bb.dimension, p)
                        if bb.get("last_furnace_anchor") == p:
                            bb.set("last_furnace_anchor", None, writer="smelt")
                        return "COMPLETED"
                    if out.status == "UNKNOWN":
                        return "UNKNOWN"
            return "COMPLETED"

        raw_have = bb.inventory_counts.get(smelt.input_item, 0)
        fuel_avail = self._pick_fuel(bb.inventory_counts)

        # 0) Inspect an immediately adjacent (<= 5m) furnace ONCE to catch an already-smelting batch
        if not self._initial_near_check_done and not self._furnace_has_active_batch and (raw_have < missing or fuel_avail is None):
            near_f = self.ctx.gateway.scan_blocks(bb.dimension, ["furnace"], radius=5, limit=2)
            near_coord: Optional[Coord3] = None
            for r in near_f:
                pos_r = r.get("pos")
                if isinstance(pos_r, list) and len(pos_r) == 3:
                    c = (int(pos_r[0]), int(pos_r[1]), int(pos_r[2]))
                    if dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)) <= 5.0:
                        near_coord = c
                        break
            if near_coord is None:
                self._initial_near_check_done = True
            else:
                view = self._container.inspect_window(bb, near_coord)
                if view.status == "OK":
                    self._initial_near_check_done = True
                    snap = self._extract_window(view.details)
                    self._last_window = snap
                    self._last_window_pos = near_coord
                    self._last_window_at = time.monotonic()
                    if snap is not None:
                        s_out = _section_count(snap, "output")
                        s_in = _section_count(snap, "input", smelt.input_item)
                        s_fuel = _section_count(snap, "fuel")
                        s_burn = bool((snap.get("info") if isinstance(snap.get("info"), dict) else {}).get("burning", False))
                        if s_out > 0 or (s_in > 0 and (s_burn or s_fuel > 0)):
                            self._furnace_has_active_batch = True
                    return "RUNNING"
                if view.status == "PROGRESSED":
                    return "RUNNING"
                self._initial_near_check_done = True
                if view.status == "UNKNOWN":
                    return "UNKNOWN"

        # 0.5) Ensure raw material & fuel BEFORE walking to or window/viewing any distant furnace!
        if not self._furnace_has_active_batch:
            if smelt.input_item in _RAW_FOOD_INPUTS:
                if raw_have == 0:
                    if have > 0:
                        return "COMPLETED"
                    bb.last_failure_reason = f"no_raw_food:{smelt.input_item}"
                    return "FAILED_LOCAL"
                if raw_have < missing:
                    self.count = have + raw_have
                    missing = raw_have
            batch_eligible = (
                self.output_item in {"iron_ingot", "gold_ingot"}
                and self.count >= 3
                and bb.health >= 12.0
                and not self._batch_mine_done
            )
            if raw_have < missing or (batch_eligible and self._batch_mine_active):
                if batch_eligible:
                    target_raw = min(10, max(6, missing + 3))
                    self._batch_mine_active = True
                    res = self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, smelt.input_item, target_raw))
                    if bb.inventory_counts.get(smelt.input_item, 0) >= target_raw or res in {"COMPLETED", "FAILED_LOCAL", "NEEDS_AI"}:
                        self._batch_mine_done = True
                        self._batch_mine_active = False
                        self._clear_child(bb)
                        if bb.inventory_counts.get(smelt.input_item, 0) >= missing:
                            return "RUNNING"
                    return "RUNNING" if res == "COMPLETED" else res
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, smelt.input_item, missing))
            if fuel_avail is None:
                coal_need = max(1, math.ceil(max(missing, min(raw_have, 16)) / 8))
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "coal", coal_need))

        # 1) Check placed furnaces within 16m (reuse 24m landmark or place/craft) ONLY once ready or active batch exists
        furnaces = self.ctx.gateway.scan_blocks(bb.dimension, ["furnace"], radius=16, limit=6)
        if not furnaces:
            if not self._furnace_landmark_exhausted:
                anchor = load_anchor_landmark(self.ctx.gateway.db, bb, "last_furnace_anchor", "furnace", writer="smelt")
                if (
                    anchor is not None
                    and dist3(bb.pos, (anchor[0] + 0.5, anchor[1] + 0.5, anchor[2] + 0.5)) <= 24.0
                    and not self.ctx.gateway.unreachable.is_blocked(bb.dimension, anchor, "furnace")
                ):
                    center = (anchor[0] + 0.5, anchor[1] + 0.5, anchor[2] + 0.5)
                    if dist3(bb.pos, center) <= 12.0:
                        self._furnace_landmark_exhausted = True
                    else:
                        if self._furnace_nav_last_pos is not None and dist3(self._furnace_nav_last_pos, bb.pos) < 0.35:
                            self._furnace_nav_stalls += 1
                        elif self._furnace_nav_last_pos is not None:
                            self._furnace_nav_stalls = 0
                        self._furnace_nav_last_pos = bb.pos
                        self._furnace_nav_steps += 1
                        if self._furnace_nav_stalls >= 4 or self._furnace_nav_steps > 12:
                            self._furnace_landmark_exhausted = True
                        else:
                            n_out = self._nav.step_to(bb, center, tolerance=3.5)
                            if n_out.status == "UNKNOWN":
                                return "UNKNOWN"
                            if n_out.status in {"OK", "PROGRESSED"}:
                                return "RUNNING"
                            if n_out.status == "FAILED_LOCAL":
                                self.ctx.gateway.unreachable.record_failure(bb.dimension, anchor, "furnace", n_out.reason)
                                self._furnace_landmark_exhausted = True
                            if bb.writes_this_tick >= 1:
                                return "RUNNING"
                else:
                    self._furnace_landmark_exhausted = True

            if bb.inventory_counts.get("furnace", 0) > 0:
                out = self._place.place_near_player(bb, "furnace")
                if out.status == "OK":
                    raw_pos = out.details.get("pos") if isinstance(out.details, dict) else None
                    if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3:
                        placed: Coord3 = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
                        self._placed_furnace_pos = placed
                        save_anchor_landmark(self.ctx.gateway.db, bb, "last_furnace_anchor", "furnace", placed, writer="smelt")
                    else:
                        self._awaiting_placed_furnace = True
                return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "furnace", 1))

        furnaces.sort(key=lambda r: float(r.get("dist", 999.0)))
        fpos: Coord3 = tuple(int(v) for v in furnaces[0]["pos"])  # type: ignore[assignment]
        if self._awaiting_placed_furnace and self._placed_furnace_pos is None:
            self._placed_furnace_pos = fpos
            self._awaiting_placed_furnace = False
        save_anchor_landmark(self.ctx.gateway.db, bb, "last_furnace_anchor", "furnace", fpos, writer="smelt")
        now = time.monotonic()

        # 2) Check cached furnace snapshot; if stale or unknown, inspect via window/view (1 action)
        win = self._last_window if self._last_window_pos == fpos else None
        if win is not None:
            out_cnt = _section_count(win, "output")
            in_cnt = _section_count(win, "input", smelt.input_item)
            fuel_cnt = _section_count(win, "fuel")
            info = win.get("info") if isinstance(win.get("info"), dict) else {}
            burning = bool(info.get("burning", False))

            # 2a) Output ready -> harvest immediately!
            if out_cnt > 0:
                res = self._container.harvest_furnace(bb, fpos)
                if res.status == "OK":
                    self._last_window = self._extract_window(res.details)
                    self._last_window_pos = fpos
                    self._last_window_at = time.monotonic()
                    return "RUNNING"
                if res.status == "PROGRESSED":
                    return "RUNNING"
                # If harvest reported no_output, force re-inspect next tick
                self._last_window = None
                return "RUNNING" if res.reason == "no_output" else res.status

            # 2b) Furnace is actively smelting our raw input -> wait until next poll, then re-inspect!
            if in_cnt > 0 and (burning or fuel_cnt > 0):
                if now < self._next_inspect_at:
                    bb.last_action_outcome = "smelt_waiting_furnace"
                    return "RUNNING"
                view = self._container.inspect_window(bb, fpos)
                if view.status == "OK":
                    self._last_window = self._extract_window(view.details)
                    self._last_window_pos = fpos
                    self._last_window_at = time.monotonic()
                    self._next_inspect_at = self._last_window_at + self.poll_interval_seconds
                    return "RUNNING"
                return "RUNNING" if view.status == "PROGRESSED" else view.status
        else:
            # First time seeing this furnace -> inspect before assuming it's empty!
            view = self._container.inspect_window(bb, fpos)
            if view.status == "OK":
                self._last_window = self._extract_window(view.details)
                self._last_window_pos = fpos
                self._last_window_at = time.monotonic()
                return "RUNNING"
            return "RUNNING" if view.status == "PROGRESSED" else view.status

        # 3) Furnace needs input and/or fuel
        in_cnt = _section_count(win, "input", smelt.input_item) if win else 0
        fuel_cnt = _section_count(win, "fuel") if win else 0
        info = (win.get("info") if isinstance(win.get("info"), dict) else {}) if win else {}
        burning = bool(info.get("burning", False))
        if win is not None and in_cnt == 0 and _section_count(win, "output") == 0:
            self._furnace_has_active_batch = False

        need_raw_more = max(0, missing - in_cnt)
        raw_have = bb.inventory_counts.get(smelt.input_item, 0)
        if need_raw_more > 0 and raw_have < need_raw_more:
            if smelt.input_item in _RAW_FOOD_INPUTS:
                if raw_have == 0:
                    if in_cnt > 0:
                        need_raw_more = 0
                    elif have > 0:
                        return "COMPLETED"
                    else:
                        bb.last_failure_reason = f"no_raw_food:{smelt.input_item}"
                        return "FAILED_LOCAL"
                else:
                    need_raw_more = raw_have
            else:
                self._furnace_has_active_batch = False
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, smelt.input_item, need_raw_more))

        need_fuel = not burning and fuel_cnt <= 0
        fuel = self._pick_fuel(bb.inventory_counts) if need_fuel else None
        if need_fuel and fuel is None:
            self._furnace_has_active_batch = False
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "coal", max(1, math.ceil(missing / 8))))

        if smelt.input_item in _BATCH_SMELT_INPUTS and raw_have > need_raw_more:
            if fuel in {"coal", "charcoal"}:
                max_by_fuel = max(1, bb.inventory_counts.get(fuel, 0)) * 8
                load_input_count = max(need_raw_more, min(raw_have, 16, max_by_fuel))
            else:
                load_input_count = min(raw_have, 8)
        else:
            load_input_count = need_raw_more

        if fuel is None:
            fuel_load_count = 0
        elif smelt.input_item in _BATCH_SMELT_INPUTS and raw_have > need_raw_more:
            fuel_load_count = min(
                bb.inventory_counts.get(fuel, 1),
                max(1, math.ceil(load_input_count / 8)) if fuel in {"coal", "charcoal", "coal_block", "blaze_rod"} else max(1, load_input_count),
            )
        else:
            fuel_load_count = max(1, math.ceil(missing / 8))

        load_out = self._container.load_furnace(
            bb,
            fpos,
            input_item=smelt.input_item if load_input_count > 0 else None,
            input_count=load_input_count,
            fuel_item=fuel,
            fuel_count=fuel_load_count,
        )
        if load_out.status == "OK":
            self._last_window = self._extract_window(load_out.details)
            self._last_window_pos = fpos
            self._last_window_at = time.monotonic()
            self._next_inspect_at = self._last_window_at + self.poll_interval_seconds
            self._furnace_has_active_batch = True
            return "RUNNING"
        return "RUNNING" if load_out.status == "PROGRESSED" else load_out.status

    def stop(self, bb: Blackboard) -> None:
        self._last_window = None
        self._last_window_pos = None
        self._placed_furnace_pos = None
        self._awaiting_placed_furnace = False
        self._furnace_pickup_attempted = False
        self._batch_mine_active = False
        self._batch_mine_done = False
        self._furnace_landmark_exhausted = False
        self._furnace_nav_stalls = 0
        self._furnace_nav_steps = 0
        self._furnace_nav_last_pos = None
        self._furnace_has_active_batch = False
        self._initial_near_check_done = False
        super().stop(bb)


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
        self._placed_table_pos: Optional[Coord3] = None
        self._table_pickup_attempted: bool = False

    def _should_pickup_placed_table(self, bb: Blackboard) -> bool:
        if self._depth != 0 or self._table_pickup_attempted or self._placed_table_pos is None:
            return False
        if bb.inventory_counts.get("crafting_table", 0) != 0:
            return False
        p = self._placed_table_pos
        if dist3(bb.pos, (p[0] + 0.5, p[1] + 0.5, p[2] + 0.5)) > 5.0:
            return False
        ent = self.ctx.gateway.peek_block(bb.dimension, p)
        if ent is not None and ent.name != "crafting_table":
            return False
        return True

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.item, 0) >= self.count and not self._should_pickup_placed_table(bb)

    def stop(self, bb: Blackboard) -> None:
        self._dig.clear_cache()
        self._placed_table_pos = None
        self._table_pickup_attempted = False
        super().stop(bb)

    def _acquire_liquid_bucket(self, bb: Blackboard, liquid_name: str) -> str:
        if bb.inventory_counts.get("bucket", 0) < 1:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "bucket", 1, _depth=self._depth + 1))
        if liquid_name == "water" and is_underground(bb, self.ctx.gateway):
            return self.run_child(bb, ReturnToSurfaceTask(self.ctx))
        rows = self.ctx.gateway.scan_blocks(
            bb.dimension,
            [liquid_name],
            radius=48,
            limit=24,
            properties={"level": 0},
        )
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
                    lambda _bb, gw: bool(
                        gw.scan_blocks(_bb.dimension, [liquid_name], radius=32, limit=4, properties={"level": 0})
                    ),
                    name=f"find_{liquid_name}",
                ),
            )
        sources.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)))
        out = self._fluid.pickup_liquid(bb, sources[0])
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"
        # sources[0] was blacklisted on FAILED_LOCAL -> try next candidate source on next tick if any remain!
        if out.status == "FAILED_LOCAL" and len(sources) > 1:
            return "RUNNING"
        return out.status

    def on_tick(self, bb: Blackboard) -> str:
        if self._depth > 10:
            bb.last_failure_reason = f"recursion_depth_exceeded:{self.item}"
            return "NEEDS_AI"

        have = bb.inventory_counts.get(self.item, 0)
        if have >= self.count:
            if self._should_pickup_placed_table(bb):
                p = self._placed_table_pos
                assert p is not None
                self._table_pickup_attempted = True
                self._placed_table_pos = None
                ent = self.ctx.gateway.get_block(bb.dimension, p, allow_fetch=True)
                if ent is not None and ent.name == "crafting_table":
                    out = self._craft.ctx.execute_write(
                        bb,
                        "dig",
                        {"pos": list(p), "collect": True},
                        lambda _p=p: self._craft.ctx.client.dig(list(_p), collect=True),
                        unknown_center=p,
                        record_unreachable_action="dig",
                    )
                    if out.status == "OK":
                        self.ctx.gateway.write_through_dig(bb.dimension, p)
                        return "COMPLETED"
                    if out.status == "UNKNOWN":
                        return "UNKNOWN"
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
            # Pure surface gathers (logs, sand, dirt) must return via shaft mouth first instead of pacing below trees!
            if gather.preferred_y is None and self.item not in {"flint", "gravel"} and is_underground(bb, self.ctx.gateway):
                return self.run_child(bb, ReturnToSurfaceTask(self.ctx))
            # Prioritize carried gravel place->dig loop for flint before hunting distant natural gravel!
            if self.item == "flint" and bb.inventory_counts.get("gravel", 0) >= 1:
                near_out = self._dig.dig_nearest_of_types(bb, ("gravel",), radius=5, collect=True, max_dy=2.0)
                if near_out.status in {"OK", "PROGRESSED"}:
                    self.local_failures = 0
                    return "RUNNING"
                if near_out.reason == "no_exposed_block_found":
                    place_out = self._place.place_near_player(bb, "gravel")
                    if place_out.status in {"OK", "PROGRESSED"}:
                        return "RUNNING"
            max_dy = 4.0 if gather.preferred_y is not None else None
            out = self._dig.dig_nearest_of_types(bb, gather.block_types, radius=32, collect=True, max_dy=max_dy)
            if out.status in {"OK", "PROGRESSED"}:
                self.local_failures = 0
                return "RUNNING"
            # If the nearest exposed block was an unreachable pocket, it has now been blacklisted -> retry up to 2x,
            # then fall through to MineResourceTask for underground-minable items!
            if out.status == "FAILED_LOCAL" and out.reason in {"no_3d_path", "path_not_found", "out_of_reach", "blacklisted_unreachable"}:
                self.local_failures += 1
                if self.local_failures < 2:
                    return "RUNNING"
                if gather.preferred_y is not None:
                    from v2.tasks.mining import MineResourceTask
                    return self.run_child(bb, MineResourceTask(self.ctx, self.catalogue, self.item, self.count))
                return "RUNNING"
            # Non-oak biome rescue: if looking for oak_log (or any *_log) and none exposed nearby,
            # harvest ANY nearby overworld log species so parent recipe re-scores to that wood species on next tick!
            if out.reason == "no_exposed_block_found" and self.item in OVERWORLD_LOG_TYPES:
                any_wood = self._dig.dig_nearest_of_types(bb, OVERWORLD_LOG_TYPES, radius=32, collect=True)
                if any_wood.status in {"OK", "PROGRESSED"}:
                    return "RUNNING"
                explore_types = list(OVERWORLD_LOG_TYPES)
            else:
                explore_types = list(gather.block_types)

            if out.reason == "no_exposed_block_found":
                # Inventory gravel -> place and re-dig in place to farm flint before wandering surface
                if self.item == "flint" and bb.inventory_counts.get("gravel", 0) >= 1:
                    place_out = self._place.place_near_player(bb, "gravel")
                    if place_out.status in {"OK", "PROGRESSED"}:
                        return "RUNNING"
                # For underground stone/ores, dig a safe staircase down instead of wandering grassy plains!
                if gather.preferred_y is not None:
                    from v2.tasks.mining import MineResourceTask
                    return self.run_child(bb, MineResourceTask(self.ctx, self.catalogue, self.item, self.count))
                return self.run_child(
                    bb,
                    ExploreUntilTask(
                        self.ctx,
                        lambda _bb, gw, _types=explore_types: bool(gw.scan_blocks(_bb.dimension, _types, radius=24, limit=4)),
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
                if out.status == "OK":
                    raw_pos = out.details.get("pos") if isinstance(out.details, dict) else None
                    if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3:
                        self._placed_table_pos = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
                    else:
                        self._placed_table_pos = self._craft.find_nearby_table(bb)
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
