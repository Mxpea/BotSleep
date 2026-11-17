"""Pure bounded cave probing, classification, safe walkable-neighbor, and ore-exposure helpers."""

from collections import deque
import heapq
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from core.domain import AIR
from core.mining_targets import evaluate_ore_block
from core.structure_spec import (
    CAVE_DIGGABLE_BLOCKS,
    CAVE_LIMITS,
    LAVA_LIKE_BLOCKS,
    MINESHAFT_MARKER_BLOCKS,
    MINE_HAZARD_BLOCKS,
    MINE_TORCH_BLOCKS,
    ORE_DROP_MAP,
    WATER_LIKE_BLOCKS,
)

PASSABLE_NON_AIR: frozenset = frozenset(
    set(MINE_TORCH_BLOCKS)
    | {
        "rail",
        "powered_rail",
        "detector_rail",
        "activator_rail",
        "short_grass",
        "tall_grass",
        "fern",
        "large_fern",
        "glow_lichen",
        "sculk_vein",
        "moss_carpet",
        "snow",
    }
)
FALLING_BLOCKS: frozenset = frozenset({"sand", "red_sand", "gravel"})
NON_SOLID_FLOOR_BLOCKS: frozenset = frozenset(
    set(AIR)
    | set(WATER_LIKE_BLOCKS)
    | set(LAVA_LIKE_BLOCKS)
    | set(PASSABLE_NON_AIR)
    | {"cobweb", "not_loaded", "unknown", "chest_minecart"}
)


def _block_name(blk: Mapping[str, Any]) -> str:
    return str(blk.get("name") or "unknown")


def is_water_like(blk: Mapping[str, Any]) -> bool:
    name = _block_name(blk)
    props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
    return name in WATER_LIKE_BLOCKS or props.get("waterlogged") in (True, "true")


def is_lava_like(blk: Mapping[str, Any]) -> bool:
    return _block_name(blk) in LAVA_LIKE_BLOCKS


def is_passable(blk: Mapping[str, Any], *, allow_cobweb: bool = False) -> bool:
    if is_water_like(blk) or is_lava_like(blk):
        return False
    name = _block_name(blk)
    if name in AIR or name in PASSABLE_NON_AIR:
        return True
    if allow_cobweb and name == "cobweb":
        return True
    return False


def is_solid_floor(blk: Mapping[str, Any]) -> bool:
    if is_water_like(blk) or is_lava_like(blk):
        return False
    name = _block_name(blk)
    if name in NON_SOLID_FLOOR_BLOCKS or name in MINE_HAZARD_BLOCKS:
        return False
    return True


def _cached_reader(
    read_block: Callable[[List[int]], Dict[str, Any]],
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]],
) -> Callable[[Tuple[int, int, int]], Dict[str, Any]]:
    cache = block_cache if block_cache is not None else {}

    def _read(pos: Sequence[int]) -> Dict[str, Any]:
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key not in cache:
            raw = read_block([key[0], key[1], key[2]])
            cache[key] = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        return cache[key]

    return _read


def probe_cavity(
    origin_air: Sequence[int],
    entry_foot: Sequence[int],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    limits: Optional[Mapping[str, Any]] = None,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
    blocked_backtracks: Optional[Sequence[Sequence[int]]] = None,
) -> Dict[str, Any]:
    """Run a bounded 3D flood fill from an exposed cavity block and compute height/drop/hazards."""
    cfg = dict(CAVE_LIMITS)
    if limits:
        cfg.update(limits)

    max_cells = max(1, int(cfg["flood_max_cells"]))
    horiz_r = max(1, int(cfg["horizontal_radius"]))
    vert_span = max(1, int(cfg["vertical_span"]))

    ox, oy, oz = int(origin_air[0]), int(origin_air[1]), int(origin_air[2])
    entry_y = int(entry_foot[1])
    reader = _cached_reader(read_block, block_cache)
    backtrack_set = {
        (int(p[0]), int(p[1]), int(p[2]))
        for p in (blocked_backtracks or ())
    }

    start_pos = (ox, oy, oz)
    start_blk = reader(start_pos)

    visited_open: Set[Tuple[int, int, int]] = set()
    water_positions: Set[Tuple[int, int, int]] = set()
    lava_positions: Set[Tuple[int, int, int]] = set()
    spawner_positions: Set[Tuple[int, int, int]] = set()
    marker_positions: Set[Tuple[int, int, int]] = set()
    checked_shell: Set[Tuple[int, int, int]] = set()
    column_open_ys: Dict[Tuple[int, int], List[int]] = {}
    floor_ys: List[int] = []
    hit_limit = False

    def inspect_neighbor(npos: Tuple[int, int, int]) -> Dict[str, Any]:
        nblk = reader(npos)
        if npos not in checked_shell:
            checked_shell.add(npos)
            nname = _block_name(nblk)
            if is_lava_like(nblk):
                lava_positions.add(npos)
            elif nname == "spawner":
                spawner_positions.add(npos)
            elif is_water_like(nblk):
                water_positions.add(npos)
            if nname in MINESHAFT_MARKER_BLOCKS:
                marker_positions.add(npos)
        return nblk

    inspect_neighbor(start_pos)
    queue: deque = deque()
    if is_passable(start_blk, allow_cobweb=True):
        visited_open.add(start_pos)
        column_open_ys.setdefault((ox, oz), []).append(oy)
        queue.append(start_pos)

    while queue:
        cx, cy, cz = queue.popleft()
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nx, ny, nz = cx + dx, cy + dy, cz + dz
            npos = (nx, ny, nz)
            if npos in backtrack_set:
                continue
            nblk = inspect_neighbor(npos)
            if abs(nx - ox) > horiz_r or abs(nz - oz) > horiz_r or abs(ny - oy) > vert_span:
                if is_passable(nblk, allow_cobweb=True):
                    hit_limit = True
                continue
            if npos in visited_open:
                continue
            if is_passable(nblk, allow_cobweb=True):
                if len(visited_open) >= max_cells:
                    hit_limit = True
                    continue
                visited_open.add(npos)
                column_open_ys.setdefault((nx, nz), []).append(ny)
                queue.append(npos)

    # Measure maximum contiguous vertical air span ("height") and landing drop
    max_height = 0
    for (col_x, col_z), ys in column_open_ys.items():
        ys_sorted = sorted(set(ys))
        run = 0
        prev_y: Optional[int] = None
        for y in ys_sorted:
            if prev_y is not None and y == prev_y + 1:
                run += 1
            else:
                run = 1
            if run > max_height:
                max_height = run
            prev_y = y

            below = inspect_neighbor((col_x, y - 1, col_z))
            above = inspect_neighbor((col_x, y + 1, col_z))
            if is_solid_floor(below) and is_passable(above, allow_cobweb=True):
                floor_ys.append(y)

    # If a column opened downward beyond vertical_span with no solid floor inside window, count the open drop.
    min_open_y = min((p[1] for p in visited_open), default=entry_y)
    if floor_ys:
        drop = max(0, entry_y - min(floor_ys))
        if min_open_y == oy - vert_span and (entry_y - min_open_y) > drop:
            drop = max(drop, entry_y - min_open_y)
    else:
        drop = max(0, entry_y - min_open_y)

    hazards = sorted(lava_positions | spawner_positions | water_positions)
    return {
        "origin": [ox, oy, oz],
        "entry_foot": [int(entry_foot[0]), entry_y, int(entry_foot[2])],
        "air_count": len(visited_open),
        "height": max_height,
        "drop": drop,
        "water_count": len(water_positions),
        "lava_found": len(lava_positions) > 0,
        "spawner_found": len(spawner_positions) > 0,
        "mineshaft_markers": len(marker_positions),
        "hit_limit": hit_limit,
        "hazard_positions": [list(p) for p in hazards],
        "visited_cells": [list(p) for p in visited_open],
    }


def classify_cavity(
    report: Mapping[str, Any],
    *,
    torch_count: int = 0,
    min_enter_torches: Optional[int] = None,
    limits: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify a CavityReport according to Plan v2 safety and enterability rules."""
    cfg = dict(CAVE_LIMITS)
    if limits:
        cfg.update(limits)
    required_torches = int(cfg.get("min_enter_torches", 0) if min_enter_torches is None else min_enter_torches)

    if report.get("lava_found"):
        return {"decision": "unsafe", "kind": "lava", "reason": "Lava detected in cavity"}
    if report.get("spawner_found"):
        return {"decision": "unsafe", "kind": "spawner", "reason": "Monster spawner detected in cavity"}
    if int(report.get("water_count", 0)) >= int(cfg["max_water_blocks"]):
        return {"decision": "unsafe", "kind": "water_cave", "reason": "Too many water-like blocks"}

    air_count = int(report.get("air_count", 0))
    height = int(report.get("height", 0))
    drop = int(report.get("drop", 0))

    if air_count <= int(cfg["small_bubble_max_air"]) and drop < int(cfg["max_drop"]):
        return {"decision": "small_bubble", "kind": "small_bubble", "reason": "Tiny air pocket; patch and continue"}

    markers = int(report.get("mineshaft_markers", 0))
    if markers >= int(cfg["min_mineshaft_markers"]):
        if int(torch_count) < required_torches:
            return {"decision": "unsafe", "kind": "missing_torch", "reason": "Insufficient torches to enter mineshaft"}
        return {"decision": "enter", "kind": "mineshaft", "reason": "Mineshaft structure detected and clear of hazards"}

    if height >= int(cfg["max_height"]):
        return {"decision": "unsafe", "kind": "tall_cave", "reason": f"Cavity height {height} >= {cfg['max_height']}"}
    if drop >= int(cfg["max_drop"]):
        return {"decision": "unsafe", "kind": "deep_drop", "reason": f"Cavity drop {drop} >= {cfg['max_drop']}"}

    if int(torch_count) < required_torches:
        return {"decision": "unsafe", "kind": "missing_torch", "reason": "Insufficient torches to enter cave"}
    return {"decision": "enter", "kind": "small", "reason": "Safe low cave within height and drop bounds"}


def _lateral_clearance(
    pos: Sequence[int],
    reader: Callable[[Tuple[int, int, int]], Dict[str, Any]],
) -> int:
    """Internal center-scoring using an already-cached tuple reader."""
    x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
    score = 0
    axis_open = [0, 0]  # X axis openness, Z axis openness
    for idx, (dx, dz) in enumerate(((1, 0), (-1, 0), (0, 1), (0, -1))):
        open1 = (
            is_passable(reader((x + dx, y, z + dz)))
            and is_passable(reader((x + dx, y + 1, z + dz)))
        )
        if open1:
            score += 2
            axis_open[idx // 2] += 1
            if is_passable(reader((x + 2 * dx, y, z + 2 * dz))) and is_passable(reader((x + 2 * dx, y + 1, z + 2 * dz))):
                score += 1
        elif is_solid_floor(reader((x + dx, y, z + dz))):
            score -= 1
    if axis_open[0] == 2:
        score += 3
    if axis_open[1] == 2:
        score += 3
    return score


def lateral_air_clearance(
    pos: Sequence[int],
    read_block: Callable[[Any], Dict[str, Any]],
    *,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
) -> int:
    """Score how centered pos is in an open cave corridor/chamber (higher = more open space around bot)."""
    reader = _cached_reader(read_block, block_cache)
    return _lateral_clearance(pos, reader)


def _cell_safe_border_ignore_floor(
    pos: Tuple[int, int, int],
    reader: Callable[[Tuple[int, int, int]], Dict[str, Any]],
) -> bool:
    for dx, dy, dz in ((0, 0, 0), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, -1)):
        blk = reader((pos[0] + dx, pos[1] + dy, pos[2] + dz))
        name = _block_name(blk)
        if name in {"not_loaded", "unknown", "spawner", "chest_minecart"}:
            return False
        if is_lava_like(blk) or is_water_like(blk):
            return False
    return True


def _cell_safe_border(
    pos: Tuple[int, int, int],
    reader: Callable[[Tuple[int, int, int]], Dict[str, Any]],
) -> bool:
    for dx, dy, dz in ((0, 0, 0), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        blk = reader((pos[0] + dx, pos[1] + dy, pos[2] + dz))
        name = _block_name(blk)
        if name in {"not_loaded", "unknown", "spawner", "chest_minecart"}:
            return False
        if is_lava_like(blk) or is_water_like(blk):
            return False
    return True


def walkable_neighbors(
    pos: Sequence[int],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    allow_cobweb: bool = False,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
) -> List[List[int]]:
    """Return cardinal adjacent foot cells with solid floor, >=2 headroom, |dy|<=1, and no hazards."""
    cx, cy, cz = int(pos[0]), int(pos[1]), int(pos[2])
    reader = _cached_reader(read_block, block_cache)
    out: List[List[int]] = []

    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, nz = cx + dx, cz + dz
        for dy in (0, 1, -1):
            ny = cy + dy
            floor_blk = reader((nx, ny - 1, nz))
            foot_blk = reader((nx, ny, nz))
            head_blk = reader((nx, ny + 1, nz))
            if not is_solid_floor(floor_blk):
                continue
            if not (is_passable(foot_blk, allow_cobweb=allow_cobweb) and is_passable(head_blk, allow_cobweb=allow_cobweb)):
                continue
            # Require vanilla step headroom clearance when changing elevation
            if dy == 1 and not is_passable(reader((cx, cy + 2, cz)), allow_cobweb=allow_cobweb):
                continue
            if dy == -1 and not is_passable(reader((nx, cy + 1, nz)), allow_cobweb=allow_cobweb):
                continue
            if not (_cell_safe_border((nx, ny, nz), reader) and _cell_safe_border((nx, ny + 1, nz), reader)):
                continue
            out.append([nx, ny, nz])
            break
    return out


def has_exposed_air_face(
    cell: Sequence[int],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
) -> bool:
    """Return True iff at least one orthogonal neighbor of cell is air/passable."""
    x, y, z = int(cell[0]), int(cell[1]), int(cell[2])
    reader = _cached_reader(read_block, block_cache)
    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        if is_passable(reader((x + dx, y + dy, z + dz)), allow_cobweb=True):
            return True
    return False


def ore_cell_hazard_free(
    cell: Sequence[int],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
) -> bool:
    """Return True if digging cell has no adjacent fluid/spawner and no fluid above falling sand/gravel."""
    x, y, z = int(cell[0]), int(cell[1]), int(cell[2])
    reader = _cached_reader(read_block, block_cache)
    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        nblk = reader((x + dx, y + dy, z + dz))
        nname = _block_name(nblk)
        if nname in {"not_loaded", "unknown", "spawner"} or is_lava_like(nblk) or is_water_like(nblk):
            return False
        if dy == 1 and nname in FALLING_BLOCKS:
            above2 = reader((x, y + 2, z))
            if is_lava_like(above2) or is_water_like(above2):
                return False
    return True


def new_exposed_face_positions(foot_pos: Sequence[int], forward_vec: Sequence[int]) -> List[List[int]]:
    """Return the 8 newly exposed wall/floor/ceiling/front positions for one 1x2 tunnel slice."""
    fx, fy, fz = int(foot_pos[0]), int(foot_pos[1]), int(foot_pos[2])
    vx, vz = int(forward_vec[0]), int(forward_vec[2])
    rx, rz = -vz, vx
    coords = [
        [fx + rx, fy, fz + rz],
        [fx + rx, fy + 1, fz + rz],
        [fx - rx, fy, fz - rz],
        [fx - rx, fy + 1, fz - rz],
        [fx, fy - 1, fz],
        [fx, fy + 2, fz],
        [fx + vx, fy, fz + vz],
        [fx + vx, fy + 1, fz + vz],
    ]
    return coords


def reachable_exposed_ores(
    stance_pos: Sequence[float],
    read_block: Callable[[List[int]], Dict[str, Any]],
    targets: Mapping[str, Mapping[str, Any]],
    *,
    harvested_counts: Mapping[str, int],
    best_tool_tier: int,
    max_dist: float = 4.5,
    scan_radius: Optional[int] = None,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Scan nearby exposed blocks around stance_pos and classify all visible ore candidates."""
    sx, sy, sz = float(stance_pos[0]), float(stance_pos[1]), float(stance_pos[2])
    eye_x = sx + 0.5 if sx == math.floor(sx) else sx
    eye_y = sy + 1.62
    eye_z = sz + 0.5 if sz == math.floor(sz) else sz
    stance_floor = (int(math.floor(sx)), int(math.floor(sy)) - 1, int(math.floor(sz)))

    r = int(scan_radius if scan_radius is not None else math.ceil(float(max_dist)))
    bx, by, bz = int(math.floor(eye_x)), int(math.floor(eye_y)), int(math.floor(eye_z))
    reader = _cached_reader(read_block, block_cache)

    candidates: List[Dict[str, Any]] = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            for dz in range(-r, r + 1):
                cpos = (bx + dx, by + dy, bz + dz)
                if cpos == stance_floor:
                    continue
                dist = math.sqrt((cpos[0] + 0.5 - eye_x) ** 2 + (cpos[1] + 0.5 - eye_y) ** 2 + (cpos[2] + 0.5 - eye_z) ** 2)
                if dist > float(max_dist):
                    continue
                if cpos[1] > int(math.floor(sy)) + 2:
                    feet_corner_dist = math.sqrt((cpos[0] - (bx + 0.5)) ** 2 + (cpos[1] - int(math.floor(sy))) ** 2 + (cpos[2] - (bz + 0.5)) ** 2)
                    if feet_corner_dist > 3.95:
                        continue
                blk = reader(cpos)
                name = _block_name(blk)
                if name not in ORE_DROP_MAP:
                    continue
                if not has_exposed_air_face(cpos, read_block, block_cache=block_cache):
                    continue
                action, target_key, drop_item = evaluate_ore_block(
                    name,
                    targets,
                    harvested_counts=harvested_counts,
                    best_tool_tier=best_tool_tier,
                )
                status = "mined" if action == "mine" else action
                if action == "mine" and not ore_cell_hazard_free(cpos, read_block, block_cache=block_cache):
                    status = "skipped_hazard"
                candidates.append(
                    {
                        "pos": [cpos[0], cpos[1], cpos[2]],
                        "name": name,
                        "target_key": target_key,
                        "drop_item": drop_item,
                        "status": status,
                        "distance": round(dist, 3),
                    }
                )
    candidates.sort(key=lambda row: (row["distance"], row["pos"][1], row["pos"][0], row["pos"][2]))
    return candidates


def _can_dig_step_cell(
    pos: Tuple[int, int, int],
    reader: Callable[[Tuple[int, int, int]], Dict[str, Any]],
) -> bool:
    blk = reader(pos)
    name = _block_name(blk)
    if name not in CAVE_DIGGABLE_BLOCKS and name not in ORE_DROP_MAP:
        return False
    # Never dig if adjacent to liquid, falling block under liquid, or spawner
    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        nb = reader((pos[0] + dx, pos[1] + dy, pos[2] + dz))
        nname = _block_name(nb)
        if is_lava_like(nb) or is_water_like(nb) or nname in {"spawner", "chest_minecart"}:
            return False
    above = _block_name(reader((pos[0], pos[1] + 1, pos[2])))
    if above in FALLING_BLOCKS:
        ab2 = reader((pos[0], pos[1] + 2, pos[2]))
        if is_lava_like(ab2) or is_water_like(ab2):
            return False
    return True


def reconstruct_flood_path(
    parents: Mapping[Tuple[int, int, int], Optional[Tuple[int, int, int]]],
    edge_meta: Mapping[Tuple[int, int, int], Mapping[str, Any]],
    target: Sequence[int],
) -> List[Dict[str, Any]]:
    """Reconstruct ordered traversal steps from start_foot to target cell."""
    cur: Optional[Tuple[int, int, int]] = (int(target[0]), int(target[1]), int(target[2]))
    if cur not in parents:
        return []
    rev: List[Dict[str, Any]] = []
    while cur is not None and parents.get(cur) is not None:
        meta = dict(edge_meta.get(cur) or {"mode": "walk"})
        meta["to"] = [cur[0], cur[1], cur[2]]
        meta["from"] = list(parents[cur])  # type: ignore[arg-type]
        rev.append(meta)
        cur = parents[cur]
    rev.reverse()
    return rev


def flood_safe_cave_graph(
    start_foot: Sequence[int],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    max_nodes: int = 220,
    max_radius: int = 20,
    allow_pillar: bool = True,
    max_pillar_height: int = 3,
    allow_dig: bool = True,
    max_dig_steps: int = 2,
    allow_bridge: bool = False,
    max_bridge_steps: int = 3,
    allow_drop: bool = False,
    max_drop_steps: int = 5,
    allow_cobweb: bool = False,
    targets: Optional[Mapping[str, Mapping[str, Any]]] = None,
    harvested_counts: Optional[Mapping[str, int]] = None,
    best_tool_tier: int = 0,
    eye_radius: float = 4.5,
    visited_cells: Optional[Set[Tuple[int, int, int]]] = None,
    covered_cells: Optional[Set[Tuple[int, int, int]]] = None,
    goal_hints: Optional[Sequence[Sequence[int]]] = None,
    protected_cells: Optional[Set[Tuple[int, int, int]]] = None,
    anchor_pos: Optional[Sequence[int]] = None,
    anchor_radius: Optional[int] = None,
    blocked_edges: Optional[Set[Tuple[Tuple[int, int, int], Tuple[int, int, int]]]] = None,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Flood-fill a safe cave/terrain graph supporting walk, micro-dig, pillar-up, lava/gap bridging, and safe drops.

    Walk edges subtly prefer higher lateral air clearance (cave centerlines) over wall-hugging tiles.
    """
    sx, sy, sz = int(start_foot[0]), int(start_foot[1]), int(start_foot[2])
    start = (sx, sy, sz)
    ax, ay, az = (
        (int(anchor_pos[0]), int(anchor_pos[1]), int(anchor_pos[2]))
        if anchor_pos is not None
        else (sx, sy, sz)
    )
    arad = int(anchor_radius) if anchor_radius is not None else int(max_radius)
    blocked = blocked_edges or set()
    reader = _cached_reader(read_block, block_cache)
    cov_set = covered_cells if covered_cells is not None else visited_cells
    hints = [(int(h[0]), int(h[1]), int(h[2])) for h in (goal_hints or ()) if len(h) == 3]

    costs: Dict[Tuple[int, int, int], float] = {start: 0.0}
    parents: Dict[Tuple[int, int, int], Optional[Tuple[int, int, int]]] = {start: None}
    edge_meta: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    # State tracks (cost, pos, pillar_stack_height, dig_streak, bridge_streak)
    pq: List[Tuple[float, Tuple[int, int, int], int, int, int]] = [(0.0, start, 0, 0, 0)]
    explored: List[Tuple[int, int, int]] = []
    expanded: Set[Tuple[int, int, int]] = set()
    frontiers: List[Tuple[float, List[int]]] = []
    seen_ore_vantages: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    scanned_wall_cells: Dict[Tuple[int, int, int], Optional[Dict[str, Any]]] = {}

    while pq and len(explored) < int(max_nodes):
        cur_cost, cur, pillar_h, dig_streak, bridge_streak = heapq.heappop(pq)
        if cur in expanded:
            continue
        if cur_cost > costs.get(cur, float("inf")) + 1e-6:
            continue
        expanded.add(cur)
        explored.append(cur)

        cx, cy, cz = cur

        # Inspect exposed wall/ceiling shell around this node once per coordinate (r<=2 horizontal, y in -1..+4)
        if targets is not None:
            for ox in (-2, -1, 0, 1, 2):
                for oz in (-2, -1, 0, 1, 2):
                    if abs(ox) + abs(oz) > 3:
                        continue
                    for oy in (-2, -1, 0, 1, 2, 3, 4, 5):
                        okey = (cx + ox, cy + oy, cz + oz)
                        dist = math.sqrt((okey[0] - cx) ** 2 + (okey[1] + 0.5 - (cy + 1.62)) ** 2 + (okey[2] - cz) ** 2)
                        if dist > float(eye_radius):
                            continue
                        if okey[1] > cy + 2:
                            feet_corner_dist = math.sqrt((okey[0] - (cx + 0.5)) ** 2 + (okey[1] - cy) ** 2 + (okey[2] - (cz + 0.5)) ** 2)
                            if feet_corner_dist > 3.95:
                                continue
                        if okey not in scanned_wall_cells:
                            blk = reader(okey)
                            bname = _block_name(blk)
                            if bname in ORE_DROP_MAP and has_exposed_air_face(okey, read_block, block_cache=block_cache):
                                action, target_key, drop_item = evaluate_ore_block(
                                    bname,
                                    targets,
                                    harvested_counts=harvested_counts or {},
                                    best_tool_tier=best_tool_tier,
                                )
                                if action == "mine" and ore_cell_hazard_free(okey, read_block, block_cache=block_cache):
                                    scanned_wall_cells[okey] = {"name": bname, "target_key": target_key, "drop_item": drop_item}
                                else:
                                    scanned_wall_cells[okey] = None
                            else:
                                scanned_wall_cells[okey] = None
                        info = scanned_wall_cells[okey]
                        if info is None:
                            continue
                        score = cur_cost + dist * 0.25
                        prev = seen_ore_vantages.get(okey)
                        if prev is None or score < prev["score"]:
                            seen_ore_vantages[okey] = {
                                "ore_pos": list(okey),
                                "name": info["name"],
                                "target_key": info["target_key"],
                                "drop_item": info["drop_item"],
                                "vantage": [cx, cy, cz],
                                "cost": round(cur_cost, 2),
                                "distance": round(dist, 3),
                                "score": round(score, 2),
                            }

        # Check whether this node borders unexplored 3D walkable/seam/frontier space
        unseen_adj = 0
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, nz = cx + dx, cz + dz
            if abs(nx - sx) > int(max_radius) or abs(nz - sz) > int(max_radius):
                continue
            if abs(nx - ax) > arad or abs(nz - az) > arad:
                continue

            # Pass A: always evaluate ALL free walk heights (0, +1, -1) first so 1-block stairs/ledges
            # are never masked by a diggable block at dy=0 (prevents meaningless pillar_up on 1-block steps).
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
                f_pass = is_passable(f_blk, allow_cobweb=allow_cobweb)
                h_pass = is_passable(h_blk, allow_cobweb=allow_cobweb)
                up_clear = True
                if dy == 1:
                    up_clear = is_passable(reader((cx, cy + 2, cz)), allow_cobweb=allow_cobweb)
                elif dy == -1:
                    up_clear = is_passable(reader((nx, ny + 2, nz)), allow_cobweb=allow_cobweb)

                if f_pass and h_pass and up_clear:
                    if (cur, nxt) in blocked:
                        continue
                    clr = _lateral_clearance(nxt, reader)
                    base_delta = 1.0 if dy == 0 else 1.15
                    revisit_pen = 0.35 if (cov_set is not None and nxt in cov_set) else 0.0
                    step_cost = cur_cost + max(0.75, base_delta - 0.03 * clr) + revisit_pen
                    if nxt not in expanded and step_cost < costs.get(nxt, float("inf")):
                        costs[nxt] = step_cost
                        parents[nxt] = cur
                        edge_meta[nxt] = {"mode": "walk", "clearance": clr}
                        heapq.heappush(pq, (step_cost, nxt, 0, 0, 0))
                    is_unseen = (visited_cells is not None and nxt not in visited_cells) or (
                        cov_set is not None and nxt not in cov_set
                    )
                    if is_unseen:
                        unseen_adj += 1
                    walk_pushed = True
                    break

            # Pass B: only if NO free walk step exists to (nx, nz), try micro-dig through a 1..2 block choke/seam
            if not walk_pushed and allow_dig and dig_streak < int(max_dig_steps):
                for dy in (0, 1, -1):
                    ny = cy + dy
                    nxt = (nx, ny, nz)
                    if not _cell_safe_border(nxt, reader):
                        continue
                    if not is_solid_floor(reader((nx, ny - 1, nz))):
                        continue
                    f_blk = reader((nx, ny, nz))
                    h_blk = reader((nx, ny + 1, nz))
                    f_pass = is_passable(f_blk, allow_cobweb=allow_cobweb)
                    h_pass = is_passable(h_blk, allow_cobweb=allow_cobweb)
                    up_dig_needed: Optional[Tuple[int, int, int]] = None
                    if dy == 1:
                        top_blk = reader((cx, cy + 2, cz))
                        if not is_passable(top_blk, allow_cobweb=allow_cobweb):
                            up_dig_needed = (cx, cy + 2, cz)
                    elif dy == -1:
                        top_blk = reader((nx, ny + 2, nz))
                        if not is_passable(top_blk, allow_cobweb=allow_cobweb):
                            up_dig_needed = (nx, ny + 2, nz)

                    dig_cells: List[Tuple[int, int, int]] = []
                    can_dig_all = True
                    for need_pass, cell_pos in ((h_pass, (nx, ny + 1, nz)), (f_pass, (nx, ny, nz))):
                        if not need_pass:
                            if (protected_cells and cell_pos in protected_cells) or not _can_dig_step_cell(cell_pos, reader):
                                can_dig_all = False
                                break
                            dig_cells.append(cell_pos)
                    if up_dig_needed is not None and can_dig_all:
                        if (protected_cells and up_dig_needed in protected_cells) or not _can_dig_step_cell(up_dig_needed, reader):
                            can_dig_all = False
                        else:
                            dig_cells.insert(0, up_dig_needed)
                    if can_dig_all and 1 <= len(dig_cells) <= 2:
                        # Micro-dig is only for clearing a 1-block overhang/choke (len==1) or punching through a
                        # 1-block stone partition whose far side (nx+dx, ny..ny+1, nz+dz) is already an open 2-high chamber.
                        dig_set = set(dig_cells)
                        if len(dig_cells) == 1:
                            opens_to_air = any(
                                (px, py, pz) not in {(cx, cy, cz), (cx, cy + 1, cz)}
                                and (px, py, pz) not in dig_set
                                and is_passable(reader((px, py, pz)), allow_cobweb=allow_cobweb)
                                for dcell in dig_cells
                                for px, py, pz in (
                                    (dcell[0] + dx, dcell[1], dcell[2] + dz),
                                    (dcell[0], dcell[1] + 1, dcell[2]),
                                    (dcell[0], dcell[1], dcell[2] + 1),
                                    (dcell[0], dcell[1], dcell[2] - 1),
                                    (dcell[0] + 1, dcell[1], dcell[2]),
                                    (dcell[0] - 1, dcell[1], dcell[2]),
                                )
                            )
                        else:
                            far_x, far_z = nx + dx, nz + dz
                            opens_to_air = (
                                is_passable(reader((far_x, ny, far_z)), allow_cobweb=allow_cobweb)
                                and is_passable(reader((far_x, ny + 1, far_z)), allow_cobweb=allow_cobweb)
                            )
                        if not opens_to_air or (cur, nxt) in blocked:
                            continue
                        step_cost = cur_cost + 3.5 + 1.5 * len(dig_cells)
                        if nxt not in expanded and step_cost < costs.get(nxt, float("inf")):
                            costs[nxt] = step_cost
                            parents[nxt] = cur
                            edge_meta[nxt] = {"mode": "dig", "dig_cells": [list(p) for p in dig_cells]}
                            heapq.heappush(pq, (step_cost, nxt, 0, dig_streak + 1, 0))
                        if (visited_cells is not None and nxt not in visited_cells) or (
                            cov_set is not None and nxt not in cov_set
                        ):
                            unseen_adj += 1
                        break

            # Controlled drop / scaffold-cushioned descent across 2..max_drop_steps vertical cliffs
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
                        if abs(ny - sy) > int(max_radius):
                            break
                        if not is_passable(reader((nx, ny, nz)), allow_cobweb=allow_cobweb):
                            break
                        floor_d = reader((nx, ny - 1, nz))
                        if is_lava_like(floor_d) or is_water_like(floor_d):
                            break
                        if is_solid_floor(floor_d):
                            nxt = (nx, ny, nz)
                            if (cur, nxt) not in blocked and _cell_safe_border(nxt, reader) and _cell_safe_border((nx, ny + 1, nz), reader):
                                if drop_d <= 3:
                                    step_cost = cur_cost + 1.35 + 0.25 * drop_d
                                    if nxt not in expanded and step_cost < costs.get(nxt, float("inf")):
                                        costs[nxt] = step_cost
                                        parents[nxt] = cur
                                        edge_meta[nxt] = {"mode": "drop", "drop_blocks": drop_d}
                                        heapq.heappush(pq, (step_cost, nxt, 0, 0, 0))
                                    if visited_cells is not None and nxt not in visited_cells:
                                        unseen_adj += 1
                                elif allow_pillar:
                                    # Place stepping block(s) to ensure max single drop <= 3 (zero fall damage)
                                    step_cells: List[List[int]] = []
                                    cur_anchor_y = cy
                                    while cur_anchor_y - ny > 3:
                                        mid_floor_y = cur_anchor_y - 3
                                        step_cells.append([nx, mid_floor_y, nz])
                                        cur_anchor_y = mid_floor_y + 1
                                    step_cost = cur_cost + 3.0 + 0.6 * len(step_cells) + 0.2 * drop_d
                                    if nxt not in expanded and step_cost < costs.get(nxt, float("inf")):
                                        costs[nxt] = step_cost
                                        parents[nxt] = cur
                                        edge_meta[nxt] = {
                                            "mode": "scaffold_drop",
                                            "drop_blocks": drop_d,
                                            "step_cells": step_cells,
                                        }
                                        heapq.heappush(pq, (step_cost, nxt, 0, 0, 0))
                                    if visited_cells is not None and nxt not in visited_cells:
                                        unseen_adj += 1
                            break

            # Stone/cobblestone bridging ONLY when anchored to a reachable opposite solid landing (or ore within 2 blocks)
            if allow_bridge and not stepped_floor and bridge_streak < int(max_bridge_steps):
                nxt = (nx, cy, nz)
                cur_floor_blk = reader((cx, cy - 1, cz))
                prev_parent = parents.get(cur)
                prev_mode = edge_meta.get(cur, {}).get("mode")
                # Allow bridging from real ground, or continuing straight along an already-validated bridge direction
                same_bridge_dir = (
                    prev_mode == "bridge"
                    and prev_parent is not None
                    and (cx - prev_parent[0], cz - prev_parent[2]) == (dx, dz)
                )
                if (is_solid_floor(cur_floor_blk) or same_bridge_dir) and (cur, nxt) not in blocked:
                    max_ahead = max(1, int(max_bridge_steps) - bridge_streak)
                    anchored_ahead = False
                    for span in range(1, max_ahead + 1):
                        gx, gz = cx + span * dx, cz + span * dz
                        if abs(gx - sx) > int(max_radius) or abs(gz - sz) > int(max_radius):
                            break
                        if abs(gx - ax) > arad or abs(gz - az) > arad:
                            break
                        gf = reader((gx, cy, gz))
                        gh = reader((gx, cy + 1, gz))
                        gfl = reader((gx, cy - 1, gz))
                        gfl_name = _block_name(gfl)
                        if not (
                            is_passable(gf, allow_cobweb=allow_cobweb)
                            and is_passable(gh, allow_cobweb=allow_cobweb)
                            and not is_lava_like(gf)
                            and not is_water_like(gf)
                            and not is_lava_like(gh)
                            and not is_water_like(gh)
                            and _cell_safe_border_ignore_floor((gx, cy, gz), reader)
                            and _cell_safe_border((gx, cy + 1, gz), reader)
                            and gfl_name not in {"not_loaded", "unknown", "spawner", "chest_minecart"}
                            and (is_lava_like(gfl) or is_water_like(gfl) or is_passable(gfl))
                        ):
                            break
                        # Check real natural solid landing immediately past step `span`
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
                                    and (ly_off != 1 or is_passable(reader((gx, cy + 2, gz)), allow_cobweb=allow_cobweb))
                                    and (ly_off != -1 or is_passable(reader((lx, cy + 1, lz)), allow_cobweb=allow_cobweb))
                                ):
                                    anchored_ahead = True
                                    break
                        if anchored_ahead:
                            break
                        # Also allow <=2 bridge steps if directly bringing an exposed target ore within reach
                        if not anchored_ahead and targets is not None and span <= 2:
                            for ox in (-2, -1, 0, 1, 2):
                                for oz in (-2, -1, 0, 1, 2):
                                    for oy in (-1, 0, 1, 2, 3):
                                        ocell = (gx + ox, cy + oy, gz + oz)
                                        oname = _block_name(reader(ocell))
                                        if oname in ORE_DROP_MAP and has_exposed_air_face(ocell, reader, block_cache=block_cache):
                                            oact, _, _ = evaluate_ore_block(oname, targets, harvested_counts=harvested_counts or {}, best_tool_tier=best_tool_tier)
                                            if oact == "mine" and ore_cell_hazard_free(ocell, reader, block_cache=block_cache):
                                                anchored_ahead = True
                                                break
                                    if anchored_ahead:
                                        break
                                if anchored_ahead:
                                    break
                        if anchored_ahead:
                            break
                    if anchored_ahead:
                        fl_blk = reader((nx, cy - 1, nz))
                        is_lava_bridge = is_lava_like(fl_blk)
                        step_cost = cur_cost + (4.2 if is_lava_bridge else 3.2)
                        if nxt not in expanded and step_cost < costs.get(nxt, float("inf")):
                            costs[nxt] = step_cost
                            parents[nxt] = cur
                            edge_meta[nxt] = {
                                "mode": "bridge",
                                "bridge_cells": [[nx, cy - 1, nz]],
                                "over_lava": is_lava_bridge,
                            }
                            heapq.heappush(pq, (step_cost, nxt, 0, 0, bridge_streak + 1))

        # In-place vertical pillar-up edge onto (cx, cy + 1, cz) only when genuinely needed:
        # either already mid-pillar, beside a >=2-block cliff/seam (solid at cy+1), or directly below high ores/hints.
        if allow_pillar and pillar_h < int(max_pillar_height) and abs(cy + 1 - sy) <= int(max_radius):
            need_pillar = pillar_h > 0
            if not need_pillar:
                for pdx, pdz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    # A >=2-block cliff/seam has solid rock at y = cy + 1 (1-block steps are climbed directly by walk)
                    lx, lz = cx + pdx, cz + pdz
                    if is_solid_floor(reader((lx, cy + 1, lz))):
                        # 2-block climbable ledge: 2-high headroom at cy+2..cy+3
                        if (
                            is_passable(reader((lx, cy + 2, lz)), allow_cobweb=allow_cobweb)
                            and is_passable(reader((lx, cy + 3, lz)), allow_cobweb=allow_cobweb)
                        ):
                            need_pillar = True
                            break
                        # 3-block climbable ledge inside a cave (require cave roof, don't punch out into open sky)
                        if (
                            is_solid_floor(reader((lx, cy + 2, lz)))
                            and is_passable(reader((lx, cy + 3, lz)), allow_cobweb=allow_cobweb)
                            and is_passable(reader((lx, cy + 4, lz)), allow_cobweb=allow_cobweb)
                            and any(not is_passable(reader((lx, cy + ky, lz))) for ky in range(5, 16))
                        ):
                            need_pillar = True
                            break
                        if targets is not None and _block_name(reader((lx, cy + 4, lz))) in ORE_DROP_MAP:
                            need_pillar = True
                            break
                    if need_pillar:
                        break
            if not need_pillar and targets is not None:
                for ox in (-2, -1, 0, 1, 2):
                    for oz in (-2, -1, 0, 1, 2):
                        if abs(ox) + abs(oz) > 2:
                            continue
                        for oy in (3, 4, 5):
                            if _block_name(reader((cx + ox, cy + oy, cz + oz))) in ORE_DROP_MAP:
                                need_pillar = True
                                break
                        if need_pillar:
                            break
                    if need_pillar:
                        break
            if not need_pillar and hints:
                need_pillar = any(
                    hy >= cy + 3 and abs(hx - cx) + abs(hz - cz) <= 4
                    for hx, hy, hz in hints
                )
            if need_pillar:
                up_node = (cx, cy + 1, cz)
                if (cur, up_node) not in blocked and _cell_safe_border(up_node, reader):
                    head2_cell = (cx, cy + 2, cz)
                    head2_blk = reader(head2_cell)
                    head2_pass = is_passable(head2_blk, allow_cobweb=allow_cobweb)
                    head2_dig = (not head2_pass) and allow_dig and _can_dig_step_cell(head2_cell, reader)
                    if head2_pass or head2_dig:
                        # Only pillar up onto air/replaceable or clearable space
                        foot_above = reader(up_node)
                        if is_passable(foot_above, allow_cobweb=allow_cobweb) or _block_name(foot_above) in AIR:
                            step_cost = cur_cost + (5.5 if head2_pass else 8.0)
                            if up_node not in expanded and step_cost < costs.get(up_node, float("inf")):
                                costs[up_node] = step_cost
                                parents[up_node] = cur
                                edge_meta[up_node] = {
                                    "mode": "pillar",
                                    "pillar_steps": 1,
                                    "dig_cells": [list(head2_cell)] if head2_dig else [],
                                }
                                heapq.heappush(pq, (step_cost, up_node, pillar_h + 1, dig_streak, 0))

        unseen_edges = unseen_adj
        if cov_set is not None and cur not in cov_set and cur != start:
            unseen_adj += 1
        # Only expose frontiers on natural solid floors bordering real traversable unseen steps
        # (never treat mid-air bridge nodes or dead-end pillars as open exploration frontiers).
        if (
            unseen_edges > 0
            and cur != start
            and (cov_set is None or cur not in cov_set)
            and (pillar_h == 0 or unseen_edges >= 1)
            and is_solid_floor(reader((cx, cy - 1, cz)))
        ):
            center_bonus = 0.08 * _lateral_clearance(cur, reader)
            hint_penalty = 0.0
            if hints:
                hint_penalty = 0.28 * min(abs(cx - hx) + abs(cy - hy) + abs(cz - hz) for hx, hy, hz in hints)
            frontiers.append((0.62 * cur_cost - 1.15 * unseen_adj - center_bonus + hint_penalty, [cx, cy, cz]))

    ore_targets = sorted(seen_ore_vantages.values(), key=lambda r: (r["score"], r["cost"], r["ore_pos"][1]))
    for row in ore_targets:
        row["path"] = reconstruct_flood_path(parents, edge_meta, row["vantage"])

    frontiers.sort(key=lambda item: (item[0], abs(item[1][0] - sx) + abs(item[1][2] - sz)))
    frontier_rows = [
        {"pos": pos, "score": round(score, 2), "cost": round(costs[tuple(pos)], 2), "path": reconstruct_flood_path(parents, edge_meta, pos)}
        for score, pos in frontiers[:32]
    ]

    return {
        "start": [sx, sy, sz],
        "nodes": [[p[0], p[1], p[2]] for p in explored],
        "costs": {f"{k[0]},{k[1]},{k[2]}": round(v, 2) for k, v in costs.items()},
        "ore_targets": ore_targets,
        "frontiers": frontier_rows,
        "parents": parents,
        "edge_meta": edge_meta,
    }
