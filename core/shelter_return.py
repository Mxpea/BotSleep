"""Choose one freshly observed shelter return stance; perform no I/O.

The construction anchor provides direction only. The caller owns the bounded
return history/budget, and PositioningSkills must revalidate the selected route.
"""

import math

from agent.survival_planner import Decision, RANGED_HOSTILES
from core.domain import HAZARDS, block_key, blocked_targets, cell_coords, finite_number, parse_pos
from skills.daily_survival import DailySurvivalSkills


MAX_RETURN_STEPS = 48
MAX_SIDE_STEPS = 8
MAX_DETOUR = 3


def _distance_to_segment(point, origin, target):
    direction = [target[i] - origin[i] for i in range(3)]
    length = sum(value * value for value in direction)
    fraction = max(0, min(1, sum((point[i] - origin[i]) * direction[i] for i in range(3)) / length))
    closest = [origin[i] + fraction * direction[i] for i in range(3)]
    return math.dist(point, closest)


def choose_shelter_return(observation, anchor, context, *, visited, allow_step):
    """Return a checked-candidate proposal, never a remembered route or write.

    ``visited`` contains actual successful arrival positions, starting with the
    initial return origin. ``allow_step`` is the controller's existing cardinal
    one-level filter. Returning None asks the caller to stop/escalate this return
    attempt; it does not authorize new-site search or another movement mode.
    """
    if not isinstance(observation, dict) or not isinstance(anchor, dict) or not isinstance(context, dict):
        return None
    status = observation.get("status")
    if not isinstance(status, dict):
        return None
    origin, destination = parse_pos(status.get("pos")), parse_pos(anchor.get("pos"))
    counts = observation.get("counts")
    previous = context.get("last_result") if isinstance(context.get("last_result"), dict) else {}
    previous_data = previous.get("data") if isinstance(previous.get("data"), dict) else {}
    if (origin is None or destination is None or anchor.get("dimension") != status.get("dimension")
            or status.get("dimension") not in {"overworld", "minecraft:overworld"}
            or observation.get("fresh") is not True or observation.get("stale") is True
            or observation.get("observation_scope") == "safety" or observation.get("inventory_fresh") is False
            or observation.get("local_geometry_origin") != origin
            or not isinstance(counts, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in counts.items())
            or status.get("alive") is not True or status.get("ready") is not True
            or status.get("busy") not in (None, False) or status.get("gamemode") != "survival"
            or status.get("on_ground") is not True or status.get("is_day") is not True
            or not finite_number(status.get("health")) or status["health"] <= 0
            or not finite_number(status.get("food")) or status["food"] <= 6
            or any(status.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged", "thundering", "is_sleeping", "in_vehicle"))
            or context.get("pending") or context.get("action_state") == "unknown"
            or previous_data.get("action_state") == "unknown"
            or previous.get("error_code") in {"action_unconfirmed", "dig_unconfirmed", "request_timeout", "network_error", "invalid_response", "invalid_json"}):
        return None
    if not isinstance(visited, (list, tuple)) or len(visited) > 64:
        return None
    history = [parse_pos(point) for point in visited]
    if any(point is None for point in history):
        return None
    if not history or history[-1] != origin:
        history.append(origin)
    seen = {cell_coords(point) for point in history}
    before = math.dist(origin, destination)
    best = min(math.dist(point, destination) for point in history)
    sideways = 0
    for previous_pos, current_pos in reversed(list(zip(history, history[1:]))):
        if math.dist(previous_pos, destination) - math.dist(current_pos, destination) > .25:
            break
        sideways += 1
    radius = context.get("threat_radius", 8)
    if not finite_number(radius) or not 1 <= radius <= 32:
        return None
    entities = observation.get("entities")
    if not isinstance(entities, list) or len(entities) >= 40:
        return None
    threats = []
    for entity in entities:
        if not isinstance(entity, dict):
            return None
        if entity.get("type") != "hostile":
            continue
        position = parse_pos(entity.get("pos"))
        if position is None or not isinstance(entity.get("name"), str) or not entity["name"]:
            return None
        limit = max(radius, 16) if entity["name"] in RANGED_HOSTILES else radius
        if math.dist(origin, position) <= limit:
            return None  # Emergency retreat owns an already-dangerous origin.
        threats.append((position, limit, entity.get("id")))
    nearest = status.get("nearest_hostile")
    if nearest is not None:
        if (not isinstance(nearest, dict) or not isinstance(nearest.get("name"), str) or not nearest["name"]
                or not finite_number(nearest.get("dist")) or nearest["dist"] < 0):
            return None
        limit = max(radius, 16) if nearest["name"] in RANGED_HOSTILES else radius
        if (nearest["dist"] <= limit or nearest["dist"] <= limit + 3
                and not any(entity_id is not None and entity_id == nearest.get("id") for _, _, entity_id in threats)):
            return None
    blocks = observation.get("blocks", [])
    if not isinstance(blocks, list):
        return None
    hazards = [parse_pos(row.get("pos")) for row in blocks
               if isinstance(row, dict) and row.get("name") in HAZARDS]
    if any(point is None for point in hazards):
        return None
    blocked = blocked_targets(context)
    rows = observation.get("safe_stand_candidates")
    if not isinstance(rows, list) or len(rows) > 8:
        return None
    forward, detours = [], []
    for row in rows:
        if not isinstance(row, dict) or row.get("support") not in DailySurvivalSkills.SUPPORT:
            continue
        target = parse_pos(row.get("pos"))
        if (target is None or target[1] != math.floor(target[1])
                or any(abs(target[i] - math.floor(target[i]) - .5) > 1e-6 for i in (0, 2))
                or not .4 < math.dist(origin, target) <= 3 or cell_coords(target) in seen
                or block_key(target) in blocked):
            continue
        if abs(target[1] - origin[1]) > .05 and (not callable(allow_step) or allow_step(origin, target, observation) is not True):
            continue
        if (any(_distance_to_segment(point, origin, target) <= limit for point, limit, _ in threats)
                or any(_distance_to_segment(point, origin, target) <= 1.5 for point in hazards)):
            continue
        after = math.dist(target, destination)
        row = (after, abs(target[1] - destination[1]), -math.dist(origin, target), tuple(target))
        if before - after > .25:
            forward.append(row)
        elif sideways < MAX_SIDE_STEPS and after <= before + 1.5 and after <= best + MAX_DETOUR:
            detours.append(row)
    candidates = forward or detours
    if not candidates:
        return None
    selected = list(min(candidates)[-1])
    kind = "toward" if forward else "around a local obstruction toward"
    return Decision("position_for_task", {"pos": selected},
        f"Take one freshly observed stance {kind} the construction anchor; recheck the complete route before moving",
        "shelter_return")
