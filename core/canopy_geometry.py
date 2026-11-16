"""Bounded, read-only canopy steps. Missing cells never mean air."""

import math

from skills.survival import SurvivalSkills
from agent.survival_planner import SurvivalPlanner


def plan_canopy_step(status, read, *, target=None, excluded=()):
    """Find one adjacent step toward a checked lower foothold, at most 96 reads.

    A step may remove one dry leaf in the adjacent column below the old feet.
    It never removes the current support, drops more than two blocks, or treats
    a partial route as verified solid ground. Reads must return current cells.
    """
    try:
        origin = SurvivalSkills._pos(status.get("pos"))
    except Exception:
        return None
    if status.get("on_ground") is not True:
        return None
    feet = tuple(math.floor(value + .0001) for value in origin)
    if any(abs(origin[axis] - (feet[axis] + .5)) > .125 for axis in (0, 2)) or abs(origin[1] - feet[1]) > .05:
        return None  # Never cut a neighbouring support while straddling its edge.
    x, y, z = feet
    cache = {}
    denied = {SurvivalPlanner._key(pos) if isinstance(pos, (list, tuple)) else pos for pos in excluded}

    def block(key):
        if key not in cache:
            if len(cache) >= 96:
                return None
            value = read(key)
            cache[key] = value if isinstance(value, dict) and isinstance(value.get("name"), str) else None
        return cache[key]

    def dry(value):
        return value is not None and SurvivalSkills._safe_block(value)

    def support(value):
        return dry(value) and (value["name"] in SurvivalSkills.SUPPORT or
            value["name"] in SurvivalSkills.RETREAT_LEAVES and
            (value.get("properties") or {}).get("waterlogged") in (False, "false"))

    def clear(key):
        value = block(key)
        return dry(value) and value["name"] in SurvivalSkills.RETREAT_PASSABLE

    initial = block((x, y - 1, z))
    if (not support(initial) or initial["name"] not in SurvivalSkills.RETREAT_LEAVES
            or not clear(feet) or not clear((x, y + 1, z))):
        return None

    def neighbours(cell, top):
        for level in range(cell[1] - 1, top + 1):
            for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                value = block((cell[0] + dx, level, cell[2] + dz))
                if not dry(value):
                    return False
        return True

    paths = {(1, 0): True, (-1, 0): True, (0, 1): True, (0, -1): True}
    for distance in (1, 2, 3):
        for (dx, dz), reachable in list(paths.items()):
            if not reachable:
                continue
            cx, cz = x + distance * dx, z + distance * dz
            for drop in (1, 2):
                cell = (cx, y - drop, cz)
                floor = block((cx, cell[1] - 1, cz))
                if not support(floor):
                    continue
                cut = None
                cut_name = None
                valid = True
                for level in range(cell[1], y + 2):
                    key = (cx, level, cz)
                    value = block(key)
                    if not dry(value):
                        valid = False
                        break
                    if value["name"] in SurvivalSkills.RETREAT_PASSABLE:
                        continue
                    if (level == y - 1 and value["name"] in SurvivalSkills.RETREAT_LEAVES
                            and (value.get("properties") or {}).get("waterlogged") in (False, "false")
                            and value.get("diggable") is True and value.get("can_see") is not False):
                        cut, cut_name = list(key), value["name"]
                    else:
                        valid = False
                        break
                if not valid:
                    continue
                end = [cx + .5, cell[1], cz + .5]
                point = end if distance == 1 else [x + dx + .5, y, z + dz + .5]
                if (SurvivalPlanner._key(point) in denied
                        or target is not None and list(target) != point):
                    continue
                first = tuple(math.floor(v + .0001) for v in point)
                if not neighbours(first, y + 1):
                    continue
                return {"origin": list(origin), "pos": point,
                    "clear_pos": cut if distance == 1 else None,
                    "expected_name": cut_name if distance == 1 else None,
                    "support": floor["name"] if distance == 1 else "canopy_relay",
                    "drop": drop if distance == 1 else 0, "route_end": end,
                    "block_reads": len(cache)}
            # Only traverse a completely checked flat canopy segment to reach
            # a lower step within three columns. Do not wander without a descent.
            same_floor = block((cx, y - 1, cz))
            paths[(dx, dz)] = (support(same_floor) and clear((cx, y, cz))
                               and clear((cx, y + 1, cz)))
    return None
