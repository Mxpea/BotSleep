"""General-purpose 3D A* pathfinder and horizon explorer operating on VoxelWorld3D."""

from __future__ import annotations

import heapq
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from core.cave_probe import (
    _block_name,
    _can_dig_step_cell,
    _cell_safe_border,
    _cell_safe_border_ignore_floor,
    _lateral_clearance,
    flood_safe_cave_graph,
    is_lava_like,
    is_passable,
    is_solid_floor,
    is_water_like,
    reconstruct_flood_path,
)
from core.structure_spec import AIR
from core.world_3d import Coord3D, VoxelWorld3D, norm_coord


class Pathfinder3D:
    """Memory-backed 3D A* pathfinder and incremental frontier scanner over VoxelWorld3D."""

    @staticmethod
    def plan_horizon(
        world: VoxelWorld3D,
        start_foot: Sequence[int],
        read_fn: Optional[Callable[[Sequence[int]], Dict[str, Any]]] = None,
        *,
        max_nodes: int = 560,
        max_radius: int = 48,
        anchor_pos: Optional[Sequence[int]] = None,
        anchor_radius: Optional[int] = None,
        allow_pillar: bool = True,
        max_pillar_height: int = 4,
        allow_dig: bool = True,
        max_dig_steps: int = 3,
        allow_bridge: bool = True,
        max_bridge_steps: int = 4,
        allow_drop: bool = True,
        max_drop_steps: int = 5,
        allow_cobweb: bool = False,
        targets: Optional[Mapping[str, Mapping[str, Any]]] = None,
        harvested_counts: Optional[Mapping[str, int]] = None,
        best_tool_tier: int = 0,
        eye_radius: float = 4.5,
        goal_hints: Optional[Sequence[Sequence[int]]] = None,
        extra_protected: Optional[Set[Coord3D]] = None,
    ) -> Dict[str, Any]:
        """Expand local/regional 3D safe graph in VoxelWorld3D and update open_frontiers & ore_registry."""
        visited_set: Set[Coord3D] = set(world.visited_stances.keys())
        protected: Set[Coord3D] = set(extra_protected or ())
        for sc in world.placed_scaffolds:
            protected.add(sc)
            protected.add((sc[0], sc[1] - 1, sc[2]))

        blocked_set = {e for e, cnt in world.blocked_edges.items() if cnt >= 1}

        def world_reader(pos: Sequence[int]) -> Dict[str, Any]:
            return world.read_block(pos, read_fn)

        graph = flood_safe_cave_graph(
            start_foot,
            world_reader,
            max_nodes=max_nodes,
            max_radius=max_radius,
            allow_pillar=allow_pillar,
            max_pillar_height=max_pillar_height,
            allow_dig=allow_dig,
            max_dig_steps=max_dig_steps,
            allow_bridge=allow_bridge,
            max_bridge_steps=max_bridge_steps,
            allow_drop=allow_drop,
            max_drop_steps=max_drop_steps,
            allow_cobweb=allow_cobweb,
            targets=targets,
            harvested_counts=harvested_counts,
            best_tool_tier=best_tool_tier,
            eye_radius=eye_radius,
            visited_cells=visited_set,
            covered_cells=world.scanned_cells,
            goal_hints=goal_hints,
            protected_cells=protected,
            anchor_pos=anchor_pos,
            anchor_radius=anchor_radius,
            blocked_edges=blocked_set,
            block_cache=world.block_cache,
        )
        world.update_frontiers_from_graph(
            graph,
            entry_pos=anchor_pos if anchor_pos is not None else start_foot,
            max_radius=anchor_radius if anchor_radius is not None else max_radius,
        )
        return graph

    @staticmethod
    def find_path(
        world: VoxelWorld3D,
        start_foot: Sequence[int],
        goal_foot: Sequence[int],
        read_fn: Optional[Callable[[Sequence[int]], Dict[str, Any]]] = None,
        *,
        goal_tolerance: int = 0,
        allow_pillar: bool = True,
        max_pillar_height: int = 4,
        allow_dig: bool = True,
        max_dig_steps: int = 3,
        allow_bridge: bool = True,
        max_bridge_steps: int = 4,
        allow_drop: bool = True,
        max_drop_steps: int = 5,
        allow_cobweb: bool = False,
        anchor_pos: Optional[Sequence[int]] = None,
        anchor_radius: Optional[int] = None,
        max_nodes: int = 2400,
        cache_only: bool = False,
        extra_protected: Optional[Set[Coord3D]] = None,
    ) -> List[Dict[str, Any]]:
        """Directed 3D A* pathfinder from start_foot to goal_foot over VoxelWorld3D."""
        start = norm_coord(start_foot)
        goal = norm_coord(goal_foot)
        tol = max(0, int(goal_tolerance))
        if (
            abs(start[0] - goal[0]) <= tol
            and abs(start[1] - goal[1]) <= tol
            and abs(start[2] - goal[2]) <= tol
        ):
            return []

        sx, sy, sz = start
        gx, gy, gz = goal
        ax, ay, az = norm_coord(anchor_pos) if anchor_pos is not None else start
        arad = int(anchor_radius) if anchor_radius is not None else max(24, abs(gx - sx) + abs(gz - sz) + 12)
        protected = set(extra_protected or ())
        for sc in world.placed_scaffolds:
            protected.add(sc)
            protected.add((sc[0], sc[1] - 1, sc[2]))

        def reader(pos: Tuple[int, int, int]) -> Dict[str, Any]:
            if cache_only or read_fn is None:
                return world.get_block_cached(pos) or {"name": "unknown", "properties": {}}
            return world.read_block(pos, read_fn)

        def heuristic(p: Coord3D) -> float:
            return float(abs(p[0] - gx) + 1.4 * abs(p[1] - gy) + abs(p[2] - gz))

        g_costs: Dict[Coord3D, float] = {start: 0.0}
        parents: Dict[Coord3D, Optional[Coord3D]] = {start: None}
        edge_meta: Dict[Coord3D, Dict[str, Any]] = {}
        # Heap entries: (f_score, g_cost, pos, pillar_h, dig_streak, bridge_streak)
        pq: List[Tuple[float, float, Coord3D, int, int, int]] = [(heuristic(start), 0.0, start, 0, 0, 0)]
        expanded: Set[Coord3D] = set()

        while pq and len(expanded) < int(max_nodes):
            _, cur_cost, cur, pillar_h, dig_streak, bridge_streak = heapq.heappop(pq)
            if cur in expanded:
                continue
            if cur_cost > g_costs.get(cur, float("inf")) + 1e-6:
                continue
            cx, cy, cz = cur
            if abs(cx - gx) <= tol and abs(cy - gy) <= tol and abs(cz - gz) <= tol:
                return reconstruct_flood_path(parents, edge_meta, cur)
            expanded.add(cur)

            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = cx + dx, cz + dz
                if abs(nx - ax) > arad or abs(nz - az) > arad:
                    continue

                # Pass A: free walk across dy in (0, +1, -1)
                walk_pushed = False
                stepped_floor = False
                for dy in (0, 1, -1):
                    ny = cy + dy
                    nxt = (nx, ny, nz)
                    if not _cell_safe_border(nxt, reader):
                        continue
                    if not is_solid_floor(reader((nx, ny - 1, nz))):
                        continue
                    stepped_floor = True
                    f_blk = reader((nx, ny, nz))
                    h_blk = reader((nx, ny + 1, nz))
                    if not (
                        is_passable(f_blk, allow_cobweb=allow_cobweb)
                        and is_passable(h_blk, allow_cobweb=allow_cobweb)
                    ):
                        continue
                    up_clear = True
                    if dy == 1:
                        up_clear = is_passable(reader((cx, cy + 2, cz)), allow_cobweb=allow_cobweb)
                    elif dy == -1:
                        up_clear = is_passable(reader((nx, ny + 2, nz)), allow_cobweb=allow_cobweb)
                    if not up_clear:
                        continue
                    if world.is_edge_blocked(cur, nxt):
                        continue
                    clr = _lateral_clearance(nxt, reader)
                    base_delta = 1.0 if dy == 0 else 1.15
                    step_cost = cur_cost + max(0.78, base_delta - 0.025 * clr)
                    if nxt not in expanded and step_cost < g_costs.get(nxt, float("inf")):
                        g_costs[nxt] = step_cost
                        parents[nxt] = cur
                        edge_meta[nxt] = {"mode": "walk", "clearance": clr}
                        heapq.heappush(pq, (step_cost + heuristic(nxt), step_cost, nxt, 0, 0, 0))
                    walk_pushed = True
                    break

                # Pass B: micro-dig (when no free walk exists on this column)
                if not walk_pushed and allow_dig and dig_streak < int(max_dig_steps):
                    for dy in (0, 1, -1):
                        ny = cy + dy
                        nxt = (nx, ny, nz)
                        if world.is_edge_blocked(cur, nxt):
                            continue
                        if not _cell_safe_border(nxt, reader):
                            continue
                        if not is_solid_floor(reader((nx, ny - 1, nz))):
                            continue
                        f_pass = is_passable(reader((nx, ny, nz)), allow_cobweb=allow_cobweb)
                        h_pass = is_passable(reader((nx, ny + 1, nz)), allow_cobweb=allow_cobweb)
                        up_dig_needed: Optional[Coord3D] = None
                        if dy == 1 and not is_passable(reader((cx, cy + 2, cz)), allow_cobweb=allow_cobweb):
                            up_dig_needed = (cx, cy + 2, cz)
                        elif dy == -1 and not is_passable(reader((nx, ny + 2, nz)), allow_cobweb=allow_cobweb):
                            up_dig_needed = (nx, ny + 2, nz)

                        dig_cells: List[Coord3D] = []
                        ok = True
                        for need_pass, cell_pos in ((h_pass, (nx, ny + 1, nz)), (f_pass, (nx, ny, nz))):
                            if not need_pass:
                                if cell_pos in protected or not _can_dig_step_cell(cell_pos, reader):
                                    ok = False
                                    break
                                dig_cells.append(cell_pos)
                        if up_dig_needed is not None and ok:
                            if up_dig_needed in protected or not _can_dig_step_cell(up_dig_needed, reader):
                                ok = False
                            else:
                                dig_cells.insert(0, up_dig_needed)
                        if ok and 1 <= len(dig_cells) <= 2:
                            dig_set = set(dig_cells)
                            if len(dig_cells) == 1:
                                opens_to_air = any(
                                    (px, py, pz) not in {(cx, cy, cz), (cx, cy + 1, cz)}
                                    and (px, py, pz) not in dig_set
                                    and is_passable(reader((px, py, pz)), allow_cobweb=allow_cobweb)
                                    for dc in dig_cells
                                    for px, py, pz in (
                                        (dc[0] + dx, dc[1], dc[2] + dz),
                                        (dc[0], dc[1] + 1, dc[2]),
                                        (dc[0], dc[1], dc[2] + 1),
                                        (dc[0], dc[1], dc[2] - 1),
                                        (dc[0] + 1, dc[1], dc[2]),
                                        (dc[0] - 1, dc[1], dc[2]),
                                    )
                                )
                            else:
                                far_x, far_z = nx + dx, nz + dz
                                opens_to_air = (
                                    is_passable(reader((far_x, ny, far_z)), allow_cobweb=allow_cobweb)
                                    and is_passable(reader((far_x, ny + 1, far_z)), allow_cobweb=allow_cobweb)
                                )
                            if not opens_to_air:
                                continue
                            step_cost = cur_cost + 3.5 + 1.5 * len(dig_cells)
                            if nxt not in expanded and step_cost < g_costs.get(nxt, float("inf")):
                                g_costs[nxt] = step_cost
                                parents[nxt] = cur
                                edge_meta[nxt] = {"mode": "dig", "dig_cells": [list(c) for c in dig_cells]}
                                heapq.heappush(pq, (step_cost + heuristic(nxt), step_cost, nxt, 0, dig_streak + 1, 0))
                            break

                # Safe drop & scaffold drop
                if allow_drop and not stepped_floor:
                    c_head_ok = (
                        is_passable(reader((nx, cy, nz)), allow_cobweb=allow_cobweb)
                        and is_passable(reader((nx, cy + 1, nz)), allow_cobweb=allow_cobweb)
                        and _cell_safe_border_ignore_floor((nx, cy, nz), reader)
                        and _cell_safe_border((nx, cy + 1, nz), reader)
                    )
                    if c_head_ok:
                        for drop_d in range(2, max(2, int(max_drop_steps)) + 1):
                            ny = cy - drop_d
                            if abs(ny - ay) > max(arad, 32):
                                break
                            if not is_passable(reader((nx, ny, nz)), allow_cobweb=allow_cobweb):
                                break
                            floor_d = reader((nx, ny - 1, nz))
                            if is_lava_like(floor_d) or is_water_like(floor_d):
                                break
                            if is_solid_floor(floor_d):
                                nxt = (nx, ny, nz)
                                if not world.is_edge_blocked(cur, nxt) and _cell_safe_border(nxt, reader) and _cell_safe_border((nx, ny + 1, nz), reader):
                                    if drop_d <= 3:
                                        step_cost = cur_cost + 1.35 + 0.25 * drop_d
                                        if nxt not in expanded and step_cost < g_costs.get(nxt, float("inf")):
                                            g_costs[nxt] = step_cost
                                            parents[nxt] = cur
                                            edge_meta[nxt] = {"mode": "drop", "drop_blocks": drop_d}
                                            heapq.heappush(pq, (step_cost + heuristic(nxt), step_cost, nxt, 0, 0, 0))
                                    elif allow_pillar:
                                        step_cells: List[List[int]] = []
                                        cur_anchor_y = cy
                                        while cur_anchor_y - ny > 3:
                                            mid_floor_y = cur_anchor_y - 3
                                            step_cells.append([nx, mid_floor_y, nz])
                                            cur_anchor_y = mid_floor_y + 1
                                        step_cost = cur_cost + 3.0 + 0.6 * len(step_cells) + 0.2 * drop_d
                                        if nxt not in expanded and step_cost < g_costs.get(nxt, float("inf")):
                                            g_costs[nxt] = step_cost
                                            parents[nxt] = cur
                                            edge_meta[nxt] = {"mode": "scaffold_drop", "drop_blocks": drop_d, "step_cells": step_cells}
                                            heapq.heappush(pq, (step_cost + heuristic(nxt), step_cost, nxt, 0, 0, 0))
                                break

                # Anchored bridge (only when landing exists ahead within remaining max_bridge_steps)
                if allow_bridge and not stepped_floor and bridge_streak < int(max_bridge_steps):
                    nxt = (nx, cy, nz)
                    cur_floor_blk = reader((cx, cy - 1, cz))
                    prev_parent = parents.get(cur)
                    prev_mode = edge_meta.get(cur, {}).get("mode")
                    same_dir = prev_mode == "bridge" and prev_parent is not None and (cx - prev_parent[0], cz - prev_parent[2]) == (dx, dz)
                    if (is_solid_floor(cur_floor_blk) or same_dir) and not world.is_edge_blocked(cur, nxt):
                        max_ahead = max(1, int(max_bridge_steps) - bridge_streak)
                        anchored = False
                        for span in range(1, max_ahead + 1):
                            bx, bz = cx + span * dx, cz + span * dz
                            if abs(bx - ax) > arad or abs(bz - az) > arad:
                                break
                            bf = reader((bx, cy, bz))
                            bh = reader((bx, cy + 1, bz))
                            bfl = reader((bx, cy - 1, bz))
                            bfl_name = _block_name(bfl)
                            if not (
                                is_passable(bf, allow_cobweb=allow_cobweb)
                                and is_passable(bh, allow_cobweb=allow_cobweb)
                                and not is_lava_like(bf)
                                and not is_water_like(bf)
                                and not is_lava_like(bh)
                                and not is_water_like(bh)
                                and _cell_safe_border_ignore_floor((bx, cy, bz), reader)
                                and _cell_safe_border((bx, cy + 1, bz), reader)
                                and bfl_name not in {"not_loaded", "unknown", "spawner", "chest_minecart"}
                                and (is_lava_like(bfl) or is_water_like(bfl) or is_passable(bfl))
                            ):
                                break
                            lx, lz = cx + (span + 1) * dx, cz + (span + 1) * dz
                            if abs(lx - ax) <= arad and abs(lz - az) <= arad:
                                for ly_off in (0, 1, -1):
                                    ly = cy + ly_off
                                    if (
                                        is_solid_floor(reader((lx, ly - 1, lz)))
                                        and is_passable(reader((lx, ly, lz)), allow_cobweb=allow_cobweb)
                                        and is_passable(reader((lx, ly + 1, lz)), allow_cobweb=allow_cobweb)
                                        and _cell_safe_border((lx, ly, lz), reader)
                                        and _cell_safe_border((lx, ly + 1, lz), reader)
                                    ):
                                        anchored = True
                                        break
                            if anchored:
                                break
                        if anchored:
                            fl_blk = reader((nx, cy - 1, nz))
                            is_lava_bridge = is_lava_like(fl_blk)
                            step_cost = cur_cost + (4.2 if is_lava_bridge else 3.2)
                            if nxt not in expanded and step_cost < g_costs.get(nxt, float("inf")):
                                g_costs[nxt] = step_cost
                                parents[nxt] = cur
                                edge_meta[nxt] = {"mode": "bridge", "bridge_cells": [[nx, cy - 1, nz]], "over_lava": is_lava_bridge}
                                heapq.heappush(pq, (step_cost + heuristic(nxt), step_cost, nxt, 0, 0, bridge_streak + 1))

            # Pillar up onto >=2-block climbable cliff or toward higher goal
            if allow_pillar and pillar_h < int(max_pillar_height):
                up_node = (cx, cy + 1, cz)
                need_pillar = pillar_h > 0 or (gy > cy + 1 and abs(gx - cx) + abs(gz - cz) <= 4)
                if not need_pillar:
                    for pdx, pdz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        lx, lz = cx + pdx, cz + pdz
                        if is_solid_floor(reader((lx, cy + 1, lz))) and (
                            (
                                is_passable(reader((lx, cy + 2, lz)), allow_cobweb=allow_cobweb)
                                and is_passable(reader((lx, cy + 3, lz)), allow_cobweb=allow_cobweb)
                            )
                            or (
                                is_solid_floor(reader((lx, cy + 2, lz)))
                                and is_passable(reader((lx, cy + 3, lz)), allow_cobweb=allow_cobweb)
                                and is_passable(reader((lx, cy + 4, lz)), allow_cobweb=allow_cobweb)
                            )
                        ):
                            need_pillar = True
                            break
                if need_pillar and not world.is_edge_blocked(cur, up_node) and _cell_safe_border(up_node, reader):
                    head2_cell = (cx, cy + 2, cz)
                    head2_pass = is_passable(reader(head2_cell), allow_cobweb=allow_cobweb)
                    head2_dig = (not head2_pass) and allow_dig and _can_dig_step_cell(head2_cell, reader)
                    if head2_pass or head2_dig:
                        foot_above = reader(up_node)
                        if is_passable(foot_above, allow_cobweb=allow_cobweb) or _block_name(foot_above) in AIR:
                            step_cost = cur_cost + (5.5 if head2_pass else 8.0)
                            if up_node not in expanded and step_cost < g_costs.get(up_node, float("inf")):
                                g_costs[up_node] = step_cost
                                parents[up_node] = cur
                                edge_meta[up_node] = {
                                    "mode": "pillar",
                                    "pillar_steps": 1,
                                    "dig_cells": [list(head2_cell)] if head2_dig else [],
                                }
                                heapq.heappush(pq, (step_cost + heuristic(up_node), step_cost, up_node, pillar_h + 1, dig_streak, 0))
        return []

    @classmethod
    def find_ore_vantage_path(
        cls,
        world: VoxelWorld3D,
        start_foot: Sequence[int],
        ore_pos: Sequence[int],
        read_fn: Optional[Callable[[Sequence[int]], Dict[str, Any]]] = None,
        *,
        eye_radius: float = 4.5,
        anchor_pos: Optional[Sequence[int]] = None,
        anchor_radius: Optional[int] = None,
        allow_pillar: bool = True,
        allow_dig: bool = True,
        allow_bridge: bool = True,
        allow_drop: bool = True,
        allow_cobweb: bool = False,
        max_nodes: int = 1600,
    ) -> Optional[Tuple[List[int], List[Dict[str, Any]]]]:
        """Find best reachable walkable stance adjacent/below/around an exposed ore block."""
        ox, oy, oz = norm_coord(ore_pos)
        sx, sy, sz = norm_coord(start_foot)

        def reader(pos: Tuple[int, int, int]) -> Dict[str, Any]:
            if read_fn is None:
                return world.get_block_cached(pos) or {"name": "unknown", "properties": {}}
            return world.read_block(pos, read_fn)

        vantages: List[Tuple[float, Coord3D]] = []
        for dx in range(-3, 4):
            for dz in range(-3, 4):
                if abs(dx) + abs(dz) > 4:
                    continue
                for dy in range(-5, 3):
                    vx, vy, vz = ox + dx, oy + dy, oz + dz
                    dist = math.sqrt((ox - vx) ** 2 + (oy + 0.5 - (vy + 1.62)) ** 2 + (oz - vz) ** 2)
                    if dist > float(eye_radius):
                        continue
                    if oy > vy + 2:
                        feet_corner_dist = math.sqrt((ox - (vx + 0.5)) ** 2 + (oy - vy) ** 2 + (oz - (vz + 0.5)) ** 2)
                        if feet_corner_dist > 3.95:
                            continue
                    if (
                        is_solid_floor(reader((vx, vy - 1, vz)))
                        and is_passable(reader((vx, vy, vz)), allow_cobweb=allow_cobweb)
                        and is_passable(reader((vx, vy + 1, vz)), allow_cobweb=allow_cobweb)
                        and _cell_safe_border((vx, vy, vz), reader)
                        and _cell_safe_border((vx, vy + 1, vz), reader)
                    ):
                        cost_est = abs(vx - sx) + 1.4 * abs(vy - sy) + abs(vz - sz) + 0.25 * dist
                        vantages.append((cost_est, (vx, vy, vz)))

        vantages.sort(key=lambda pair: pair[0])
        for _, vpos in vantages[:6]:
            if vpos == (sx, sy, sz):
                return list(vpos), []
            path = cls.find_path(
                world,
                start_foot,
                vpos,
                read_fn,
                goal_tolerance=0,
                allow_pillar=allow_pillar,
                allow_dig=allow_dig,
                allow_bridge=allow_bridge,
                allow_drop=allow_drop,
                allow_cobweb=allow_cobweb,
                anchor_pos=anchor_pos,
                anchor_radius=anchor_radius,
                max_nodes=max_nodes,
            )
            if path:
                return list(vpos), path
        return None
