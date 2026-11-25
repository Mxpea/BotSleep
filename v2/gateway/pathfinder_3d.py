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
    SCAFFOLD_BLOCKS,
    SOFT_OCCLUDERS,
    dist3,
)
from v2.gateway.world_proxy import VirtualWorldGateway


MICRO_DIG_ALLOWED = SOFT_OCCLUDERS | frozenset({
    "stone", "cobblestone", "deepslate", "netherrack", "sandstone", "clay", "gravel", "sand",
})


@dataclass(frozen=True)
class NavStep:
    action: str  # "walk" | "dig" | "pillar_up" | "bridge"
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
        max_expansions: int = 800,
    ) -> Optional[List[NavStep]]:
        """Find a short 3D step sequence from start to within `tolerance` of goal."""
        gw = self.gateway
        avoid_fn = avoid_block_breaking or (lambda _c: False)

        # Prefetch a local 3D corridor wide enough for lateral detours around obstacles/walls
        dx = goal[0] - start[0]
        dy = goal[1] - start[1]
        dz = goal[2] - start[2]
        dist = max(1.0, math.sqrt(dx * dx + dy * dy + dz * dz))
        steps_preview = min(14, int(math.ceil(dist)))
        probe_coords: List[Coord3] = []
        for s in range(0, steps_preview + 1):
            t = s / max(1, steps_preview)
            cx = int(round(start[0] + dx * min(1.0, t * (steps_preview / dist))))
            cy = int(round(start[1] + dy * min(1.0, t * (steps_preview / dist))))
            cz = int(round(start[2] + dz * min(1.0, t * (steps_preview / dist))))
            for ox in range(-2, 3):
                for oz in range(-3, 4):
                    for oy in (-1, 0, 1, 2):
                        probe_coords.append((cx + ox, cy + oy, cz + oz))
        gw.prefetch_blocks(dimension, probe_coords)

        def block_name(c: Coord3) -> Optional[str]:
            e = gw.peek_block(dimension, c)
            return e.name if e is not None else None

        def is_safe_solid(c: Coord3) -> bool:
            nm = block_name(c)
            return nm is not None and nm not in NON_SOLID_BLOCKS and nm not in HAZARD_BLOCKS

        def is_safe_air(c: Coord3) -> bool:
            nm = block_name(c)
            return nm is not None and nm in NON_SOLID_BLOCKS and nm not in HAZARD_BLOCKS and nm != "lava"

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
            neighbors: List[Tuple[Coord3, NavStep, float]] = []

            # 1) Pillar up when stuck in pit or goal is higher
            if allow_pillar and has_scaffold and is_safe_air((cx, cy + 2, cz)) and is_safe_solid((cx, cy - 1, cz)):
                nxt = (cx, cy + 1, cz)
                if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                    neighbors.append((nxt, NavStep("pillar_up", cur, nxt), 3.2))

            # 2) Cardinal moves: walk / step-up / drop / micro-dig / bridge
            for ox, oz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = cx + ox, cz + oz

                # Same level walk
                if is_safe_solid((nx, cy - 1, nz)) and is_safe_air((nx, cy, nz)) and is_safe_air((nx, cy + 1, nz)):
                    nxt = (nx, cy, nz)
                    if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                        neighbors.append((nxt, NavStep("walk", nxt, nxt), 1.0))

                # Step up +1
                elif (
                    is_safe_solid((nx, cy, nz))
                    and is_safe_air((cx, cy + 2, cz))
                    and is_safe_air((nx, cy + 1, nz))
                    and is_safe_air((nx, cy + 2, nz))
                ):
                    nxt = (nx, cy + 1, nz)
                    if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                        neighbors.append((nxt, NavStep("walk", nxt, nxt), 1.4))

                # Safe drop 1..3
                elif is_safe_air((nx, cy, nz)) and is_safe_air((nx, cy + 1, nz)):
                    dropped = False
                    for drop in (1, 2, 3):
                        if is_safe_solid((nx, cy - drop - 1, nz)) and is_safe_air((nx, cy - drop, nz)):
                            nxt = (nx, cy - drop, nz)
                            if not gw.unreachable.is_edge_blocked(dimension, cur, nxt):
                                neighbors.append((nxt, NavStep("walk", nxt, nxt), 1.3 + 0.2 * drop))
                            dropped = True
                            break
                    # Bridge across 1-block gap/water/lava if holding scaffold
                    if not dropped and allow_bridge and has_scaffold:
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

            for nxt, step, step_cost in neighbors:
                tentative_g = cur_g + step_cost
                if tentative_g < g_score.get(nxt, math.inf):
                    g_score[nxt] = tentative_g
                    came_from[nxt] = (cur, step)
                    heapq.heappush(open_heap, (tentative_g + heuristic(nxt), tentative_g, nxt))

        return None
