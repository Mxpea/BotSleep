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
from core.structure_spec import MINE_DIGGABLE_BLOCKS, ORE_DROP_MAP, SCAFFOLD_BLOCKS
from core.world_3d import VoxelWorld3D
from skills.action_workflow import (
    WorkflowContext,
    WorkflowStepError,
    aim_at,
    dig_block,
    pillar_up,
    stand_at,
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
        finish_vein: bool = True,
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
            # Never start a brand-new vein whose target quota is already met, but once
            # inside an opened connected vein (`dug > 0`), finish exposed vein blocks
            # cleanly so leftover ore isn't wasted (while still checking tool tier).
            if action == "skipped_quota" and finish_vein and dug > 0:
                tool_chk, _, _ = evaluate_ore_block(
                    bname,
                    targets,
                    harvested_counts={},
                    best_tool_tier=tool_tier,
                )
                if tool_chk == "mine":
                    action = "mine"
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
        """Collect settled drops in corners, 1-high pockets, high cliffs, or 1x1 pits and return safely."""
        ox, oy, oz = int(ore_pos[0]), int(ore_pos[1]), int(ore_pos[2])
        sx, sy, sz = int(stance_pos[0]), int(stance_pos[1]), int(stance_pos[2])
        dig_allowed = set(ORE_DROP_MAP) | set(MINE_DIGGABLE_BLOCKS)

        def cached_read(p: Sequence[int]) -> Dict[str, Any]:
            return world.read_block(p, read_fn)

        def _has_gained(counts_map: Mapping[str, int]) -> bool:
            return bool(drop_item and counts_map.get(drop_item, 0) > before_counts.get(drop_item, 0))

        def _update_best(best_c: Optional[Dict[str, int]], cur_c: Dict[str, int]) -> Dict[str, int]:
            if best_c is None or (drop_item and cur_c.get(drop_item, 0) > best_c.get(drop_item, 0)):
                return cur_c
            return best_c

        def _is_two_high_standable(cx: int, cy: int, cz: int) -> bool:
            return (
                is_solid_floor(cached_read((cx, cy - 1, cz)))
                and is_passable(cached_read((cx, cy, cz)))
                and is_passable(cached_read((cx, cy + 1, cz)))
                and ore_cell_hazard_free((cx, cy, cz), cached_read)
            )

        def _live_cur_cell() -> List[int]:
            snap = WorldNavigator.sync_live_pos(ctx)
            return snap if snap is not None else [sx, sy, sz]

        def _return_to_stance(from_spot: Sequence[int]) -> None:
            fx, fy, fz = int(from_spot[0]), int(from_spot[1]), int(from_spot[2])
            if [fx, fy, fz] == [sx, sy, sz]:
                return
            try:
                stand_cell(ctx, stance_pos, tolerance=0.95)
                return
            except WorkflowStepError:
                pass
            # Case D pit escape: pillar_up underfoot inside a 1x1 well back up to sy
            if fy < sy and (sy - fy) <= 4:
                try:
                    pillar_up(
                        ctx,
                        steps=min(4, sy - fy),
                        blocks=SCAFFOLD_BLOCKS,
                        allow_dig_headroom=True,
                    )
                    for py in range(fy, sy):
                        world.invalidate((fx, py, fz), radius=1)
                    try:
                        stand_cell(ctx, stance_pos, tolerance=0.95)
                        return
                    except WorkflowStepError:
                        pass
                except WorkflowStepError:
                    pass
            cur_after = _live_cur_cell()
            path_back = Pathfinder3D.find_path(
                world,
                cur_after,
                stance_pos,
                read_fn,
                goal_tolerance=1,
                allow_pillar=True,
                max_pillar_height=4,
                allow_dig=True,
                max_dig_steps=2,
                allow_bridge=False,
                allow_drop=True,
                max_nodes=400,
            )
            if path_back:
                WorldNavigator.follow_path(ctx, world, cur_after, path_back, read_fn, allow_micro_dig=True)

        # 1. Live dropped item entity hints + downward solid floor scan beneath ore_pos
        entity_cells: List[Tuple[int, int, int]] = []
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
                        and abs(epos[1] - oy) <= 4.5
                    ):
                        entity_cells.append((math.floor(epos[0]), math.floor(epos[1]), math.floor(epos[2])))
            except Exception:
                pass

        def _scan_land_y() -> Optional[int]:
            for y_try in range(min(oy + 2, sy + 4), max(oy - 4, sy - 4) - 1, -1):
                if (
                    is_solid_floor(cached_read((ox, y_try - 1, oz)))
                    and is_passable(cached_read((ox, y_try, oz)))
                ):
                    return y_try
            return None

        land_y = _scan_land_y()
        best_counts: Optional[Dict[str, int]] = None

        # Case C: High cliff/ledge under-dig gravity drop — if floor below drop is reachable & safe to dig, drop item down
        for lx, ly, lz in list(entity_cells) + ([(ox, land_y, oz)] if land_y is not None else []):
            if ly < sy + 1 or ly > sy + 4:
                continue
            support = (lx, ly - 1, lz)
            if support == (sx, sy - 1, sz):
                continue
            s_dist = math.sqrt(
                (support[0] + 0.5 - (sx + 0.5)) ** 2
                + (support[1] + 0.5 - (sy + 1.62)) ** 2
                + (support[2] + 0.5 - (sz + 0.5)) ** 2
            )
            sname = str(cached_read(support).get("name") or "air")
            if s_dist > 4.5 or sname not in dig_allowed or not ore_cell_hazard_free(support, cached_read):
                continue
            safe_drop_zone = any(
                is_solid_floor(cached_read((lx, yd - 1, lz)))
                and not is_lava_like(cached_read((lx, yd, lz)))
                and not is_water_like(cached_read((lx, yd, lz)))
                and ore_cell_hazard_free((lx, yd, lz), cached_read)
                for yd in range(ly - 1, max(ly - 5, sy - 4) - 1, -1)
            )
            if not safe_drop_zone:
                continue
            try:
                _, cur_items = cls.read_inventory(ctx)
                pick_name = best_usable_pickaxe(cur_items)
                if pick_name:
                    ctx.guard.write(ctx.guard.client.equip, item=pick_name, slot="hand")
                aim_at(ctx, support, relative=False)
                dig_block(ctx, support, relative=False, allowed_blocks=dig_allowed, collect=False)
                world.invalidate(support, radius=1)
                time.sleep(0.25)
                chk_c, _ = cls.read_inventory(ctx)
                best_counts = _update_best(best_counts, chk_c)
                if _has_gained(chk_c):
                    return best_counts
                land_y = _scan_land_y()
                break
            except WorkflowStepError:
                pass

        # Case B: 1-block-high wall pocket / gap — step close to lip first, otherwise open ceiling block & step in
        opened_pocket_spots: List[List[int]] = []
        pocket_targets: List[Tuple[int, int, int]] = []
        for cell in entity_cells + ([(ox, land_y, oz)] if land_y is not None else []):
            if cell not in pocket_targets:
                pocket_targets.append(cell)
        for lx, ly, lz in pocket_targets:
            if (
                is_solid_floor(cached_read((lx, ly - 1, lz)))
                and is_passable(cached_read((lx, ly, lz)))
                and not is_passable(cached_read((lx, ly + 1, lz)))
                and abs(lx - sx) + abs(lz - sz) <= 6
                and -4 <= ly - sy <= 4
            ):
                # First try tight step onto closest 2-high adjacent lip cell
                lip_cells: List[Tuple[float, List[int]]] = []
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                    for dy in (0, -1, 1):
                        nx, ny, nz = lx + dx, ly + dy, lz + dz
                        if _is_two_high_standable(nx, ny, nz):
                            lip_cells.append((math.hypot(dx, dz) + 0.5 * abs(dy), [nx, ny, nz]))
                lip_cells.sort(key=lambda it: it[0])
                for _, lip in lip_cells[:1]:
                    try:
                        stand_at(ctx, [lip[0] + 0.5, float(lip[1]), lip[2] + 0.5], relative=False, tolerance=0.35, exact=True)
                        time.sleep(0.15)
                        chk_lip, _ = cls.read_inventory(ctx)
                        best_counts = _update_best(best_counts, chk_lip)
                        if _has_gained(chk_lip):
                            _return_to_stance(lip)
                            return best_counts
                    except WorkflowStepError:
                        pass

                ceil_cell = (lx, ly + 1, lz)
                cname = str(cached_read(ceil_cell).get("name") or "air")
                above_ceil = cached_read((lx, ly + 2, lz))
                above_name = str(above_ceil.get("name") or "air")
                cur_eye = _live_cur_cell()
                cdist = math.sqrt(
                    (ceil_cell[0] + 0.5 - (cur_eye[0] + 0.5)) ** 2
                    + (ceil_cell[1] + 0.5 - (cur_eye[1] + 1.62)) ** 2
                    + (ceil_cell[2] + 0.5 - (cur_eye[2] + 0.5)) ** 2
                )
                if (
                    cname in dig_allowed
                    and cdist <= 4.8
                    and ore_cell_hazard_free(ceil_cell, cached_read)
                    and above_name not in FALLING_BLOCKS
                    and not is_lava_like(above_ceil)
                    and not is_water_like(above_ceil)
                ):
                    try:
                        _, cur_items = cls.read_inventory(ctx)
                        pick_name = best_usable_pickaxe(cur_items)
                        if pick_name:
                            ctx.guard.write(ctx.guard.client.equip, item=pick_name, slot="hand")
                        aim_at(ctx, ceil_cell, relative=False)
                        dig_block(ctx, ceil_cell, relative=False, allowed_blocks=dig_allowed, collect=False)
                        world.invalidate(ceil_cell, radius=1)
                        if _is_two_high_standable(lx, ly, lz):
                            opened_pocket_spots.append([lx, ly, lz])
                    except WorkflowStepError:
                        pass

        # Collect candidate standable pickup cells across corners (±1,±1), cliffs (±4 Y), and 1x1 pits (down to sy-4)
        cand_spots: List[List[int]] = []
        seen_spots: Set[Tuple[int, int, int]] = {(sx, sy, sz)}

        def _add_if_standable(cx: int, cy: int, cz: int) -> None:
            key = (cx, cy, cz)
            if key in seen_spots or abs(cx - sx) + abs(cz - sz) > 6 or cy - sy > 4 or sy - cy > 4:
                return
            seen_spots.add(key)
            if _is_two_high_standable(cx, cy, cz):
                cand_spots.append([cx, cy, cz])

        for spot_p in opened_pocket_spots:
            _add_if_standable(spot_p[0], spot_p[1], spot_p[2])
        for ex, ey, ez in entity_cells:
            for dy in (0, -1, 1, -2):
                _add_if_standable(ex, ey + dy, ez)
        if land_y is not None:
            _add_if_standable(ox, land_y, oz)

        ref_y = land_y if land_y is not None else min(oy, sy)
        nearby: List[Tuple[float, Tuple[int, int, int]]] = []
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for y_try in (ref_y, sy, sy - 1, oy, oy - 1, oy + 1, sy + 1, sy - 2):
                    if -4 <= y_try - sy <= 4:
                        dist_drop = math.hypot(dx, dz) + 0.8 * abs(y_try - ref_y) + 0.2 * abs(y_try - sy)
                        nearby.append((dist_drop, (ox + dx, y_try, oz + dz)))
        nearby.sort(key=lambda it: it[0])
        for _, cell in nearby:
            _add_if_standable(cell[0], cell[1], cell[2])

        for spot in cand_spots[:4]:
            moved_ok = False
            try:
                stand_at(ctx, [spot[0] + 0.5, float(spot[1]), spot[2] + 0.5], relative=False, tolerance=0.38, exact=True)
                moved_ok = True
            except WorkflowStepError:
                cur_chk, _ = cls.read_inventory(ctx)
                best_counts = _update_best(best_counts, cur_chk)
                if _has_gained(cur_chk):
                    _return_to_stance(_live_cur_cell())
                    return best_counts
                # Case A / Case C fallback: route around corners or climb up cliff ledges via 3D A*
                cur_here = _live_cur_cell()
                path_to = Pathfinder3D.find_path(
                    world,
                    cur_here,
                    spot,
                    read_fn,
                    goal_tolerance=0,
                    allow_pillar=True,
                    max_pillar_height=4,
                    allow_dig=True,
                    max_dig_steps=2,
                    allow_bridge=False,
                    allow_drop=True,
                    max_nodes=320,
                )
                if path_to:
                    all_done, _ = WorldNavigator.follow_path(
                        ctx,
                        world,
                        cur_here,
                        path_to,
                        read_fn,
                        allow_micro_dig=True,
                        tolerance=0.45,
                    )
                    moved_ok = bool(all_done)

            if not moved_ok:
                continue
            time.sleep(0.18)
            after_mid, _ = cls.read_inventory(ctx)
            best_counts = _update_best(best_counts, after_mid)
            _return_to_stance(spot)
            if _has_gained(after_mid):
                break

        return best_counts
