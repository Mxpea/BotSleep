"""Pure shared canopy action candidates, used by local execution and model takeover."""

import math

from agent.survival_planner import Decision, SurvivalPlanner
from core.daily_policy import DailySafetyPolicy
from skills.daily_survival import DailySurvivalSkills
from skills.survival import SurvivalSkills


def canopy_clearances(observation: dict, context: dict, settings: dict) -> list[Decision]:
    """Return all supported one-leaf preparations, ordered by obstruction and distance."""
    recovery = context.get("retreat_recovery") or context.get("recovery") or {}
    policy = DailySafetyPolicy(settings, allow_controls=True)
    if policy._force_environment_error(observation) is not None:
        return []
    status = observation["status"]
    if status["health"] < 12 or status["food"] < 8:
        return []
    origin = status["pos"]
    feet = [math.floor(value + .0001) for value in origin]
    blockers = {tuple(row["pos"]) for row in recovery.get("blocked_cells", [])
                if isinstance(row, dict) and SurvivalPlanner._pos(row.get("pos")) is not None}
    blocked = policy.rules._blocked(context)
    candidates = []
    for row in observation.get("blocks", []):
        if not isinstance(row, dict) or row.get("name") not in DailySurvivalSkills.FOLIAGE:
            continue
        pos = SurvivalPlanner._pos(row.get("pos"))
        if (pos is None or any(value != math.floor(value) for value in pos)
                or row.get("fresh") is False or row.get("can_see") is not True or row.get("can_dig") is False
                or max(abs(pos[0] - feet[0]), abs(pos[2] - feet[2])) != 1
                or pos[1] not in (feet[1], feet[1] + 1) or math.dist(origin, pos) > 3
                or policy.rules._key(pos) in blocked):
            continue
        if row["name"] != "vine" and (row.get("properties") or {}).get("waterlogged") not in (False, "false"):
            continue
        decision = Decision("clear_foliage", {"pos": list(pos), "expected_name": row["name"]},
                            "Open one freshly verified canopy obstruction before rechecking descent", "safety")
        if policy.validate_force(decision, observation) is None:
            candidates.append((tuple(pos) not in blockers, math.dist(origin, pos), tuple(pos), decision))
    return [row[-1] for row in sorted(candidates, key=lambda row: row[:3])]


def canopy_descent(observation: dict, context: dict, settings: dict):
    """Bind only a collector-verified adjacent canopy step, never model arguments."""
    recovery = context.get("retreat_recovery") or {}
    plan = observation.get("canopy_step")
    if (recovery.get("canopy_blocked") is not True or not isinstance(plan, dict)
            or plan.get("fresh") is not True or plan.get("verified") is not True
            or observation.get("fresh") is not True or observation.get("stale") is True
            or DailySafetyPolicy(settings, allow_controls=True)._force_environment_error(observation) is not None):
        return None
    status = observation["status"]
    origin, point = SurvivalPlanner._pos(status.get("pos")), SurvivalPlanner._pos(plan.get("pos"))
    end = SurvivalPlanner._pos(plan.get("route_end"))
    if (origin is None or point is None or end is None or plan.get("origin") != origin
            or plan.get("dimension") != status["dimension"] or status["health"] < 12 or status["food"] < 8):
        return None
    feet = [math.floor(v + .0001) for v in origin]
    cell = [math.floor(v + .0001) for v in point]
    drop = feet[1] - cell[1]
    if (abs(cell[0] - feet[0]) + abs(cell[2] - feet[2]) != 1
            or drop not in (0, 1, 2) or plan.get("drop") != drop
            or point != [cell[0] + .5, cell[1], cell[2] + .5]
            or not 0 < origin[1] - end[1] <= 2
            or SurvivalPlanner._key(point) in (context.get("blocked_targets") or [])):
        return None
    cut = plan.get("clear_pos")
    if cut is None and plan.get("expected_name") is not None:
        return None
    if cut is not None and (cut != [cell[0], feet[1] - 1, cell[2]]
            or drop == 0 or plan.get("expected_name") not in SurvivalSkills.RETREAT_LEAVES):
        return None
    return Decision("descend_canopy", {"pos": list(point), "clear_pos": cut,
        "expected_name": plan.get("expected_name")},
        "Follow a freshly checked lower foothold; reobserve after one canopy step", "safety")
