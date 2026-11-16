"""Exposed ore vein mining and physics/entity-guided drop pickup over VoxelWorld3D."""

from __future__ import annotations

from collections import deque
import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from core.cave_probe import (
    FALLING_BLOCKS,
    is_lava_like,
    is_passable,
    is_solid_floor,
    is_water_like,
    ore_cell_hazard_free,
    reachable_exposed_ores,
)
from core.domain import parse_pos
from core.pathfinder_3d import Pathfinder3D
from core.mining_targets import (
    best_usable_pickaxe,
    evaluate_ore_block,
    pickaxe_tier,
)
from core.structure_spec import MINE_DIGGABLE_BLOCKS, ORE_DROP_MAP
from core.world_3d import VoxelWorld3D
from skills.action_workflow import (
    WorkflowContext,
    WorkflowStepError,
    aim_at,
    dig_block,
    stand_cell,
)
from skills.world_navigator import WorldNavigator


class OreHarvester:
    """Mine hazard-free exposed ore veins with collect=False and physically collect settled drops."""

    @staticmethod
    def read_inventory(ctx: WorkflowContext) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
        inv_fn = getattr(ctx.guard.client, "inventory", None)
        if not callable(inv_fn):
            counts = ctx.inventory_counts()
            return counts, [{"name": k, "count": v} for k, v in counts.items()]
        env = ctx.guard.read(inv_fn, timeout_seconds=ctx.guard.remaining_timeout(10.0))
        res = env.get("result") if isinstance(env, dict) and isinstance(env.get("result"), dict) else {}
        items = res.get("items") if isinstance(res.get("items"), list) else []
        counts: Dict[str, int] = {}
        for row in items:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                c = row.get("count", 0)
                if isinstance(c, int) and not isinstance(c, bool) and c > 0:
                    counts[row["name"]] = counts.get(row["name"], 0) + c
        return counts, items

    @classmethod
    def harvest_reachable_at(
        cls,
        ctx: WorkflowContext,
        world: VoxelWorld3D,
        stance_pos: Sequence[int],
        inv_items: Sequence[Dict[str, Any]],
        targets: Mapping[str, Mapping[str, Any]],
        harvested_totals: Dict[str, int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        ore_records: Dict[Tuple[int, int, int], Dict[str, Any]],
        *,
        max_dist: float = 4.5,
        max_vein: int = 16,
    ) -> Tuple[int, int]:
        """Scan reachable exposed ores from stance_pos, mine veins matching targets, and update world registry."""
        pick_name = best_usable_pickaxe(inv_items)
        tool_tier = pickaxe_tier(pick_name)
        found = reachable_exposed_ores(
            stance_pos,
            lambda p: world.read_block(p, read_fn),
            targets,
            harvested_counts=harvested_totals,
            best_tool_tier=tool_tier,
            max_dist=max_dist,
        )
        dug_total = 0
        for row in found:
            key = (int(row["pos"][0]), int(row["pos"][1]), int(row["pos"][2]))
            if row["status"] != "mined":
                prev = ore_records.get(key)
                if prev is None or prev.get("status") not in {"mined", "skipped_hazard"}:
                    ore_records[key] = {"pos": list(row["pos"]), "name": row["name"], "status": row["status"]}
                world.mark_ore_status(key, row["status"], name=row["name"])
                continue
            vein_dug = cls.mine_vein(
                ctx,
                world,
                stance_pos,
                row["pos"],
                row["target_key"],
                targets,
                harvested_totals,
                read_fn,
                ore_records,
                max_dist=max_dist,
                max_vein=max_vein,
            )
            dug_total += vein_dug
        return dug_total, len(ore_records)

    @classmethod
    def mine_vein(
        cls,
        ctx: WorkflowContext,
        world: VoxelWorld3D,
        stance_pos: Sequence[int],
        seed_pos: Sequence[int],
        target_key: str,
        targets: Mapping[str, Mapping[str, Any]],
        harvested_totals: Dict[str, int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        ore_records: Dict[Tuple[int, int, int], Dict[str, Any]],
        *,
        invalidate: Optional[Callable[[Sequence[int]], None]] = None,
        max_dist: float = 4.5,
        max_vein: int = 16,
    ) -> int:
        queue: deque = deque([list(seed_pos)])
        seen_vein: Set[Tuple[int, int, int]] = {(int(seed_pos[0]), int(seed_pos[1]), int(seed_pos[2]))}
        dug = 0
        sx, sy, sz = int(stance_pos[0]), int(stance_pos[1]), int(stance_pos[2])
        eye = (sx + 0.5, sy + 1.62, sz + 0.5)

        def cached_read(p: Sequence[int]) -> Dict[str, Any]:
            return world.read_block(p, read_fn)

        while queue and dug < int(max_vein):
            cur = queue.popleft()
            ckey = (int(cur[0]), int(cur[1]), int(cur[2]))
            if ckey == (sx, sy - 1, sz):
                continue
            blk = cached_read(ckey)
            bname = str(blk.get("name") or "air")
            if bname not in ORE_DROP_MAP:
                continue

            _, cur_items = cls.read_inventory(ctx)
            pick_name = best_usable_pickaxe(cur_items)
            tool_tier = pickaxe_tier(pick_name)
            action, tk, drop_item = evaluate_ore_block(
                bname,
                targets,
                harvested_counts=harvested_totals,
                best_tool_tier=tool_tier,
            )
            if tk != target_key:
                continue
            if action != "mine":
                if not ore_cell_hazard_free(ckey, cached_read):
                    action = "skipped_hazard"
                prev = ore_records.get(ckey)
                if prev is None or prev.get("status") not in {"mined", "skipped_hazard"}:
                    ore_records[ckey] = {"pos": list(cur), "name": bname, "status": action}
                world.mark_ore_status(ckey, action, name=bname)
                continue

            dist = math.sqrt((cur[0] + 0.5 - eye[0]) ** 2 + (cur[1] + 0.5 - eye[1]) ** 2 + (cur[2] + 0.5 - eye[2]) ** 2)
            if dist > float(max_dist):
                ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "unreachable"}
                world.mark_ore_status(ckey, "unreachable", name=bname)
                continue

            if not ore_cell_hazard_free(ckey, cached_read):
                ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "skipped_hazard"}
                world.mark_ore_status(ckey, "skipped_hazard", name=bname)
                continue

            above_name = str(cached_read((cur[0], cur[1] + 1, cur[2])).get("name") or "air")
            if above_name in FALLING_BLOCKS:
                above2 = cached_read((cur[0], cur[1] + 2, cur[2]))
                if is_lava_like(above2) or is_water_like(above2):
                    ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "skipped_hazard"}
                    world.mark_ore_status(ckey, "skipped_hazard", name=bname)
                    continue

            if pick_name:
                ctx.guard.write(ctx.guard.client.equip, item=pick_name, slot="hand")
            aim_at(ctx, cur, relative=False)
            before_one, _ = cls.read_inventory(ctx)
            res = dig_block(
                ctx,
                cur,
                relative=False,
                allowed_blocks=set(ORE_DROP_MAP) | MINE_DIGGABLE_BLOCKS,
                collect=False,
            )
            world.invalidate(cur, radius=1)
            if callable(invalidate):
                invalidate(cur)
            if above_name in FALLING_BLOCKS:
                time.sleep(0.35)
                world.invalidate(cur, radius=1)
                if callable(invalidate):
                    invalidate(cur)

            if not res.get("skipped"):
                dug += 1
                ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "mined"}
                world.mark_ore_status(ckey, "mined", name=bname)
                time.sleep(0.22 if cur[1] > sy else 0.12)
                after_one, _ = cls.read_inventory(ctx)
                direct_gained = (
                    max(0, after_one.get(drop_item, 0) - before_one.get(drop_item, 0))
                    if drop_item
                    else 0
                )
                if direct_gained == 0:
                    after_mid = cls.collect_mined_drop(
                        ctx,
                        world,
                        cur,
                        stance_pos,
                        drop_item,
                        before_one,
                        read_fn,
                    )
                    if after_mid and drop_item and after_mid.get(drop_item, 0) > after_one.get(drop_item, 0):
                        after_one = after_mid
                    else:
                        after_retry, _ = cls.read_inventory(ctx)
                        if drop_item and after_retry.get(drop_item, 0) > after_one.get(drop_item, 0):
                            after_one = after_retry
                if drop_item:
                    gained = max(0, after_one.get(drop_item, 0) - before_one.get(drop_item, 0))
                    if gained > 0:
                        harvested_totals[drop_item] = harvested_totals.get(drop_item, 0) + gained

                for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nkey = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
                    if nkey not in seen_vein:
                        seen_vein.add(nkey)
                        nblk = cached_read(nkey)
                        if str(nblk.get("name") or "") in ORE_DROP_MAP:
                            queue.append(list(nkey))
        return dug

    @classmethod
    def collect_mined_drop(
        cls,
        ctx: WorkflowContext,
        world: VoxelWorld3D,
        ore_pos: Sequence[int],
        stance_pos: Sequence[int],
        drop_item: Optional[str],
        before_counts: Mapping[str, int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
    ) -> Optional[Dict[str, int]]:
        """Step onto item-entity / gravity-settled landing floor of a dug ore drop and return safely."""
        ox, oy, oz = int(ore_pos[0]), int(ore_pos[1]), int(ore_pos[2])
        sx, sy, sz = int(stance_pos[0]), int(stance_pos[1]), int(stance_pos[2])

        def cached_read(p: Sequence[int]) -> Dict[str, Any]:
            return world.read_block(p, read_fn)

        cand_spots: List[List[int]] = []
        seen_spots: Set[Tuple[int, int, int]] = {(sx, sy, sz)}

        def _add_if_standable(cx: int, cy: int, cz: int) -> None:
            key = (cx, cy, cz)
            if key in seen_spots or abs(cx - sx) + abs(cz - sz) > 6 or abs(cy - sy) > 2:
                return
            seen_spots.add(key)
            if cy > sy and not is_passable(cached_read((sx, sy + 2, sz))):
                return
            if (
                is_solid_floor(cached_read((cx, cy - 1, cz)))
                and is_passable(cached_read((cx, cy, cz)))
                and is_passable(cached_read((cx, cy + 1, cz)))
                and ore_cell_hazard_free((cx, cy, cz), cached_read)
            ):
                cand_spots.append([cx, cy, cz])

        # 1. Live dropped item entity hint if exposed by mc-agent-api
        ent_fn = getattr(ctx.guard.client, "get_entities", None)
        if callable(ent_fn):
            try:
                raw_ents = ent_fn(radius=8)
                ent_list = raw_ents.get("result", {}).get("entities", []) if isinstance(raw_ents, dict) else (raw_ents or [])
                for ent in ent_list if isinstance(ent_list, list) else ():
                    if not isinstance(ent, dict):
                        continue
                    etype = str(ent.get("type") or ent.get("kind") or "").lower()
                    ename = str(ent.get("name") or "").lower()
                    epos = parse_pos(ent.get("pos"))
                    if epos is None:
                        continue
                    if ("item" in etype or (drop_item and drop_item in ename)) and (
                        math.hypot(epos[0] - (ox + 0.5), epos[2] - (oz + 0.5)) <= 4.5
                        and abs(epos[1] - oy) <= 3.5
                    ):
                        ex, ey, ez = math.floor(epos[0]), math.floor(epos[1]), math.floor(epos[2])
                        for dy in (0, -1, 1):
                            _add_if_standable(ex, ey + dy, ez)
            except Exception:
                pass

        # 2. Scan physically downward beneath ore_pos for the resting solid floor
        land_y: Optional[int] = None
        for y_try in range(min(oy + 1, sy + 2), max(oy - 4, sy - 4) - 1, -1):
            if (
                is_solid_floor(cached_read((ox, y_try - 1, oz)))
                and is_passable(cached_read((ox, y_try, oz)))
                and is_passable(cached_read((ox, y_try + 1, oz)))
            ):
                land_y = y_try
                _add_if_standable(ox, y_try, oz)
                break

        # 3. Adjacent lower/same-level floor cells ordered closest to landing point
        ref_y = land_y if land_y is not None else min(oy, sy)
        nearby: List[Tuple[float, Tuple[int, int, int]]] = []
        for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            for y_try in (ref_y, sy, sy - 1, oy, oy - 1):
                if abs(y_try - sy) <= 2:
                    dist_drop = math.hypot(dx, dz) + 0.8 * abs(y_try - ref_y) + 0.25 * abs(y_try - sy)
                    nearby.append((dist_drop, (ox + dx, y_try, oz + dz)))
        nearby.sort(key=lambda it: it[0])
        for _, cell in nearby:
            _add_if_standable(cell[0], cell[1], cell[2])

        best_counts: Optional[Dict[str, int]] = None
        for spot in cand_spots[:2]:
            try:
                stand_cell(ctx, spot, tolerance=0.95)
            except WorkflowStepError:
                cur_chk, _ = cls.read_inventory(ctx)
                if best_counts is None or (drop_item and cur_chk.get(drop_item, 0) > best_counts.get(drop_item, 0)):
                    best_counts = cur_chk
                if drop_item and cur_chk.get(drop_item, 0) > before_counts.get(drop_item, 0):
                    break
                continue
            time.sleep(0.15)
            after_mid, _ = cls.read_inventory(ctx)
            if best_counts is None or (drop_item and after_mid.get(drop_item, 0) > best_counts.get(drop_item, 0)):
                best_counts = after_mid
            try:
                stand_cell(ctx, stance_pos, tolerance=0.95)
            except WorkflowStepError:
                path_back = Pathfinder3D.find_path(
                    world,
                    spot,
                    stance_pos,
                    read_fn,
                    goal_tolerance=1,
                    allow_pillar=True,
                    allow_dig=False,
                    allow_bridge=False,
                    allow_drop=True,
                    max_nodes=400,
                )
                if path_back:
                    WorldNavigator.follow_path(ctx, world, spot, path_back, read_fn, allow_micro_dig=False)
            if drop_item and after_mid.get(drop_item, 0) > before_counts.get(drop_item, 0):
                break
        return best_counts
