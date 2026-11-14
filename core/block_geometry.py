"""Bounded, conservative voxel visibility using only the original block API.

This checks a few sight rays, not collision shapes or path reachability. Unknown
block shapes are opaque; an inconclusive read must never become visible=True.
"""

import itertools
import math


PASS_THROUGH = frozenset({
    "air", "cave_air", "void_air", "short_grass", "tall_grass", "fern", "large_fern",
    "dead_bush", "leaf_litter", "vine", "glow_lichen", "hanging_roots",
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "oxeye_daisy",
    "cornflower", "lily_of_the_valley", "orange_tulip", "pink_tulip", "red_tulip",
    "white_tulip", "sunflower", "lilac", "rose_bush", "peony",
})


def _position(value, *, integer=False):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(type(v) not in (int, float) or not -30000000 <= v <= 30000000
                   or not math.isfinite(v) or (integer and v != math.floor(v)) for v in value)):
        raise ValueError("Position must contain three finite coordinates; target must be integer")
    return tuple(int(v) if integer else float(v) for v in value)


def _ray_cells(start, end):
    """Supercover DDA: edge/corner contacts include all adjacent touched cells."""
    cell = [math.floor(v) for v in start]
    final = tuple(math.floor(v) for v in end)
    yield tuple(cell)
    delta = [end[i] - start[i] for i in range(3)]
    step = [1 if v > 0 else -1 if v < 0 else 0 for v in delta]
    interval = [abs(1 / v) if v else math.inf for v in delta]
    crossing = [((cell[i] + (1 if step[i] > 0 else 0) - start[i]) / delta[i])
                if step[i] else math.inf for i in range(3)]
    # Public range is at most 64 blocks; 256 crossings safely bounds every ray.
    for _ in range(256):
        if tuple(cell) == final:
            return
        nearest = min(crossing)
        if nearest > 1 + 1e-10:
            return
        axes = [i for i in range(3) if abs(crossing[i] - nearest) <= 1e-10]
        for length in range(1, len(axes) + 1):
            for subset in itertools.combinations(axes, length):
                touched = cell.copy()
                for axis in subset:
                    touched[axis] += step[axis]
                yield tuple(touched)
        for axis in axes:
            cell[axis] += step[axis]
            crossing[axis] += interval[axis]


def trace_block_visibility(origin, target, get_block, *, cache=None, max_queries=32,
                           max_distance=8, eye_height=1.62):
    """Return state/visible/reason/queries, with an optional blocking voxel.

    ``origin`` is the feet position; ``target`` is an integer block position.
    ``get_block([x,y,z])`` returns a block dict, not an API envelope. The callback
    may check cancellation: all exceptions propagate except ``not_loaded``,
    which is inconclusive. A supplied cache is caller-owned and should only be
    shared within one fresh observation. ``queries`` counts new callback reads.
    """
    origin = _position(origin)
    target = _position(target, integer=True)
    if type(max_queries) is not int or not 0 <= max_queries <= 4096:
        raise ValueError("max_queries must be an integer from 0 to 4096")
    if type(max_distance) not in (int, float) or not 0 < max_distance <= 64:
        raise ValueError("max_distance must be positive and at most 64")
    if type(eye_height) not in (int, float) or not 0 < eye_height <= 3:
        raise ValueError("eye_height must be positive and at most 3")
    cache = {} if cache is None else cache
    eye = (origin[0], origin[1] + eye_height, origin[2])
    center = tuple(v + 0.5 for v in target)
    queries = 0

    def result(state, reason, blocker=None):
        value = {"state": state, "visible": {"visible": True, "occluded": False, "unknown": None}[state],
                 "reason": reason, "queries": queries}
        if blocker is not None:
            value["blocker"] = list(blocker)
        return value

    if math.dist(eye, center) > max_distance:
        return result("unknown", "out_of_range")
    samples = [center]
    for axis in range(3):
        # Aim just inside the eye-facing face, avoiding boundary rounding into
        # its neighbour. These are bounded samples, not proof of every surface.
        if eye[axis] < target[axis] or eye[axis] > target[axis] + 1:
            face = list(center)
            face[axis] = target[axis] + (0.001 if eye[axis] < target[axis] else 0.999)
            samples.append(tuple(face))

    unknown = None
    blocker = None
    for sample in samples:
        reached = False
        for cell in _ray_cells(eye, sample):
            if cell == target:
                reached = True
                break  # The target's own solid volume is the sight endpoint.
            if cell not in cache:
                if queries >= max_queries:
                    return result("unknown", "query_budget_exhausted", blocker)
                queries += 1
                try:
                    cache[cell] = get_block(list(cell))
                except Exception as error:
                    if getattr(error, "code", None) != "not_loaded":
                        raise
                    cache[cell] = None
            block = cache[cell]
            if not isinstance(block, dict) or not isinstance(block.get("name"), str):
                unknown = "unknown_block"
                break
            if block["name"] not in PASS_THROUGH:
                blocker = cell
                break
        if reached:
            return result("visible", "clear_sample")
    if unknown is not None:
        return result("unknown", unknown, blocker)
    return result("occluded", "blocked_samples", blocker)
