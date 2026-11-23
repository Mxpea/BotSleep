"""Verify a short level walk to the exterior of a freshly inspected, finished house."""

import math
import time

from core.block_geometry import PASS_THROUGH
from core.client import APIError
from core.domain import parse_pos
from core.structure_spec import transform_rel_pos
from skills.positioning import PositioningSkills
from skills.survival import _Failure


MAX_STEPS = 16


def exterior_step(status, shelter):
    """Validate the complete observed path before proposing its first cell only."""
    route = shelter.get("exterior_route")
    home = parse_pos(shelter.get("pos"), integer=True)
    facing = shelter.get("orientation")
    if (not isinstance(route, dict) or route.get("verified") is not True or route.get("fresh") is not True
            or route.get("source") != "api_verified" or route.get("dimension") != status.get("dimension")
            or route.get("origin") != status.get("pos") or route.get("life") != status.get("last_death")
            or shelter.get("inside") is not False or home is None or facing not in ("north", "south", "east", "west")
            or math.dist(status["pos"], home) > MAX_STEPS):
        return None
    door = transform_rel_pos((0, 0, 2), home, facing, integer=True)
    outside = transform_rel_pos((0, 0, 3), home, facing, integer=True)
    path = route.get("path")
    if (shelter.get("door", {}).get("pos") != door or not isinstance(path, list)
            or not 1 <= len(path) <= MAX_STEPS or path[-1] != outside):
        return None
    start = PositioningSkills._cell(status["pos"])
    previous, visited = start, {start}
    for point in path:
        cell = parse_pos(point, integer=True)
        if (cell is None or tuple(cell) in visited or cell[1] != home[1] or cell[1] != previous[1]
                or abs(cell[0] - previous[0]) + abs(cell[2] - previous[2]) != 1
                or abs(cell[0] - home[0]) <= 2 and abs(cell[2] - home[2]) <= 2):
            return None
        previous = tuple(cell)
        visited.add(previous)
    return [path[0][0] + .5, start[1], path[0][2] + .5]


def inspect_exterior(collector, observation, shelter, cancel):
    """Check both axis orders to the door. Each candidate is a complete level path."""
    status = observation["status"]
    origin, home = status["pos"], shelter["pos"]
    if (not (status.get("is_day") is False or status.get("time_of_day", 0) >= 11000)
            or status.get("thundering") is not False or status.get("on_ground") is not True
            or abs(origin[1] - home[1]) > .05 or shelter.get("inside") is not False
            or shelter.get("orientation") not in ("north", "east", "south", "west")
            or math.dist(origin, home) > MAX_STEPS):
        return None
    start = PositioningSkills._cell(origin)
    outside = transform_rel_pos((0, 0, 3), home, shelter["orientation"], integer=True)
    length = abs(start[0] - outside[0]) + abs(start[2] - outside[2])
    if not 1 <= length <= MAX_STEPS:
        return None
    cache = {}
    deadline = time.monotonic() + 4
    guarded = PositioningSkills(collector.client)
    context = {"cancel": cancel}
    hazards = [row["pos"] for row in observation.get("blocks", [])
               if row.get("name") in guarded.HAZARDS]

    def clear(col):
        if col in cache:
            return cache[col]
        x, z = col
        y = home[1]
        if (time.monotonic() >= deadline or len(cache) >= 38
                or abs(x - home[0]) <= 2 and abs(z - home[2]) <= 2
                or any(math.dist([x + .5, y, z + .5], p) <= 1.5 for p in hazards)):
            return False
        cache[col] = False
        for level, allowed in ((y - 1, guarded.SUPPORT - guarded.RETREAT_TRUNKS),
                               (y, PASS_THROUGH), (y + 1, PASS_THROUGH)):
            block = collector._read([x, level, z], cancel)
            if block["name"] not in allowed or not guarded._safe_block(block):
                return False
        cache[col] = True
        return True

    try:
        for order in ((0, 2), (2, 0)):
            p = list(start)
            path = []
            for axis in order:
                while p[axis] != outside[axis]:
                    p = p.copy()
                    p[axis] += 1 if outside[axis] > p[axis] else -1
                    path.append(p)
            first = [path[0][0] + .5, home[1], path[0][2] + .5]
            columns = list(guarded._columns(origin, first)) + [(p[0], p[2]) for p in path]
            if not all(clear(col) for col in columns) or time.monotonic() >= deadline:
                continue
            # Radius 36 covers a 16-cell walk and the entire ranged threat margin.
            enemies = collector.client.get_entities(radius=36, types=["hostile"], timeout_seconds=2)
            last = origin
            current = collector._status(status, cancel, grounded=True)
            guarded._step_stable(current, status)
            if current["pos"] != origin:
                return None
            for x, y, z in path:
                point = [x + .5, y, z + .5]
                guarded._step_threats(last, point, context, entities=enemies)
                last = point
            if time.monotonic() < deadline:
                return {"origin": list(origin), "dimension": status["dimension"], "life": status.get("last_death"),
                        "path": path, "fresh": True, "verified": True, "source": "api_verified"}
    except _Failure as error:
        if error.code == "cancelled":
            raise APIError("cancelled", "Return-route inspection cancelled") from error
    except APIError as error:
        if error.code not in {"not_loaded", "observation_changed", "request_timeout"}:
            raise
    return None
