"""L1 Pathfinder3D: cached 3D A* with walk, micro-dig, pillar-up, bridge, and avoid_block_breaking."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from v2.domain import (
    AIR_BLOCKS,
    Coord3,
    GRAVITY_BLOCKS,
    HAZARD_BLOCKS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    PASS_THROUGH_BLOCKS,
    SCAFFOLD_BLOCKS,
    SOFT_OCCLUDERS,
    WATER_BLOCKS,
    dist3,
)
from v2.gateway.world_proxy import VirtualWorldGateway


MICRO_DIG_ALLOWED = SOFT_OCCLUDERS | frozenset({
    "stone", "cobblestone", "deepslate", "netherrack", "sandstone", "clay", "gravel", "sand", "red_sand", "mud",
})


@dataclass(frozen=True)
class NavStep:
    action: str  # "walk" | "dig" | "pillar_up" | "bridge" | "swim" | "climb_shore"
    target: Coord3
    stand_after: Coord3
    extra: Optional[Coord3] = None


class Pathfinder3D:
    """Bounded 3D A* over VirtualWorldGateway supporting obstacles, pits, and gaps."""

    def __init__(self, gateway: VirtualWorldGateway) -> None:
        self.gateway = gateway

    def plan(
        self,
        dimension: str,
        start: Coord3,
        goal: Coord3,
        *,
        tolerance: float = 2.0,
        has_scaffold: bool = False,
        allow_dig: bool = True,
        allow_pillar: bool = True,
        allow_bridge: bool = True,
        avoid_block_breaking: Optional[Callable[[Coord3], bool]] = None,
        threat_positions: Optional[Sequence[Tuple[float, float, float]]] = None,
        threat_avoid_radius: float = 10.0,
        threat_repulsion_weight: float = 14.0,
        max_expansions: int = 800,
    ) -> Optional[List[NavStep]]:
        """Find a short 3D step sequence from start to within `tolerance` of goal."""
        gw = self.gateway
        avoid_fn = avoid_block_breaking or (lambda _c: False)
        threats = [tuple(float(v) for v in tp) for tp in (threat_positions or ()) if len(tp) == 3]
        avoid_r = max(1.0, float(threat_avoid_radius))
        repulse_w = max(0.0, float(threat_repulsion_weight))

        def threat_penalty(c: Coord3) -> float:
            if not threats or repulse_w <= 0.0:
                return 0.0
            cx_f, cy_f, cz_f = c[0] + 0.5, float(c[1]), c[2] + 0.5
            pen = 0.0
            for tx, ty, tz in threats:
                d = math.dist((cx_f, cy_f, cz_f), (tx, ty, tz))
                if d < avoid_r:
                    ratio = (avoid_r - d) / avoid_r
                    pen += repulse_w * (ratio * ratio)
                    if d < 4.5:
                        pen += 8.0
            return pen

        # Dynamic start-in-fluid relaxation: widen corridor & lower swim cost when starting in water
        start_entry = gw.peek_block(dimension, start)
        if start_entry is None:
            start_entry = gw.get_block(dimension, start, max_age=30.0)
        start_in_water = start_entry is not None and start_entry.name in WATER_BLOCKS
        water_swim_cost = 1.6 if start_in_water else 3.2

        # Prefetch a compact local 3D corridor (bounded read budget; warm cache hits avoid HTTP reads)
        dx = goal[0] - start[0]
        dy = goal[1] - start[1]
        dz = goal[2] - start[2]
        dist = max(1.0, math.sqrt(dx * dx + dy * dy + dz * dz))
        steps_preview = min(9, int(math.ceil(dist)))
        probe_coords: List[Coord3] = []
        corridor_xz = 4 if (threats or start_in_water) else 2
        for s in range(0, steps_preview + 1):
            t = s / max(1, steps_preview)
            cx = int(round(start[0] + dx * min(1.0, t * (steps_preview / dist))))
            cy = int(round(start[1] + dy * min(1.0, t * (steps_preview / dist))))
            cz = int(round(start[2] + dz * min(1.0, t * (steps_preview / dist))))
            for ox in range(-corridor_xz, corridor_xz + 1):
                for oz in range(-corridor_xz, corridor_xz + 1):
                    for oy in (-1, 0, 1, 2):
                        probe_coords.append((cx + ox, cy + oy, cz + oz))
        local_xz = 4 if start_in_water else 3
        for ox in range(-local_xz, local_xz + 1):
            for oz in range(-local_xz, local_xz + 1):
                for oy in (-2, -1, 0, 1, 2, 3):
                    probe_coords.append((start[0] + ox, start[1] + oy, start[2] + oz))
        gw.prefetch_blocks(dimension, probe_coords)

        lazy_fetches = 0

        def block_name(c: Coord3) -> Optional[str]:
            nonlocal lazy_fetches
            e = gw.peek_block(dimension, c)
            if e is None and lazy_fetches < 48:
                lazy_fetches += 1
                e = gw.get_block(dimension, c, max_age=30.0)
            return e.name if e is not None else None

        def is_water(c: Coord3) -> bool:
            nm = block_name(c)
            return nm is not None and nm in WATER_BLOCKS

        def is_safe_solid(c: Coord3) -> bool:
            nm = block_name(c)
            return nm is not None and nm not in NON_SOLID_BLOCKS and nm not in HAZARD_BLOCKS

        def is_safe_air(c: Coord3) -> bool:
            nm = block_name(c)
            return nm is not None and nm in PASS_THROUGH_BLOCKS and nm not in HAZARD_BLOCKS

        def is_water_or_air(c: Coord3) -> bool:
            nm = block_name(c)
            return nm is not None and (nm in WATER_BLOCKS or (nm in PASS_THROUGH_BLOCKS and nm not in HAZARD_BLOCKS))

        def can_dig_cell(c: Coord3) -> bool:
            if not allow_dig or avoid_fn(c) or gw.unreachable.is_blocked(dimension, c, "dig"):
                return False
            nm = block_name(c)
            if nm is None or nm not in MICRO_DIG_ALLOWED:
                return False
            # Never dig directly under lava or liquid
            above = block_name((c[0], c[1] + 1, c[2]))
            if above in LIQUID_BLOCKS:
                return False
            return True

        def heuristic(c: Coord3) -> float:
            return dist3(c, goal)

        if heuristic(start) <= tolerance:
            return []

        open_heap: List[Tuple[float, float, Coord3]] = [(heuristic(start), 0.0, start)]
        g_score: Dict[Coord3, float] = {start: 0.0}
        came_from: Dict[Coord3, Tuple[Coord3, NavStep]] = {}
        expansions = 0

        while open_heap and expansions < max_expansions:
            _, cur_g, cur = heapq.heappop(open_heap)
            if cur_g > g_score.get(cur, math.inf) + 1e-6:
                continue
            expansions += 1
            if heuristic(cur) <= tolerance:
                path: List[NavStep] = []
                node = cur
                while node in came_from:
                    prev, step = came_from[node]
                    path.append(step)
                    node = prev
                path.reverse()
                return path

            cx, cy, cz = cur
            cur_in_water = is_water(cur)
            neighbors: List[Tuple[Coord3, NavStep, float]] = []

            if cur_in_water:
                # (a) Vertical buoyant ascent so submerged bots surface to reach climb_shore
                if is_water_or_air((cx, cy + 1, cz)) and is_water_or_air((cx, cy + 2, cz)):
                    nxt = (cx, cy + 1, cz)
                    if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                        neighbors.append((nxt, NavStep("swim", nxt, nxt), 1.2))
            elif allow_pillar and has_scaffold and is_safe_air((cx, cy + 2, cz)) and is_safe_solid((cx, cy - 1, cz)):
                # 1) Pillar up when stuck in pit or goal is higher (higher cost than step-up dig so staircase ramps are preferred)
                nxt = (cx, cy + 1, cz)
                if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                    neighbors.append((nxt, NavStep("pillar_up", cur, nxt), 5.5))

            # 2) Cardinal moves: swim / climb_shore / walk / step-up / drop / micro-dig / bridge
            for ox, oz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = cx + ox, cz + oz

                if cur_in_water:
                    # (b) Horizontal water swim moves or same-level shore step
                    if is_water((nx, cy, nz)) and is_water_or_air((nx, cy + 1, nz)):
                        nxt = (nx, cy, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            neighbors.append((nxt, NavStep("swim", nxt, nxt), water_swim_cost))
                    elif is_safe_solid((nx, cy - 1, nz)) and is_safe_air((nx, cy, nz)) and is_safe_air((nx, cy + 1, nz)):
                        nxt = (nx, cy, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            neighbors.append((nxt, NavStep("swim", nxt, nxt), 1.8))
                    elif is_safe_air((nx, cy, nz)) and is_safe_air((nx, cy + 1, nz)) and is_water((nx, cy - 1, nz)):
                        nxt = (nx, cy - 1, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            neighbors.append((nxt, NavStep("swim", nxt, nxt), water_swim_cost + 0.2))

                    # (c) Diagonal climb_shore from water up onto safe solid bank
                    if (
                        is_safe_solid((nx, cy, nz))
                        and is_water_or_air((cx, cy + 1, cz))
                        and is_safe_air((cx, cy + 2, cz))
                        and is_safe_air((nx, cy + 1, nz))
                        and is_safe_air((nx, cy + 2, nz))
                    ):
                        nxt = (nx, cy + 1, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            neighbors.append((nxt, NavStep("climb_shore", nxt, nxt), 2.0))
                else:
                    # Same level walk
                    if is_safe_solid((nx, cy - 1, nz)) and is_safe_air((nx, cy, nz)) and is_safe_air((nx, cy + 1, nz)):
                        nxt = (nx, cy, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            neighbors.append((nxt, NavStep("walk", nxt, nxt), 1.0))

                    # Step up +1 (guard against mid-air double-jump directly over water)
                    elif (
                        not is_water((cx, cy - 1, cz))
                        and is_safe_solid((nx, cy, nz))
                        and is_safe_air((cx, cy + 2, cz))
                        and is_safe_air((nx, cy + 1, nz))
                        and is_safe_air((nx, cy + 2, nz))
                    ):
                        nxt = (nx, cy + 1, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            neighbors.append((nxt, NavStep("walk", nxt, nxt), 1.4))

                    # Same-level dry-to-water entry (high cost so dry bots route around puddles)
                    elif is_water((nx, cy, nz)) and is_water_or_air((nx, cy + 1, nz)):
                        nxt = (nx, cy, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            neighbors.append((nxt, NavStep("swim", nxt, nxt), 4.5))

                    # Safe drop 1..3 (or drop into water / bridge across gap)
                    elif is_safe_air((nx, cy, nz)) and is_safe_air((nx, cy + 1, nz)):
                        dropped = False
                        for drop in (1, 2, 3):
                            if is_safe_solid((nx, cy - drop - 1, nz)) and is_safe_air((nx, cy - drop, nz)):
                                nxt = (nx, cy - drop, nz)
                                if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                                    neighbors.append((nxt, NavStep("walk", nxt, nxt), 1.3 + 0.2 * drop))
                                dropped = True
                                break
                            if drop <= 2 and is_water((nx, cy - drop, nz)):
                                nxt = (nx, cy - drop, nz)
                                if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                                    neighbors.append((nxt, NavStep("swim", nxt, nxt), 4.5))
                                dropped = True
                                break
                        # Bridge across 1-block gap/water/lava if holding scaffold
                        if not dropped or (allow_bridge and has_scaffold):
                            if allow_bridge and has_scaffold:
                                below_nm = block_name((nx, cy - 1, nz))
                                if below_nm in (AIR_BLOCKS | LIQUID_BLOCKS):
                                    nxt = (nx, cy, nz)
                                    if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                                        neighbors.append((nxt, NavStep("bridge", (nx, cy - 1, nz), nxt), 3.5))

                # Micro-dig through 1x2 barrier (leaves/dirt/wall)
                if allow_dig and is_safe_solid((nx, cy - 1, nz)):
                    need_dig_low = not is_safe_air((nx, cy, nz))
                    need_dig_high = not is_safe_air((nx, cy + 1, nz))
                    if (need_dig_low or need_dig_high) and (not need_dig_low or can_dig_cell((nx, cy, nz))) and (not need_dig_high or can_dig_cell((nx, cy + 1, nz))):
                        dig_target = (nx, cy + 1, nz) if need_dig_high else (nx, cy, nz)
                        extra = (nx, cy, nz) if (need_dig_high and need_dig_low) else None
                        nxt = (nx, cy, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            cost = 2.6 + (1.0 if extra is not None else 0.0)
                            neighbors.append((nxt, NavStep("dig", dig_target, nxt, extra=extra), cost))

                # Step-up +1 micro-dig (climb out of pits/caves/riverbanks via staircase without pillar blocked_by_self!)
                if allow_dig and is_safe_solid((nx, cy, nz)) and (not cur_in_water or is_water_or_air((cx, cy + 1, cz))):
                    c_head = (cx, cy + 2, cz)
                    n_low = (nx, cy + 1, nz)
                    n_high = (nx, cy + 2, nz)
                    need_cells = [c for c in (c_head, n_high, n_low) if not is_safe_air(c)]
                    if need_cells and all(can_dig_cell(c) for c in need_cells):
                        nxt = (nx, cy + 1, nz)
                        if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                            extra = need_cells[1] if len(need_cells) > 1 else None
                            cost = 2.8 + 0.9 * len(need_cells)
                            neighbors.append((nxt, NavStep("dig", need_cells[0], nxt, extra=extra), cost))

            for nxt, step, step_cost in neighbors:
                tentative_g = cur_g + step_cost + threat_penalty(nxt)
                if tentative_g < g_score.get(nxt, math.inf):
                    g_score[nxt] = tentative_g
                    came_from[nxt] = (cur, step)
                    heapq.heappush(open_heap, (tentative_g + heuristic(nxt), tentative_g, nxt))

        return None
